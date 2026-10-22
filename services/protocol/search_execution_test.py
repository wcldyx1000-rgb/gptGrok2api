import contextlib
import unittest
from unittest import mock

from services.protocol import search_execution
from utils.helper import UpstreamHTTPError


class FakeAccounts:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.used = []
        self.slot_calls = []

    def list_tokens(self):
        return list(self.tokens)

    @contextlib.contextmanager
    def text_slot(self, preferred_token="", excluded_tokens=None, timeout_secs=120.0):
        excluded = set(excluded_tokens or ())
        self.slot_calls.append(excluded)
        token = next((item for item in self.tokens if item not in excluded), "")
        yield token

    def get_account(self, token):
        return {"email": f"{token}@example.com"} if token else {}

    def mark_text_used(self, token):
        self.used.append(token)


class FakeBackend:
    def __init__(self, behaviours, token):
        self.behaviours = behaviours
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def search(self, query, timeout_secs=0):
        action = self.behaviours[self.token]
        if isinstance(action, BaseException):
            raise action
        return {"answer": f"{query}:{self.token}", "timeout": timeout_secs}


def run(accounts, behaviours, **kwargs):
    with mock.patch.object(search_execution, "account_service", accounts):
        return search_execution.execute_search(
            "q", backend_factory=lambda token: FakeBackend(behaviours, token), **kwargs
        )


class SearchExecutionTests(unittest.TestCase):
    def test_success_returns_result_and_account(self):
        accounts = FakeAccounts(["a"])
        result, account = run(accounts, {"a": None})
        self.assertEqual(result["answer"], "q:a")
        self.assertLessEqual(result["timeout"], search_execution.SEARCH_TIMEOUT_SECS)
        self.assertEqual(account["email"], "a@example.com")
        self.assertEqual(accounts.used, ["a"])

    def test_auth_rejection_switches_to_next_account_once(self):
        accounts = FakeAccounts(["a", "b"])
        result, account = run(accounts, {"a": UpstreamHTTPError("/search", 401, "denied"), "b": None})
        self.assertEqual(result["answer"], "q:b")
        self.assertEqual(account["email"], "b@example.com")
        self.assertEqual(accounts.slot_calls, [set(), {"a"}])
        self.assertEqual(accounts.used, ["b"])

    def test_gives_up_after_switch_budget(self):
        accounts = FakeAccounts(["a", "b", "c"])
        error = UpstreamHTTPError("/search", 401, "denied")
        with self.assertRaises(UpstreamHTTPError):
            run(accounts, {"a": error, "b": error, "c": None})
        self.assertEqual(accounts.used, [])

    def test_non_auth_error_is_not_retried(self):
        accounts = FakeAccounts(["a", "b"])
        with self.assertRaises(RuntimeError):
            run(accounts, {"a": RuntimeError("boom"), "b": None})
        self.assertEqual(accounts.slot_calls, [set()])

    def test_no_account_available_when_pool_is_busy(self):
        accounts = FakeAccounts(["a"])
        accounts.text_slot = contextlib.contextmanager(lambda *a, **k: (yield ""))
        with self.assertRaisesRegex(RuntimeError, "no available text account"):
            run(accounts, {})

    def test_anonymous_path_when_pool_is_empty(self):
        accounts = FakeAccounts([])
        result, account = run(accounts, {"": None})
        self.assertEqual(result["answer"], "q:")
        self.assertEqual(account, {})

    def test_exhausted_deadline_times_out(self):
        accounts = FakeAccounts(["a"])
        with mock.patch.object(search_execution.time, "monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaises(TimeoutError):
                run(accounts, {"a": None}, timeout_secs=5)


if __name__ == "__main__":
    unittest.main()

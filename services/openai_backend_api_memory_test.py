import unittest

from services.openai_backend_api import OpenAIBackendAPI


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = ""
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, enabled: object, memories: int) -> None:
        self.enabled = enabled
        self.memories = memories
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(("GET", url))
        if "/settings/user" in url:
            return _Response({"settings": {"m3m": self.enabled}})
        return _Response({"memories": [{"id": i} for i in range(self.memories)]})

    def patch(self, url: str, **_kwargs):
        self.calls.append(("PATCH", url))
        return _Response({})

    def delete(self, url: str, **_kwargs):
        self.calls.append(("DELETE", url))
        return _Response({})


def _backend(session: _Session) -> OpenAIBackendAPI:
    backend = object.__new__(OpenAIBackendAPI)
    backend.base_url = "https://chatgpt.com"
    backend.session = session
    backend._settings_headers = lambda path: {}
    return backend


class DisableMemoryTests(unittest.TestCase):
    def test_enabled_with_memories_turns_off_and_clears(self) -> None:
        session = _Session(True, 3)
        result = _backend(session).disable_memory()
        self.assertEqual(result, {"was_enabled": True, "cleared": 3})
        methods = [method for method, _ in session.calls]
        self.assertEqual(methods, ["GET", "GET", "PATCH", "DELETE"])
        self.assertIn("feature=m3m&value=false", session.calls[2][1])
        self.assertIn("clear_account_user_memory", session.calls[3][1])

    def test_already_off_and_empty_only_reads(self) -> None:
        session = _Session(False, 0)
        result = _backend(session).disable_memory()
        self.assertEqual(result, {"was_enabled": False, "cleared": 0})
        self.assertEqual([method for method, _ in session.calls], ["GET", "GET"])

    def test_unknown_setting_is_turned_off(self) -> None:
        session = _Session(None, 0)
        _backend(session).disable_memory()
        self.assertIn("PATCH", [method for method, _ in session.calls])


if __name__ == "__main__":
    unittest.main()

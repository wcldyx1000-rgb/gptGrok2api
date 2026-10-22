import unittest
from contextlib import nullcontext
from unittest.mock import patch

from services import account_test_service as module
from services.account_test_service import AccountTestService


def _account(**extra) -> dict:
    return {"access_token": "tok-secret", "management_id": "acct_x", "email": "a@example.test", "quota": 5, **extra}


class AccountTestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service_patch = patch.object(module, "account_service")
        self.account_service = self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.account_service.is_unlimited_image_quota_account.return_value = False
        self.account_service.refresh_access_token.side_effect = lambda token, **_kwargs: token
        slot = patch.object(module, "account_processing_slot", lambda: nullcontext())
        slot.start()
        self.addCleanup(slot.stop)
        backend = patch.object(module, "OpenAIBackendAPI")
        self.backend_cls = backend.start()
        self.addCleanup(backend.stop)

    def test_unknown_account_raises_lookup_error(self) -> None:
        self.account_service.get_account_by_id.return_value = None
        with self.assertRaises(LookupError):
            AccountTestService().execute({"account_id": "acct_x", "mode": "chat"})

    def test_chat_success_returns_text_and_marks_usage(self) -> None:
        self.account_service.get_account_by_id.return_value = _account()
        events = [{"type": "conversation.delta", "delta": "po"}, {"type": "other"}, {"type": "conversation.delta", "delta": "ng"}]
        with patch.object(module, "conversation_events", return_value=iter(events)) as stream:
            result = AccountTestService().execute({"account_id": "acct_x", "mode": "chat", "model": "gpt-5", "prompt": "ping"})
        self.assertEqual((result["status"], result["content"], result["mode"]), ("success", "pong", "chat"))
        self.assertEqual((result["quota_before_label"], result["quota_after_label"], result["quota_deducted"]), ("5", "5", False))
        self.assertEqual(stream.call_args.kwargs, {"model": "gpt-5", "prompt": "ping"})
        self.account_service.mark_text_used.assert_called_once_with("tok-secret")
        self.backend_cls.return_value.close.assert_called_once()

    def test_image_success_reports_quota_deduction(self) -> None:
        self.account_service.get_account_by_id.return_value = _account(quota=5)
        self.account_service.mark_image_result.return_value = _account(quota=4)
        output = {"data": [{"url": "https://img.example/1.png"}, {"b64_json": "QUJD"}]}
        with patch.object(module, "stream_image_outputs", return_value=iter(())), \
                patch.object(module, "collect_image_outputs", return_value=output):
            result = AccountTestService().execute({"account_id": "acct_x", "mode": "image", "model": "gpt-image-1", "prompt": "cat"})
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["quota_deducted"])
        self.assertEqual((result["quota_before_label"], result["quota_after_label"]), ("5", "4"))
        self.assertIn("![image_1](https://img.example/1.png)", result["content"])
        self.assertIn("data:image/png;base64,QUJD", result["content"])
        self.account_service.mark_image_result.assert_called_once_with("tok-secret", True)

    def test_codex_image_model_uses_codex_stream(self) -> None:
        self.account_service.get_account_by_id.return_value = _account()
        self.account_service.mark_image_result.return_value = _account()
        with patch.object(module, "stream_codex_image_outputs", return_value=iter(())) as codex, \
                patch.object(module, "stream_image_outputs", return_value=iter(())) as web, \
                patch.object(module, "collect_image_outputs", return_value={"data": [{"url": "u"}]}):
            AccountTestService().execute({"account_id": "acct_x", "mode": "image", "model": "codex-gpt-image-2", "prompt": "p"})
        codex.assert_called_once()
        web.assert_not_called()

    def test_failure_is_reported_without_leaking_tokens_and_does_not_touch_quota(self) -> None:
        self.account_service.get_account_by_id.return_value = _account(refresh_token="rt-secret")
        with patch.object(module, "conversation_events", side_effect=RuntimeError("401 for tok-secret / rt-secret")):
            result = AccountTestService().execute({"account_id": "acct_x", "mode": "chat", "model": "m", "prompt": "p"})
        self.assertEqual((result["status"], result["tone"], result["content"]), ("failed", "danger", ""))
        self.assertNotIn("tok-secret", result["error_message"])
        self.assertNotIn("rt-secret", result["error_message"])
        self.assertEqual(result["error_code"], "RuntimeError")
        self.account_service.mark_text_used.assert_not_called()
        self.account_service.mark_image_result.assert_not_called()

    def test_image_without_results_fails(self) -> None:
        self.account_service.get_account_by_id.return_value = _account()
        with patch.object(module, "stream_image_outputs", return_value=iter(())), \
                patch.object(module, "collect_image_outputs", return_value={"data": [], "message": "blocked"}):
            result = AccountTestService().execute({"account_id": "acct_x", "mode": "image", "model": "m", "prompt": "p"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_message"], "blocked")


if __name__ == "__main__":
    unittest.main()

"""Image call log diagnostics: actual resolution, generation attempts, detail lookup, compact list.

Attempt timeline design follows yukkcat/chatgpt2api (AGPL-3.0-only); this is an independent test module.
"""
import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from services import log_service as log_module
from services.config import config
from services.log_service import (
    LOG_TYPE_CALL,
    LoggedCall,
    LogService,
    collect_image_attempts,
    collect_result_images,
    compact_log_item,
    compact_log_page,
    format_result_resolutions,
    image_result_summary,
    mask_account_email,
)
from services.protocol import conversation as conv
from services.protocol.conversation_image_retry_test import (
    FakeAccounts,
    FakeBackend,
    ZH_ERROR,
    _stream,
)
from services.storage.call_record_repository import CallRecordRepository
from services.storage.database_storage import DatabaseStorageBackend


class _Recorder:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []

    def add(self, log_type, summary, detail):
        self.items.append((log_type, summary, detail))


class ConfigAttemptsTest(unittest.TestCase):
    KEYS = ("image_account_retry_enabled", "image_max_account_attempts", "image_account_retry_count")

    def setUp(self) -> None:
        self.saved = {key: config.data.get(key) for key in self.KEYS}
        for key in self.KEYS:
            config.data.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                config.data.pop(key, None)
            else:
                config.data[key] = value

    def test_defaults(self) -> None:
        self.assertTrue(config.image_account_retry_enabled)
        self.assertEqual(config.image_max_account_attempts, 4)
        self.assertEqual(config.image_account_retry_count, 3)

    def test_legacy_retry_count_converts_to_attempts(self) -> None:
        config.data["image_account_retry_count"] = 2
        self.assertEqual(config.image_max_account_attempts, 3)
        self.assertEqual(config.image_account_retry_count, 2)
        config.data["image_account_retry_count"] = 0
        self.assertEqual(config.image_max_account_attempts, 1)
        self.assertEqual(config.image_account_retry_count, 0)

    def test_new_key_wins_over_legacy_and_is_clamped(self) -> None:
        config.data["image_account_retry_count"] = 0
        config.data["image_max_account_attempts"] = 99
        self.assertEqual(config.image_max_account_attempts, 10)
        config.data["image_max_account_attempts"] = 0
        self.assertEqual(config.image_max_account_attempts, 1)
        config.data["image_max_account_attempts"] = "abc"
        self.assertEqual(config.image_max_account_attempts, 4)

    def test_disabled_switch_turns_retries_off(self) -> None:
        config.data["image_max_account_attempts"] = 5
        config.data["image_account_retry_enabled"] = False
        self.assertEqual(config.image_account_retry_count, 0)
        config.data["image_account_retry_enabled"] = True
        self.assertEqual(config.image_account_retry_count, 4)


class ImageAttemptRecordingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {key: config.data.get(key) for key in ConfigAttemptsTest.KEYS}
        for key in ConfigAttemptsTest.KEYS:
            config.data.pop(key, None)
        config.data["image_max_account_attempts"] = 3
        self.accounts = FakeAccounts(["a", "b", "c"])
        egress = SimpleNamespace(
            image_concurrency_limit=0,
            egress_key="proxy:test",
            egress_label="node-1",
            proxy_source="account_group",
            proxy_url="",
        )

        class Backend(FakeBackend):
            def __init__(self, access_token, **kwargs):
                super().__init__(access_token, **kwargs)
                self.proxy_profile = egress

        self.patches = [
            patch.object(conv, "account_service", self.accounts),
            patch.object(conv, "OpenAIBackendAPI", Backend),
            patch.object(conv, "proxy_settings", SimpleNamespace(
                acquire_image_egress=lambda *a, **k: 0,
                release_image_egress=lambda *a, **k: None,
                get_fallback_proxy_reference=lambda: None,
            )),
            patch.object(conv, "_cleanup_image_conversations_after_success", lambda *a, **k: None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        for key, value in self.saved.items():
            if value is None:
                config.data.pop(key, None)
            else:
                config.data[key] = value

    def _run(self, behaviors):
        with patch.object(conv, "stream_image_outputs", _stream(behaviors)):
            return conv._generate_single_image(conv.ConversationRequest(model="gpt-image-2"), 1, 1)

    def test_plain_success_records_single_attempt_with_egress_and_duration(self) -> None:
        outputs = self._run({})
        attempts = outputs[-1].image_attempts
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "success")
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(attempts[0]["egress_key"], "proxy:test")
        self.assertEqual(attempts[0]["egress_label"], "node-1")
        self.assertFalse(attempts[0]["switched_account"])
        self.assertGreaterEqual(attempts[0]["duration_ms"], 0)

    def test_switch_records_failed_then_success_attempt(self) -> None:
        outputs = self._run({"a": conv.ImageTextReplyError(ZH_ERROR)})
        attempts = outputs[-1].image_attempts
        self.assertEqual([a["status"] for a in attempts], ["failed", "success"])
        self.assertEqual([a["attempt"] for a in attempts], [1, 2])
        self.assertTrue(attempts[0]["switched_account"])
        self.assertEqual(attempts[0]["failure_code"], "upstream_error_reply")
        self.assertEqual(attempts[0]["account_email"], "a@x")
        self.assertEqual(attempts[1]["account_email"], "b@x")

    def test_attempts_flow_into_result_dict_and_log_masks_account(self) -> None:
        outputs = self._run({"a": conv.ImageTextReplyError(ZH_ERROR)})
        result = conv.collect_image_outputs(outputs)
        self.assertEqual(len(result["_image_attempts"]), 2)
        normalized = collect_image_attempts(result)
        self.assertEqual([a["account"] for a in normalized], ["a***@x", "b***@x"])
        self.assertTrue(all("account_email" not in a for a in normalized))

    def test_max_attempts_one_disables_switching(self) -> None:
        config.data["image_max_account_attempts"] = 1
        with patch.object(conv, "stream_image_outputs", _stream({"a": conv.ImageTextReplyError(ZH_ERROR)})):
            with self.assertRaises(conv.ImageGenerationError) as info:
                conv._generate_single_image(conv.ConversationRequest(model="gpt-image-2"), 1, 1)
        self.assertEqual(len(info.exception.image_attempts), 1)
        self.assertFalse(info.exception.image_attempts[0]["switched_account"])

    def test_disabled_switch_stops_after_first_account(self) -> None:
        config.data["image_account_retry_enabled"] = False
        with patch.object(conv, "stream_image_outputs", _stream({"a": conv.ImageTextReplyError(ZH_ERROR)})):
            with self.assertRaises(conv.ImageGenerationError):
                conv._generate_single_image(conv.ConversationRequest(model="gpt-image-2"), 1, 1)
        self.assertEqual(self.accounts.penalized, [("a", "upstream_error_reply")])

    def test_exhausted_error_carries_all_attempts_to_log_fields(self) -> None:
        behaviors = {t: conv.ImageTextReplyError(ZH_ERROR) for t in "abc"}
        with patch.object(conv, "stream_image_outputs", _stream(behaviors)):
            with self.assertRaises(conv.ImageGenerationError) as info:
                conv._generate_single_image(conv.ConversationRequest(model="gpt-image-2"), 1, 1)
        fields = log_module._exception_log_fields(info.exception)
        self.assertEqual(len(fields["image_attempts"]), 3)
        self.assertEqual([a["switched_account"] for a in fields["image_attempts"]], [True, True, False])


def _png_item(width: int, height: int) -> dict:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return {"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii"), "width": width, "height": height}


class ResultImageTest(unittest.TestCase):
    def test_resolution_from_response_data_and_sse_chunks(self) -> None:
        self.assertEqual(
            collect_result_images({"data": [{"url": "/images/a.png", "width": 1024, "height": 1536}]}),
            [{"width": 1024, "height": 1536}],
        )
        sse = {"type": "image_generation.completed", "b64_json": "xx", "_image_result": {"width": 512, "height": 512}}
        self.assertEqual(collect_result_images([sse]), [{"width": 512, "height": 512}])
        summary = image_result_summary({"data": [_png_item(64, 32), _png_item(64, 32)]})
        self.assertEqual(summary["result_image_count"], 2)

    def test_resolution_falls_back_to_local_file_for_chat_style_replies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "x").mkdir()
            Image.new("RGB", (30, 20)).save(Path(tmp, "x", "a.png"))
            fake_config = SimpleNamespace(images_dir=Path(tmp))
            with patch("services.image_service.config", fake_config), patch(
                "services.image_service.image_storage_service"
            ):
                images = collect_result_images("no sizes here", ["http://h/images/x/a.png?v=1"])
        self.assertEqual(images, [{"width": 30, "height": 20}])

    def test_format_resolutions(self) -> None:
        one = [{"width": 1024, "height": 1024}]
        self.assertEqual(format_result_resolutions(one), "1024×1024")
        self.assertEqual(format_result_resolutions(one * 3), "1024×1024 ×3")
        self.assertEqual(
            format_result_resolutions(one + [{"width": 2, "height": 3}]), "1024×1024 / 2×3"
        )
        self.assertEqual(format_result_resolutions([]), "")

    def test_mask_email(self) -> None:
        self.assertEqual(mask_account_email("alice@example.com"), "al***@example.com")
        self.assertEqual(mask_account_email("ab@example.com"), "a***@example.com")
        self.assertEqual(mask_account_email(""), "")

    def test_logged_call_non_stream_records_resolution_and_attempts(self) -> None:
        recorder = _Recorder()
        result = {
            "created": 1,
            "data": [{"url": "/images/a.png", "width": 1024, "height": 1024}],
            "_image_attempts": [
                {"slot": 1, "attempt": 1, "status": "success", "account_email": "bob@x.com", "duration_ms": 5}
            ],
        }
        call = LoggedCall({"id": "k"}, "/v1/images/generations", "gpt-image-2", "文生图")
        with patch.object(log_module, "log_service", recorder), patch.object(
            log_module.realtime_monitor_service, "finish", lambda detail: None
        ):
            call.log("调用完成", result)
        detail = recorder.items[0][2]
        self.assertEqual(detail["result_images"], [{"width": 1024, "height": 1024}])
        self.assertEqual(detail["result_image_count"], 1)
        self.assertEqual(detail["image_attempts"][0]["account"], "bo***@x.com")

    def test_logged_call_stream_records_resolution(self) -> None:
        recorder = _Recorder()
        call = LoggedCall({"id": "k"}, "/v1/images/generations", "gpt-image-2", "文生图")
        chunks = [
            {"type": "image_generation.completed", "b64_json": "xx", "_image_result": {"width": 768, "height": 512},
             "_image_attempts": [{"slot": 1, "attempt": 1, "status": "success", "account_email": "bob@x.com"}]},
        ]
        with patch.object(log_module, "log_service", recorder), patch.object(
            log_module.realtime_monitor_service, "finish", lambda detail: None
        ):
            out = list(call.stream(iter(chunks)))
        self.assertNotIn("_image_result", out[0])
        self.assertNotIn("_image_attempts", out[0])
        detail = recorder.items[0][2]
        self.assertEqual(detail["result_images"], [{"width": 768, "height": 512}])
        self.assertEqual(detail["image_attempts"][0]["status"], "success")

    def test_logged_call_failure_keeps_attempts(self) -> None:
        recorder = _Recorder()
        err = conv.ImageGenerationError("boom")
        err.image_attempts = [{"slot": 1, "attempt": 1, "status": "failed", "account_email": "bob@x.com"}]

        def handler(*_args):
            raise err

        call = LoggedCall({"id": "k"}, "/v1/images/generations", "gpt-image-2", "文生图")
        with patch.object(log_module, "log_service", recorder), patch.object(
            log_module.realtime_monitor_service, "finish", lambda detail: None
        ), patch.object(log_module.realtime_monitor_service, "start", lambda *a, **k: None), patch.object(
            log_module.realtime_monitor_service, "stage", lambda *a, **k: None
        ):
            asyncio.run(call.run(handler, {"prompt": "x", "size": "1024x1024"}))
        detail = recorder.items[0][2]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["image_attempts"][0]["account"], "bo***@x.com")
        self.assertEqual(detail["request_meta"]["size"], "1024x1024")


class StudioTaskLogTest(unittest.TestCase):
    def test_studio_task_log_has_resolution_attempts_and_request_meta(self) -> None:
        import time

        from services.image_task_service import ImageTaskService

        identity = {"id": "u", "name": "n", "role": "admin"}

        def handler(payload):
            return {
                "created": 1,
                "data": [{"b64_json": "xx", "width": 1536, "height": 1024}],
                "_image_attempts": [{"slot": 1, "attempt": 1, "status": "success", "account_email": "bob@x.com"}],
            }

        with tempfile.TemporaryDirectory() as tmp:
            service = ImageTaskService(Path(tmp) / "tasks.json", generation_handler=handler)
            with patch("services.image_task_service.log_service.add") as add_log:
                service.submit_generation(
                    identity, client_task_id="t1", prompt="p", model="gpt-image-2", n=1, size="1536x1024",
                    base_url="http://h",
                )
                deadline = time.time() + 3
                while time.time() < deadline and not add_log.called:
                    time.sleep(0.01)
        detail = add_log.call_args.args[2]
        self.assertEqual(detail["result_images"], [{"width": 1536, "height": 1024}])
        self.assertEqual(detail["image_attempts"][0]["account"], "bo***@x.com")
        self.assertEqual(detail["request_meta"].get("size"), "1536x1024")


class LogDetailLookupTest(unittest.TestCase):
    def _detail(self) -> dict:
        return {
            "endpoint": "/v1/images/generations",
            "model": "gpt-image-2",
            "status": "success",
            "request_text": "x" * 800,
            "request_text_full": "x" * 800,
            "error": "e" * 900,
            "monitor": {"events": [{"a": 1}], "proxy_source": "account", "egress_label": "n1"},
            "image_attempts": [{"slot": 1, "attempt": 1, "status": "success"}],
            "result_images": [{"width": 1, "height": 2}],
            "perf": {"account_wait_ms": 5},
        }

    def test_json_store_get_detail_returns_full_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = LogService(Path(tmp) / "logs.jsonl")
            logs.add(LOG_TYPE_CALL, "one", self._detail())
            logs.add(LOG_TYPE_CALL, "two", {"endpoint": "/v1/chat/completions", "status": "success"})
            first = logs.list_page(limit=10)["items"][1]
            item = logs.get_detail(first["id"])
            self.assertEqual(item["summary"], "one")
            self.assertEqual(len(item["detail"]["request_text_full"]), 800)
            self.assertIsNone(logs.get_detail("missing"))
            self.assertIsNone(logs.get_detail(""))

    def test_json_store_get_detail_finds_legacy_line_without_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs.jsonl"
            path.write_text('{"time":"2025-01-01 00:00:00","type":"call","summary":"old","detail":{}}\n', encoding="utf-8")
            logs = LogService(path)
            legacy_id = logs.list_page(limit=5)["items"][0]["id"]
            self.assertEqual(logs.get_detail(legacy_id)["summary"], "old")

    def test_database_store_get_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = DatabaseStorageBackend(f"sqlite:///{(Path(tmp) / 'app.db').as_posix()}")
            repo = CallRecordRepository(backend)
            try:
                logs = LogService(Path(tmp) / "logs.jsonl", store_resolver=lambda: repo)
                logs.add(LOG_TYPE_CALL, "db", self._detail())
                record_id = logs.list_page(limit=1)["items"][0]["id"]
                item = logs.get_detail(record_id)
                self.assertEqual(item["summary"], "db")
                self.assertEqual(item["detail"]["image_attempts"][0]["attempt"], 1)
                self.assertIsNone(logs.get_detail("nope"))
            finally:
                repo.close()
                backend.engine.dispose()

    def test_compact_item_drops_heavy_fields_but_keeps_list_fields(self) -> None:
        item = {"id": "1", "type": "call", "summary": "s", "detail": self._detail()}
        compact = compact_log_item(item)["detail"]
        for key in ("request_text_full", "monitor", "image_attempts"):
            self.assertNotIn(key, compact)
        self.assertTrue(compact["detail_compact"])
        self.assertEqual(compact["image_attempt_count"], 1)
        self.assertEqual(compact["result_images"], [{"width": 1, "height": 2}])
        self.assertEqual(compact["egress_label"], "n1")
        self.assertLessEqual(len(compact["request_text"]), 400)
        self.assertLessEqual(len(compact["error"]), 400)
        # original untouched, full record still complete
        self.assertEqual(len(item["detail"]["request_text_full"]), 800)

    def test_compact_page_keeps_envelope(self) -> None:
        page = {"items": [{"id": "1", "detail": {"monitor": {}, "status": "success"}}], "total": 1}
        out = compact_log_page(page)
        self.assertEqual(out["total"], 1)
        self.assertNotIn("monitor", out["items"][0]["detail"])


if __name__ == "__main__":
    unittest.main()

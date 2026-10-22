import base64
import io
import unittest
from unittest.mock import patch

from PIL import Image

from services import log_service as log_module
from services.log_service import LoggedCall
from services.protocol import conversation as conv
from services.protocol import openai_v1_image_generations as generations
from services.protocol.conversation import ImageOutput, format_image_result, stream_image_chunks


def _png_b64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _Recorder:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []

    def add(self, log_type, summary, detail):
        self.items.append((log_type, summary, detail))


class StreamResponseFormatTest(unittest.TestCase):
    def test_stream_url_format_keeps_both_b64_and_url(self) -> None:
        with patch.object(conv, "save_image_bytes", return_value="http://host/images/a.png"):
            result = format_image_result([{"b64_json": _png_b64()}], "p", conv.STREAM_URL_RESPONSE_FORMAT)
        item = result["data"][0]
        self.assertTrue(item["b64_json"])
        self.assertEqual(item["url"], "http://host/images/a.png")

    def test_plain_formats_unchanged(self) -> None:
        with patch.object(conv, "save_image_bytes", return_value="http://host/images/a.png"):
            b64 = format_image_result([{"b64_json": _png_b64()}], "p", "b64_json")["data"][0]
            url = format_image_result([{"b64_json": _png_b64()}], "p", "url")["data"][0]
        self.assertIn("b64_json", b64)
        self.assertNotIn("url", b64)
        self.assertEqual(url["url"], "http://host/images/a.png")
        self.assertNotIn("b64_json", url)

    def test_stream_handler_requests_both_when_url_asked(self) -> None:
        seen = []

        def fake_outputs(request):
            seen.append(request.response_format)
            return iter(())

        with patch.object(generations, "stream_image_outputs_with_pool", fake_outputs):
            list(generations.handle({"prompt": "p", "stream": True, "response_format": "url"}))
            generations.handle({"prompt": "p", "response_format": "url"})
        self.assertEqual(seen, [conv.STREAM_URL_RESPONSE_FORMAT, "url"])

    def test_url_only_item_still_emits_completed(self) -> None:
        output = ImageOutput(kind="result", model="m", index=1, total=1, created=1,
                             data=[{"url": "http://host/images/a.png"}])
        events = list(stream_image_chunks(iter([output])))
        self.assertEqual(events[0]["type"], "image_generation.completed")
        self.assertEqual(events[0]["url"], "http://host/images/a.png")

    def test_empty_stream_emits_failed_event(self) -> None:
        progress = ImageOutput(kind="progress", model="m", index=1, total=1, created=1, text="")
        events = list(stream_image_chunks(iter([progress])))
        self.assertEqual(events[-1]["type"], "image_generation.failed")
        self.assertEqual(list(stream_image_chunks(iter(())))[-1]["type"], "image_generation.failed")


class LoggedStreamFailureTest(unittest.TestCase):
    def _run(self, chunks):
        recorder = _Recorder()
        call = LoggedCall({"id": "k"}, "/v1/images/generations", "gpt-image-2", "文生图")
        with patch.object(log_module, "log_service", recorder), patch.object(
            log_module.realtime_monitor_service, "finish", lambda detail: None
        ):
            list(call.stream(iter(chunks)))
        return recorder.items[0][2]

    def test_stream_with_only_failed_event_is_logged_as_failed(self) -> None:
        detail = self._run([{"type": "image_generation.failed", "error": {"message": "no image"}}])
        self.assertEqual(detail["status"], "failed")
        self.assertIn("no image", detail["error"])

    def test_completed_stream_is_logged_as_success(self) -> None:
        detail = self._run([{"type": "image_generation.completed", "b64_json": "xx"}])
        self.assertEqual(detail["status"], "success")


if __name__ == "__main__":
    unittest.main()

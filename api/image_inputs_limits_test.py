import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from api import ai, image_inputs
from api.errors import install_exception_handlers


def _upload(data: bytes, name: str = "a.png") -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name, headers=None)


class ReadImageSourcesLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def assert_invalid(self, sources):
        with self.assertRaises(HTTPException) as ctx:
            await image_inputs.read_image_sources(sources)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["error"]["code"], "invalid_image_input")
        self.assertEqual(ctx.exception.detail["error"]["type"], "invalid_request_error")

    async def test_empty_sources(self):
        await self.assert_invalid([])

    async def test_too_many_images(self):
        await self.assert_invalid([(b"x", "a.png", "image/png")] * 17)

    async def test_sixteen_images_ok(self):
        result = await image_inputs.read_image_sources([(b"x", "a.png", "image/png")] * 16)
        self.assertEqual(len(result), 16)

    async def test_empty_upload(self):
        await self.assert_invalid([_upload(b"")])

    async def test_upload_over_single_limit_stops_early(self):
        data = io.BytesIO(b"x" * (10 * 64 * 1024))
        upload = UploadFile(data, filename="a.png", headers=None)
        with patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 100 * 1024):
            await self.assert_invalid([upload])
        # 超限即停：没有把剩余内容读完（读到 128KB 时已超 100KB）
        self.assertTrue(data.closed)

    async def test_upload_chunked_ok(self):
        result = await image_inputs.read_image_sources([_upload(b"y" * 200_000)])
        self.assertEqual(len(result[0][0]), 200_000)

    async def test_total_over_limit(self):
        with (
            patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 10),
            patch.object(image_inputs, "MAX_IMAGE_INPUT_BYTES", 15),
        ):
            await self.assert_invalid([(b"x" * 8, "a.png", "image/png"), (b"x" * 8, "b.png", "image/png")])

    async def test_url_is_downloaded_via_ssrf_guarded_helper(self):
        with patch.object(image_inputs, "_download_image_url", return_value=(b"z", "i.png", "image/png")) as dl:
            result = await image_inputs.read_image_sources(["https://example.com/i.png"])
        dl.assert_called_once()
        self.assertEqual(result[0][0], b"z")

    def test_base64_length_precheck(self):
        with patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 30):
            with patch.object(image_inputs.base64, "b64decode") as decode:
                with self.assertRaises(HTTPException) as ctx:
                    image_inputs._decode_base64_image("A" * 200, "a.png", "image/png")
                decode.assert_not_called()
        self.assertEqual(ctx.exception.detail["error"]["code"], "invalid_image_input")


class ImageRoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(ai.create_router())
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer k"}

    def post_json(self, path, body):
        with (
            patch("api.ai.require_identity", return_value={"id": "u"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
        ):
            return self.client.post(path, headers=self.headers, json=body)

    def test_generations_n_too_large(self):
        response = self.post_json("/v1/images/generations", {"prompt": "p", "n": 5})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "n_too_large")

    def test_generations_n_invalid(self):
        response = self.post_json("/v1/images/generations", {"prompt": "p", "n": 0})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_n")

    def test_edits_n_too_large(self):
        response = self.post_json("/v1/images/edits", {"prompt": "p", "n": 5, "image": "https://example.com/a.png"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "n_too_large")

    def test_edits_missing_image(self):
        response = self.post_json("/v1/images/edits", {"prompt": "p"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_image_input")

    def test_edits_without_mask_does_not_set_payload_mask(self):
        captured = {}

        async def fake_run(self_call, handler, payload):
            captured.update(payload)
            return {"data": []}

        with (
            patch("api.ai.require_identity", return_value={"id": "u"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch.object(ai.LoggedCall, "run", fake_run),
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=self.headers,
                json={"prompt": "p", "image": "data:image/png;base64,eA=="},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mask", captured)
        self.assertEqual(len(captured["images"]), 1)

    def test_responses_image_tool_n_too_large(self):
        response = self.post_json(
            "/v1/responses",
            {"model": "gpt-5", "input": "draw", "tools": [{"type": "image_generation", "n": 9}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "n_too_large")

    def test_chat_n_too_large(self):
        from services.protocol import openai_v1_chat_complete

        with self.assertRaises(HTTPException) as ctx:
            openai_v1_chat_complete.chat_image_args({"prompt": "draw", "n": 7})
        self.assertEqual(ctx.exception.detail["error"]["code"], "n_too_large")


if __name__ == "__main__":
    unittest.main()

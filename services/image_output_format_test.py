import base64
import io
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from services.image_output_format import convert_image_bytes, normalize_image_output_format
from services.image_storage_service import _content_type_for_rel, _image_extension
from utils.helper import build_chat_image_markdown_content


def _png(mode: str = "RGB", color=(10, 20, 30)) -> bytes:
    output = io.BytesIO()
    Image.new(mode, (8, 6), color).save(output, format="PNG")
    return output.getvalue()


def _format(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        return str(image.format)


class NormalizeOutputFormatTest(unittest.TestCase):
    def test_aliases_and_empty(self):
        self.assertIsNone(normalize_image_output_format(None))
        self.assertIsNone(normalize_image_output_format("  "))
        self.assertEqual(normalize_image_output_format("JPG"), "jpeg")
        self.assertEqual(normalize_image_output_format("jpeg"), "jpeg")
        self.assertEqual(normalize_image_output_format("webp"), "webp")
        self.assertEqual(normalize_image_output_format("png"), "png")

    def test_unsupported_rejected_with_400(self):
        for value in ("gif", "bmp", "svg"):
            with self.assertRaises(HTTPException) as ctx:
                normalize_image_output_format(value)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail["error"]["code"], "invalid_output_format")


class ConvertImageBytesTest(unittest.TestCase):
    def test_png_to_jpeg_and_webp(self):
        source = _png()
        self.assertEqual(_format(convert_image_bytes(source, "jpg")), "JPEG")
        self.assertEqual(_format(convert_image_bytes(source, "webp")), "WEBP")

    def test_transparent_png_to_jpeg_uses_white_background(self):
        converted = convert_image_bytes(_png("RGBA", (0, 0, 0, 0)), "jpeg")
        with Image.open(io.BytesIO(converted)) as image:
            self.assertEqual(image.mode, "RGB")
            r, g, b = image.getpixel((0, 0))
            self.assertGreater(min(r, g, b), 240)

    def test_same_format_or_unset_returns_original_object(self):
        source = _png()
        self.assertIs(convert_image_bytes(source, "png"), source)
        self.assertIs(convert_image_bytes(source, None), source)

    def test_broken_bytes_fall_back_to_original(self):
        broken = b"not an image"
        self.assertIs(convert_image_bytes(broken, "jpeg"), broken)


class StorageAndMarkdownTest(unittest.TestCase):
    def test_extension_and_content_type_follow_bytes(self):
        source = _png()
        self.assertEqual(_image_extension(source), ".png")
        self.assertEqual(_image_extension(convert_image_bytes(source, "jpeg")), ".jpg")
        self.assertEqual(_image_extension(convert_image_bytes(source, "webp")), ".webp")
        self.assertEqual(_content_type_for_rel("2026/11/20/a.jpg"), "image/jpeg")
        self.assertEqual(_content_type_for_rel("2026/11/20/a.webp"), "image/webp")
        self.assertEqual(_content_type_for_rel("2026/11/20/a.png"), "image/png")

    def test_markdown_data_url_uses_real_mime(self):
        source = _png()
        cases = {
            "image/png": source,
            "image/jpeg": convert_image_bytes(source, "jpeg"),
            "image/webp": convert_image_bytes(source, "webp"),
        }
        for mime, data in cases.items():
            content = build_chat_image_markdown_content({"data": [{"b64_json": base64.b64encode(data).decode()}]})
            self.assertIn(f"data:{mime};base64,", content)


class FormatImageResultTest(unittest.TestCase):
    def test_output_format_converts_after_upscale_and_reencodes_b64(self):
        from services.protocol import conversation

        source = _png()
        saved: list[bytes] = []

        def fake_save(data, base_url=None, **_kwargs):
            saved.append(data)
            return "http://x/images/a"

        with (
            patch.object(conversation, "save_image_bytes", side_effect=fake_save),
            patch.object(conversation, "upscale_image_if_needed", side_effect=lambda data, size: data),
        ):
            result = conversation.format_image_result(
                [{"b64_json": base64.b64encode(source).decode()}],
                "p",
                "b64_json",
                output_format="jpeg",
            )
        returned = base64.b64decode(result["data"][0]["b64_json"])
        self.assertEqual(_format(returned), "JPEG")
        self.assertEqual(saved, [returned])


if __name__ == "__main__":
    unittest.main()

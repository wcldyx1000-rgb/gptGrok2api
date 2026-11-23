import base64
import io
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from services.image_output_format import (
    convert_image_bytes,
    gif_prompt_hints,
    image_output_options,
    is_gif_bytes,
    normalize_image_output_format,
)
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
        self.assertEqual(normalize_image_output_format("GIF"), "gif")

    def test_unsupported_rejected_with_400(self):
        for value in ("bmp", "svg"):
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


def _gif(frames: int = 3) -> bytes:
    images = [Image.new("RGB", (8, 6), (index * 40, 0, 0)) for index in range(frames)]
    output = io.BytesIO()
    images[0].save(output, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return output.getvalue()


class GifOptionsTest(unittest.TestCase):
    def test_frame_options_kept_only_for_gif(self):
        self.assertEqual(
            image_output_options({"output_format": "gif", "frame_count": "8", "frame_duration_ms": 150}),
            {"output_format": "gif", "frame_count": 8, "frame_duration_ms": 150},
        )
        self.assertEqual(
            image_output_options({"output_format": "png", "frame_count": 8}),
            {"output_format": "png", "frame_count": None, "frame_duration_ms": None},
        )
        self.assertEqual(
            image_output_options({}),
            {"output_format": None, "frame_count": None, "frame_duration_ms": None},
        )

    def test_invalid_frame_options_rejected(self):
        for body, param in (
            ({"output_format": "gif", "frame_count": "abc"}, "frame_count"),
            ({"output_format": "gif", "frame_count": 1}, "frame_count"),
            ({"output_format": "gif", "frame_duration_ms": 10}, "frame_duration_ms"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                image_output_options(body)
            self.assertEqual(ctx.exception.detail["error"]["param"], param)

    def test_prompt_hints(self):
        self.assertEqual(gif_prompt_hints("png", 8), [])
        self.assertEqual(gif_prompt_hints("gif"), ["生成格式为 gif。"])
        self.assertEqual(
            gif_prompt_hints("gif", 8, 150),
            ["生成格式为 gif。", "frame_count: 8", "frame_duration_ms: 150"],
        )

    def test_build_image_prompt_appends_gif_lines(self):
        from services.protocol.conversation import build_image_prompt

        prompt = build_image_prompt("一只猫", None, "auto", "gif", 8, None)
        self.assertEqual(prompt, "一只猫\n\n输出图片质量为 auto。\n生成格式为 gif。\nframe_count: 8")
        self.assertNotIn("gif", build_image_prompt("一只猫", None, "auto", "png", 8, 150))

    def test_gif_bytes_kept_and_stored_as_gif(self):
        gif = _gif()
        self.assertTrue(is_gif_bytes(gif))
        # 官网给了 GIF：要 gif 或不指定格式都原样保留；官网给的是 PNG 时不硬转成 GIF。
        self.assertIs(convert_image_bytes(gif, "gif"), gif)
        self.assertIs(convert_image_bytes(gif, None), gif)
        png = _png()
        self.assertIs(convert_image_bytes(png, "gif"), png)
        self.assertEqual(_image_extension(gif), ".gif")
        self.assertEqual(_content_type_for_rel("2026/11/20/a.gif"), "image/gif")
        markdown = build_chat_image_markdown_content({"data": [{"b64_json": base64.b64encode(gif).decode()}]})
        self.assertIn("data:image/gif;base64,", markdown)


class FormatImageResultTest(unittest.TestCase):
    def test_gif_skips_upscale_and_keeps_all_frames(self):
        from services.protocol import conversation

        source = _gif(4)
        upscale_calls: list[bytes] = []
        with (
            patch.object(conversation, "save_image_bytes", return_value="http://x/images/a.gif"),
            patch.object(conversation, "upscale_image_if_needed",
                         side_effect=lambda data, size: upscale_calls.append(data) or _png()),
        ):
            result = conversation.format_image_result(
                [{"b64_json": base64.b64encode(source).decode()}],
                "p",
                "b64_json",
                requested_size="2048x2048",
                output_format="gif",
            )
        self.assertEqual(upscale_calls, [])
        self.assertEqual(base64.b64decode(result["data"][0]["b64_json"]), source)

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

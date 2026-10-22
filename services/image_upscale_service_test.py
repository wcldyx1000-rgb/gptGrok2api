import io
import unittest
from unittest.mock import PropertyMock, patch

from PIL import Image

from services import image_upscale_service
from services.config import config


def _png(size) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


class ImageUpscaleTest(unittest.TestCase):
    def _enabled(self, value: bool):
        return patch.object(type(config), "image_upscale_enabled", new_callable=PropertyMock, return_value=value)

    def test_disabled_returns_original(self) -> None:
        data = _png((100, 100))
        with self._enabled(False):
            self.assertIs(image_upscale_service.upscale_image_if_needed(data, "200x200"), data)

    def test_upscales_smaller_image_to_requested_size(self) -> None:
        data = _png((100, 50))
        with self._enabled(True):
            result = image_upscale_service.upscale_image_if_needed(data, "400x200")
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (400, 200))
            self.assertEqual(image.format, "PNG")

    def test_skips_when_already_large_enough_or_size_invalid(self) -> None:
        data = _png((300, 300))
        with self._enabled(True):
            self.assertIs(image_upscale_service.upscale_image_if_needed(data, "200x200"), data)
            self.assertIs(image_upscale_service.upscale_image_if_needed(data, "auto"), data)
            self.assertIs(image_upscale_service.upscale_image_if_needed(data, "99999x99999"), data)

    def test_failure_returns_original(self) -> None:
        data = _png((10, 10))
        with self._enabled(True), patch.object(image_upscale_service, "_pillow_lanczos", side_effect=OSError("boom")):
            self.assertIs(image_upscale_service.upscale_image_if_needed(data, "200x200"), data)


if __name__ == "__main__":
    unittest.main()


class ImageUpscaleEngineTest(unittest.TestCase):
    def _patches(self, engine: str):
        return (
            patch.object(type(config), "image_upscale_enabled", new_callable=PropertyMock, return_value=True),
            patch.object(type(config), "image_upscale_engine", new_callable=PropertyMock, return_value=engine),
        )

    def test_sharp_engine_used_when_available(self) -> None:
        data = _png((100, 50))
        fake = _png((400, 200))
        a, b = self._patches("sharp_lanczos3")
        with a, b, patch.object(image_upscale_service, "_sharp_lanczos3", return_value=fake) as sharp:
            self.assertEqual(image_upscale_service.upscale_image_if_needed(data, "400x200"), fake)
        sharp.assert_called_once()

    def test_sharp_unavailable_falls_back_to_pillow(self) -> None:
        data = _png((100, 50))
        a, b = self._patches("sharp_lanczos3")
        with a, b, patch.object(image_upscale_service, "_sharp_lanczos3", side_effect=RuntimeError("no node")), \
                patch.object(image_upscale_service.logger, "warning") as warn:
            result = image_upscale_service.upscale_image_if_needed(data, "400x200")
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (400, 200))
        self.assertEqual(warn.call_args.args[0]["event"], "image_upscale_sharp_fallback")

    def test_missing_node_raises_runtime_error(self) -> None:
        with patch.object(image_upscale_service.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                image_upscale_service._sharp_lanczos3(b"x", (10, 10))

    def test_pillow_engine_does_not_call_sharp(self) -> None:
        data = _png((100, 50))
        a, b = self._patches("pillow_lanczos")
        with a, b, patch.object(image_upscale_service, "_sharp_lanczos3") as sharp:
            image_upscale_service.upscale_image_if_needed(data, "400x200")
        sharp.assert_not_called()

    def test_config_engine_defaults_to_pillow_and_rejects_unknown(self) -> None:
        with patch.dict(config.data, {}, clear=False):
            config.data.pop("image_upscale_engine", None)
            self.assertEqual(config.image_upscale_engine, "pillow_lanczos")
        with patch.dict(config.data, {"image_upscale_engine": "bogus"}):
            self.assertEqual(config.image_upscale_engine, "pillow_lanczos")
        with patch.dict(config.data, {"image_upscale_engine": "SHARP_LANCZOS3"}):
            self.assertEqual(config.image_upscale_engine, "sharp_lanczos3")

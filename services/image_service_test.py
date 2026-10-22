import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from services import image_service
from services.config import config


def _png_bytes(size=(640, 480), color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    return buf.getvalue()


class _TempImageDirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.images_dir = root / "images"
        self.thumbs_dir = root / "thumbs"
        self.images_dir.mkdir()
        self.thumbs_dir.mkdir()
        cls = type(config)
        self._patches = [
            patch.object(cls, "images_dir", new_callable=PropertyMock, return_value=self.images_dir),
            patch.object(cls, "image_thumbnails_dir", new_callable=PropertyMock, return_value=self.thumbs_dir),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _write_image(self, rel: str, data: bytes | None = None) -> Path:
        path = self.images_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data or _png_bytes())
        return path


class ThumbnailTest(_TempImageDirs):
    def test_concurrent_requests_generate_thumbnail_once(self) -> None:
        rel = "2026/10/21/1760000000_0123456789abcdef0123456789abcdef.png"
        self._write_image(rel)
        real_open = image_service.Image.open
        opened = []
        barrier = threading.Barrier(8)

        def counting_open(*args, **kwargs):
            opened.append(1)
            return real_open(*args, **kwargs)

        results: list[Path] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                results.append(image_service.ensure_thumbnail(rel))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with patch.object(image_service.image_storage_service, "has_local", return_value=True), \
                patch.object(image_service.Image, "open", side_effect=counting_open):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(set(results)), 1)
        with Image.open(results[0]) as thumb:
            self.assertLessEqual(max(thumb.size), 320)

    def test_thumbnail_is_written_via_temp_file_and_atomic_replace(self) -> None:
        rel = "a/b.png"
        self._write_image(rel)
        target = image_service._thumbnail_path(rel)
        replaced = []
        real_replace = image_service.os.replace

        def recording_replace(src, dst):
            # 替换发生前目标文件还不存在，临时文件已经完整写好。
            self.assertFalse(Path(dst).exists())
            self.assertTrue(Path(src).is_file())
            self.assertNotEqual(Path(src), Path(dst))
            replaced.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        with patch.object(image_service.image_storage_service, "has_local", return_value=True), \
                patch.object(image_service.os, "replace", side_effect=recording_replace):
            path = image_service.ensure_thumbnail(rel)

        self.assertEqual(path, target)
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0][1], target)
        self.assertFalse(replaced[0][0].exists())

    def test_failed_thumbnail_leaves_no_partial_or_temp_file(self) -> None:
        rel = "broken.png"
        self._write_image(rel, b"not an image")
        with patch.object(image_service.image_storage_service, "has_local", return_value=True):
            with self.assertRaises(image_service.HTTPException) as ctx:
                image_service.ensure_thumbnail(rel)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual([p for p in self.thumbs_dir.rglob("*") if p.is_file()], [])


class ImageResponseHeadersTest(_TempImageDirs):
    def test_content_addressed_image_is_cached_immutably(self) -> None:
        rel = "2026/10/21/1760000000_0123456789abcdef0123456789abcdef.png"
        self._write_image(rel)
        with patch.object(image_service.image_storage_service, "has_local", return_value=True):
            response = image_service.get_image_response(rel)
            thumb = image_service.get_thumbnail_response(rel)
        self.assertEqual(response.headers["cache-control"], image_service.IMMUTABLE_CACHE_CONTROL)
        self.assertEqual(thumb.headers["cache-control"], image_service.IMMUTABLE_CACHE_CONTROL)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    def test_other_files_get_short_cache(self) -> None:
        rel = "manual/cover.png"
        self._write_image(rel)
        with patch.object(image_service.image_storage_service, "has_local", return_value=True):
            response = image_service.get_image_response(rel)
        self.assertEqual(response.headers["cache-control"], image_service.SHORT_CACHE_CONTROL)

    def test_remote_image_media_type_comes_from_content(self) -> None:
        rel = "remote/1760000000_0123456789abcdef0123456789abcdef.png"
        with patch.object(image_service.image_storage_service, "has_local", return_value=False), \
                patch.object(image_service.image_storage_service, "get_bytes", return_value=_jpeg_bytes()):
            response = image_service.get_image_response(rel)
            download = image_service.get_image_download_response(rel)
        self.assertEqual(response.media_type, "image/jpeg")
        self.assertEqual(download.media_type, "image/jpeg")


class MediaTypeTest(unittest.TestCase):
    def test_sniffs_known_signatures(self) -> None:
        self.assertEqual(image_service.media_type_for_bytes(_png_bytes((4, 4))), "image/png")
        self.assertEqual(image_service.media_type_for_bytes(_jpeg_bytes()), "image/jpeg")
        self.assertEqual(image_service.media_type_for_bytes(b"GIF89a\x00\x00"), "image/gif")
        self.assertEqual(image_service.media_type_for_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertEqual(image_service.media_type_for_bytes(b"\x00\x00\x00\x18ftypmp42"), "video/mp4")

    def test_falls_back_to_extension_then_png(self) -> None:
        self.assertEqual(image_service.media_type_for_bytes(b"????", "x/clip.webm"), "video/webm")
        self.assertEqual(image_service.media_type_for_bytes(b"????", "x/noext"), "image/png")


class ImageRoutesUseThreadpoolTest(unittest.TestCase):
    def setUp(self) -> None:
        from api import system

        self.system = system
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def test_image_and_thumbnail_routes_run_off_event_loop(self) -> None:
        from fastapi.responses import Response

        calls: list[tuple[str, str]] = []

        def fake_image(path: str):
            calls.append(("image", threading.current_thread().name))
            return Response(content=b"x", media_type="image/png")

        def fake_thumb(path: str):
            calls.append(("thumb", threading.current_thread().name))
            return Response(content=b"y", media_type="image/png")

        loop_threads: list[str] = []
        real_run = self.system.run_in_threadpool

        async def recording_run(func, *args, **kwargs):
            loop_threads.append(threading.current_thread().name)
            return await real_run(func, *args, **kwargs)

        with patch.object(self.system, "get_image_response", side_effect=fake_image), \
                patch.object(self.system, "get_thumbnail_response", side_effect=fake_thumb), \
                patch.object(self.system, "run_in_threadpool", side_effect=recording_run):
            self.assertEqual(self.client.get("/images/a/b.png").status_code, 200)
            self.assertEqual(self.client.get("/image-thumbnails/a/b.png").status_code, 200)

        self.assertEqual([kind for kind, _ in calls], ["image", "thumb"])
        self.assertEqual(len(loop_threads), 2)
        for (_, worker_thread), loop_thread in zip(calls, loop_threads):
            self.assertNotEqual(worker_thread, loop_thread)


if __name__ == "__main__":
    unittest.main()

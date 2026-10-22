"""Tests for ImageStorageService delete tombstones, lock granularity and WebDAV sync.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only) test intent; uses a fake WebDAV
client and temporary directories only (never the real data/ directory).
"""

import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from services import image_service, image_storage_service as iss
from services.config import config
from services.image_storage_service import (
    ImageBatchDeleteError,
    ImageStorageError,
    ImageStorageService,
)


def _png(color=(10, 20, 30), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeWebDAV:
    """In-memory WebDAV double shared by every client instance created in a test."""

    def __init__(self, service: ImageStorageService | None = None) -> None:
        self.remote: dict[str, bytes] = {}
        self.fail_delete = False
        self.fail_put = False
        self.sessions_closed = 0
        self.clients_created = 0
        self.lock_held_during_io: list[str] = []
        self.service = service

    def factory(self, settings):
        outer = self
        outer.clients_created += 1

        class _Session:
            def close(self_inner) -> None:
                outer.sessions_closed += 1

        class _Client:
            session = _Session()

            def _probe(self_inner, op: str) -> None:
                # The global index lock must never be held while talking to WebDAV.
                if outer.service is not None and outer.service._index_lock.locked():
                    outer.lock_held_during_io.append(op)

            def put(self_inner, rel, payload, content_type="image/png"):
                self_inner._probe("put")
                if outer.fail_put:
                    raise ImageStorageError("put failed")
                outer.remote[rel] = bytes(payload)
                return f"https://dav.example/{rel}"

            def get(self_inner, rel):
                self_inner._probe("get")
                return outer.remote[rel]

            def delete(self_inner, rel):
                self_inner._probe("delete")
                if outer.fail_delete:
                    raise ImageStorageError("delete failed")
                return outer.remote.pop(rel, None) is not None

        return _Client()


class _Base(unittest.TestCase):
    mode = "local"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.images_dir = root / "images"
        self.thumbs_dir = root / "thumbs"
        self.images_dir.mkdir()
        self.thumbs_dir.mkdir()
        cls = type(config)
        self.service = ImageStorageService(root / "image_index.json")
        self.dav = FakeWebDAV(self.service)
        self._patches = [
            patch.object(cls, "images_dir", new_callable=PropertyMock, return_value=self.images_dir),
            patch.object(cls, "image_thumbnails_dir", new_callable=PropertyMock, return_value=self.thumbs_dir),
            patch.object(config, "get_image_storage_settings", side_effect=lambda: {
                "mode": self.mode, "webdav_url": "https://dav.example", "public_base_url": "",
            }),
            patch.object(iss, "WebDAVClient", side_effect=self.dav.factory),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def index(self) -> dict:
        return self.service._load_clean_index()

    def pending(self) -> dict:
        return self.service._load_remote_delete_pending()


class SaveTest(_Base):
    def test_save_does_not_scan_or_expire_the_tree(self) -> None:
        old = self.images_dir / "2020" / "01" / "01" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(_png())
        os.utime(old, (1, 1))
        with patch.object(type(config), "cleanup_old_images", side_effect=AssertionError("must not be called")):
            stored = self.service.save(_png(), "http://x")
        self.assertTrue(old.exists())  # save() never unlinks other files
        self.assertTrue((self.images_dir / stored.rel).is_file())
        item = self.index()[stored.rel]
        self.assertTrue(item["generation"])
        self.assertEqual(item["size"], len(_png()))
        leftovers = [p for p in self.images_dir.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_save_clears_pending_delete_tombstone_for_same_path(self) -> None:
        payload = _png()
        rel = "2026/01/02/same.png"
        self.service._save_remote_delete_pending({rel: self.service._delete_tombstone(generation="g", scope="asset", remote=True)})
        with patch.object(self.service, "make_relative_path", return_value=rel):
            self.service.save(payload, "")
        self.assertEqual(self.pending(), {})
        self.assertIn(rel, self.index())

    def test_webdav_session_is_closed_after_save(self) -> None:
        self.mode = "both"
        self.service.save(_png(), "")
        self.assertEqual(self.dav.clients_created, 1)
        self.assertEqual(self.dav.sessions_closed, 1)
        self.assertEqual(self.dav.lock_held_during_io, [])


class DeleteTombstoneTest(_Base):
    mode = "both"

    def _saved(self) -> str:
        stored = self.service.save(_png(), "")
        return stored.rel

    def test_successful_delete_clears_tombstone_and_remote(self) -> None:
        rel = self._saved()
        self.assertTrue(self.service.delete(rel))
        self.assertNotIn(rel, self.dav.remote)
        self.assertFalse((self.images_dir / rel).exists())
        self.assertEqual(self.pending(), {})
        self.assertNotIn(rel, self.index())
        self.assertEqual(self.dav.sessions_closed, 2)  # save + delete
        self.assertEqual(self.dav.lock_held_during_io, [])

    def test_remote_failure_keeps_tombstone_and_retry_completes(self) -> None:
        rel = self._saved()
        self.dav.fail_delete = True
        # Local copy is gone, so the call succeeds but the remote delete stays pending.
        self.assertTrue(self.service.delete(rel))
        self.assertFalse((self.images_dir / rel).exists())
        self.assertIn(rel, self.dav.remote)
        tombstone = self.pending()[rel]
        self.assertTrue(tombstone["remote"])
        self.assertEqual(tombstone["scope"], "asset")
        self.assertNotIn(rel, self.index())  # no longer listed

        # Still failing: retry reports failure and keeps the tombstone.
        result = self.service.retry_pending_remote_deletes()
        self.assertEqual(result, {"pending": 1, "failed": 1})
        self.assertIn(rel, self.pending())

        self.dav.fail_delete = False
        result = self.service.retry_pending_remote_deletes()
        self.assertEqual(result["failed"], 0)
        self.assertNotIn(rel, self.dav.remote)
        self.assertEqual(self.pending(), {})

    def test_remote_only_failure_raises_and_keeps_asset_and_tombstone(self) -> None:
        rel = self._saved()
        (self.images_dir / rel).unlink()
        self.dav.fail_delete = True
        with self.assertRaises(ImageStorageError):
            self.service.delete(rel)
        self.assertIn(rel, self.dav.remote)
        self.assertIn(rel, self.pending())
        self.assertIn(rel, self.index())

    def test_delete_many_reports_completed_on_batch_error(self) -> None:
        a = self._saved()
        time.sleep(0.01)
        with patch.object(self.service, "make_relative_path", return_value="2026/01/01/b.png"):
            b = self.service.save(_png((1, 2, 3)), "").rel
        (self.images_dir / b).unlink()  # remote-only
        self.dav.fail_delete = True
        with self.assertRaises(ImageBatchDeleteError) as ctx:
            self.service.delete_many([b, a])
        self.assertNotIn(b, ctx.exception.completed_rels)
        self.assertIn(b, self.pending())

    def test_stale_tombstone_does_not_delete_newer_generation(self) -> None:
        rel = self._saved()
        planned = self.service._prepare_delete_batch([rel])
        # A newer save of the same path replaces the asset before the mutation runs.
        item = self.index()[rel]
        item["generation"] = "newer-generation"
        items = self.index()
        items[rel] = item
        self.service._save_index(items)
        result, error = self.service._mutate_delete_tombstone(rel, planned[rel], self.dav.factory({}))
        self.assertFalse(result.completed)
        self.assertIsNone(error)
        self.assertTrue((self.images_dir / rel).exists())
        self.assertIn(rel, self.dav.remote)

    def test_pending_tombstone_survives_restart(self) -> None:
        rel = self._saved()
        self.dav.fail_delete = True
        self.service.delete(rel)
        restarted = ImageStorageService(self.service.index_file)
        self.assertIn(rel, restarted._load_remote_delete_pending())
        self.dav.fail_delete = False
        restarted.retry_pending_remote_deletes()
        self.assertNotIn(rel, self.dav.remote)
        self.assertEqual(restarted._load_remote_delete_pending(), {})

    def test_retry_without_webdav_url_keeps_tombstones(self) -> None:
        rel = self._saved()
        self.dav.fail_delete = True
        self.service.delete(rel)
        with patch.object(config, "get_image_storage_settings", return_value={"mode": "local", "webdav_url": ""}):
            self.assertEqual(self.service.retry_pending_remote_deletes(), {"pending": 1, "failed": 1})
        self.assertIn(rel, self.pending())

    def test_local_only_delete_has_no_pending_tombstone(self) -> None:
        self.mode = "local"
        rel = self.service.save(_png(), "").rel
        self.assertTrue(self.service.delete(rel))
        self.assertEqual(self.pending(), {})
        self.assertEqual(self.dav.clients_created, 0)


class SyncTest(_Base):
    mode = "both"

    def test_sync_uploads_without_holding_index_lock_and_closes_session(self) -> None:
        self.mode = "local"
        rels = [self.service.save(_png((i, 0, 0)), "").rel for i in range(1)]
        with patch.object(self.service, "make_relative_path", return_value="2026/01/01/two.png"):
            rels.append(self.service.save(_png((0, 9, 0)), "").rel)
        self.mode = "both"
        result = self.service.sync_all()
        self.assertEqual(result, {"uploaded": 2, "skipped": 0, "failed": 0})
        self.assertEqual(set(self.dav.remote), set(rels))
        self.assertEqual(self.dav.lock_held_during_io, [])
        self.assertEqual(self.dav.sessions_closed, 1)
        self.assertTrue(all(item["webdav"] for item in self.index().values()))
        # Second run only skips.
        self.assertEqual(self.service.sync_all()["skipped"], 2)

    def test_stale_webdav_flag_with_changed_size_is_reuploaded(self) -> None:
        rel = self.service.save(_png(), "").rel
        self.assertIn(rel, self.dav.remote)
        # Local bytes change behind the index (e.g. an external recompression).
        new_payload = _png((200, 100, 50), size=(80, 80))
        (self.images_dir / rel).write_bytes(new_payload)
        result = self.service.sync_all()
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(self.dav.remote[rel], new_payload)
        item = self.index()[rel]
        self.assertEqual(item["size"], len(new_payload))
        self.assertNotIn("remote_sync_pending", item)

    def test_upload_failure_counts_failed_and_keeps_local(self) -> None:
        self.mode = "local"
        rel = self.service.save(_png(), "").rel
        self.mode = "both"
        self.dav.fail_put = True
        result = self.service.sync_all()
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertTrue((self.images_dir / rel).exists())
        self.assertFalse(self.index().get(rel, {}).get("webdav"))

    def test_asset_deleted_during_upload_is_not_resurrected(self) -> None:
        self.mode = "local"
        rel = self.service.save(_png(), "").rel
        self.mode = "both"
        real_put = self.dav.factory

        def racing_factory(settings):
            client = real_put(settings)
            original = client.put

            def put(r, payload, content_type="image/png"):
                url = original(r, payload, content_type)
                # Delete the asset after its bytes reached the remote but before the merge.
                self.mode_before = self.mode
                self.service.delete_many([r])
                return url

            client.put = put
            return client

        with patch.object(iss, "WebDAVClient", side_effect=racing_factory):
            self.service.sync_all()
        self.assertNotIn(rel, self.index())
        self.assertFalse((self.images_dir / rel).exists())
        self.assertEqual(self.pending(), {})  # remote copy cleaned (tombstone replay done)
        self.assertNotIn(rel, self.dav.remote)

    def test_sync_rejects_local_mode_and_concurrent_runs(self) -> None:
        self.mode = "local"
        with self.assertRaises(ImageStorageError):
            self.service.sync_all()
        self.mode = "both"
        self.service._sync_lock.acquire()
        try:
            with self.assertRaises(ImageStorageError):
                self.service.sync_all()
        finally:
            self.service._sync_lock.release()

    def test_save_and_list_not_blocked_during_slow_upload(self) -> None:
        self.mode = "local"
        self.service.save(_png(), "")
        self.mode = "both"
        started = threading.Event()
        release = threading.Event()
        real_factory = self.dav.factory

        def slow_factory(settings):
            client = real_factory(settings)
            original = client.put

            def put(rel, payload, content_type="image/png"):
                started.set()
                release.wait(5)
                return original(rel, payload, content_type)

            client.put = put
            return client

        results: dict[str, object] = {}
        with patch.object(iss, "WebDAVClient", side_effect=slow_factory):
            worker = threading.Thread(target=lambda: results.update(sync=self.service.sync_all()))
            worker.start()
            self.assertTrue(started.wait(5))
            # While the upload is in flight, list/delete/save-like index access must not block.
            acquired = self.service._index_lock.acquire(timeout=1)
            self.assertTrue(acquired)
            self.service._index_lock.release()
            items = self.service.list_items("", refresh_index=False)
            self.assertEqual(len(items), 1)
            release.set()
            worker.join(10)
        self.assertEqual(results["sync"]["uploaded"], 1)


class ListItemsTest(_Base):
    def test_unindexed_discovery_runs_outside_the_index_lock(self) -> None:
        path = self.images_dir / "2026" / "01" / "01" / "manual.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(_png())
        observed: list[bool] = []
        original = self.service._discover_unindexed

        def probe(known, skip):
            observed.append(self.service._index_lock.locked())
            return original(known, skip)

        with patch.object(self.service, "_discover_unindexed", side_effect=probe):
            items = self.service.list_items("http://x")
        self.assertEqual(observed, [False])
        self.assertEqual([i["rel"] for i in items], ["2026/01/01/manual.png"])
        self.assertEqual(items[0]["width"], 64)
        self.assertTrue(self.index()["2026/01/01/manual.png"]["generation"])

    def test_paged_listing_never_reads_unindexed_files(self) -> None:
        path = self.images_dir / "2026" / "01" / "01" / "manual.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(_png())
        with patch.object(self.service, "_discover_unindexed", side_effect=AssertionError("no scan")):
            self.assertEqual(self.service.list_items("", refresh_index=False, verify_existing=False), [])

    def test_tombstoned_asset_is_not_reindexed_from_disk(self) -> None:
        rel = "2026/01/01/pending.png"
        path = self.images_dir / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(_png())
        self.service._save_remote_delete_pending({rel: self.service._delete_tombstone(generation="", scope="asset", remote=False)})
        self.assertEqual(self.service.list_items(""), [])

    def test_verify_marks_size_drift_as_remote_sync_pending(self) -> None:
        self.mode = "both"
        rel = self.service.save(_png(), "").rel
        (self.images_dir / rel).write_bytes(_png(size=(10, 10)))
        self.service.list_items("")
        item = self.index()[rel]
        self.assertTrue(item["remote_sync_pending"])
        self.assertEqual(item["size"], len(_png(size=(10, 10))))


class LocalCopyAndCompressTest(_Base):
    mode = "both"

    def test_disk_cleanup_keeps_remote_copy_and_skips_pending(self) -> None:
        a = self.service.save(_png((1, 1, 1)), "").rel
        with patch.object(self.service, "make_relative_path", return_value="2026/01/01/b.png"):
            b = self.service.save(_png((2, 2, 2), size=(65, 49)), "").rel
        items = self.index()
        items[b]["remote_sync_pending"] = True
        self.service._save_index(items)

        removals = self.service.delete_local_copies_until([b, a], 10**9)
        self.assertEqual([r.rel for r in removals], [a])
        self.assertTrue(removals[0].remote_remains)
        self.assertFalse((self.images_dir / a).exists())
        self.assertTrue((self.images_dir / b).exists())  # pending copy is the only up-to-date one
        self.assertIn(a, self.dav.remote)  # remote retained
        self.assertEqual(self.index()[a]["storage"], "webdav")
        self.assertFalse(self.index()[a]["local"])

    def test_disk_cleanup_skips_copy_whose_size_differs_from_index(self) -> None:
        rel = self.service.save(_png(), "").rel
        (self.images_dir / rel).write_bytes(_png(size=(9, 9)))
        self.assertEqual(self.service.delete_local_copies_until([rel], 10**9), [])
        self.assertTrue((self.images_dir / rel).exists())

    def test_dry_run_changes_nothing(self) -> None:
        rel = self.service.save(_png(), "").rel
        removals = self.service.delete_local_copies_until([rel], 10**9, dry_run=True)
        self.assertEqual([r.rel for r in removals], [rel])
        self.assertTrue((self.images_dir / rel).exists())
        self.assertTrue(self.index()[rel]["local"])

    def test_compress_updates_index_marks_remote_stale_and_keeps_mtime(self) -> None:
        big = io.BytesIO()
        Image.new("RGB", (200, 200), (3, 4, 5)).save(big, format="PNG", compress_level=0)
        rel = self.service.save(big.getvalue(), "").rel
        path = self.images_dir / rel
        os.utime(path, (1_000_000_000, 1_000_000_000))
        thumb = self.thumbs_dir / f"{rel}.png"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"x")

        result = self.service.compress_local_images()
        self.assertEqual(result["compressed"], 1)
        self.assertGreater(result["saved_bytes"], 0)
        item = self.index()[rel]
        self.assertEqual(item["size"], path.stat().st_size)
        self.assertTrue(item["remote_sync_pending"])
        self.assertEqual(int(path.stat().st_mtime), 1_000_000_000)
        self.assertFalse(thumb.exists())
        self.assertEqual(list(self.images_dir.rglob("*.tmp")), [])
        # The next sync repairs the remote copy and clears the flag.
        self.service.sync_all()
        self.assertNotIn("remote_sync_pending", self.index()[rel])
        self.assertEqual(self.dav.remote[rel], path.read_bytes())


class ImageServiceIntegrationTest(_Base):
    mode = "both"

    def setUp(self) -> None:
        super().setUp()
        self._svc_patches = [
            patch.object(image_service, "image_storage_service", self.service),
            patch.object(image_service, "remove_tags"),
        ]
        for item in self._svc_patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._svc_patches):
            item.stop()
        super().tearDown()

    def test_retention_cleanup_goes_through_tombstones_and_retry(self) -> None:
        rel = self.service.save(_png(), "").rel
        path = self.images_dir / rel
        old = time.time() - 5 * 3600
        os.utime(path, (old, old))
        self.dav.fail_delete = True
        with patch.object(type(config), "image_retention_hours", new_callable=PropertyMock, return_value=1):
            result = image_service.cleanup_image_retention(1)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(path.exists())
        self.assertIn(rel, self.pending())  # remote delete failed -> tombstone kept
        self.dav.fail_delete = False
        with patch.object(type(config), "image_retention_hours", new_callable=PropertyMock, return_value=1):
            image_service.cleanup_image_retention(1)  # also replays pending remote deletes
        self.assertEqual(self.pending(), {})
        self.assertNotIn(rel, self.dav.remote)

    def test_delete_to_target_uses_storage_service(self) -> None:
        rel = self.service.save(_png(), "").rel
        usage = type("U", (), {"free": 0, "total": 10, "used": 10})
        with patch("shutil.disk_usage", return_value=usage):
            dry = image_service.delete_to_target(1, dry_run=True)
            self.assertEqual(dry["removed"], 1)
            self.assertTrue((self.images_dir / rel).exists())
            real = image_service.delete_to_target(1)
        self.assertEqual(real["removed"], 1)
        self.assertFalse((self.images_dir / rel).exists())
        self.assertIn(rel, self.dav.remote)  # WebDAV copy survives disk-pressure cleanup

    def test_compress_images_delegates(self) -> None:
        with patch.object(self.service, "compress_local_images", return_value={"compressed": 0}) as mocked:
            image_service.compress_images(40)
        mocked.assert_called_once_with(40)

    def test_delete_images_removes_remote_and_derivatives(self) -> None:
        rel = self.service.save(_png(), "").rel
        thumb = self.thumbs_dir / f"{rel}.png"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"x")
        result = image_service.delete_images([rel])
        self.assertEqual(result, {"removed": 1})
        self.assertNotIn(rel, self.dav.remote)
        self.assertFalse(thumb.exists())

    def test_config_cleanup_old_images_is_a_delegating_shim(self) -> None:
        with patch.object(image_service, "cleanup_image_retention", return_value={"removed": 3}) as mocked:
            self.assertEqual(config.cleanup_old_images(), 3)
        mocked.assert_called_once()


class ImagesRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        from api import system

        self.system = system
        app = FastAPI()
        app.include_router(system.create_router("1.0.0"))
        self.client = TestClient(app)

    def test_images_default_is_paged_and_zero_stays_supported(self) -> None:
        seen: list[int] = []

        def fake_list(base_url, **kwargs):
            seen.append(kwargs["limit"])
            return {"items": []}

        with patch.object(self.system, "require_admin"), patch.object(self.system, "list_images", side_effect=fake_list):
            self.assertEqual(self.client.get("/api/images").status_code, 200)
            self.assertEqual(self.client.get("/api/images?limit=0").status_code, 200)
            self.assertEqual(self.client.get("/api/images?limit=501").status_code, 422)
        self.assertEqual(seen, [60, 0])

    def test_cleanup_to_target_rejects_non_positive_target(self) -> None:
        with patch.object(self.system, "require_admin"), \
                patch.object(self.system.retention_cleanup_coordinator, "cleanup_images_to_target",
                             return_value={"removed": 0}) as mocked:
            self.assertEqual(self.client.post("/api/images/storage/cleanup-to-target?target_free_mb=0").status_code, 422)
            self.assertEqual(self.client.post("/api/images/storage/cleanup-to-target?target_free_mb=-5").status_code, 422)
            mocked.assert_not_called()
            self.assertEqual(self.client.post("/api/images/storage/cleanup-to-target?target_free_mb=100").status_code, 200)
            mocked.assert_called_once_with(100, dry_run=False)


if __name__ == "__main__":
    unittest.main()

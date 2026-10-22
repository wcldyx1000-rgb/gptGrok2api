import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import image_service


class ImageRetentionHoursTest(unittest.TestCase):
    def test_rolling_hour_cutoff_and_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "2026" / "01" / "old.png"
            fresh = root / "2026" / "01" / "fresh.png"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"x" * 10)
            fresh.write_bytes(b"y" * 5)
            now = time.time()
            os.utime(old, (now - 3 * 3600, now - 3 * 3600))
            fake = SimpleNamespace(images_dir=root, image_retention_hours=24)
            with patch.object(image_service, "config", fake):
                preview = image_service.preview_image_retention_cleanup(2)
                self.assertEqual(preview["removed"], 1)
                self.assertEqual(preview["removed_size_bytes"], 10)
                self.assertEqual(preview["retention_hours"], 2)
                self.assertTrue(preview["dry_run"])
                # 24 小时保留期下 3 小时前的文件不应被清理。
                self.assertEqual(image_service.preview_image_retention_cleanup()["removed"], 0)
            self.assertTrue(old.exists())

    def test_expiry_for_item_uses_hours(self):
        created = image_service.beijing_now().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        expired, remaining = image_service._expiry_for_item({"created_at": created}, 2)
        self.assertFalse(expired)
        self.assertTrue(0 < remaining <= 2 * 3600)

    def test_expires_at_for_item_adds_retention_hours(self):
        item = {"created_at": "2026-01-01 10:00:00"}
        self.assertEqual(image_service._expires_at_for_item(item, 2), "2026-01-01 12:00:00")
        self.assertIsNone(image_service._expires_at_for_item(item, 0))
        self.assertIsNone(image_service._expires_at_for_item({"created_at": "bad"}, 2))


if __name__ == "__main__":
    unittest.main()

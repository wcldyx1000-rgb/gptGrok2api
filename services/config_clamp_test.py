from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.config import ConfigStore, _normalize_backup_settings


class ConfigClampTest(unittest.TestCase):
    def _store(self, data: dict) -> ConfigStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        env = patch.dict("os.environ", {"CHATGPT2API_AUTH_KEY": "k"})
        env.start()
        self.addCleanup(env.stop)
        return ConfigStore(path)

    def test_poll_interval_and_initial_wait_are_capped(self) -> None:
        store = self._store({"image_poll_interval_secs": 9999, "image_poll_initial_wait_secs": 9999})
        self.assertEqual(store.image_poll_interval_secs, 60.0)
        self.assertEqual(store.image_poll_initial_wait_secs, 60.0)
        store.data.update({"image_poll_interval_secs": 0.01, "image_poll_initial_wait_secs": -3})
        self.assertEqual(store.image_poll_interval_secs, 0.5)
        self.assertEqual(store.image_poll_initial_wait_secs, 0.0)

    def test_poll_values_reject_nan_and_inf(self) -> None:
        store = self._store({"image_poll_interval_secs": float("nan"), "image_poll_initial_wait_secs": float("inf")})
        self.assertEqual(store.image_poll_interval_secs, 2.0)
        self.assertEqual(store.image_poll_initial_wait_secs, 1.0)

    def test_refresh_interval_has_lower_and_upper_bound(self) -> None:
        store = self._store({"refresh_account_interval_minute": 0})
        self.assertEqual(store.refresh_account_interval_minute, 1)
        store.data["refresh_account_interval_minute"] = 10**9
        self.assertEqual(store.refresh_account_interval_minute, 10080)
        store.data["refresh_account_interval_minute"] = "abc"
        self.assertEqual(store.refresh_account_interval_minute, 5)

    def test_backup_interval_upper_bound(self) -> None:
        self.assertEqual(_normalize_backup_settings({"interval_minutes": 10**12})["interval_minutes"], 525_600)
        self.assertEqual(_normalize_backup_settings({"interval_minutes": 0})["interval_minutes"], 1)
        self.assertEqual(_normalize_backup_settings({})["interval_minutes"], 360)


if __name__ == "__main__":
    unittest.main()

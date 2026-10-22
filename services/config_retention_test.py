import unittest

from services import config as config_module


class RetentionMigrationTest(unittest.TestCase):
    def promote(self, data):
        return config_module._promote_legacy_basic_settings(data)

    def test_days_are_converted_to_hours_and_removed(self):
        result = self.promote({"image_retention_days": 3, "log_retention_days": 7})
        self.assertEqual(result["image_retention_hours"], 72)
        self.assertEqual(result["log_retention_hours"], 168)
        self.assertNotIn("image_retention_days", result)
        self.assertNotIn("log_retention_days", result)

    def test_new_keys_win_over_legacy_days(self):
        result = self.promote({
            "image_retention_hours": 5,
            "image_retention_days": 30,
            "log_retention_hours": 12,
            "log_retention_days": 30,
        })
        self.assertEqual(result["image_retention_hours"], 5)
        self.assertEqual(result["log_retention_hours"], 12)

    def test_legacy_basic_image_expire_is_days(self):
        result = self.promote({"basic": {"image_expire_hours": 2}})
        self.assertEqual(result["image_retention_hours"], 48)

    def test_invalid_values_fall_back_without_crashing(self):
        result = self.promote({"image_retention_days": "abc", "log_retention_hours": "oops"})
        self.assertNotIn("image_retention_hours", result)
        self.assertEqual(result["log_retention_hours"], config_module.DEFAULT_RETENTION_HOURS)

    def test_hours_are_clamped(self):
        self.assertEqual(config_module._clamp_retention_hours(0), 1)
        self.assertEqual(config_module._clamp_retention_hours(10**9), config_module.MAX_RETENTION_HOURS)

    def test_legacy_basic_roundtrip_writes_days(self):
        basic = config_module._legacy_basic_from_settings({}, {"image_retention_hours": 25})
        self.assertEqual(basic["image_expire_hours"], 2)


if __name__ == "__main__":
    unittest.main()

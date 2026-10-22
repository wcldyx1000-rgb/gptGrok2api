import unittest
from unittest.mock import patch

from services.config import config


class ImageAccountConcurrencyTest(unittest.TestCase):
    def read(self, value="__missing__"):
        data = dict(config.data)
        data.pop("image_account_concurrency", None)
        if value != "__missing__":
            data["image_account_concurrency"] = value
        with patch.object(config, "data", data):
            return config.image_account_concurrency

    def test_default_is_one(self):
        self.assertEqual(self.read(), 1)

    def test_saved_two_and_three_are_kept(self):
        self.assertEqual(self.read(2), 2)
        self.assertEqual(self.read(3), 3)

    def test_clamped_to_one_through_three(self):
        self.assertEqual(self.read(0), 1)
        self.assertEqual(self.read(-5), 1)
        self.assertEqual(self.read(8), 3)

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(self.read("abc"), 1)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timezone

from utils.locale_env import LocaleEnv, locale_from_fields, normalize_language, normalize_locale_fields


class LocaleEnvTest(unittest.TestCase):
    def test_offset_follows_daylight_saving(self) -> None:
        env = LocaleEnv("America/New_York", "en-US")
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)

        self.assertEqual(env.offset_min(winter), 300)
        self.assertEqual(env.offset_min(summer), 240)
        self.assertIn("GMT-0500 (Eastern Standard Time)", env.date_string(winter))
        self.assertIn("GMT-0400 (Eastern Daylight Time)", env.date_string(summer))

    def test_east_eight_matches_js_sign(self) -> None:
        env = LocaleEnv("Asia/Shanghai", "zh-CN")
        self.assertEqual(env.offset_min(datetime(2026, 7, 1, tzinfo=timezone.utc)), -480)

    def test_unknown_long_name_falls_back_to_gmt(self) -> None:
        env = LocaleEnv("Asia/Kathmandu", "ne-NP")
        self.assertTrue(env.date_string(datetime(2026, 1, 1, tzinfo=timezone.utc)).endswith("(GMT+05:45)"))

    def test_languages_and_accept_language(self) -> None:
        env = LocaleEnv("Asia/Taipei", "zh-TW")
        self.assertEqual(env.languages, ["zh-TW", "zh", "en-US", "en"])
        self.assertEqual(env.accept_language, "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7")
        self.assertEqual(LocaleEnv("America/New_York", "en-US").languages, ["en-US", "en"])

    def test_normalize_language(self) -> None:
        self.assertEqual(normalize_language("zh_hant_tw"), "zh-Hant-TW")
        with self.assertRaises(ValueError):
            normalize_language("not a language")

    def test_normalize_locale_fields_requires_both(self) -> None:
        self.assertEqual(normalize_locale_fields("", ""), ("", ""))
        self.assertEqual(normalize_locale_fields("Europe/Berlin", "de_de"), ("Europe/Berlin", "de-DE"))
        with self.assertRaises(ValueError):
            normalize_locale_fields("Europe/Berlin", "")
        with self.assertRaises(ValueError):
            normalize_locale_fields("Mars/Base", "en-US")

    def test_locale_from_fields(self) -> None:
        self.assertIsNone(locale_from_fields({"timezone": "Europe/Berlin"}))
        self.assertIsNone(locale_from_fields({"timezone": "Mars/Base", "language": "en-US"}))
        self.assertEqual(
            locale_from_fields({"timezone": "Europe/Berlin", "language": "de-DE"}),
            LocaleEnv("Europe/Berlin", "de-DE"),
        )
        self.assertEqual(
            locale_from_fields({"locale": {"timezone": "Asia/Tokyo", "language": "ja-JP"}}),
            LocaleEnv("Asia/Tokyo", "ja-JP"),
        )


if __name__ == "__main__":
    unittest.main()

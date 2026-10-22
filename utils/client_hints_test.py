import unittest

from utils.client_hints import build_client_hints, consistent_overrides, parse_user_agent

CHROME_142_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
CHROME_124_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
EDGE_143_FULL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.7499.147 Safari/537.36 Edg/143.0.3650.96"
)
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


class ClientHintsTest(unittest.TestCase):
    def test_chrome_142_matches_real_browser_brand_list(self) -> None:
        hints = build_client_hints(CHROME_142_WINDOWS)

        self.assertEqual(
            hints["sec-ch-ua"],
            '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        )
        self.assertEqual(hints["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(hints["sec-ch-ua-mobile"], "?0")
        # 精简 UA 不知道真实构建号，不发送完整版本类高熵头。
        self.assertNotIn("sec-ch-ua-full-version", hints)
        self.assertNotIn("sec-ch-ua-full-version-list", hints)

    def test_mac_ua_reports_macos_without_windows_platform_version(self) -> None:
        hints = build_client_hints(CHROME_124_MAC)

        self.assertEqual(hints["sec-ch-ua-platform"], '"macOS"')
        self.assertNotIn("sec-ch-ua-platform-version", hints)
        self.assertIn('v="124"', hints["sec-ch-ua"])

    def test_full_version_edge_ua_sends_full_version_list(self) -> None:
        hints = build_client_hints(EDGE_143_FULL)

        self.assertEqual(hints["sec-ch-ua-full-version"], '"143.0.3650.96"')
        self.assertIn('"Microsoft Edge";v="143.0.3650.96"', hints["sec-ch-ua-full-version-list"])
        self.assertIn('"Chromium";v="143.0.7499.147"', hints["sec-ch-ua-full-version-list"])

    def test_non_chromium_ua_sends_no_client_hints(self) -> None:
        self.assertFalse(parse_user_agent(FIREFOX).is_chromium)
        self.assertEqual(build_client_hints(FIREFOX), {})

    def test_consistent_overrides_keeps_matching_values(self) -> None:
        stored = {
            "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            "sec-ch-ua-platform": '"Windows"',
            "user-agent": CHROME_142_WINDOWS,
        }

        self.assertEqual(
            consistent_overrides(CHROME_142_WINDOWS, stored),
            {
                "sec-ch-ua": stored["sec-ch-ua"],
                "sec-ch-ua-platform": '"Windows"',
            },
        )

    def test_consistent_overrides_drops_stale_values(self) -> None:
        chrome_136 = CHROME_142_WINDOWS.replace("142.0.0.0", "136.0.0.0")
        stored = {
            # 旧注册流程写死的 GREASE 品牌，与 Chrome 136 的真实品牌不一致。
            "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not_A Brand";v="99"',
            "sec-ch-ua-platform": '"macOS"',
        }

        self.assertEqual(consistent_overrides(chrome_136, stored), {})
        hints = build_client_hints(chrome_136, consistent_overrides(chrome_136, stored))
        self.assertNotIn("Not_A Brand", hints["sec-ch-ua"])
        self.assertEqual(hints["sec-ch-ua-platform"], '"Windows"')


if __name__ == "__main__":
    unittest.main()

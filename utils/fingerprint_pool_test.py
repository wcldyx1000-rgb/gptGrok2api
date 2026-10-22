import re
import unittest

from curl_cffi.requests import BrowserType

from utils.fingerprint_pool import FINGERPRINT_POOL, has_browser_fingerprint, pick_fingerprint, random_fingerprint


class FingerprintPoolTest(unittest.TestCase):
    def test_pick_is_stable_for_same_seed(self) -> None:
        first = pick_fingerprint("user@example.com")
        for _ in range(5):
            self.assertEqual(pick_fingerprint("user@example.com"), first)

    def test_different_seeds_spread_over_pool(self) -> None:
        picked = {pick_fingerprint(f"user-{index}@example.com")["user-agent"] for index in range(200)}
        self.assertGreater(len(picked), 3)

    def test_user_agent_major_matches_tls_target(self) -> None:
        supported = {item.value for item in BrowserType}
        for _, impersonate, user_agent in FINGERPRINT_POOL:
            self.assertIn(impersonate, supported)
            major = re.search(r"Chrome/(\d+)", user_agent).group(1)
            self.assertEqual(impersonate, f"chrome{major}")

    def test_pool_excludes_challenged_targets(self) -> None:
        targets = {impersonate for _, impersonate, _ in FINGERPRINT_POOL}
        self.assertNotIn("chrome131", targets)
        self.assertNotIn("chrome136", targets)

    def test_random_fingerprint_returns_pool_entry(self) -> None:
        impersonate, user_agent = random_fingerprint()
        self.assertIn((impersonate, user_agent), {(item[1], item[2]) for item in FINGERPRINT_POOL})

    def test_has_browser_fingerprint(self) -> None:
        self.assertFalse(has_browser_fingerprint({"email": "a@example.com"}))
        self.assertTrue(has_browser_fingerprint({"fp": {"User-Agent": "Mozilla/5.0"}}))
        self.assertTrue(has_browser_fingerprint({"impersonate": "chrome142"}))


if __name__ == "__main__":
    unittest.main()

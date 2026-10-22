import unittest

from utils import totp

# RFC 6238 附录 B 的 SHA1 测试密钥 "12345678901234567890" 的 base32 形式。
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TotpTest(unittest.TestCase):
    def test_rfc6238_sha1_vectors(self) -> None:
        vectors = {
            59: "94287082",
            1111111109: "07081804",
            1111111111: "14050471",
            1234567890: "89005924",
            2000000000: "69279037",
            20000000000: "65353130",
        }
        for at, expected in vectors.items():
            self.assertEqual(totp.generate(RFC_SECRET, at=at, digits=8), expected, at)

    def test_six_digit_code_is_suffix_of_eight_digit_code(self) -> None:
        self.assertEqual(totp.generate(RFC_SECRET, at=59), "287082")

    def test_normalize_accepts_spaces_lowercase_and_otpauth(self) -> None:
        self.assertEqual(totp.normalize_secret(" gezd gnbv gy3t qojq "), "GEZDGNBVGY3TQOJQ")
        uri = f"otpauth://totp/OpenAI:a@b.c?secret={RFC_SECRET.lower()}&issuer=OpenAI"
        self.assertEqual(totp.normalize_secret(uri), RFC_SECRET)
        self.assertEqual(totp.generate(uri, at=59, digits=8), "94287082")

    def test_invalid_secret(self) -> None:
        self.assertFalse(totp.is_valid_secret(""))
        self.assertFalse(totp.is_valid_secret("not-base32!!"))
        self.assertTrue(totp.is_valid_secret(RFC_SECRET))
        with self.assertRaises(ValueError):
            totp.generate("")

    def test_window_puts_current_code_first_and_includes_neighbours(self) -> None:
        at = 1111111111
        codes = totp.generate_window(RFC_SECRET, at=at, spread=1)
        self.assertEqual(codes[0], totp.generate(RFC_SECRET, at=at))
        self.assertIn(totp.generate(RFC_SECRET, at=at - 30), codes)
        self.assertIn(totp.generate(RFC_SECRET, at=at + 30), codes)
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main()

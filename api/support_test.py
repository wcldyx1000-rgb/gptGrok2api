import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import support


class AdminKeyComparisonTest(unittest.TestCase):
    def test_secure_equals_handles_non_ascii_and_mismatch(self):
        self.assertTrue(support.secure_equals("密钥-abc", "密钥-abc"))
        self.assertFalse(support.secure_equals("密钥-abc", "密钥-abd"))
        self.assertFalse(support.secure_equals("", "x"))

    def test_admin_identity_uses_constant_time_compare(self):
        with patch.object(support, "hmac") as fake_hmac:
            fake_hmac.compare_digest.return_value = True
            with patch.object(type(support.config), "auth_key", "admin-secret", create=True):
                identity = support._legacy_admin_identity("admin-secret")
        self.assertEqual(identity["role"], "admin")
        fake_hmac.compare_digest.assert_called_once_with(b"admin-secret", b"admin-secret")

    def test_wrong_and_non_ascii_token_rejected_without_error(self):
        with patch.object(type(support.config), "auth_key", "admin-secret", create=True):
            self.assertIsNone(support._legacy_admin_identity("wrong"))
            self.assertIsNone(support._legacy_admin_identity("错误"))
            with patch.object(support.auth_service, "authenticate", return_value=None):
                with self.assertRaises(HTTPException):
                    support.require_identity("Bearer 错误")


if __name__ == "__main__":
    unittest.main()

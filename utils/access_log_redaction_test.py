from __future__ import annotations

import logging
import unittest

from utils.access_log_redaction import AccessLogRedactionFilter, redact_query_secrets


class AccessLogRedactionTest(unittest.TestCase):
    def test_redacts_sensitive_query_params(self) -> None:
        out = redact_query_secrets("/api/x?a=1&token=SECRET&key=K2&ticket=T3&b=2")
        self.assertNotIn("SECRET", out)
        self.assertNotIn("K2", out)
        self.assertNotIn("T3", out)
        self.assertIn("a=1", out)
        self.assertIn("b=2", out)

    def test_filter_rewrites_uvicorn_args(self) -> None:
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/api/register/events?token=abc123", "1.1", 200), None,
        )
        AccessLogRedactionFilter().filter(record)
        self.assertNotIn("abc123", record.getMessage())
        self.assertIn("token=***", record.getMessage())


if __name__ == "__main__":
    unittest.main()

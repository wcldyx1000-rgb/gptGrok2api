from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from services.register import domain_mail
from services.register.domain_mail import (
    CloudflareError,
    DomainMailExhausted,
    _cf,
    _decode,
    _load,
    _today,
    cf_setup,
    fetch_imap_messages,
    human_local_part,
    issue_address,
    mask_secret,
    message_addressed_to,
    parse_domains,
    parse_imap_message,
    usage_today,
)


class ParseDomainsTest(unittest.TestCase):
    def test_parses_list_and_normalizes(self) -> None:
        raw = ["@Example.COM.", "  SUB.DOMAIN.ORG  ", "test.net"]
        self.assertEqual(parse_domains(raw), ["example.com", "sub.domain.org", "test.net"])

    def test_parses_comma_and_newline_separated_string(self) -> None:
        raw = "alpha.com, beta.org\ngamma.io, @delta.dev."
        self.assertEqual(parse_domains(raw), ["alpha.com", "beta.org", "gamma.io", "delta.dev"])

    def test_deduplicates_preserving_order(self) -> None:
        raw = ["a.com", "b.com", "A.COM", "c.com", "b.com"]
        self.assertEqual(parse_domains(raw), ["a.com", "b.com", "c.com"])

    def test_filters_invalid_and_empty_domains(self) -> None:
        raw = ["", "   ", None, "invalid_domain", "http://domain.com", "a..com", ".com", "valid.com"]
        self.assertEqual(parse_domains(raw), ["valid.com"])

    def test_handles_empty_or_none_input(self) -> None:
        self.assertEqual(parse_domains(None), [])
        self.assertEqual(parse_domains(""), [])
        self.assertEqual(parse_domains([]), [])


class HumanLocalPartTest(unittest.TestCase):
    def test_generates_plausible_name_with_custom_rng(self) -> None:
        rng = random.Random(42)
        name1 = human_local_part(rng)
        rng = random.Random(42)
        name2 = human_local_part(rng)
        self.assertEqual(name1, name2)
        self.assertTrue(any(c.isalpha() for c in name1))

    def test_formats_match_expected_patterns(self) -> None:
        for seed in range(50):
            name = human_local_part(random.Random(seed))
            self.assertTrue(
                all(c.isalnum() or c in "._" for c in name),
                f"Unexpected character in generated name: {name}",
            )


class IssueAddressAndUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.usage_file = Path(self.temp_dir.name) / "domain_usage.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_raises_when_no_domains(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            issue_address([], 10, usage_file=self.usage_file)
        self.assertIn("没有填域名", str(ctx.exception))

    def test_issues_address_and_records_usage(self) -> None:
        domains = ["test1.com", "test2.com"]
        address = issue_address(domains, daily_limit=5, usage_file=self.usage_file)
        self.assertTrue(address.endswith("@test1.com") or address.endswith("@test2.com"))

        data = _load(self.usage_file)
        today = _today()
        self.assertEqual(len(data["issued"]), 1)
        self.assertEqual(data["issued"][0], address)
        chosen_domain = address.split("@")[1]
        self.assertEqual(data["days"][today][chosen_domain], 1)

    def test_rotates_to_domain_with_lowest_usage(self) -> None:
        domains = ["a.com", "b.com"]
        now = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
        rng = random.Random(123)

        # Issue 1 -> chosen domain gets 1
        addr1 = issue_address(domains, daily_limit=10, usage_file=self.usage_file, rng=rng, now=now)
        domain1 = addr1.split("@")[1]

        # Issue 2 -> other domain should be chosen because it has 0 usage
        addr2 = issue_address(domains, daily_limit=10, usage_file=self.usage_file, rng=rng, now=now)
        domain2 = addr2.split("@")[1]

        self.assertNotEqual(domain1, domain2)
        # 固定的 now 与真实“今天”不同日时 usage_today() 读不到，直接按该日期取。
        usage = _load(self.usage_file)["days"][_today(now)]
        self.assertEqual(usage.get("a.com"), 1)
        self.assertEqual(usage.get("b.com"), 1)

    def test_exhaustion_raises_domain_mail_exhausted(self) -> None:
        domains = ["full.com"]
        now = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
        addr = issue_address(domains, daily_limit=1, usage_file=self.usage_file, now=now)
        self.assertTrue(addr.endswith("@full.com"))

        with self.assertRaises(DomainMailExhausted) as ctx:
            issue_address(domains, daily_limit=1, usage_file=self.usage_file, now=now)
        self.assertIn("今天每个域名都已用满 1 个", str(ctx.exception))

    def test_unlimited_daily_limit_when_zero_or_negative(self) -> None:
        domains = ["unlimited.com"]
        for _ in range(5):
            addr = issue_address(domains, daily_limit=0, usage_file=self.usage_file)
            self.assertTrue(addr.endswith("@unlimited.com"))
        usage = usage_today(usage_file=self.usage_file)
        self.assertEqual(usage.get("unlimited.com"), 5)

    def test_prunes_days_older_than_days_keep(self) -> None:
        today = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
        old_day = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        recent_day = (today - timedelta(days=2)).strftime("%Y-%m-%d")

        initial_data = {
            "days": {
                old_day: {"old.com": 10},
                recent_day: {"recent.com": 3},
            },
            "issued": ["old@old.com"],
        }
        self.usage_file.write_text(json.dumps(initial_data), encoding="utf-8")

        issue_address(["recent.com"], daily_limit=10, usage_file=self.usage_file, now=today)
        data = _load(self.usage_file)

        self.assertNotIn(old_day, data["days"])
        self.assertIn(recent_day, data["days"])

    def test_handles_collision_with_already_issued_addresses(self) -> None:
        domains = ["test.com"]
        # Seed rng so human_local_part produces a known candidate
        rng_mock = MagicMock()
        # Pretend human_local_part produces "alice.smith" repeatedly
        with patch("services.register.domain_mail.human_local_part", return_value="alice.smith"):
            addr1 = issue_address(domains, daily_limit=10, usage_file=self.usage_file)
            self.assertEqual(addr1, "alice.smith@test.com")

            # Second call: "alice.smith@test.com" is already in issued; fallback appends timestamp
            with patch("services.register.domain_mail.time.time", return_value=12345678):
                addr2 = issue_address(domains, daily_limit=10, usage_file=self.usage_file)
                self.assertEqual(addr2, f"alice.smith{12345678 % 10000}@test.com")

    def test_concurrent_issuance_thread_safety(self) -> None:
        domains = ["dom1.com", "dom2.com", "dom3.com"]
        results: list[str] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(10):
                    addr = issue_address(domains, daily_limit=100, usage_file=self.usage_file)
                    results.append(addr)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 50)
        self.assertEqual(len(set(results)), 50, "All issued addresses across threads must be unique")


class ParseImapMessageTest(unittest.TestCase):
    def test_parses_single_part_text_message(self) -> None:
        raw = (
            b"From: Support <support@example.com>\r\n"
            b"To: user@mycustomdomain.com\r\n"
            b"Subject: Your verification code\r\n"
            b"Message-ID: <msg123@example.com>\r\n"
            b"Date: Wed, 21 Oct 2026 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Your code is 654321.\r\n"
        )
        parsed = parse_imap_message(raw)
        self.assertEqual(parsed["message_id"], "<msg123@example.com>")
        self.assertEqual(parsed["subject"], "Your verification code")
        self.assertEqual(parsed["sender"], "Support <support@example.com>")
        self.assertEqual(parsed["to"], "user@mycustomdomain.com")
        self.assertEqual(parsed["text_content"], "Your code is 654321.")
        self.assertEqual(parsed["html_content"], "")
        self.assertIsNotNone(parsed["received_at"])
        self.assertEqual(parsed["received_at"].year, 2026)

    def test_parses_multipart_text_and_html(self) -> None:
        raw = (
            b"From: service@test.com\r\n"
            b"To: inbox@recipient.com\r\n"
            b"Delivered-To: alias@forward.com\r\n"
            b"X-Forwarded-To: original@custom.com\r\n"
            b"Subject: =?utf-8?B?6aqM6K+B56CB?=\r\n"
            b"Content-Type: multipart/alternative; boundary=\"boundary123\"\r\n"
            b"\r\n"
            b"--boundary123\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Plain text verification: 123456\r\n"
            b"--boundary123\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<p>HTML verification: 123456</p>\r\n"
            b"--boundary123--\r\n"
        )
        parsed = parse_imap_message(raw)
        self.assertEqual(parsed["subject"], "验证码")
        self.assertEqual(parsed["delivered_to"], "alias@forward.com")
        self.assertEqual(parsed["x_forwarded_to"], "original@custom.com")
        self.assertIn("Plain text verification: 123456", parsed["text_content"])
        self.assertIn("<p>HTML verification: 123456</p>", parsed["html_content"])

    def test_handles_rfc2047_encoded_headers(self) -> None:
        self.assertEqual(_decode("=?utf-8?B?VGVzdCBTdWJqZWN0?="), "Test Subject")
        self.assertEqual(_decode(None), "")
        self.assertEqual(_decode("plain header"), "plain header")

    def test_handles_malformed_date(self) -> None:
        raw = b"Subject: No Date\r\n\r\nBody text"
        parsed = parse_imap_message(raw)
        self.assertIsNone(parsed["received_at"])


class MessageAddressedToTest(unittest.TestCase):
    def test_matches_to_header(self) -> None:
        msg = {"to": "User Name <john.doe@domain.com>"}
        self.assertTrue(message_addressed_to(msg, "john.doe@domain.com"))
        self.assertTrue(message_addressed_to(msg, "JOHN.DOE@DOMAIN.COM"))

    def test_matches_forwarded_and_delivered_headers(self) -> None:
        msg = {
            "to": "catchall@myinbox.com",
            "x_original_to": "alice@custom.com",
            "x_forwarded_to": "bob@custom.com",
            "delivered_to": "carol@custom.com",
        }
        self.assertTrue(message_addressed_to(msg, "alice@custom.com"))
        self.assertTrue(message_addressed_to(msg, "bob@custom.com"))
        self.assertTrue(message_addressed_to(msg, "carol@custom.com"))

    def test_strict_word_boundary_avoids_partial_matches(self) -> None:
        msg = {"to": "notjohn.doe@domain.com, john.doe@domain.com.cn"}
        self.assertFalse(message_addressed_to(msg, "john.doe@domain.com"))

    def test_returns_false_for_empty_address(self) -> None:
        msg = {"to": "john@domain.com"}
        self.assertFalse(message_addressed_to(msg, ""))
        self.assertFalse(message_addressed_to(msg, "   "))


class FetchImapMessagesTest(unittest.TestCase):
    def test_raises_when_credentials_missing(self) -> None:
        with self.assertRaises(RuntimeError):
            fetch_imap_messages("", "user", "pass")
        with self.assertRaises(RuntimeError):
            fetch_imap_messages("imap.test.com", "", "pass")
        with self.assertRaises(RuntimeError):
            fetch_imap_messages("imap.test.com", "user", "")

    def test_fetches_and_parses_messages_in_reverse_order(self) -> None:
        mock_imap = MagicMock()
        mock_imap.select.return_value = ("OK", [b"10"])
        mock_imap.uid.side_effect = [
            ("OK", [b"101 102 103"]),
            ("OK", [(b"103 (RFC822)", b"Subject: Mail 3\r\nTo: a@dom.com\r\n\r\nBody 3")]),
            ("OK", [(b"102 (RFC822)", b"Subject: Mail 2\r\nTo: b@dom.com\r\n\r\nBody 2")]),
        ]

        def factory(*args, **kwargs):
            return mock_imap

        messages = fetch_imap_messages(
            "imap.test.com",
            "user@test.com",
            "secret",
            port=993,
            limit=2,
            imap_factory=factory,
        )

        mock_imap.login.assert_called_once_with("user@test.com", "secret")
        mock_imap.select.assert_called_once_with("INBOX", readonly=True)
        mock_imap.uid.assert_has_calls([
            call("search", None, "ALL"),
            call("fetch", b"103", "(RFC822)"),
            call("fetch", b"102", "(RFC822)"),
        ])
        mock_imap.logout.assert_called_once()

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["subject"], "Mail 3")
        self.assertEqual(messages[0]["uid"], "103")
        self.assertEqual(messages[1]["subject"], "Mail 2")
        self.assertEqual(messages[1]["uid"], "102")

    def test_raises_if_select_fails(self) -> None:
        mock_imap = MagicMock()
        mock_imap.select.return_value = ("NO", [b"cannot open mailbox"])

        with self.assertRaises(RuntimeError) as ctx:
            fetch_imap_messages("imap.test.com", "user", "pass", imap_factory=lambda *a, **k: mock_imap)
        self.assertIn("IMAP select INBOX 失败", str(ctx.exception))
        mock_imap.logout.assert_called_once()

    def test_raises_if_search_fails(self) -> None:
        mock_imap = MagicMock()
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.uid.return_value = ("NO", [b"search error"])

        with self.assertRaises(RuntimeError) as ctx:
            fetch_imap_messages("imap.test.com", "user", "pass", imap_factory=lambda *a, **k: mock_imap)
        self.assertIn("IMAP search 失败", str(ctx.exception))


class MaskSecretTest(unittest.TestCase):
    def test_masks_long_secrets(self) -> None:
        self.assertEqual(mask_secret("cf_token_1234567890"), "cf_t***90")

    def test_masks_short_secrets(self) -> None:
        self.assertEqual(mask_secret("12345678"), "***")
        self.assertEqual(mask_secret("short"), "***")

    def test_handles_empty_secret(self) -> None:
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret(None), "")


class CloudflareApiHelperTest(unittest.TestCase):
    def test_cf_success_returns_result(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {"success": True, "result": {"id": "zone-123"}}
        session.request.return_value = response

        result = _cf(session, "my-token", "GET", "/zones")
        self.assertEqual(result, {"id": "zone-123"})
        session.request.assert_called_once_with(
            "GET",
            f"{domain_mail.CF_API}/zones",
            headers={"Authorization": "Bearer my-token", "Content-Type": "application/json"},
            json=None,
            timeout=30,
        )

    def test_cf_error_masks_token_and_adds_permission_hint(self) -> None:
        session = MagicMock()
        response = MagicMock()
        response.status_code = 403
        response.json.return_value = {
            "success": False,
            "errors": [{"message": "Access denied for token token-secret-xyz"}],
        }
        session.request.return_value = response

        with self.assertRaises(CloudflareError) as ctx:
            _cf(session, "token-secret-xyz", "GET", "/zones")

        error_msg = str(ctx.exception)
        self.assertNotIn("token-secret-xyz", error_msg)
        self.assertIn("toke***yz", error_msg)
        self.assertIn("令牌无效或权限不够", error_msg)


class CloudflareSetupTest(unittest.TestCase):
    def test_validates_required_arguments(self) -> None:
        with self.assertRaises(CloudflareError) as ctx:
            cf_setup("", ["example.com"], "relay@test.com")
        self.assertIn("没有填 Cloudflare API 令牌", str(ctx.exception))

        with self.assertRaises(CloudflareError) as ctx:
            cf_setup("token", ["example.com"], "invalid_email")
        self.assertIn("没有填「转发到」的收信邮箱", str(ctx.exception))

        with self.assertRaises(CloudflareError) as ctx:
            cf_setup("token", [], "relay@test.com")
        self.assertIn("没有填域名", str(ctx.exception))

    def test_successful_cf_setup(self) -> None:
        session = MagicMock()

        def request_handler(method: str, url: str, **kwargs):
            resp = MagicMock()
            path = url.replace(domain_mail.CF_API, "")
            if path == "/zones?name=domain1.com":
                resp.json.return_value = {
                    "success": True,
                    "result": [{"id": "zone-1", "account": {"id": "acc-1"}}],
                }
            elif path == "/zones/zone-1/email/routing/dns":
                resp.json.return_value = {"success": True, "result": {}}
            elif path == "/accounts/acc-1/email/routing/addresses":
                resp.json.return_value = {
                    "success": True,
                    "result": [{"email": "forward@inbox.com", "verified": "2026-01-01T00:00:00Z"}],
                }
            elif path == "/zones/zone-1/email/routing/rules/catch_all":
                resp.json.return_value = {"success": True, "result": {}}
            else:
                resp.json.return_value = {"success": True, "result": {}}
            return resp

        session.request.side_effect = request_handler

        results = cf_setup(
            token="cf-secret-token-123456",
            domains=["domain1.com"],
            forward_to="forward@inbox.com",
            session_factory=lambda: session,
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res["domain"], "domain1.com")
        self.assertTrue(res["ok"])
        self.assertTrue(res["verified"])
        self.assertIn("已开启 Email Routing", res["steps"])
        self.assertIn("目标邮箱已验证", res["steps"])
        self.assertIn("所有地址都转发到收信邮箱", res["steps"])
        session.close.assert_called_once()

    def test_cf_setup_destination_address_unverified(self) -> None:
        session = MagicMock()

        def request_handler(method: str, url: str, **kwargs):
            resp = MagicMock()
            path = url.replace(domain_mail.CF_API, "")
            if path == "/zones?name=domain1.com":
                resp.json.return_value = {
                    "success": True,
                    "result": [{"id": "zone-1", "account": {"id": "acc-1"}}],
                }
            elif path == "/zones/zone-1/email/routing/dns":
                resp.json.return_value = {"success": True, "result": {}}
            elif path == "/accounts/acc-1/email/routing/addresses":
                if method == "GET":
                    # Address does not exist yet -> returns empty list
                    resp.json.return_value = {"success": True, "result": []}
                elif method == "POST":
                    # Newly created address, unverified
                    resp.json.return_value = {"success": True, "result": {"email": "forward@inbox.com", "verified": None}}
            elif path == "/zones/zone-1/email/routing/rules/catch_all":
                resp.json.return_value = {"success": True, "result": {}}
            return resp

        session.request.side_effect = request_handler

        results = cf_setup(
            token="cf-secret-token-123456",
            domains=["domain1.com"],
            forward_to="forward@inbox.com",
            session_factory=lambda: session,
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertTrue(res["ok"])
        self.assertFalse(res["verified"])
        self.assertTrue(any("点击 Cloudflare 的验证邮件" in step for step in res["steps"]))

    def test_cf_setup_ignores_dns_already_configured_error(self) -> None:
        session = MagicMock()

        def request_handler(method: str, url: str, **kwargs):
            resp = MagicMock()
            path = url.replace(domain_mail.CF_API, "")
            if path == "/zones?name=domain1.com":
                resp.json.return_value = {"success": True, "result": [{"id": "zone-1", "account": {"id": "acc-1"}}]}
            elif path == "/zones/zone-1/email/routing/dns":
                # Simulated already configured error
                resp.json.return_value = {"success": False, "errors": [{"message": "Email routing already enabled"}]}
            elif path == "/accounts/acc-1/email/routing/addresses":
                resp.json.return_value = {"success": True, "result": [{"email": "forward@inbox.com", "verified": True}]}
            elif path == "/zones/zone-1/email/routing/rules/catch_all":
                resp.json.return_value = {"success": True, "result": {}}
            return resp

        session.request.side_effect = request_handler

        results = cf_setup(
            token="cf-secret-token-123456",
            domains=["domain1.com"],
            forward_to="forward@inbox.com",
            session_factory=lambda: session,
        )
        self.assertTrue(results[0]["ok"])

    def test_cf_setup_isolates_failures_per_domain(self) -> None:
        session = MagicMock()

        def request_handler(method: str, url: str, **kwargs):
            resp = MagicMock()
            path = url.replace(domain_mail.CF_API, "")
            if path == "/zones?name=notfound.com":
                # Domain not in account
                resp.json.return_value = {"success": True, "result": []}
            elif path == "/zones?name=good.com":
                resp.json.return_value = {"success": True, "result": [{"id": "zone-good", "account": {"id": "acc-1"}}]}
            elif path == "/zones/zone-good/email/routing/dns":
                resp.json.return_value = {"success": True, "result": {}}
            elif path == "/accounts/acc-1/email/routing/addresses":
                resp.json.return_value = {"success": True, "result": [{"email": "forward@inbox.com", "verified": True}]}
            elif path == "/zones/zone-good/email/routing/rules/catch_all":
                resp.json.return_value = {"success": True, "result": {}}
            return resp

        session.request.side_effect = request_handler

        results = cf_setup(
            token="cf-secret-token-123456",
            domains=["notfound.com", "good.com"],
            forward_to="forward@inbox.com",
            session_factory=lambda: session,
        )

        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]["ok"])
        self.assertIn("不在当前 Cloudflare 账户里", results[0]["error"])
        self.assertTrue(results[1]["ok"])


class DomainMailProviderTest(unittest.TestCase):
    def _entry(self, **overrides) -> dict:
        data = {
            "type": "custom_domain",
            "provider_ref": "custom_domain#1",
            "label": "MyDomainMail",
            "domains": "test1.com, test2.com",
            "daily_limit_per_domain": 25,
            "imap_host": "imap.test.com",
            "imap_port": 993,
            "imap_user": "forward@test.com",
            "imap_password": " secret pass ",
        }
        data.update(overrides)
        return data

    def test_init_normalizes_entry_and_disables_proxy(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.DomainMailProvider(
            self._entry(),
            {"proxy": "http://proxy.test:8080", "request_timeout": 15},
        )
        self.assertEqual(provider.name, "custom_domain")
        self.assertEqual(provider.domains, ["test1.com", "test2.com"])
        self.assertEqual(provider.daily_limit, 25)
        self.assertEqual(provider.imap_host, "imap.test.com")
        self.assertEqual(provider.imap_port, 993)
        self.assertEqual(provider.imap_user, "forward@test.com")
        self.assertEqual(provider.imap_password, "secretpass")
        self.assertEqual(provider.conf.get("proxy"), "")
        self.assertEqual(provider.label, "MyDomainMail")

    def test_create_mailbox_calls_issue_address(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.DomainMailProvider(self._entry(), {})
        with patch("services.register.domain_mail.issue_address", return_value="alice.smith@test1.com") as mock_issue:
            mailbox = provider.create_mailbox()

        mock_issue.assert_called_once_with(["test1.com", "test2.com"], 25)
        self.assertEqual(
            mailbox,
            {
                "provider": "custom_domain",
                "provider_ref": "custom_domain#1",
                "address": "alice.smith@test1.com",
                "label": "MyDomainMail",
            },
        )

    def test_fetch_recent_and_latest_messages_filters_addressed_to(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.DomainMailProvider(self._entry(), {"request_timeout": 10})
        mailbox = {"address": "target@test1.com"}

        raw_messages = [
            {
                "uid": "100",
                "subject": "Code 1",
                "to": "other@test1.com",
                "text_content": "111111",
            },
            {
                "uid": "101",
                "subject": "Code 2",
                "to": "target@test1.com",
                "text_content": "222222",
            },
        ]

        with patch("services.register.domain_mail.fetch_imap_messages", return_value=raw_messages) as mock_fetch:
            recent = provider.fetch_recent_messages(mailbox)
            latest = provider.fetch_latest_message(mailbox)

        mock_fetch.assert_called_with(
            "imap.test.com",
            "forward@test.com",
            "secretpass",
            port=993,
            limit=20,
            timeout=10.0,
        )
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["subject"], "Code 2")
        self.assertEqual(recent[0]["mailbox"], "target@test1.com")
        self.assertEqual(recent[0]["provider"], "custom_domain")

        self.assertIsNotNone(latest)
        self.assertEqual(latest["subject"], "Code 2")

    def test_fetch_latest_message_returns_none_when_no_match(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.DomainMailProvider(self._entry(), {})
        mailbox = {"address": "target@test1.com"}

        with patch("services.register.domain_mail.fetch_imap_messages", return_value=[]):
            latest = provider.fetch_latest_message(mailbox)

        self.assertIsNone(latest)

    def test_create_provider_resolves_custom_domain(self) -> None:
        from services.register import mail_provider

        mail_config = {
            "providers": [
                {
                    "type": "custom_domain",
                    "enable": True,
                    "id": "cd-1",
                    "domains": ["example.com"],
                    "imap_host": "imap.example.com",
                    "imap_user": "user@example.com",
                    "imap_password": "secret",
                }
            ]
        }
        instance = mail_provider._create_provider(mail_config, provider="custom_domain")
        self.assertIsInstance(instance, mail_provider.DomainMailProvider)
        self.assertEqual(instance.domains, ["example.com"])
        self.assertEqual(instance.provider_ref, "custom_domain:cd-1")


class RegisterServiceDomainMailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_file = Path(self.temp_dir.name) / "register.json"
        from services.register_service import RegisterService

        self.service = RegisterService(self.store_file)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_redact_domain_mail_secrets(self) -> None:
        from services.register_service import RegisterService

        snapshot = {
            "mail": {
                "providers": [
                    {
                        "type": "custom_domain",
                        "id": "cd-1",
                        "cf_api_token": "cf-token-12345678",
                        "imap_password": "my-imap-password",
                    },
                    {
                        "type": "duckmail",
                        "id": "duck-1",
                    },
                ]
            }
        }
        RegisterService._redact_domain_mail_secrets(snapshot)
        provider = snapshot["mail"]["providers"][0]
        self.assertEqual(provider["cf_api_token"], "")
        self.assertTrue(provider["cf_api_token_set"])
        self.assertEqual(provider["cf_api_token_preview"], "cf-t***78")
        self.assertEqual(provider["imap_password"], "")
        self.assertTrue(provider["imap_password_set"])
        self.assertEqual(provider["imap_password_preview"], "my-i***rd")

    def test_merge_domain_mail_secrets_keeps_existing_on_empty(self) -> None:
        self.service.update({
            "mail": {
                "providers": [
                    {
                        "type": "custom_domain",
                        "id": "cd-1",
                        "domains": "example.com",
                        "cf_api_token": "initial-cf-token-123",
                        "imap_password": "initial-imap-password",
                    }
                ]
            }
        })
        internal = self.service._config["mail"]["providers"][0]
        self.assertEqual(internal["cf_api_token"], "initial-cf-token-123")
        self.assertEqual(internal["imap_password"], "initial-imap-password")

        self.service.update({
            "mail": {
                "providers": [
                    {
                        "type": "custom_domain",
                        "id": "cd-1",
                        "domains": "example.com, new.org",
                        "cf_api_token": "",
                        "imap_password": "   ",
                        "cf_api_token_set": True,
                        "cf_api_token_preview": "init***23",
                    }
                ]
            }
        })
        internal = self.service._config["mail"]["providers"][0]
        self.assertEqual(internal["domains"], "example.com, new.org")
        self.assertEqual(internal["cf_api_token"], "initial-cf-token-123")
        self.assertEqual(internal["imap_password"], "initial-imap-password")
        self.assertNotIn("cf_api_token_set", internal)
        self.assertNotIn("cf_api_token_preview", internal)

    def test_domain_mail_cf_setup_invokes_cf_setup_and_returns_usage(self) -> None:
        self.service.update({
            "mail": {
                "providers": [
                    {
                        "type": "custom_domain",
                        "id": "cd-setup",
                        "domains": "domain.com",
                        "forward_to": "forward@inbox.com",
                        "cf_api_token": "cf-token",
                    }
                ]
            }
        })
        cf_results = [{"domain": "domain.com", "ok": True, "steps": ["已开启 Email Routing"]}]
        with patch("services.register.domain_mail.cf_setup", return_value=cf_results) as mock_cf, patch(
            "services.register.domain_mail.usage_today", return_value={"domain.com": 3}
        ) as mock_usage:
            out = self.service.domain_mail_cf_setup("cd-setup")

        mock_cf.assert_called_once_with("cf-token", ["domain.com"], "forward@inbox.com")
        self.assertEqual(out, {"results": cf_results, "usage_today": {"domain.com": 3}})

    def test_domain_mail_cf_setup_missing_provider_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.service.domain_mail_cf_setup("non-existent-id")
        self.assertIn("找不到这个自建域名邮箱配置", str(ctx.exception))


class RegisterDomainMailApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api.register as register_api

        self.register_api = register_api
        app = FastAPI()
        app.include_router(register_api.create_router())
        self.client = TestClient(app)

    def test_domain_mail_cf_setup_endpoint_success(self) -> None:
        result_payload = {
            "results": [
                {
                    "domain": "example.com",
                    "ok": True,
                    "verified": True,
                    "steps": ["已开启 Email Routing"],
                    "error": "",
                }
            ],
            "usage_today": {"example.com": 2},
        }
        with patch.object(self.register_api, "require_admin"), patch.object(
            self.register_api.register_service,
            "domain_mail_cf_setup",
            return_value=result_payload,
        ) as mock_setup:
            response = self.client.post(
                "/api/register/domain-mail/cf-setup",
                json={"provider_id": "custom-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result_payload)
        mock_setup.assert_called_once_with("custom-1")

    def test_domain_mail_cf_setup_endpoint_error_handling(self) -> None:
        with patch.object(self.register_api, "require_admin"), patch.object(
            self.register_api.register_service,
            "domain_mail_cf_setup",
            side_effect=ValueError("找不到这个自建域名邮箱配置，请先保存"),
        ):
            response = self.client.post(
                "/api/register/domain-mail/cf-setup",
                json={"provider_id": "invalid-id"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("找不到这个自建域名邮箱配置", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()

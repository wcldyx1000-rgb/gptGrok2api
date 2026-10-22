from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.register import mail_provider


def _response(status: int = 200, payload=None, text: str = "", headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text or str(payload)
    response.json.return_value = payload if payload is not None else {}
    response.headers = headers or {}
    return response


class _OutlookStateCase(unittest.TestCase):
    """每个用例使用独立的 SQLite 状态文件，绝不触碰真实 data/。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(mail_provider, "OUTLOOK_TOKEN_USED_FILE", Path(self._tmp.name) / "outlook_token_used.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        mail_provider._outlook_transient_state.clear()
        self.addCleanup(mail_provider._outlook_transient_state.clear)

    @staticmethod
    def _credential(email: str, **extra) -> dict:
        return {"email": email, "password": "p", "client_id": "cid", "refresh_token": "rt", **extra}

    def _claim(self, pool: list[dict]) -> dict:
        credential, lease, _ = mail_provider._claim_outlook_credential_ex(pool)
        self.assertIsNotNone(credential)
        return {
            "provider": mail_provider.OutlookTokenProvider.name,
            "address": credential["email"],
            "login_email": credential.get("login_email") or credential["email"],
            "alias_of": credential.get("alias_of", ""),
            "_outlook_lease_token": lease,
        }

    @staticmethod
    def _state(address: str) -> str:
        return str(mail_provider._load_outlook_token_state().get(address.lower(), {}).get("state") or "")


class ClassifyOutlookFailureTest(unittest.TestCase):
    def test_transient_errors_are_released_not_burned(self) -> None:
        for error in (
            "Cloudflare challenge blocked",
            RuntimeError("curl: (28) Operation timed out after 30000 milliseconds"),
            "curl: (35) TLS connect error",
            "platform_authorize_http_429: rate_limit_exceeded",
            "create_account_http_503",
            "validate_otp_http_409 invalid_state",
            "passwordless_send_otp_http_409",
            "unsupported_country_region_territory",
            "验证码投递超时：邮箱服务未返回新邮件",
            mail_provider.OutlookTokenRateLimitError("OutlookToken 刷新被 Microsoft 限流: HTTP 429, AADSTS90055"),
            mail_provider.OutlookTokenTransportError("OutlookToken 刷新请求失败: connection reset"),
        ):
            with self.subTest(error=str(error)):
                self.assertEqual(mail_provider.classify_outlook_failure(error), "transient")

    def test_deterministic_errors_are_not_transient(self) -> None:
        cases = {
            "token_invalid": [
                mail_provider.OutlookTokenError("OutlookToken 刷新失败: HTTP 400, invalid_grant"),
                "AADSTS50053: account is locked",
            ],
            "login_required": ["Outlook 登录流异常: login_required"],
            "failed": ["unsupported_email: the email you provided is not supported", "create_account_http_400 weird"],
        }
        for expected, errors in cases.items():
            for error in errors:
                with self.subTest(error=str(error)):
                    self.assertEqual(mail_provider.classify_outlook_failure(error), expected)

    def test_status_codes_match_whole_numbers_only(self) -> None:
        self.assertEqual(mail_provider.classify_outlook_failure("weird id 14290 value"), "failed")
        self.assertEqual(mail_provider.classify_outlook_failure("upstream HTTP 502"), "transient")


class MarkMailboxResultTest(_OutlookStateCase):
    def setUp(self) -> None:
        super().setUp()
        self.pool = [self._credential("a@outlook.com")]

    def test_transient_failure_releases_mailbox_with_cooldown(self) -> None:
        mailbox = self._claim(self.pool)
        mail_provider.mark_mailbox_result(mailbox, success=False, error="curl: (28) timed out")

        self.assertEqual(self._state("a@outlook.com"), "")
        credential, _lease, waitable = mail_provider._claim_outlook_credential_ex(self.pool)
        self.assertIsNone(credential)
        self.assertTrue(waitable)  # 只是冷却中，不是用尽

        mail_provider._outlook_transient_state.clear()
        self.assertIsNotNone(mail_provider._claim_outlook_credential_ex(self.pool)[0])

    def test_create_mailbox_waits_out_the_cooldown_instead_of_reporting_exhaustion(self) -> None:
        mailbox = self._claim(self.pool)
        mail_provider.mark_mailbox_result(mailbox, success=False, error="Cloudflare")
        entry = {"type": "outlook_token", "provider_ref": "outlook_token:t", "mailboxes": "a@outlook.com----p----cid----rt"}
        with patch.object(mail_provider, "_create_session", return_value=MagicMock()):
            provider = mail_provider.OutlookTokenProvider(entry, {"request_timeout": 5, "wait_timeout": 1, "wait_interval": 0.01, "user_agent": "t"})

        def release_cooldown(_seconds: float) -> None:
            mail_provider._outlook_transient_state.clear()

        with patch.object(mail_provider.time, "sleep", side_effect=release_cooldown):
            claimed = provider.create_mailbox()
        self.assertEqual(claimed["address"], "a@outlook.com")

    def test_pool_exhausted_message_is_kept_when_nothing_can_become_available(self) -> None:
        mailbox = self._claim(self.pool)
        mail_provider.mark_mailbox_result(mailbox, success=True)
        entry = {"type": "outlook_token", "provider_ref": "outlook_token:t", "mailboxes": "a@outlook.com----p----cid----rt"}
        with patch.object(mail_provider, "_create_session", return_value=MagicMock()):
            provider = mail_provider.OutlookTokenProvider(entry, {"request_timeout": 5, "wait_timeout": 1, "wait_interval": 0.01, "user_agent": "t"})
        with self.assertRaisesRegex(RuntimeError, "暂无可用邮箱"):
            provider.create_mailbox()

    def test_repeated_transient_failures_are_capped_and_marked_failed(self) -> None:
        for round_index in range(mail_provider.OUTLOOK_TRANSIENT_RELEASE_LIMIT + 1):
            mailbox = self._claim(self.pool)
            mail_provider.mark_mailbox_result(mailbox, success=False, error="connection reset by peer")
            if round_index < mail_provider.OUTLOOK_TRANSIENT_RELEASE_LIMIT:
                self.assertEqual(self._state("a@outlook.com"), "")
                # 跳过冷却，模拟下一轮重新领用。
                record = mail_provider._outlook_transient_state["a@outlook.com"]
                record[1] = 0.0

        self.assertEqual(self._state("a@outlook.com"), "failed")
        self.assertIn("连续", mail_provider._load_outlook_token_state()["a@outlook.com"]["reason"])

    def test_success_resets_transient_counter_and_marks_used(self) -> None:
        mailbox = self._claim(self.pool)
        mail_provider.mark_mailbox_result(mailbox, success=False, error="timeout")
        self.assertIn("a@outlook.com", mail_provider._outlook_transient_state)
        mail_provider._outlook_transient_state["a@outlook.com"][1] = 0.0
        mailbox = self._claim(self.pool)
        mail_provider.mark_mailbox_result(mailbox, success=True)
        self.assertEqual(self._state("a@outlook.com"), "used")
        self.assertNotIn("a@outlook.com", mail_provider._outlook_transient_state)

    def test_deterministic_failures_still_burn_the_mailbox(self) -> None:
        cases = [
            (mail_provider.OutlookTokenError("OutlookToken 刷新失败: HTTP 400, invalid_grant"), "token_invalid"),
            ("Outlook 登录流异常 login_required", "login_required"),
            ("unsupported_email", "failed"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                mail_provider.reset_outlook_token_pool_state("all")
                mailbox = self._claim(self.pool)
                mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
                self.assertEqual(self._state("a@outlook.com"), expected)

    def test_stale_lease_cannot_overwrite_newer_claim_on_transient_release(self) -> None:
        first = self._claim(self.pool)
        mail_provider.release_mailbox(first)
        second = self._claim(self.pool)
        mail_provider.mark_mailbox_result(first, success=False, error="timeout")
        self.assertEqual(self._state("a@outlook.com"), "in_use")
        self.assertEqual(
            mail_provider._load_outlook_token_state()["a@outlook.com"]["lease_token"],
            second["_outlook_lease_token"],
        )


class OutlookDirectConnectionTest(unittest.TestCase):
    CONF = {"request_timeout": 5, "wait_timeout": 1, "wait_interval": 0.01, "user_agent": "t", "proxy": "socks5://1.2.3.4:1080"}
    ENTRY = {"type": "outlook_token", "provider_ref": "outlook_token:t", "mailboxes": "a@outlook.com----p----cid----rt"}

    def _provider(self, sessions: list[MagicMock]):
        created_confs: list[dict] = []

        def factory(conf: dict):
            created_confs.append(dict(conf))
            return sessions[len(created_confs) - 1] if len(created_confs) <= len(sessions) else MagicMock()

        with patch.object(mail_provider, "_create_session", side_effect=factory):
            provider = mail_provider.OutlookTokenProvider(dict(self.ENTRY), dict(self.CONF))
        return provider, created_confs, factory

    def test_register_proxy_is_never_used_for_outlook(self) -> None:
        _provider, confs, _factory = self._provider([MagicMock()])
        self.assertEqual(confs[0]["proxy"], "direct")
        self.assertEqual(_provider.conf["proxy"], "direct")

    def test_proxy_tunnel_error_rebuilds_session_directly_and_retries_once(self) -> None:
        broken = MagicMock()
        broken.post.side_effect = RuntimeError("curl: (56) CONNECT tunnel failed, response 502")
        healthy = MagicMock()
        healthy.post.return_value = _response(200, {"access_token": "AT"})
        provider, confs, factory = self._provider([broken, healthy])

        with patch.object(mail_provider, "_create_session", side_effect=factory), patch.object(mail_provider.time, "sleep"):
            token = provider._exchange_refresh_token("cid", "rt", "scope")

        self.assertEqual(token, "AT")
        self.assertEqual(len(confs), 2)
        self.assertEqual(confs[1]["proxy"], "direct")
        broken.close.assert_called_once()

    def test_token_refresh_transport_failure_is_reported_not_bubbled(self) -> None:
        session = MagicMock()
        session.post.side_effect = OSError("connection reset by peer")
        provider, _confs, _factory = self._provider([session])

        with self.assertRaises(mail_provider.OutlookTokenTransportError) as ctx:
            provider._exchange_refresh_token("cid", "rt", "scope")

        self.assertIn("connection reset", str(ctx.exception))
        self.assertEqual(mail_provider.classify_outlook_failure(ctx.exception), "transient")
        self.assertTrue(mail_provider._is_outlook_transient_fetch_error(ctx.exception))

    def test_tunnel_error_is_retried_only_once(self) -> None:
        broken = MagicMock()
        broken.post.side_effect = RuntimeError("CONNECT tunnel failed")
        provider, confs, factory = self._provider([broken, broken, broken])
        with patch.object(mail_provider, "_create_session", side_effect=factory), patch.object(mail_provider.time, "sleep"):
            with self.assertRaises(mail_provider.OutlookTokenTransportError):
                provider._exchange_refresh_token("cid", "rt", "scope")
        self.assertEqual(len(confs), 2)  # 初始会话 + 一次直连重建

    def test_graph_read_retries_after_tunnel_error(self) -> None:
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("curl: (97) proxy handshake")
        healthy = MagicMock()
        healthy.get.return_value = _response(200, {"value": [{"id": "m1"}]})
        provider, _confs, factory = self._provider([broken, healthy])
        with patch.object(mail_provider, "_create_session", side_effect=factory), patch.object(mail_provider.time, "sleep"):
            items = provider._read_graph("AT")
        self.assertEqual(items, [{"id": "m1"}])


class OutlookAliasFamilyTest(_OutlookStateCase):
    def setUp(self) -> None:
        super().setUp()
        entry = {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": "t", "alias_include_original": True}
        base_a = self._credential("a@outlook.com")
        base_b = self._credential("b@outlook.com")
        self.pool = mail_provider.expand_outlook_aliases([base_a, base_b], entry)

    def test_aliases_of_one_mailbox_are_not_claimed_concurrently(self) -> None:
        first = self._claim(self.pool)
        second = self._claim(self.pool)
        # 第二个领到的必须来自另一个原邮箱。
        self.assertNotEqual(first["login_email"], second["login_email"])

        credential, _lease, waitable = mail_provider._claim_outlook_credential_ex(self.pool)
        self.assertIsNone(credential)
        self.assertTrue(waitable)  # 只是同家族占用中，可稍后再领

    def test_busy_family_is_released_with_the_mailbox(self) -> None:
        first = self._claim(self.pool)
        self._claim(self.pool)
        mail_provider.release_mailbox(first)
        credential, _lease, _waitable = mail_provider._claim_outlook_credential_ex(self.pool)
        self.assertEqual(mail_provider._outlook_family_key(credential), first["login_email"].lower())

    def test_registered_alias_blocks_whole_family(self) -> None:
        entry = {"alias_enabled": True, "alias_per_email": 3, "alias_prefix": "t", "alias_include_original": False}
        pool = mail_provider.expand_outlook_aliases(
            [self._credential("a@outlook.com"), self._credential("b@outlook.com")], entry
        )
        first = self._claim(pool)
        self.assertEqual(first["alias_of"], "a@outlook.com")
        # “已注册”走 success=True 路径：整个家族都不该再被领用，也不会再空转撞同一个人。
        mail_provider.mark_mailbox_result(first, success=True)

        self.assertEqual(self._state("a@outlook.com"), "family_used")
        claimed_families = []
        for _ in range(len(pool)):
            credential, lease, _waitable = mail_provider._claim_outlook_credential_ex(pool)
            if credential is None:
                break
            claimed_families.append(mail_provider._outlook_family_key(credential))
            mail_provider.mark_mailbox_result(
                {
                    "provider": mail_provider.OutlookTokenProvider.name,
                    "address": credential["email"],
                    "login_email": credential.get("login_email") or credential["email"],
                    "alias_of": credential.get("alias_of", ""),
                    "_outlook_lease_token": lease,
                },
                success=False,
                error="unsupported_email",
            )
        self.assertNotIn("a@outlook.com", claimed_families)
        self.assertEqual(set(claimed_families), {"b@outlook.com"})

    def test_original_mailbox_already_used_blocks_its_aliases(self) -> None:
        mail_provider._set_outlook_token_state("a@outlook.com", "used")
        claimed = []
        for _ in range(len(self.pool)):
            credential, lease, _ = mail_provider._claim_outlook_credential_ex(self.pool)
            if credential is None:
                break
            claimed.append(credential["email"])
        self.assertTrue(all(email.startswith("b") for email in claimed), claimed)

    def test_family_used_is_reported_in_stats_and_not_counted_unused(self) -> None:
        mail_provider._set_outlook_token_state("a@outlook.com", "family_used", "别名 a+t1@outlook.com 已注册")
        stats = mail_provider.outlook_token_pool_stats(self.pool)
        self.assertEqual(stats["family_used"], 3)  # 原邮箱 + 两个别名
        self.assertEqual(stats["unused"], 3)  # 只剩 b 家族

    def test_without_aliases_nothing_changes(self) -> None:
        plain = [self._credential("x@outlook.com"), self._credential("y@outlook.com")]
        first = self._claim(plain)
        second = self._claim(plain)
        self.assertNotEqual(first["address"], second["address"])
        mail_provider.mark_mailbox_result(first, success=True)
        self.assertEqual(self._state("x@outlook.com"), "used")
        self.assertNotIn("family_used", {self._state("y@outlook.com")})


class GptMailRateLimitTest(unittest.TestCase):
    ENTRY = {"type": "gptmail", "provider_ref": "gptmail:t", "api_key": "k", "key_mode": "custom"}
    CONF = {"request_timeout": 5, "wait_timeout": 1, "wait_interval": 0.01, "user_agent": "t", "proxy": ""}

    def _provider(self, session: MagicMock) -> mail_provider.GptMailProvider:
        session.headers = {}
        with patch.object(mail_provider, "_create_session", return_value=session):
            return mail_provider.GptMailProvider(dict(self.ENTRY), dict(self.CONF))

    def test_429_and_cloudflare_1015_back_off_then_succeed(self) -> None:
        session = MagicMock()
        session.request.side_effect = [
            _response(429, text="Too Many Requests"),
            _response(403, text="error code: 1015 You are being rate limited"),
            _response(200, {"data": {"email": "x@gptmail.test"}}),
        ]
        provider = self._provider(session)
        with patch.object(mail_provider.time, "sleep") as sleep:
            data = provider._request("GET", "/api/generate-email")
        self.assertEqual(data, {"email": "x@gptmail.test"})
        self.assertEqual(sleep.call_count, 2)
        self.assertLess(sleep.call_args_list[0].args[0], sleep.call_args_list[1].args[0])

    def test_retry_after_header_is_honoured(self) -> None:
        session = MagicMock()
        session.request.side_effect = [
            _response(429, text="slow down", headers={"Retry-After": "7"}),
            _response(200, {"data": []}),
        ]
        provider = self._provider(session)
        with patch.object(mail_provider.time, "sleep") as sleep:
            provider._request("GET", "/api/emails")
        self.assertEqual(sleep.call_args.args[0], 7.0)

    def test_gives_up_after_bounded_attempts_with_rate_limit_error(self) -> None:
        session = MagicMock()
        session.request.return_value = _response(429, text="Too Many Requests")
        provider = self._provider(session)
        with patch.object(mail_provider.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                provider._request("GET", "/api/emails")
        self.assertEqual(session.request.call_count, mail_provider.GPTMAIL_REQUEST_ATTEMPTS)
        self.assertTrue(mail_provider.is_mailbox_creation_rate_limited(ctx.exception))

    def test_other_http_errors_are_not_retried(self) -> None:
        session = MagicMock()
        session.request.return_value = _response(500, text="boom")
        provider = self._provider(session)
        with patch.object(mail_provider.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                provider._request("GET", "/api/emails")
        self.assertEqual(session.request.call_count, 1)
        sleep.assert_not_called()

    def test_mailbox_creation_keeps_a_minimum_interval(self) -> None:
        mail_provider._gptmail_next_create.clear()
        self.addCleanup(mail_provider._gptmail_next_create.clear)
        session = MagicMock()
        session.request.return_value = _response(200, {"data": {"email": "x@gptmail.test"}})
        provider = self._provider(session)
        with patch.object(mail_provider.time, "sleep") as sleep:
            provider.create_mailbox()
            provider.create_mailbox()
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(waits), 1)  # 第一次不等待，第二次补足间隔
        self.assertGreater(waits[0], 0)
        self.assertLessEqual(waits[0], mail_provider.GPTMAIL_CREATE_MIN_INTERVAL_SECONDS)

    def test_rate_limited_source_is_skipped_for_the_next_source(self) -> None:
        class Limited:
            name = "gptmail"
            provider_ref = "gptmail:1"

            def create_mailbox(self, username=None):
                raise RuntimeError("GPTMail 被限流（已退避重试 4 次）: HTTP 429")

            def close(self):
                pass

        class Healthy:
            name = "duckmail"
            provider_ref = "duckmail:2"

            def create_mailbox(self, username=None):
                return {"provider": "duckmail", "provider_ref": "duckmail:2", "address": "ok@duck.test"}

            def close(self):
                pass

        providers = [Limited(), Healthy()]
        entries = [{"provider_ref": "gptmail:1"}, {"provider_ref": "duckmail:2"}]
        with (
            patch.object(mail_provider, "_enabled_entries", return_value=entries),
            patch.object(mail_provider, "_create_provider", side_effect=lambda *a, **k: providers.pop(0)),
        ):
            mailbox = mail_provider.create_mailbox({"providers": []})
        self.assertEqual(mailbox["address"], "ok@duck.test")


class RecentMessageScanTest(unittest.TestCase):
    CONF = {"request_timeout": 5, "wait_timeout": 0.3, "wait_interval": 0.01, "user_agent": "t", "proxy": ""}

    def _gptmail(self, listing: list[dict], details: dict[str, dict]) -> mail_provider.GptMailProvider:
        session = MagicMock()
        session.headers = {}
        entry = {"type": "gptmail", "provider_ref": "gptmail:t", "api_key": "k", "key_mode": "custom"}
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.GptMailProvider(entry, dict(self.CONF))

        def request(method, path, params=None, payload=None):
            if path == "/api/emails":
                return listing
            return details[path.rsplit("/", 1)[1]]

        provider._request = MagicMock(side_effect=request)  # type: ignore[method-assign]
        return provider

    @staticmethod
    def _mail(mail_id: str, content: str, age_seconds: float) -> dict:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).timestamp()
        return {"id": mail_id, "subject": "OpenAI", "content": content, "from_address": "noreply@openai.com", "timestamp": stamp}

    def test_scans_past_a_newer_non_code_message(self) -> None:
        notice = self._mail("2", "Welcome to ChatGPT", 5)
        otp = self._mail("1", "Your verification code is 112233", 20)
        provider = self._gptmail([{"id": "2", "timestamp": notice["timestamp"]}, {"id": "1", "timestamp": otp["timestamp"]}], {"1": otp, "2": notice})
        mailbox = {"address": "x@gptmail.test"}
        self.assertEqual(provider.wait_for_code(mailbox), "112233")

    def test_received_after_boundary_skips_stale_messages(self) -> None:
        old = self._mail("1", "Your verification code is 111111", 600)
        provider = self._gptmail([{"id": "1", "timestamp": old["timestamp"]}], {"1": old})
        mailbox = {"address": "x@gptmail.test", "_received_after": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()}
        self.assertIsNone(provider.wait_for_code(mailbox))

    def test_already_tried_codes_are_filtered_even_in_a_new_message(self) -> None:
        dup = self._mail("2", "Your verification code is 111111", 2)
        fresh = self._mail("3", "Your verification code is 222222", 1)
        provider = self._gptmail(
            [{"id": "3", "timestamp": fresh["timestamp"]}, {"id": "2", "timestamp": dup["timestamp"]}],
            {"2": dup, "3": fresh},
        )
        mailbox = {"address": "x@gptmail.test", "_tried_otp_codes": ["222222"]}
        provider.conf = {**provider.conf, "wait_timeout": 0.3}
        self.assertEqual(provider.wait_for_code(mailbox), "111111")
        # 返回过的码记入已试集合，之后同码不再返回。
        self.assertIn("111111", mailbox["_tried_otp_codes"])
        self.assertIsNone(provider.wait_for_code(mailbox))

    def test_detail_requests_are_cached_between_polls(self) -> None:
        notice = self._mail("2", "Welcome", 5)
        provider = self._gptmail([{"id": "2", "timestamp": notice["timestamp"]}], {"2": notice})
        provider.fetch_recent_messages({"address": "x@gptmail.test"})
        provider.fetch_recent_messages({"address": "x@gptmail.test"})
        detail_calls = [c for c in provider._request.call_args_list if c.args[1].startswith("/api/email/")]
        self.assertEqual(len(detail_calls), 1)

    def test_scan_limit_bounds_detail_requests(self) -> None:
        listing = [{"id": str(i), "timestamp": 1000 + i} for i in range(20)]
        details = {str(i): {"id": str(i), "subject": "s", "content": "none", "timestamp": 1000 + i} for i in range(20)}
        provider = self._gptmail(listing, details)
        messages = provider.fetch_recent_messages({"address": "x@gptmail.test"})
        self.assertEqual(len(messages), provider.recent_scan_limit)
        self.assertEqual(messages[0]["message_id"], "19")

    def test_tempmail_lol_scans_recent_messages_newest_first(self) -> None:
        session = MagicMock()
        session.headers = {}
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.TempMailLolProvider({"type": "tempmail_lol", "provider_ref": "t:1"}, dict(self.CONF))
        now = datetime.now(timezone.utc)
        provider._request = MagicMock(return_value={"emails": [  # type: ignore[method-assign]
            {"id": "old", "subject": "Your code", "body": "Verification code: 445566", "created_at": (now - timedelta(seconds=30)).isoformat()},
            {"id": "new", "subject": "Hello", "body": "welcome", "created_at": now.isoformat()},
        ]})
        self.assertEqual(provider.wait_for_code({"address": "a@tm.test", "token": "t"}), "445566")


if __name__ == "__main__":
    unittest.main()

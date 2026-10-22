from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from services import proxy_service
from services.proxy_service import ClearanceBundle, FlareSolverrClearanceProvider, ProxySettingsStore
from utils.fingerprint_pool import impersonate_for_user_agent


CHROME_120_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _FakeFlareSolverr:
    """记录发往 FlareSolverr 的每个命令，按命令返回预设响应。"""

    def __init__(self, user_agent: str = CHROME_120_UA) -> None:
        self.calls: list[dict] = []
        self.user_agent = user_agent
        self.created = 0
        self.fail_next_request = False
        self.lock = threading.Lock()

    def __call__(self, endpoint: str, body: bytes, headers: dict, timeout: float) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            self.calls.append(payload)
            cmd = payload.get("cmd")
            if cmd == "sessions.create":
                self.created += 1
                return json.dumps({"status": "ok", "session": f"s{self.created}"}).encode()
            if cmd == "sessions.destroy":
                return json.dumps({"status": "ok"}).encode()
            if self.fail_next_request:
                self.fail_next_request = False
                raise OSError("browser crashed")
        return json.dumps({
            "status": "ok",
            "solution": {
                "userAgent": self.user_agent,
                "cookies": [
                    {"name": "cf_clearance", "value": "clear-1", "domain": ".openai.com"},
                    {"name": "__cf_bm", "value": "bm-1", "domain": ".openai.com"},
                    {"name": "oai-did", "value": "leaked-device", "domain": ".openai.com"},
                    {"name": "__Secure-next-auth.session-token", "value": "secret", "domain": "auth.openai.com"},
                ],
            },
        }).encode()

    def commands(self, cmd: str) -> list[dict]:
        return [call for call in self.calls if call.get("cmd") == cmd]


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _ClearanceConfig:
    def __init__(self, clearance: dict | None = None, proxy_groups: list[dict] | None = None) -> None:
        self.clearance = {
            "enabled": True,
            "mode": "flaresolverr",
            "flaresolverr_url": "http://flaresolverr:8191",
            "timeout_sec": 30,
            "refresh_interval": 1000,
            **(clearance or {}),
        }
        self.data = {"proxy_groups": proxy_groups or []}

    def get_proxy_runtime_settings(self) -> dict:
        return {"enabled": True, "egress_mode": "direct", "clearance": dict(self.clearance)}

    def get_proxy_settings(self) -> str:
        return ""


def _provider(fake: _FakeFlareSolverr, **kwargs) -> FlareSolverrClearanceProvider:
    kwargs.setdefault("start_sweeper", False)
    return FlareSolverrClearanceProvider("http://flaresolverr:8191", request_method=fake, **kwargs)


class FlareSolverrSessionTest(unittest.TestCase):
    def test_same_egress_reuses_one_browser_session(self) -> None:
        fake = _FakeFlareSolverr()
        provider = _provider(fake)

        first = provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")
        second = provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(fake.commands("sessions.create")), 1)
        sessions = {call.get("session") for call in fake.commands("request.get")}
        self.assertEqual(sessions, {"s1"})
        self.assertEqual(fake.commands("sessions.create")[0]["proxy"], {"url": "http://p1:8080"})
        self.assertTrue(fake.commands("request.get")[0]["returnOnlyCookies"])
        self.assertEqual(provider.session_count(), 1)

    def test_only_cloudflare_cookies_are_returned(self) -> None:
        bundle = _provider(_FakeFlareSolverr()).get_clearance("https://auth.openai.com")

        self.assertEqual(set(bundle.cookies), {"cf_clearance", "__cf_bm"})
        self.assertEqual(bundle.user_agent, CHROME_120_UA)

    def test_session_cap_evicts_least_recently_used_idle_session(self) -> None:
        fake = _FakeFlareSolverr()
        clock = _Clock()
        provider = _provider(fake, max_sessions=1, clock=clock)

        provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")
        clock.now += 5
        provider.get_clearance("https://auth.openai.com", proxy_url="http://p2:8080")

        self.assertEqual(provider.session_count(), 1)
        self.assertEqual([call["session"] for call in fake.commands("sessions.destroy")], ["s1"])

    def test_all_sessions_busy_falls_back_to_one_shot_request(self) -> None:
        fake = _FakeFlareSolverr()
        provider = _provider(fake, max_sessions=1)
        busy = provider._ensure_session("http://p1:8080", 30)  # 占着不放

        bundle = provider.get_clearance("https://auth.openai.com", proxy_url="http://p2:8080")

        self.assertEqual(busy, "s1")
        self.assertIsNotNone(bundle)
        last_request = fake.commands("request.get")[-1]
        self.assertNotIn("session", last_request)
        self.assertEqual(last_request["proxy"], {"url": "http://p2:8080"})
        self.assertEqual(len(fake.commands("sessions.create")), 1)

    def test_idle_sessions_are_reclaimed(self) -> None:
        fake = _FakeFlareSolverr()
        clock = _Clock()
        provider = _provider(fake, idle_sec=60, clock=clock)
        provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")

        clock.now += 30
        self.assertEqual(provider.close_idle_sessions(), 0)
        clock.now += 31
        self.assertEqual(provider.close_idle_sessions(), 1)

        self.assertEqual(provider.session_count(), 0)
        self.assertEqual([call["session"] for call in fake.commands("sessions.destroy")], ["s1"])

    def test_broken_session_is_replaced_and_request_retried(self) -> None:
        fake = _FakeFlareSolverr()
        provider = _provider(fake)
        provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")
        fake.fail_next_request = True

        bundle = provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")

        self.assertIsNotNone(bundle)
        self.assertEqual([call["session"] for call in fake.commands("sessions.destroy")], ["s1"])
        self.assertEqual(fake.commands("request.get")[-1]["session"], "s2")

    def test_shutdown_stops_sweeper_and_closes_sessions(self) -> None:
        fake = _FakeFlareSolverr()
        provider = FlareSolverrClearanceProvider(
            "http://flaresolverr:8191",
            request_method=fake,
            idle_sec=3600,
        )
        provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080")
        sweeper = provider._sweeper
        self.assertIsNotNone(sweeper)
        self.assertTrue(sweeper.is_alive())

        provider.shutdown()

        self.assertFalse(sweeper.is_alive())
        self.assertEqual(provider.session_count(), 0)
        self.assertEqual([call["session"] for call in fake.commands("sessions.destroy")], ["s1"])
        # 关闭后不再新建会话，退回无会话请求。
        self.assertIsNotNone(provider.get_clearance("https://auth.openai.com", proxy_url="http://p1:8080"))
        self.assertEqual(len(fake.commands("sessions.create")), 1)


class ClearanceStoreTest(unittest.TestCase):
    def _store(self, config: _ClearanceConfig | None = None) -> tuple[ProxySettingsStore, _FakeFlareSolverr, list]:
        fake = _FakeFlareSolverr()
        started: list = []
        store = ProxySettingsStore(
            config or _ClearanceConfig(),
            clearance_provider_factory=lambda url: _provider(fake),
        )
        store._background_starter = started.append
        return store, fake, started

    def test_early_refresh_starts_after_ratio_of_lifetime(self) -> None:
        store, _fake, started = self._store()
        with patch.object(proxy_service.time, "time", return_value=1000.0):
            store.refresh_clearance(target_url="https://auth.openai.com")

        with patch.object(proxy_service.time, "time", return_value=1000.0 + 700):
            store.refresh_clearance(target_url="https://auth.openai.com")
        self.assertEqual(started, [])

        with patch.object(proxy_service.time, "time", return_value=1000.0 + 801):
            cached = store.refresh_clearance(target_url="https://auth.openai.com")
            store.refresh_clearance(target_url="https://auth.openai.com")

        self.assertIsNotNone(cached)  # 调用方立刻拿到仍有效的旧凭证
        self.assertEqual(len(started), 1)  # 同一出口只起一个后台刷新

    def test_background_refresh_replaces_bundle_and_clears_flag(self) -> None:
        store, fake, started = self._store()
        with patch.object(proxy_service.time, "time", return_value=1000.0):
            store.refresh_clearance(target_url="https://auth.openai.com")
        with patch.object(proxy_service.time, "time", return_value=1900.0):
            store.refresh_clearance(target_url="https://auth.openai.com")
            started.pop()()

        bundle = store._get_cached_bundle(store._cache_key("", "auth.openai.com"))
        self.assertEqual(bundle.created_at, 1900.0)
        self.assertEqual(bundle.expires_at, 2900.0)
        self.assertEqual(store._background_refreshing, set())
        self.assertEqual(len(fake.commands("request.get")), 2)

    def test_header_path_also_triggers_early_refresh(self) -> None:
        store, _fake, started = self._store()
        with patch.object(proxy_service.time, "time", return_value=1000.0):
            store.refresh_clearance(target_url="https://auth.openai.com")

        # 新鲜凭证：build_headers 带出 Cookie，但不起后台刷新。
        with patch.object(proxy_service.time, "time", return_value=1000.0 + 100):
            fresh = store.build_headers({}, target_url="https://auth.openai.com")
        self.assertIn("cf_clearance=clear-1", fresh["Cookie"])
        self.assertEqual(started, [])

        # 用过 80% 之后，build_headers 仍然返回旧凭证，同时在后台换新（同出口只起一个）。
        with patch.object(proxy_service.time, "time", return_value=1000.0 + 850):
            stale = store.build_headers({}, target_url="https://auth.openai.com")
            store.build_headers({}, target_url="https://auth.openai.com")
        self.assertIn("cf_clearance=clear-1", stale["Cookie"])
        self.assertEqual(len(started), 1)

    def test_custom_early_refresh_ratio(self) -> None:
        store, _fake, started = self._store(_ClearanceConfig({"early_refresh_ratio": 0.5}))
        with patch.object(proxy_service.time, "time", return_value=1000.0):
            store.refresh_clearance(target_url="https://auth.openai.com")
        with patch.object(proxy_service.time, "time", return_value=1000.0 + 501):
            store.refresh_clearance(target_url="https://auth.openai.com")
        self.assertEqual(len(started), 1)

    def test_refresh_keeps_previous_cookies_not_reissued(self) -> None:
        store, fake, _started = self._store()
        key = store._cache_key("", "auth.openai.com")
        store._set_cached_bundle(key, ClearanceBundle(
            target_host="auth.openai.com",
            cookies={"_cfuvid": "keep-me", "cf_clearance": "old"},
            user_agent="old-ua",
        ))

        bundle = store.refresh_clearance(target_url="https://auth.openai.com", force=True)

        self.assertEqual(bundle.cookies["_cfuvid"], "keep-me")
        self.assertEqual(bundle.cookies["cf_clearance"], "clear-1")
        self.assertEqual(bundle.user_agent, fake.user_agent)

    def test_provider_follows_session_settings(self) -> None:
        store, _fake, _started = self._store(_ClearanceConfig({"max_sessions": 2, "session_idle_sec": 45}))
        store.refresh_clearance(target_url="https://auth.openai.com")

        provider = store._get_provider("http://flaresolverr:8191")
        self.assertEqual(provider.max_sessions, 2)
        self.assertEqual(provider.idle_sec, 45)
        self.assertEqual(store.get_runtime_status()["flaresolverr_sessions"], 1)

    def test_warm_up_runs_once_per_egress_and_reports(self) -> None:
        store, _fake, started = self._store()
        results: list[tuple[str, bool]] = []

        count = store.warm_up(
            ["http://p1:8080", "http://p1:8080", "http://p2:8080"],
            "https://auth.openai.com",
            on_done=lambda proxy, ok: results.append((proxy, ok)),
        )
        for run in started:
            run()

        self.assertEqual(count, 2)
        self.assertEqual(sorted(results), [("http://p1:8080", True), ("http://p2:8080", True)])

    def test_warm_up_skips_when_flaresolverr_is_not_used(self) -> None:
        store, _fake, started = self._store(_ClearanceConfig({"mode": "manual"}))
        self.assertEqual(store.warm_up(["http://p1:8080"]), 0)
        self.assertEqual(started, [])

    def test_warm_up_on_start_follows_setting(self) -> None:
        store, _fake, started = self._store()
        self.assertEqual(store.warm_up_on_start(), 0)

        store, _fake, started = self._store(_ClearanceConfig({"warm_up_on_start": True}))
        self.assertEqual(store.warm_up_on_start(), 1)
        self.assertEqual(len(started), 1)

    def test_egress_candidates_expands_proxy_group(self) -> None:
        config = _ClearanceConfig(proxy_groups=[{
            "id": "g1",
            "nodes": [
                {"id": "a", "url": "http://a:1"},
                {"id": "b", "url": "http://b:1", "enabled": False},
                {"id": "c", "url": "http://c:1"},
            ],
        }])
        store, _fake, _started = self._store(config)

        self.assertEqual(store.egress_candidates("group:g1"), ["http://a:1", "http://c:1"])
        self.assertEqual(store.egress_candidates("http://x:1"), ["http://x:1"])
        self.assertEqual(store.egress_candidates("group:missing"), [])

    def test_challenge_cooldown_staggers_retries(self) -> None:
        store, _fake, _started = self._store()
        slept: list[float] = []

        self.assertEqual(store.wait_challenge_cooldown("http://p1:8080", sleep=slept.append), 0.0)
        store.mark_challenge("http://p1:8080", cooldown_sec=3.0)
        waited = store.wait_challenge_cooldown("http://p1:8080", sleep=slept.append)

        self.assertGreater(waited, 2.0)
        self.assertLess(waited, 4.3)
        self.assertEqual(slept, [waited])
        # 其他出口不受影响。
        self.assertEqual(store.wait_challenge_cooldown("http://p2:8080", sleep=slept.append), 0.0)

    def test_shutdown_disables_background_work(self) -> None:
        store, _fake, started = self._store()
        store.refresh_clearance(target_url="https://auth.openai.com")
        provider = store._get_provider("http://flaresolverr:8191")

        store.shutdown()

        self.assertEqual(provider.session_count(), 0)
        self.assertEqual(store.warm_up(["http://p1:8080"]), 0)
        self.assertEqual(started, [])


class ClearanceFingerprintSyncTest(unittest.TestCase):
    def test_impersonate_follows_chrome_major(self) -> None:
        self.assertEqual(impersonate_for_user_agent(CHROME_120_UA), "chrome120")
        self.assertEqual(impersonate_for_user_agent(CHROME_120_UA.replace("120.0", "141.0")), "chrome136")
        self.assertEqual(impersonate_for_user_agent(CHROME_120_UA.replace("120.0", "150.0")), "chrome146")
        self.assertEqual(impersonate_for_user_agent(CHROME_120_UA.replace("120.0", "100.0")), "chrome110")
        self.assertEqual(impersonate_for_user_agent("curl/8.0", "chrome142"), "chrome142")

    def test_fingerprint_with_user_agent_aligns_tls_target(self) -> None:
        from services.register import openai_register

        base = openai_register._complete_browser_fingerprint(
            openai_register._browser_profile("chrome142", openai_register.REGISTER_BROWSER_PROFILES[0]["user_agent"])
        )
        fp = openai_register._fingerprint_with_user_agent(base, CHROME_120_UA)

        self.assertEqual(fp["user_agent"], CHROME_120_UA)
        self.assertEqual(fp["impersonate"], "chrome120")
        self.assertIn('"Chromium";v="120"', fp["sec_ch_ua"])

    def test_registrar_rebuilds_session_when_tls_target_changes(self) -> None:
        from services.register import openai_register

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.proxy = ""
        registrar.fingerprint = openai_register._complete_browser_fingerprint(
            openai_register._browser_profile("chrome142", openai_register.REGISTER_BROWSER_PROFILES[0]["user_agent"])
        )
        registrar.clearance_user_agent = ""
        old_session = openai_register.create_session("", registrar.fingerprint)
        old_session.cookies.set("oai-did", "device-1", domain="auth.openai.com")
        registrar.session = old_session
        created: list[dict] = []
        real_create = openai_register.create_session

        def tracking_create(proxy: str = "", fingerprint: dict | None = None):
            created.append(dict(fingerprint or {}))
            return real_create(proxy, fingerprint)

        bundle = ClearanceBundle(
            target_host="auth.openai.com",
            cookies={"cf_clearance": "clear-1"},
            user_agent=CHROME_120_UA,
        )
        with patch.object(openai_register, "create_session", side_effect=tracking_create):
            registrar._apply_clearance_bundle(bundle)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["impersonate"], "chrome120")
        self.assertIsNot(registrar.session, old_session)
        self.assertEqual(registrar.session.cookies.get("oai-did", domain="auth.openai.com"), "device-1")
        self.assertEqual(registrar.session.headers["user-agent"], CHROME_120_UA)
        self.assertEqual(registrar.clearance_user_agent, CHROME_120_UA)
        registrar.session.close()

    def test_registrar_keeps_session_when_tls_target_unchanged(self) -> None:
        from services.register import openai_register

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.proxy = ""
        registrar.fingerprint = openai_register._complete_browser_fingerprint(
            openai_register._browser_profile("chrome120", CHROME_120_UA)
        )
        registrar.clearance_user_agent = ""
        session = MagicMock()
        session.headers = {}
        registrar.session = session

        with patch.object(openai_register, "create_session") as create:
            registrar._apply_clearance_bundle(ClearanceBundle(
                target_host="auth.openai.com",
                cookies={"cf_clearance": "clear-1"},
                user_agent=CHROME_120_UA,
            ))

        create.assert_not_called()
        self.assertIs(registrar.session, session)
        session.cookies.set.assert_any_call("cf_clearance", "clear-1", domain=".auth.openai.com")

    def test_refresh_waits_for_challenge_cooldown(self) -> None:
        from services.register import openai_register

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.proxy = "http://p1:8080"
        registrar.clearance_failure_reason = ""
        registrar._apply_clearance_bundle = MagicMock()
        settings = MagicMock()
        settings.get_profile.return_value = MagicMock(clearance_enabled=True)
        settings.wait_challenge_cooldown.return_value = 0.0
        settings.refresh_clearance.return_value = ClearanceBundle(target_host="auth.openai.com", user_agent="ua")
        registrar.fingerprint = {"impersonate": "chrome120"}

        with (
            patch.object(openai_register, "proxy_settings", settings),
            patch.object(openai_register, "step"),
        ):
            bundle = registrar._refresh_cloudflare_clearance(openai_register.auth_base, 1)

        self.assertIsNotNone(bundle)
        settings.mark_challenge.assert_called_once_with("http://p1:8080", cooldown_sec=2.0)
        settings.wait_challenge_cooldown.assert_called_once()
        settings.refresh_clearance.assert_called_once()
        registrar._apply_clearance_bundle.assert_called_once()


if __name__ == "__main__":
    unittest.main()

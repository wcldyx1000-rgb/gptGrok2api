from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services import egress_check, egress_quota, egress_slots, free_proxy_scheduler, proxy_service
from services.register import openai_register


class _ConfigStore:
    def __init__(self, groups: list[dict] | None = None, runtime: dict | None = None) -> None:
        self.data: dict[str, object] = {"proxy_groups": groups or []}
        self.runtime = runtime or {}

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return {"enabled": False, "egress_mode": "direct", "clearance": {}, **self.runtime}

    def get_proxy_settings(self) -> str:
        return ""


def _group(*urls: str) -> list[dict]:
    return [{
        "id": "pool",
        "enabled": True,
        "nodes": [{"id": f"n{i}", "url": url, "enabled": True} for i, url in enumerate(urls)],
    }]


class EgressCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        egress_check.clear_cache()
        self.addCleanup(egress_check.clear_cache)

    def test_parse_trace(self) -> None:
        trace = egress_check.parse_trace("fl=1\nip=203.0.113.9\nloc=US\nnoise\n")
        self.assertEqual(trace["ip"], "203.0.113.9")
        self.assertEqual(trace["loc"], "US")

    def test_verdict_levels(self) -> None:
        self.assertEqual(egress_check.verdict("CN", {})[0], "blocked")
        self.assertEqual(egress_check.verdict("hk", {})[0], "blocked")
        self.assertEqual(egress_check.verdict("US", {"isp": "DigitalOcean, LLC"})[0], "warn")
        self.assertEqual(egress_check.verdict("US", {"hosting": True})[0], "warn")
        self.assertEqual(egress_check.verdict("US", {"proxy": True, "isp": "Comcast"})[0], "warn")
        self.assertEqual(egress_check.verdict("US", {"isp": "Comcast Cable"})[0], "ok")

    def test_check_egress_uses_trace_and_ip_info(self) -> None:
        result = egress_check.check_egress(
            "http://user:secret@node:1",
            use_cache=False,
            fetch_trace=lambda proxy: {"ip": "203.0.113.9", "loc": "JP"},
            fetch_ip_info=lambda ip: {"country": "Japan", "city": "Tokyo", "isp": "NTT"},
        )
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["country"], "Japan")
        self.assertNotIn("secret", result["proxy"])

    def test_check_egress_reports_connection_errors_without_credentials(self) -> None:
        def fail(proxy: str) -> dict:
            raise RuntimeError(f"curl: (7) Failed to connect to {proxy}")

        result = egress_check.check_egress("http://user:secret@node:1", use_cache=False, fetch_trace=fail)
        self.assertEqual(result["verdict"], "error")
        self.assertNotIn("secret", result["message"])

    def _preflight(self, verdicts: list[str], **kwargs) -> dict:
        candidates = [f"http://node-{i}:1" for i in range(len(verdicts))]
        by_url = dict(zip(candidates, verdicts))
        with patch.object(egress_check, "register_egress_candidates", return_value=candidates):
            return egress_check.preflight(
                "group:pool",
                checker=lambda url, use_cache=True: {"proxy": url, "verdict": by_url[url], "message": by_url[url]},
                **kwargs,
            )

    def test_preflight_blocks_unsupported_region(self) -> None:
        self.assertFalse(self._preflight(["ok", "blocked"])["ok"])
        self.assertTrue(self._preflight(["ok", "blocked"], block_unsupported=False)["ok"])

    def test_preflight_only_blocks_when_every_egress_is_unreachable(self) -> None:
        self.assertTrue(self._preflight(["ok", "error"])["ok"])
        self.assertFalse(self._preflight(["error", "error"])["ok"])
        self.assertTrue(self._preflight(["warn"])["ok"])

    def test_candidates_skip_disabled_group_nodes(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore(_group("http://a:1", "http://b:1")))
        store.report_proxy_outcome("http://a:1", ok=False)
        with patch.object(egress_check, "proxy_settings", store):
            self.assertEqual(egress_check.register_egress_candidates("group:pool"), ["http://b:1"])


class EgressQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "usage.json"

    def test_daily_limit_spreads_then_exhausts(self) -> None:
        now = 1_800_000_000.0
        picks = [
            egress_quota.acquire(["a", "b"], daily_limit=2, min_gap_secs=0, path=self.path, now=now + i)[0]
            for i in range(4)
        ]
        self.assertEqual(sorted(picks), ["a", "a", "b", "b"])
        self.assertEqual(picks[:2], ["a", "b"])
        self.assertEqual(
            egress_quota.acquire(["a", "b"], daily_limit=2, min_gap_secs=0, path=self.path, now=now + 5),
            (None, None),
        )
        # 第二天额度重置。
        proxy, _ = egress_quota.acquire(["a", "b"], daily_limit=2, min_gap_secs=0, path=self.path, now=now + 86400)
        self.assertIsNotNone(proxy)

    def test_min_gap_returns_wait(self) -> None:
        now = 1_800_000_000.0
        self.assertEqual(
            egress_quota.acquire(["a"], daily_limit=0, min_gap_secs=60, path=self.path, now=now)[0], "a"
        )
        proxy, wait = egress_quota.acquire(["a"], daily_limit=0, min_gap_secs=60, path=self.path, now=now + 10)
        self.assertIsNone(proxy)
        self.assertGreaterEqual(wait, 50)
        self.assertEqual(
            egress_quota.acquire(["a"], daily_limit=0, min_gap_secs=60, path=self.path, now=now + 200)[0], "a"
        )

    def test_settings_default_to_unlimited(self) -> None:
        self.assertFalse(egress_quota.is_active(egress_quota.normalize_settings(None)))
        self.assertTrue(egress_quota.is_active(egress_quota.normalize_settings({"daily_limit": "3"})))
        self.assertEqual(egress_quota.normalize_settings({"daily_limit": -1, "min_gap_secs": "x"})["daily_limit"], 0)

    def test_register_waits_then_aborts_when_every_egress_is_capped(self) -> None:
        sleeps: list[float] = []
        results = iter([(None, 5.0), ("http://a:1", None)])
        with patch.dict(openai_register.config, {"egress": {"daily_limit": 1, "min_gap_secs": 10}}), patch.object(
            openai_register.egress_quota, "acquire", side_effect=lambda *a, **k: next(results)
        ), patch.object(openai_register, "_interruptible_sleep", side_effect=sleeps.append), patch.object(
            openai_register, "_should_stop", return_value=False
        ):
            self.assertEqual(openai_register._acquire_register_egress(1, "http://a:1", set()), "http://a:1")
        self.assertEqual(len(sleeps), 1)

        with patch.dict(openai_register.config, {"egress": {"daily_limit": 1}}), patch.object(
            openai_register.egress_quota, "acquire", return_value=(None, None)
        ), patch.object(openai_register, "_should_stop", return_value=False):
            with self.assertRaises(openai_register.RegisterJobAborted):
                openai_register._acquire_register_egress(1, "http://a:1", set())

    def test_register_without_quota_keeps_previous_behaviour(self) -> None:
        with patch.dict(openai_register.config, {"egress": {}}), patch.object(
            openai_register.egress_quota, "acquire"
        ) as acquire:
            self.assertEqual(openai_register._acquire_register_egress(1, "http://fixed:1", set()), "http://fixed:1")
        acquire.assert_not_called()


class NodeHealthTest(unittest.TestCase):
    def test_threshold_and_cooldown(self) -> None:
        store = proxy_service.ProxySettingsStore(
            _ConfigStore(_group("http://a:1"), {"node_fail_threshold": 2, "node_cooldown_sec": 60})
        )
        self.assertFalse(store.report_proxy_outcome("http://a:1", ok=False))
        self.assertFalse(store.is_proxy_disabled("http://a:1"))
        self.assertTrue(store.report_proxy_outcome("http://a:1", ok=False, error="curl: (7) http://u:p@a:1"))
        self.assertTrue(store.is_proxy_disabled("http://a:1"))
        snapshot = store.node_health_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertNotIn("u:p", snapshot[0]["last_error"])
        # 冷却结束后自动恢复。
        self.assertFalse(store.is_proxy_disabled("http://a:1", now=time.time() + 61))
        self.assertEqual(store.disabled_proxies(), set())

    def test_success_resets_failures(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore(runtime={"node_fail_threshold": 2}))
        store.report_proxy_outcome("http://a:1", ok=False)
        store.report_proxy_outcome("http://a:1", ok=True)
        self.assertFalse(store.report_proxy_outcome("http://a:1", ok=False))
        self.assertFalse(store.is_proxy_disabled("http://a:1"))

    def test_group_selection_skips_disabled_nodes_but_never_empties(self) -> None:
        store = proxy_service.ProxySettingsStore(_ConfigStore(_group("http://a:1", "http://b:1")))
        store.report_proxy_outcome("http://a:1", ok=False)
        picks = {store.get_profile(proxy="group:pool", upstream=True).proxy_url for _ in range(20)}
        self.assertEqual(picks, {"http://b:1"})
        store.report_proxy_outcome("http://b:1", ok=False)
        picks = {store.get_profile(proxy="group:pool", upstream=True).proxy_url for _ in range(30)}
        self.assertTrue(picks <= {"http://a:1", "http://b:1"} and picks)

    def test_disabled_node_does_not_touch_saved_config(self) -> None:
        cfg = _ConfigStore(_group("http://a:1"))
        store = proxy_service.ProxySettingsStore(cfg)
        store.report_proxy_outcome("http://a:1", ok=False)
        self.assertEqual(cfg.data["proxy_groups"][0]["nodes"][0]["url"], "http://a:1")


class EgressSlotsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(egress_slots.set_store_path(Path(temp.name) / "egress.json"))
        egress_slots._save({"settings": {"enabled": True, "proxy_template": "http://mihomo:{port}"}})

    def test_build_config_rejects_by_default(self) -> None:
        data = egress_slots._load()
        data["sources"] = [{"id": "src-1", "kind": "manual", "name": "res", "lines": ["http://u:p@1.2.3.4:8000"]}]
        data["slots"] = {"1": {"node": "[res] 1.2.3.4:8000"}, "2": {"node": "[gone] x:1"}}
        payload, digest = egress_slots.build_config(data)
        groups = {group["name"]: group for group in payload["proxy-groups"]}
        self.assertEqual(groups["slot-1"]["proxies"], ["REJECT", "[res] 1.2.3.4:8000"])
        self.assertEqual(groups["slot-2"]["proxies"], ["REJECT"])
        self.assertIn(f"cfg-{digest}", groups)
        self.assertEqual(payload["sub-rules"]["slot-1"][0], "RULE-SET,openai,slot-1")

    def test_missing_node_keeps_slot_rejecting(self) -> None:
        data = egress_slots._load()
        data["slots"] = {"1": {"node": "[gone] x:1"}}
        with patch.object(egress_slots, "_select", return_value=False):
            egress_slots._sync_slots(data)
        self.assertTrue(egress_slots.slot_down(1))
        self.assertIn("拒绝", egress_slots._slot_state[1]["error"])

    def test_slot_of_proxy(self) -> None:
        self.assertEqual(egress_slots.slot_of_proxy("http://mihomo:7002"), 2)
        self.assertIsNone(egress_slots.slot_of_proxy("http://other:7002"))
        self.assertEqual(egress_slots.parse_target("slot-3"), "slot:3")
        with self.assertRaises(ValueError):
            egress_slots.parse_target("slot:99")

    def test_switch_holds_account_then_applies(self) -> None:
        service = _FakeAccounts({"tok-1": {"access_token": "tok-1", "status": "正常", "proxy": ""}})
        result = egress_slots.request_switch(["tok-1"], "slot:1", service=service)
        self.assertEqual(result["scheduled"], ["tok-1"])
        account = service.accounts["tok-1"]
        self.assertEqual(account["status"], "禁用")
        self.assertEqual(account["proxy"], "")

        self.assertEqual(egress_slots.apply_due_switches(service=service), [])
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        self.assertEqual(egress_slots.apply_due_switches(later, service=service), ["tok-1"])
        account = service.accounts["tok-1"]
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["proxy"], "http://mihomo:7001")
        self.assertIsNone(account["egress_switch_target"])

    def test_cancel_and_immediate_switch(self) -> None:
        service = _FakeAccounts({"tok-1": {"access_token": "tok-1", "status": "限流", "proxy": "http://mihomo:7001"}})
        egress_slots.request_switch(["tok-1"], "direct", service=service)
        egress_slots.cancel_switch(["tok-1"], service=service)
        self.assertEqual(service.accounts["tok-1"]["status"], "限流")
        self.assertEqual(service.accounts["tok-1"]["proxy"], "http://mihomo:7001")

        egress_slots.update_settings({"switch_hold_hours": 0})
        result = egress_slots.request_switch(["tok-1"], "direct", service=service)
        self.assertEqual(result["applied"], ["tok-1"])
        self.assertEqual(service.accounts["tok-1"]["proxy"], "direct")
        self.assertEqual(service.accounts["tok-1"]["status"], "限流")

    def test_slot_locale_set_clear_and_request_path(self) -> None:
        egress_slots._locale_cache = None
        snap = egress_slots.set_slot_locale(2, "Asia/Taipei", "zh_tw")
        self.assertEqual(snap["slots"][1]["locale"], {"timezone": "Asia/Taipei", "language": "zh-TW"})
        locale = egress_slots.slot_locale_for_proxy("http://mihomo:7002")
        self.assertIsNotNone(locale)
        self.assertEqual(locale.language, "zh-TW")
        self.assertIsNone(egress_slots.slot_locale_for_proxy("http://mihomo:7001"))
        with self.assertRaises(ValueError):
            egress_slots.set_slot_locale(2, "Asia/Taipei", "")
        with self.assertRaises(ValueError):
            egress_slots.set_slot_locale(99, "Asia/Taipei", "zh-TW")
        egress_slots.set_slot_locale(2, "", "")
        egress_slots._locale_cache = None
        self.assertIsNone(egress_slots.slot_locale_for_proxy("http://mihomo:7002"))

    def test_profile_uses_slot_locale_for_slot_proxy(self) -> None:
        egress_slots._locale_cache = None
        egress_slots.set_slot_locale(1, "Europe/Berlin", "de-DE")
        store = proxy_service.ProxySettingsStore(_ConfigStore({}))
        profile = store.get_profile(account={"proxy": "http://mihomo:7001"}, upstream=True)
        self.assertEqual(profile.locale.timezone, "Europe/Berlin")

    def test_slot_capacity_blocks_extra_accounts(self) -> None:
        self.assertEqual(egress_slots.settings()["slot_capacity"], 1000)
        egress_slots.update_settings({"slot_capacity": 1})
        service = _FakeAccounts({
            "a": {"access_token": "a", "status": "正常", "proxy": "http://mihomo:7001"},
            "b": {"access_token": "b", "status": "正常", "proxy": ""},
        })
        result = egress_slots.request_switch(["b"], "slot:1", service=service)
        self.assertEqual(result["scheduled"], [])
        self.assertIn("已满", result["skipped"][0]["reason"])
        self.assertEqual(egress_slots.overview(service=service)["slots"][0]["capacity"], 1)

    def test_operations_require_enabled(self) -> None:
        egress_slots.update_settings({"enabled": False})
        with self.assertRaises(ValueError):
            egress_slots.set_slot(1, None)


class _FakeAccounts:
    def __init__(self, accounts: dict[str, dict]) -> None:
        self.accounts = accounts

    def get_account(self, token: str) -> dict | None:
        account = self.accounts.get(token)
        return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        return [dict(item) for item in self.accounts.values()]

    def update_account(self, token: str, updates: dict, quiet: bool = False) -> dict:
        self.accounts[token] = {**self.accounts[token], **updates}
        return dict(self.accounts[token])


class FreeProxyTest(unittest.TestCase):
    def test_parse_rejects_private_and_garbage(self) -> None:
        self.assertEqual(
            free_proxy_scheduler.parse_proxy_line("8.8.4.4:3128", protocol="http")["url"], "http://8.8.4.4:3128"
        )
        self.assertIsNone(free_proxy_scheduler.parse_proxy_line("10.0.0.1:3128", protocol="http"))
        self.assertIsNone(free_proxy_scheduler.parse_proxy_line("127.0.0.1:8080", protocol="http"))
        self.assertIsNone(free_proxy_scheduler.parse_proxy_line("<html>", protocol="http"))

    def test_refresh_keeps_only_checked_nodes(self) -> None:
        body = "8.8.4.4:3128\n9.9.9.9:8080\n1.1.1.1:80\n"
        verdicts = {
            "http://8.8.4.4:3128": {"verdict": "ok", "latency_ms": 300},
            "http://9.9.9.9:8080": {"verdict": "blocked"},
            "http://1.1.1.1:80": {"verdict": "warn", "hosting": True, "latency_ms": 100},
        }
        saved: list[list[dict]] = []
        settings = free_proxy_scheduler.normalize_settings({"allow_datacenter": False})
        result = free_proxy_scheduler.refresh_pool(
            settings,
            http_get=lambda url: body if "proxyscrape" in url and "country=US" in url else "",
            checker=lambda url, use_cache=False: verdicts[url],
            save_group=saved.append,
        )
        self.assertEqual(result["ok"], 1)
        self.assertEqual([item["url"] for item in saved[0]], ["http://8.8.4.4:3128"])

    def test_nothing_saved_when_no_node_passes(self) -> None:
        saved: list = []
        result = free_proxy_scheduler.refresh_pool(
            free_proxy_scheduler.normalize_settings({}),
            http_get=lambda url: "8.8.4.4:3128" if "country=US" in url else "",
            checker=lambda url, use_cache=False: {"verdict": "error"},
            save_group=saved.append,
        )
        self.assertEqual((result["ok"], saved), (0, []))

    def test_scheduler_disabled_by_default_and_runs_on_enable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = free_proxy_scheduler.FreeProxyScheduler(Path(temp) / "free.json")
            status = scheduler.status()
            self.assertFalse(status["enabled"])
            self.assertFalse(scheduler._due(scheduler._load()))
            scheduler.update({"enabled": True, "last_run_at": "forged"})
            self.assertTrue(scheduler._due(scheduler._load()))
            self.assertEqual(scheduler.status()["last_run_at"], "")


if __name__ == "__main__":
    unittest.main()

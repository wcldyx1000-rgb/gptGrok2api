from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import proxy_service
from services.proxy_service import (
    DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT,
    MAX_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT,
    ImageEgressDeadlineError,
    ProxyReferenceUnavailableError,
    ProxySettingsStore,
    proxy_node_image_concurrency_limit,
)


class _Config:
    def __init__(self, *, groups=None, default_proxy="", fallback="", account_groups=None) -> None:
        self.data = {
            "proxy_groups": groups or [],
            "account_groups": account_groups or [],
        }
        self._default_proxy = default_proxy
        self._fallback = fallback

    def get_proxy_runtime_settings(self) -> dict:
        return {"enabled": False, "egress_mode": "direct", "clearance": {}}

    def get_proxy_settings(self) -> str:
        return self._default_proxy

    def get_proxy_fallback_settings(self) -> str:
        return self._fallback


def _group(group_id: str = "g1", *, limit: int = 1, enabled: bool = True, nodes: int = 1) -> dict:
    return {
        "id": group_id,
        "enabled": enabled,
        "nodes": [
            {"id": f"n{index}", "url": f"http://10.0.0.{index + 1}:8080", "image_concurrency_limit": limit}
            for index in range(nodes)
        ],
    }


def _profile(limit: int = 1, key: str = "egress-a") -> SimpleNamespace:
    return SimpleNamespace(
        image_egress_reserved=False,
        image_egress_wait_ms=0,
        image_concurrency_limit=limit,
        egress_key=key,
        proxy_url="",
    )


class AcquireImageEgressDeadlineTest(unittest.TestCase):
    def test_expired_deadline_fails_before_touching_capacity(self) -> None:
        store = ProxySettingsStore(_Config())
        profile = _profile()
        with self.assertRaises(ImageEgressDeadlineError):
            store.acquire_image_egress(profile, deadline_monotonic=time.monotonic() - 1)
        self.assertEqual(store._egress_inflight, {})

    def test_waits_until_deadline_then_raises(self) -> None:
        store = ProxySettingsStore(_Config())
        profile = _profile(limit=1)
        self.assertEqual(store.acquire_image_egress(profile) >= 0, True)

        started = time.monotonic()
        with self.assertRaises(ImageEgressDeadlineError):
            store.acquire_image_egress(profile, deadline_monotonic=started + 0.15)
        self.assertLess(time.monotonic() - started, 2.0)
        # 超时不能泄露名额：释放后仍能正常取得。
        store.release_image_egress(profile)
        store.acquire_image_egress(profile, deadline_monotonic=time.monotonic() + 1)
        store.release_image_egress(profile)

    def test_no_deadline_or_non_positive_deadline_means_unbounded_semantics(self) -> None:
        store = ProxySettingsStore(_Config())
        profile = _profile(limit=2)
        store.acquire_image_egress(profile)
        store.acquire_image_egress(profile, deadline_monotonic=0)
        self.assertEqual(store._egress_inflight["egress-a"], 2)

    def test_reserved_profile_does_not_wait(self) -> None:
        store = ProxySettingsStore(_Config())
        profile = _profile()
        profile.image_egress_reserved = True
        profile.image_egress_wait_ms = 7
        self.assertEqual(store.acquire_image_egress(profile, deadline_monotonic=time.monotonic() + 5), 7)

    def test_group_selection_respects_deadline(self) -> None:
        store = ProxySettingsStore(_Config(groups=[_group(limit=1)]))
        first = store.get_profile(proxy="group:g1", upstream=True, reserve_image_egress=True)
        self.assertTrue(first.image_egress_reserved)

        started = time.monotonic()
        with self.assertRaises(ImageEgressDeadlineError):
            store.get_profile(
                proxy="group:g1",
                upstream=True,
                reserve_image_egress=True,
                deadline_monotonic=started + 0.15,
            )
        self.assertLess(time.monotonic() - started, 2.0)
        store.release_image_egress(first)
        again = store.get_profile(
            proxy="group:g1",
            upstream=True,
            reserve_image_egress=True,
            deadline_monotonic=time.monotonic() + 2,
        )
        self.assertTrue(again.image_egress_reserved)
        store.release_image_egress(again)


class ProxyReferenceUnavailableTest(unittest.TestCase):
    def test_missing_group_is_an_explicit_error_not_direct(self) -> None:
        store = ProxySettingsStore(_Config())
        with self.assertRaisesRegex(ProxyReferenceUnavailableError, "proxy group is unavailable: nope"):
            store.get_profile(proxy="group:nope", upstream=True)

    def test_disabled_or_empty_group_is_unavailable(self) -> None:
        store = ProxySettingsStore(_Config(groups=[_group("off", enabled=False), _group("empty", nodes=0)]))
        for ref in ("group:off", "group:empty"):
            with self.subTest(ref=ref), self.assertRaises(ProxyReferenceUnavailableError):
                store.get_profile(proxy=ref, upstream=True)

    def test_account_and_default_references_also_raise(self) -> None:
        store = ProxySettingsStore(_Config(default_proxy="group:gone"))
        with self.assertRaises(ProxyReferenceUnavailableError):
            store.get_profile(upstream=True)
        with self.assertRaises(ProxyReferenceUnavailableError):
            ProxySettingsStore(_Config()).get_profile(account={"proxy": "group:gone"}, upstream=True)

    def test_healthy_group_still_resolves(self) -> None:
        store = ProxySettingsStore(_Config(groups=[_group("ok")]))
        profile = store.get_profile(proxy="group:ok", upstream=True)
        self.assertEqual(profile.proxy_url, "http://10.0.0.1:8080")
        self.assertEqual(profile.proxy_group_id, "ok")

    def test_runtime_status_reports_instead_of_raising(self) -> None:
        store = ProxySettingsStore(_Config(default_proxy="group:gone"))
        status = store.get_runtime_status()
        self.assertIn("proxy group is unavailable", status["reference_error"])

    def test_warm_up_skips_unavailable_reference(self) -> None:
        store = ProxySettingsStore(_Config())
        self.assertEqual(store.warm_up(["group:gone"]), 0)


class NodeConcurrencyLimitTest(unittest.TestCase):
    def test_clamps_and_falls_back(self) -> None:
        self.assertEqual(
            proxy_node_image_concurrency_limit({"image_concurrency_limit": 10**9}),
            MAX_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT,
        )
        self.assertEqual(proxy_node_image_concurrency_limit({"image_concurrency_limit": -3}), 0)
        self.assertEqual(proxy_node_image_concurrency_limit({"image_concurrency_limit": "12"}), 12)
        self.assertEqual(
            proxy_node_image_concurrency_limit({"image_concurrency_limit": "abc"}),
            DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT,
        )
        self.assertEqual(
            proxy_node_image_concurrency_limit({"image_concurrency_limit": float("inf")}),
            DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT,
        )
        self.assertEqual(proxy_node_image_concurrency_limit({}), DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT)

    def test_alias_keys_and_fallback_mapping(self) -> None:
        self.assertEqual(proxy_node_image_concurrency_limit({"max_image_concurrency": 4}), 4)
        self.assertEqual(proxy_node_image_concurrency_limit({}, fallback={"image_concurrency": 9}), 9)

    def test_selection_uses_clamped_value(self) -> None:
        group = _group("big", limit=10**9)
        store = ProxySettingsStore(_Config(groups=[group]))
        profile = store.get_profile(proxy="group:big", upstream=True)
        self.assertEqual(profile.image_concurrency_limit, MAX_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT)


class ProxyGroupReferencesTest(unittest.TestCase):
    def _store(self, **kwargs) -> ProxySettingsStore:
        return ProxySettingsStore(_Config(groups=[_group("g1")], **kwargs))

    def _no_accounts_no_register(self):
        from services.account_service import account_service
        from services.register_service import register_service

        return (
            patch.object(account_service, "list_accounts", return_value=[]),
            patch.object(register_service, "get", return_value={"proxy": ""}),
        )

    def test_unreferenced_group_is_free_to_delete(self) -> None:
        accounts, register = self._no_accounts_no_register()
        with accounts, register:
            self.assertEqual(self._store().proxy_group_references("g1"), [])

    def test_finds_every_kind_of_reference(self) -> None:
        from services.account_service import account_service
        from services.register_service import register_service

        store = self._store(
            default_proxy="group:g1",
            fallback="group:g1",
            account_groups=[{"id": "ag", "name": "A组", "proxy_group_id": "g1"}, {"id": "bg", "proxy": "group:g1"}],
        )
        accounts = [{"proxy": "group:g1"}, {"proxy": "group:g1"}, {"proxy": "group:other"}, {}]
        with (
            patch.object(account_service, "list_accounts", return_value=accounts),
            patch.object(register_service, "get", return_value={"proxy": "group:g1"}),
        ):
            refs = store.proxy_group_references("g1")
        self.assertIn("全局默认代理", refs)
        self.assertIn("备用代理", refs)
        self.assertIn("账号分组「A组」", refs)
        self.assertIn("2 个账号", refs)
        self.assertIn("注册任务代理", refs)

    def test_lookup_failures_do_not_raise(self) -> None:
        from services.account_service import account_service

        with patch.object(account_service, "list_accounts", side_effect=RuntimeError("boom")):
            self.assertIsInstance(self._store().proxy_group_references("g1"), list)


class WarpComposeFlareSolverrLimitsTest(unittest.TestCase):
    def test_flaresolverr_has_memory_cap_and_shm(self) -> None:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "docker-compose.warp.yml").read_text(encoding="utf-8")
        start = text.index("  flaresolverr:")
        block = text[start:].split("  icloud-privacy-mail:", 1)[0]
        self.assertIn("mem_limit: ${FLARESOLVERR_MEM_LIMIT:-1536m}", block)
        self.assertIn("shm_size: 256m", block)


if __name__ == "__main__":
    unittest.main()

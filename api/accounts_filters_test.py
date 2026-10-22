"""账号列表“更多筛选 / 调度状态筛选 / 汇总 / 详情”的接口测试（全部使用内存存储，不碰真实 data/）。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import api.accounts as accounts_api
from api.accounts_management_test import _ApiCase
from services import account_view
from services.proxy_service import normalize_proxy_url

SLOT_PROXY = "http://127.0.0.1:20001"
DOWN_PROXY = "http://127.0.0.1:20002"


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _local(delta: timedelta) -> str:
    return (datetime.now() + delta).strftime("%Y-%m-%d %H:%M:%S")


class AccountFilterApiTest(_ApiCase):
    ACCOUNTS = [
        # 正常、有 RT、最近用过、绑定可用槽位
        {"access_token": "tok-ok", "email": "ok@example.test", "refresh_token": "rt-ok", "type": "Plus",
         "success": 9, "fail": 1, "last_used_at": _local(timedelta(minutes=-5)), "proxy": SLOT_PROXY},
        # 没 RT、没 Outlook 凭据、失败率高、从没用过、Free、codex 来源
        {"access_token": "tok-nort", "email": "nort@example.test", "type": "free", "source_type": "codex",
         "success": 2, "fail": 3, "last_remote_check_result": "invalid"},
        # RT 缺失但有 Outlook 凭据：RT 缺失，但救得回来
        {"access_token": "tok-outlook", "email": "ol@example.test", "type": "Team",
         "outlook_client_id": "cid", "outlook_refresh_token": "ort", "last_used_at": _local(timedelta(days=-3))},
        # 自动暂停
        {"access_token": "tok-paused", "email": "p@example.test", "refresh_token": "rt-p", "type": "Pro",
         "auto_paused_until": _iso(timedelta(hours=2)), "last_used_at": _local(timedelta(minutes=-1))},
        # 换出口中
        {"access_token": "tok-switch", "email": "sw@example.test", "refresh_token": "rt-s",
         "egress_switch_target": "slot:1", "egress_switch_at": _iso(timedelta(hours=20)),
         "last_used_at": _local(timedelta(minutes=-1))},
        # 绑定的槽位不可用
        {"access_token": "tok-down", "email": "down@example.test", "refresh_token": "rt-d", "proxy": DOWN_PROXY,
         "last_used_at": _local(timedelta(minutes=-1))},
    ]

    def setUp(self) -> None:
        super().setUp()
        context = {
            "slots": {normalize_proxy_url(SLOT_PROXY): 1, normalize_proxy_url(DOWN_PROXY): 2},
            "down": {2},
            "capacity": 10,
            "groups": {},
            "mail": {},
        }
        for target in (
            patch.object(accounts_api, "_account_display_context", return_value=context),
            patch.object(account_view, "egress_context", return_value=context),
        ):
            target.start()
            self.addCleanup(target.stop)

    def emails(self, **params) -> list[str]:
        response = self.client.get("/api/accounts", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return sorted(item["email"] for item in response.json()["items"])

    def test_flag_credential_risk(self) -> None:
        self.assertEqual(self.emails(flag="rt_missing"), ["nort@example.test", "ol@example.test"])
        # Outlook 凭据能收码重登，不算“救不回”
        self.assertEqual(self.emails(flag="no_rescue"), ["nort@example.test"])

    def test_flag_performance(self) -> None:
        self.assertEqual(self.emails(flag="high_failure"), ["nort@example.test"])
        self.assertEqual(self.emails(flag="auto_paused"), ["p@example.test"])
        self.assertEqual(self.emails(flag="idle"), ["nort@example.test", "ol@example.test"])

    def test_flag_egress(self) -> None:
        self.assertEqual(self.emails(flag="egress_switching"), ["sw@example.test"])
        self.assertEqual(self.emails(flag="egress_down"), ["down@example.test"])
        # 绑了槽位的 ok / down 与换出口中的 sw 都不算“未分配槽位”
        self.assertEqual(
            self.emails(flag="egress_direct"),
            ["nort@example.test", "ol@example.test", "p@example.test"],
        )

    def test_flag_source_and_plan(self) -> None:
        self.assertEqual(self.emails(flag="source:codex"), ["nort@example.test"])
        self.assertEqual(len(self.emails(flag="source:web")), 5)
        # 未标注套餐的账号入池后默认按 free 处理
        self.assertEqual(self.emails(flag="plan:free"), ["down@example.test", "nort@example.test", "sw@example.test"])
        self.assertEqual(self.emails(flag="plan:plus"), ["ok@example.test"])
        self.assertEqual(self.emails(flag="plan:team"), ["ol@example.test"])

    def test_unknown_or_all_flag_does_not_filter(self) -> None:
        self.assertEqual(len(self.emails(flag="all")), 6)
        self.assertEqual(len(self.emails(flag="whatever")), 6)

    def test_schedule_status_filters(self) -> None:
        self.assertEqual(self.emails(status="sched:auto_paused"), ["p@example.test"])
        self.assertEqual(self.emails(status="sched:egress_switching"), ["sw@example.test"])
        self.assertEqual(self.emails(status="sched:egress_down"), ["down@example.test"])
        self.assertEqual(self.emails(status="sched:no_credential"), ["nort@example.test"])
        self.assertEqual(self.emails(status="sched:ready"), ["ok@example.test", "ol@example.test"])

    def test_summary_matches_filters_and_ignores_status(self) -> None:
        body = self.client.get("/api/accounts").json()
        summary = body["availability_summary"]
        self.assertEqual(sum(summary.values()), body["all_total"])
        # 每个芯片的数量 == 点击后筛出来的 total
        for code, count in summary.items():
            filtered = self.client.get("/api/accounts", params={"status": f"sched:{code}"}).json()
            self.assertEqual(filtered["total"], count, code)
            # 已选中某个调度状态时，汇总仍是除状态外的全量（方便点别的芯片切换）
            self.assertEqual(filtered["availability_summary"], summary)

    def test_summary_respects_other_filters(self) -> None:
        body = self.client.get("/api/accounts", params={"flag": "rt_missing", "status": "sched:ready"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["availability_summary"], {"no_credential": 1, "ready": 1})
        keyword = self.client.get("/api/accounts", params={"keyword": "down@"}).json()
        self.assertEqual(keyword["availability_summary"], {"egress_down": 1})

    def test_summary_survives_pagination(self) -> None:
        page = self.client.get("/api/accounts", params={"page": 2, "page_size": 2}).json()
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(sum(page["availability_summary"].values()), 6)

    def test_item_carries_availability(self) -> None:
        items = {item["email"]: item for item in self.client.get("/api/accounts").json()["items"]}
        self.assertEqual(items["p@example.test"]["availability"]["code"], "auto_paused")
        self.assertEqual(items["ok@example.test"]["availability"]["label"], "可接请求")

    def test_selection_preview_matches_new_filters(self) -> None:
        cases = (
            {"flag": "rt_missing"},
            {"status": "sched:auto_paused"},
            {"flag": "plan:plus"},
            {"flag": "egress_down", "status": "sched:egress_down"},
        )
        for params in cases:
            preview = self.client.post(
                "/api/accounts/selection-preview", json={"selection": {"mode": "filter", **params}},
            ).json()
            listed = self.client.get("/api/accounts", params=params).json()
            self.assertEqual(preview["matching_count"], listed["total"], params)
            self.assertEqual(sorted(preview["account_ids"]), sorted(item["id"] for item in listed["items"]), params)

    def test_selection_exclusion_and_batch_egress_cancel(self) -> None:
        sw_id = self.id_of("tok-switch")
        preview = self.client.post("/api/accounts/selection-preview", json={"selection": {
            "mode": "filter", "flag": "egress_switching", "excluded_account_ids": [self.id_of("tok-ok")],
        }}).json()
        self.assertEqual(preview["account_ids"], [sw_id])
        with patch("services.egress_slots.cancel_switch", return_value={"cancelled": ["x"]}) as cancel:
            response = self.client.post("/api/accounts/egress/cancel-switch", json={"selection": {
                "mode": "filter", "flag": "egress_switching",
            }})
        self.assertEqual(response.status_code, 200, response.text)
        cancel.assert_called_once_with(["tok-switch"])

    def test_batch_egress_switch_resolves_cross_page_selection(self) -> None:
        with patch(
            "services.egress_slots.request_switch",
            return_value={"scheduled": [], "applied": [], "skipped": []},
        ) as switch:
            response = self.client.post("/api/accounts/egress/switch", json={
                "selection": {"mode": "filter", "flag": "rt_missing"}, "target": "direct",
            })
        self.assertEqual(response.status_code, 200, response.text)
        tokens, target = switch.call_args.args
        self.assertEqual(sorted(tokens), ["tok-nort", "tok-outlook"])
        self.assertEqual(target, "direct")

    def test_detail_endpoint_redacts_secrets(self) -> None:
        account_id = self.id_of("tok-ok")
        response = self.client.get(f"/api/accounts/{account_id}")
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["item"]
        self.assertEqual(item["id"], account_id)
        self.assertEqual(item["email"], "ok@example.test")
        self.assertTrue(item["has_refresh_token"])
        self.assertNotIn("refresh_token", item)
        self.assertNotIn("management_id", item)
        self.assertIn("availability", item)
        self.assertNotIn("rt-ok", response.text)

    def test_detail_unknown_is_404_and_fixed_routes_not_shadowed(self) -> None:
        self.assertEqual(self.client.get("/api/accounts/does-not-exist").status_code, 404)
        self.assertEqual(self.client.get("/api/accounts/health").status_code, 200)
        self.assertEqual(self.client.get("/api/accounts/login-import").status_code, 200)


if __name__ == "__main__":
    unittest.main()

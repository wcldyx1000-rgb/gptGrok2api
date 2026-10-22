"""Display projection for Upstream Accounts (credential / rescue / quota / egress fields).

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Everything here is derived, read-only presentation data for the admin SPA.  It
reports booleans and timestamps only: it never returns an access / refresh / id
token, an Outlook secret, a password or a 2FA secret, and every error text goes
through ``sanitize_diagnostic_text`` first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.account_credentials import (
    access_token_expires_at,
    access_token_issued_at,
    project_upstream_credential_availability,
)
from utils.diagnostics import sanitize_diagnostic_text

_ACCESS_TOKEN_PRESENTATION = {
    "valid": ("AT 有效", "success"),
    "expiring": ("AT 临期", "warning"),
    "invalid": ("AT 失效", "error"),
}
_REFRESH_TOKEN_PRESENTATION = {
    "valid": ("RT 有效", "success"),
    "missing": ("RT 缺失", "neutral"),
    "invalid": ("RT 失效", "error"),
}
_CREDENTIAL_AVAILABILITY_PRESENTATION = {
    "usable": ("可用", "success"),
    "recoverable": ("可恢复", "warning"),
    "unavailable": ("不可用", "error"),
}

# 上游对没开始计时的额度窗口，每次查询都返回“查询时刻 + 24 小时”作为重置时间。
_IDLE_RESET_WINDOW_SECS = 24 * 3600
_IDLE_RESET_TOLERANCE_SECS = 10 * 60

# 响应里绝不能出现的账号字段（只给 has_* 布尔）。
SECRET_ACCOUNT_FIELDS = (
    "cookie_header", "session_token", "refresh_token", "id_token",
    "outlook_refresh_token", "outlook_password",
    "codex_access_token", "codex_refresh_token",
    "password", "2fa", "totp", "totp_secret",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def timestamp_seconds(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = int(value)
        return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # 账号里的无时区时间戳按本机时区写入（datetime.now()），这里同样按本机时区解释。
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def diagnostic(value: object, account: dict[str, Any], limit: int = 500) -> str:
    return sanitize_diagnostic_text(
        value,
        sensitive_values=(
            account.get("access_token"),
            account.get("refresh_token"),
            account.get("id_token"),
            account.get("password"),
            account.get("outlook_password"),
            account.get("outlook_refresh_token"),
        ),
        proxy_values=(account.get("proxy"),),
        limit=limit,
    )


def credential_lifecycle(account: dict[str, Any], *, now_seconds: int | None = None) -> dict[str, Any]:
    access_token = _text(account.get("access_token"))
    availability = project_upstream_credential_availability(
        access_token,
        _text(account.get("refresh_token")),
        access_confirmed_invalid=_text(account.get("last_remote_check_result")).lower() == "invalid",
        refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        now_seconds=now_seconds,
    )
    access_label, access_tone = _ACCESS_TOKEN_PRESENTATION[availability.access_status]
    refresh_label, refresh_tone = _REFRESH_TOKEN_PRESENTATION[availability.refresh_status]
    availability_label, availability_tone = _CREDENTIAL_AVAILABILITY_PRESENTATION[availability.status]
    return {
        "access_token_status": availability.access_status,
        "access_token_label": access_label,
        "access_token_tone": access_tone,
        "access_token_issued_at": access_token_issued_at(access_token) if access_token else None,
        "access_token_expires_at": access_token_expires_at(access_token) if access_token else None,
        "refresh_token_status": availability.refresh_status,
        "refresh_token_label": refresh_label,
        "refresh_token_tone": refresh_tone,
        "can_refresh_access_token": availability.refresh_status == "valid",
        "credential_availability": availability.status,
        "credential_availability_label": availability_label,
        "credential_availability_tone": availability_tone,
        "refresh_token_invalid_at": timestamp_seconds(account.get("refresh_token_invalid_at")),
        "last_token_refresh_at": timestamp_seconds(account.get("last_token_refresh_at")),
        "last_token_refresh_error": diagnostic(account.get("last_token_refresh_error"), account, 2000) or None,
        "last_token_refresh_error_at": timestamp_seconds(account.get("last_token_refresh_error_at")),
    }


def _has_outlook_credentials(account: dict[str, Any], mail: dict[str, Any] | None = None) -> bool:
    try:
        from services.account_relogin_service import has_account_outlook_credentials, resolve_outlook_mailbox

        if has_account_outlook_credentials(account):
            return True
        # 邮箱池里也找得到收信凭据（只看有无，不返回内容）。
        return resolve_outlook_mailbox(account, mail) is not None
    except Exception:  # noqa: BLE001 - 展示字段不能拖垮账号列表
        return False


def rescue_view(
    account: dict[str, Any],
    lifecycle: dict[str, Any],
    *,
    has_outlook: bool | None = None,
    mail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AT 失效后能不能自动救回：RT 续期 → 密码（+2FA）重登 → Outlook 收码重登 → 已放弃 / 无法救回。"""
    from services.account_relogin_service import RELOGIN_MAX_FAILURES

    outlook = _has_outlook_credentials(account, mail) if has_outlook is None else bool(has_outlook)
    has_password = bool(_text(account.get("password")))
    has_totp = bool(_text(account.get("2fa")) or _text(account.get("totp")) or _text(account.get("totp_secret")))
    try:
        failures = max(0, int(account.get("relogin_attempts") or 0))
    except (TypeError, ValueError):
        failures = 0
    if lifecycle["refresh_token_status"] == "valid":
        code, label, tone = "refresh", "RT 续期", "success"
        detail = "AT 到期后用 RT 自动续期"
    elif has_password and failures < RELOGIN_MAX_FAILURES:
        code, label, tone = "password", "密码+2FA 重登" if has_totp else "密码重登", "success"
        detail = (
            "RT 不可用时用已保存的密码" + ("和 2FA 密钥" if has_totp else "")
            + "自动重新登录" + ("，失败再用 Outlook 收码" if outlook else "") + "（需在设置里开启自动重登）"
        )
    elif outlook and failures < RELOGIN_MAX_FAILURES:
        code, label, tone = "outlook", "Outlook 重登", "success"
        detail = "RT 不可用时用 Outlook 收码自动重新登录（需在设置里开启自动重登）"
    elif outlook or has_password:
        code, label, tone = "exhausted", "重登已放弃", "error"
        detail = f"自动重登连续失败 {failures} 次，已停止尝试；可在「登录导入」里重新登录"
    else:
        code, label, tone = "none", "无法救回", "error"
        detail = "没有可用 RT，也没有 Outlook 凭据或密码，AT 失效后只能重新导入"
    if lifecycle["refresh_token_status"] == "missing" and lifecycle.get("access_token_expires_at"):
        expires = datetime.fromtimestamp(int(lifecycle["access_token_expires_at"]), timezone.utc).astimezone()
        detail += f"；AT 到期时间 {expires.strftime('%m-%d %H:%M')}"
    return {
        "rescue_code": code,
        "rescue_label": label,
        "rescue_tone": tone,
        "rescue_detail": detail,
        "has_outlook_credentials": outlook,
        "has_password": has_password,
        "has_totp": has_totp,
        "relogin_failures": failures,
        "relogin_last_error": diagnostic(account.get("last_relogin_error"), account, 300) or None,
    }


def quota_view(account: dict[str, Any], schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    """额度展示：状态/标签、数值来源（远程同步 / 本地估算）、重置时间及其是否只是“查询时刻 + 24h”。"""
    quota_unknown = bool(account.get("image_quota_unknown"))
    try:
        remaining = max(0, int(account.get("quota") or 0))
    except (TypeError, ValueError):
        remaining = 0
    try:
        from services.account_service import AccountService

        unlimited = bool(AccountService._is_unlimited_image_quota_account(account))
    except Exception:  # noqa: BLE001
        unlimited = False
    if unlimited:
        state, label = "unlimited", "无限"
    elif quota_unknown:
        state, label = "unknown", "未知"
    elif remaining <= 0:
        state, label = "exhausted", "0"
    else:
        state, label = "available", str(remaining)
    synced_at = timestamp_seconds(account.get("last_remote_checked_at"))
    debit_at = timestamp_seconds(account.get("last_quota_debit_at"))
    estimated = state in {"available", "exhausted"} and bool(debit_at and (not synced_at or debit_at > synced_at))
    reset_at = None
    for entry in account.get("limits_progress") or []:
        if isinstance(entry, dict) and entry.get("feature_name") == "image_gen":
            reset_at = timestamp_seconds(entry.get("reset_after"))
            break
    reset_at = reset_at or timestamp_seconds(account.get("restore_at"))
    reset_idle = bool(
        state == "available"
        and not estimated
        and synced_at
        and reset_at
        and abs((reset_at - synced_at) - _IDLE_RESET_WINDOW_SECS) <= _IDLE_RESET_TOLERANCE_SECS
    )
    return {
        "quota_remaining": remaining,
        "quota_unknown": quota_unknown,
        "quota_unlimited": unlimited,
        "quota_state": state,
        "quota_label": label,
        "quota_reset_at": reset_at,
        "quota_source": "estimated" if estimated else ("remote" if synced_at else "unknown"),
        "quota_synced_at": synced_at,
        "quota_reset_idle": reset_idle,
    }


# ── 出口 ──────────────────────────────────────────────────────


def egress_context(groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """批量展示用的出口上下文：槽位地址表 + 不可用槽位 + 账号组（只读一次存储）。"""
    context: dict[str, Any] = {"slots": {}, "down": set(), "capacity": 0, "groups": {}, "mail": None}
    try:
        from services.account_relogin_service import _mail_settings

        context["mail"] = _mail_settings()  # 邮箱池只读一次，供各账号判断有无 Outlook 凭据
    except Exception:  # noqa: BLE001
        pass
    for group in groups or []:
        group_id = _text(group.get("id"))
        if group_id:
            context["groups"][group_id] = {
                "name": _text(group.get("name")) or group_id,
                "proxy": _text(group.get("proxy")),
                "enabled": bool(group.get("enabled", True)),
            }
    try:
        from services import egress_slots

        data = egress_slots._load()
        for slot in egress_slots.slot_ports(data):
            context["slots"][egress_slots.slot_proxy_url(slot, data)] = slot
            if egress_slots.slot_down(slot):
                context["down"].add(slot)
        context["capacity"] = int(egress_slots.settings(data)["slot_capacity"])
    except Exception:  # noqa: BLE001 - 出口展示出错不影响账号列表
        pass
    return context


def _target_label(target: str) -> str:
    if target.startswith("slot:"):
        return f"槽位 {target.split(':', 1)[1]}"
    return {"direct": "直连", "default": "默认出口"}.get(target, target)


def egress_view(account: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """账号当前实际出口：账号 → 账号组 → 默认出口 逐级回退；不返回代理地址本身。"""
    from services.proxy_service import normalize_proxy_url

    proxy = _text(account.get("proxy"))
    source, source_label = "account", "账号指定"
    group = context.get("groups", {}).get(_text(account.get("group_id")))
    if not proxy or proxy.lower() == "global":
        if group and group["enabled"] and group["proxy"] and group["proxy"].lower() != "global":
            proxy, source, source_label = group["proxy"], "group", f"账号组「{group['name']}」"
        else:
            proxy, source, source_label = "", "default", "默认出口"
    slot = None
    mode, label, usable = "default", "默认出口", True
    if proxy:
        lowered = proxy.lower()
        try:
            slot = context.get("slots", {}).get(normalize_proxy_url(proxy))
        except Exception:  # noqa: BLE001
            slot = None
        if slot is not None:
            mode, label = "slot", f"槽位 {slot}"
            usable = slot not in context.get("down", set())
        elif lowered == "direct":
            mode, label = "direct", "直连"
        elif lowered.startswith("group:"):
            mode, label = "group", "代理组"
        elif lowered.startswith("profile:"):
            mode, label = "profile", "代理档案"
        else:
            mode, label = "custom", "自定义代理"
    switch_to = _text(account.get("egress_switch_target"))
    switch_at = timestamp_seconds(account.get("egress_switch_at")) if switch_to else None
    return {
        "mode": mode,
        "slot": slot,
        "label": label,
        "source": source,
        "source_label": source_label,
        "usable": usable,
        "switch_to": switch_to or None,
        "switch_to_label": _target_label(switch_to) if switch_to else "",
        "switch_at": switch_at,
    }


# ── 汇总 ──────────────────────────────────────────────────────


def account_display_fields(
    account: dict[str, Any],
    *,
    schedule: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    has_outlook: bool | None = None,
) -> dict[str, Any]:
    """账号页需要的全部派生展示字段（不含任何明文 secret）。"""
    context = context if context is not None else egress_context()
    lifecycle = credential_lifecycle(account)
    group = context.get("groups", {}).get(_text(account.get("group_id")))
    egress = egress_view(account, context)
    return {
        **lifecycle,
        **rescue_view(account, lifecycle, has_outlook=has_outlook, mail=context.get("mail")),
        **quota_view(account, schedule),
        "group_name": group["name"] if group else "",
        "egress": egress,
        "proxy_label": egress["label"],
        "status_raw_error": diagnostic(
            account.get("last_remote_check_error")
            or account.get("last_token_refresh_error")
            or account.get("last_refresh_error"),
            account,
        ),
        "last_remote_check_attempt_at": timestamp_seconds(account.get("last_remote_check_attempt_at")),
        "last_remote_checked_at": timestamp_seconds(account.get("last_remote_checked_at")),
    }


# ── 调度可用性（账号页调度状态汇总 / 筛选） ───────────────────────

SCHEDULE_CODE_LABELS = {
    "ready": "可接请求",
    "busy": "并发已满",
    "cooldown": "冷静期",
    "ramp": "放量期",
    "ramp_full": "今日已满",
    "cooling": "失败冷却",
    "auto_paused": "自动暂停",
    "auth_pending": "待核验",
    "no_credential": "凭据失效",
    "egress_switching": "换出口中",
    "egress_down": "出口不可用",
    "unknown": "未知",
}


def account_availability(schedule: dict[str, Any] | None, egress: dict[str, Any] | None = None) -> dict[str, str]:
    """此刻调度怎么对待这个号：同时命中多种状态时取“最能解释为什么现在不接请求”的一个。

    顺序与前端 accountScheduleCode 保持一致；只读 scheduling_view / egress_view 的投影，不做任何远程请求。
    """
    s = schedule
    if egress and egress.get("switch_to"):
        code = "egress_switching"
    elif not s:
        code = "unknown"
    elif egress and egress.get("usable") is False:
        code = "egress_down"
    elif s.get("credential_status") == "unavailable":
        code = "no_credential"
    elif s.get("auth_pending"):
        code = "auth_pending"
    elif s.get("new_account_stage") == "cooldown":
        code = "cooldown"
    elif s.get("auto_paused_until"):
        code = "auto_paused"
    elif s.get("failure_cooldown_until"):
        code = "cooling"
    elif s.get("new_account_stage") == "ramp" and s.get("ramp_exhausted"):
        code = "ramp_full"
    elif (
        int(s.get("image_inflight") or 0) >= int(s.get("image_concurrency") or 1)
        and int(s.get("text_inflight") or 0) >= int(s.get("text_concurrency") or 1)
    ):
        code = "busy"
    elif s.get("new_account_stage") == "ramp":
        code = "ramp"
    else:
        code = "ready"
    return {"code": code, "label": SCHEDULE_CODE_LABELS[code]}

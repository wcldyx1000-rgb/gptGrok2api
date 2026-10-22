"""GPT image account health and Account Pool capacity warnings.

Ported from yukkcat/chatgpt2api (AGPL-3.0). Ban risk is not observable
directly, so health is inferred from recent usage density, failures and the
stored account state. Recent usage comes from ``ImageUsageTracker`` snapshots
(per process, reset on restart); stored state such as status, quota,
credentials, auto pause and relogin markers survives restarts.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable

_HOUR = 3600.0
_DAY = 86400.0

# Usage density relative to the pool average; the minimum counts keep a quiet
# pool from flagging an account that was simply used a few times.
_DENSITY_WARNING_RATIO = 2.0
_DENSITY_WARNING_MIN_USES = 6
_DENSITY_DANGER_RATIO = 3.0
_DENSITY_DANGER_MIN_USES = 10
_FAILURE_RATE_WARNING = 0.2
_FAILURE_RATE_MIN_SAMPLES = 5
_CONSECUTIVE_FAILURES_WARNING = 2
_CONSECUTIVE_FAILURES_DANGER = 3
_LOW_QUOTA_RATIO = 0.2
_LOW_QUOTA_MIN_TYPICAL = 5
_AT_EXPIRY_WARNING_SECONDS = 24 * 3600

# Pool sizing targets 70% utilization so bursts still find a free account.
_TARGET_UTILIZATION = 0.7
_DEFAULT_HOLD_SECONDS = 60.0
_WAIT_WARNING_SECONDS = 10.0
_QUOTA_HOURS_WARNING = 3.0
_QUOTA_MIN_ATTEMPTS = 10
_BATCH_INVALID_RATIO = 0.1
_BATCH_INVALID_MIN = 2

_IDLE_REASONS: dict[str, str] = {
    "disabled": "账号已停用，不参与调度",
    "limited": "图片额度已用完，等待恢复后重新参与调度",
    "abnormal": "账号已失效，不参与调度",
}

_LEVEL_RANK: dict[str, int] = {"idle": 0, "healthy": 1, "warning": 2, "danger": 3}
_LEVEL_PRESENTATION: dict[str, tuple[str, str]] = {
    "healthy": ("健康", "success"),
    "warning": ("注意", "warning"),
    "danger": ("危险", "error"),
    "idle": ("未参与", "neutral"),
}

_STATUS_CATEGORIES: dict[str, str] = {
    "正常": "normal",
    "限流": "limited",
    "异常": "abnormal",
    "禁用": "disabled",
}


def default_status_category(account: dict[str, Any]) -> str:
    return _STATUS_CATEGORIES.get(str(account.get("status") or "").strip(), "normal")


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _jwt_exp(token: str) -> float | None:
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        exp = float(json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("exp") or 0)
    except Exception:
        return None
    return exp if exp > 0 else None


def _health(level: str, reasons: list[str], usage: dict[str, Any]) -> dict[str, Any]:
    label, tone = _LEVEL_PRESENTATION[level]
    return {
        "health_level": level,
        "health_label": label,
        "health_tone": tone,
        "health_reasons": reasons,
        "recent_uses_1h": int(usage.get("uses_1h") or 0),
        "recent_uses_24h": int(usage.get("uses_24h") or 0),
    }


def _ratio_text(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _account_health(
    account: dict[str, Any],
    usage: dict[str, Any],
    *,
    category: str,
    average_uses_1h: float,
    typical_quota: float | None,
    unlimited: bool,
    now: datetime,
) -> dict[str, Any]:
    danger: list[str] = []
    warning: list[str] = []

    forbidden = int(usage.get("forbidden_24h") or 0)
    if forbidden:
        danger.append(f"24 小时内被上游拒绝访问 {forbidden} 次（403），可能触发了风控")
    rate_limited = int(usage.get("rate_limited_1h") or 0)
    if rate_limited:
        warning.append(f"近 1 小时被上游限速 {rate_limited} 次（429）")

    consecutive = int(usage.get("consecutive_failures") or 0)
    samples = int(usage.get("recent_results") or 0)
    failures = int(usage.get("recent_failures") or 0)
    if consecutive >= _CONSECUTIVE_FAILURES_DANGER:
        danger.append(f"连续失败 {consecutive} 次")
    elif consecutive >= _CONSECUTIVE_FAILURES_WARNING:
        warning.append(f"连续失败 {consecutive} 次")
    elif samples >= _FAILURE_RATE_MIN_SAMPLES and failures / samples >= _FAILURE_RATE_WARNING:
        warning.append(f"最近 {samples} 次请求失败 {failures} 次")

    uses = int(usage.get("uses_1h") or 0)
    if average_uses_1h > 0 and uses > 0:
        ratio = uses / average_uses_1h
        text = f"近 1 小时使用 {uses} 次，是号池平均的 {_ratio_text(ratio)} 倍"
        if ratio >= _DENSITY_DANGER_RATIO and uses >= _DENSITY_DANGER_MIN_USES:
            danger.append(text)
        elif ratio >= _DENSITY_WARNING_RATIO and uses >= _DENSITY_WARNING_MIN_USES:
            warning.append(text)

    if (
        category == "normal"
        and not unlimited
        and not account.get("image_quota_unknown")
        and typical_quota is not None
        and typical_quota >= _LOW_QUOTA_MIN_TYPICAL
    ):
        quota = max(0, int(account.get("quota") or 0))
        if 0 < quota <= typical_quota * _LOW_QUOTA_RATIO:
            warning.append(f"剩余额度 {quota}，明显低于号池常见的 {int(typical_quota)}")

    relogin_needed = bool(account.get("relogin_needed_at"))
    if category == "normal":
        expires_at = _jwt_exp(str(account.get("access_token") or ""))
        refresh_usable = bool(str(account.get("refresh_token") or "").strip()) and not relogin_needed
        if (
            not refresh_usable
            and expires_at is not None
            and expires_at - now.timestamp() <= _AT_EXPIRY_WARNING_SECONDS
        ):
            warning.append("AT 将在 24 小时内过期，且没有可用 RT")

    paused_until = _parse_time(account.get("auto_paused_until"))
    if paused_until is not None and paused_until > now:
        reason = str(account.get("auto_pause_reason") or "").strip()
        local_until = paused_until.astimezone().strftime("%m-%d %H:%M")
        warning.append(f"已自动暂停至 {local_until}" + (f"：{reason}" if reason else ""))
    if relogin_needed:
        attempts = int(account.get("relogin_attempts") or 0)
        warning.append(f"刷新凭据已失效，等待自动重新登录（已尝试 {attempts} 次）")

    # Risk levels describe accounts in rotation. Accounts out of rotation are
    # already flagged by their status; they keep any recent signals as reasons
    # but do not count toward pool warnings.
    idle_reason = _IDLE_REASONS.get(category)
    if idle_reason:
        return _health("idle", [idle_reason, *danger, *warning], usage)
    if danger:
        return _health("danger", danger + warning, usage)
    if warning:
        return _health("warning", warning, usage)
    return _health("healthy", [], usage)


def _pool_warning(code: str, tone: str, message: str) -> dict[str, str]:
    return {"code": code, "tone": tone, "message": message}


def evaluate_account_health(
    accounts: list[dict[str, Any]],
    usage: dict[str, Any],
    *,
    cooldown_seconds: float,
    concurrency: int,
    is_unlimited: Callable[[dict[str, Any]], bool],
    status_category: Callable[[dict[str, Any]], str] = default_status_category,
    now: datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Project per-account health keyed by access token, plus the pool summary.

    ``usage`` is the account service's runtime snapshot: ``accounts`` maps
    access tokens to tracker stats and ``pool`` holds the pool stats.
    """
    now = now or datetime.now(timezone.utc)
    usage_by_token: dict[str, dict[str, Any]] = usage.get("accounts") or {}
    pool_usage: dict[str, Any] = usage.get("pool") or {}

    categories: dict[str, str] = {}
    for account in accounts:
        token = str(account.get("access_token") or "")
        categories[token] = status_category(account)
    ready = [
        account
        for account in accounts
        if categories[str(account.get("access_token") or "")] == "normal"
    ]
    known_quotas = [
        float(account.get("quota") or 0)
        for account in ready
        if not account.get("image_quota_unknown")
        and not is_unlimited(account)
        and int(account.get("quota") or 0) > 0
    ]
    typical_quota = float(median(known_quotas)) if known_quotas else None
    ready_uses = [
        int((usage_by_token.get(str(account.get("access_token") or "")) or {}).get("uses_1h") or 0)
        for account in ready
    ]
    average_uses_1h = sum(ready_uses) / len(ready_uses) if ready_uses else 0.0

    by_token: dict[str, dict[str, Any]] = {}
    for account in accounts:
        token = str(account.get("access_token") or "")
        by_token[token] = _account_health(
            account,
            usage_by_token.get(token) or {},
            category=categories[token],
            average_uses_1h=average_uses_1h,
            typical_quota=typical_quota,
            unlimited=is_unlimited(account),
            now=now,
        )

    pool = _pool_health(
        accounts,
        ready,
        by_token,
        pool_usage,
        categories=categories,
        typical_quota=typical_quota,
        cooldown_seconds=max(0.0, float(cooldown_seconds or 0)),
        concurrency=max(1, int(concurrency or 1)),
        is_unlimited=is_unlimited,
        now=now,
    )
    return by_token, pool


def _pool_health(
    accounts: list[dict[str, Any]],
    ready: list[dict[str, Any]],
    by_token: dict[str, dict[str, Any]],
    pool_usage: dict[str, Any],
    *,
    categories: dict[str, str],
    typical_quota: float | None,
    cooldown_seconds: float,
    concurrency: int,
    is_unlimited: Callable[[dict[str, Any]], bool],
    now: datetime,
) -> dict[str, Any]:
    danger_accounts = sum(1 for item in by_token.values() if item["health_level"] == "danger")
    warning_accounts = sum(1 for item in by_token.values() if item["health_level"] == "warning")
    attempts = int(pool_usage.get("attempts_1h") or 0)
    timeouts = int(pool_usage.get("timeouts_1h") or 0)
    unavailable = int(pool_usage.get("unavailable_1h") or 0)
    waits = int(pool_usage.get("waits_1h") or 0)
    avg_wait = float(pool_usage.get("avg_wait_seconds") or 0.0)
    hold = float(pool_usage.get("avg_hold_seconds") or _DEFAULT_HOLD_SECONDS)
    hold = max(hold, 1.0)

    # With a cooldown each account serves one request per hold + cooldown;
    # without it the concurrency cap bounds the account instead.
    per_account_hourly = (
        _HOUR / (hold + cooldown_seconds) if cooldown_seconds > 0 else _HOUR * concurrency / hold
    )
    demand = attempts + timeouts + unavailable
    capacity = per_account_hourly * len(ready)
    utilization = demand / capacity if capacity > 0 else None
    needed = math.ceil(demand / (per_account_hourly * _TARGET_UTILIZATION)) if demand else 0
    suggested = max(0, needed - len(ready))

    warnings: list[dict[str, str]] = []
    add_hint = f"，建议增加约 {suggested} 个账号" if suggested else ""
    if accounts and not ready:
        warnings.append(_pool_warning("no_ready_account", "error", "当前没有可用账号，图片请求会全部失败"))
    if timeouts:
        warnings.append(_pool_warning(
            "selection_timeout",
            "error",
            f"近 1 小时有 {timeouts} 个请求因等不到空闲账号而超时{add_hint}",
        ))
    if unavailable and ready:
        warnings.append(_pool_warning(
            "selection_unavailable",
            "error",
            f"近 1 小时有 {unavailable} 个请求没有匹配的可用账号",
        ))
    if waits and avg_wait >= _WAIT_WARNING_SECONDS:
        warnings.append(_pool_warning(
            "selection_wait",
            "warning",
            f"近 1 小时有 {waits} 个请求排队等账号，平均等待 {round(avg_wait)} 秒{add_hint}",
        ))
    if utilization is not None and utilization >= _TARGET_UTILIZATION and not timeouts:
        warnings.append(_pool_warning(
            "high_utilization",
            "warning",
            f"近 1 小时用量已达号池承载能力的 {round(utilization * 100)}%{add_hint}",
        ))

    if attempts >= _QUOTA_MIN_ATTEMPTS and ready and not any(is_unlimited(item) for item in ready):
        remaining = 0.0
        for account in ready:
            if account.get("image_quota_unknown"):
                remaining += typical_quota or 0.0
            else:
                remaining += max(0, int(account.get("quota") or 0))
        hours_left = remaining / attempts
        if hours_left < _QUOTA_HOURS_WARNING:
            warnings.append(_pool_warning(
                "quota_running_out",
                "warning",
                f"按近 1 小时的用量，剩余图片额度约 {_ratio_text(hours_left)} 小时后用完",
            ))

    invalidated = 0
    for account in accounts:
        if categories[str(account.get("access_token") or "")] != "abnormal":
            continue
        invalid_at = _parse_time(account.get("last_invalid_at"))
        if invalid_at is not None and (now - invalid_at).total_seconds() <= _DAY:
            invalidated += 1
    if invalidated >= max(_BATCH_INVALID_MIN, math.ceil(len(accounts) * _BATCH_INVALID_RATIO)):
        warnings.append(_pool_warning(
            "batch_invalidated",
            "error",
            f"24 小时内有 {invalidated} 个账号失效，可能是批量封禁或代理异常，建议检查",
        ))

    if danger_accounts:
        warnings.append(_pool_warning(
            "danger_accounts",
            "error",
            f"{danger_accounts} 个账号处于危险状态",
        ))
    elif warning_accounts:
        warnings.append(_pool_warning(
            "warning_accounts",
            "warning",
            f"{warning_accounts} 个账号需要注意",
        ))

    level = "healthy"
    for warning in warnings:
        candidate = "danger" if warning["tone"] == "error" else "warning"
        if _LEVEL_RANK[candidate] > _LEVEL_RANK[level]:
            level = candidate
    label, tone = _LEVEL_PRESENTATION[level]
    return {
        "level": level,
        "label": label,
        "tone": tone,
        "warnings": warnings,
        "suggested_additional_accounts": suggested,
        "metrics": {
            "total_accounts": len(accounts),
            "ready_accounts": len(ready),
            "danger_accounts": danger_accounts,
            "warning_accounts": warning_accounts,
            "demand_1h": demand,
            "capacity_1h": round(capacity),
            "utilization": round(utilization, 3) if utilization is not None else None,
            "waits_1h": waits,
            "avg_wait_seconds": round(avg_wait, 1),
            "timeouts_1h": timeouts,
            "avg_hold_seconds": round(hold, 1),
            "observed_seconds": round(float(pool_usage.get("observed_seconds") or 0.0)),
        },
    }


def health_matches_filter(level: str, health_filter: str) -> bool:
    health_filter = (health_filter or "").strip().lower()
    if not health_filter or health_filter == "all":
        return True
    if health_filter == "risk":
        return level in {"warning", "danger"}
    return level == health_filter


def account_health_report(
    accounts: list[dict[str, Any]] | None = None,
    *,
    status_category: Callable[[dict[str, Any]], str] = default_status_category,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Evaluate the live GPT Account Pool with the current scheduling settings."""
    from services.account_service import account_service
    from services.config import config

    return evaluate_account_health(
        account_service.list_accounts() if accounts is None else accounts,
        account_service.image_usage_snapshot(),
        cooldown_seconds=float(config.image_account_cooldown_secs or 0),
        concurrency=int(config.image_account_concurrency or 1),
        is_unlimited=account_service.is_unlimited_image_quota_account,
        status_category=status_category,
    )

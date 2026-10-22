from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from services.auth_service import auth_service

from api.support import (
    require_admin,
    sanitize_cpa_pool,
    sanitize_cpa_pools,
    sanitize_sub2api_server,
    sanitize_sub2api_servers,
)
from services import account_view
from services.account_health import account_health_report, health_matches_filter
from services.account_service import account_service
from services.account_test_service import account_test_service
from services.checkout_url import is_final_checkout_url
from services.config import config
from services.codex_oauth_service import codex_oauth_service
from services.cpa_service import cpa_config, cpa_import_service, list_remote_files
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.login_import_service import login_import_service, verify_and_import
from services.oauth_login_service import OAuthLoginError, oauth_login_service
from services.remote_import_job_status import (
    RemoteImportConflictError,
    find_active_remote_import,
    import_job_is_active,
)
from services.sub2api_service import (
    build_openai_oauth_export_account,
    ensure_openai_agent_identity,
    list_remote_accounts as sub2api_list_remote_accounts,
    list_remote_groups as sub2api_list_remote_groups,
    parse_sub2api_export,
    sub2api_config,
    sub2api_import_service,
)
from services.openai_agent_identity_store import openai_agent_identity_store
from utils.diagnostics import sanitize_diagnostic_text
from utils.helper import anonymize_token



class UserKeyCreateRequest(BaseModel):
    name: str = ""


class UserKeyUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    key: str | None = None


BACKUP_FORMAT = "gptgrok2api-accounts-backup"


class AccountSelectionScope(BaseModel):
    """账号选择范围：explicit 按 ID 列表；filter 为当前筛选结果（可跨分页）；all 为全部账号。"""

    mode: Literal["explicit", "filter", "all"] = "explicit"
    account_ids: list[str] = Field(default_factory=list)
    excluded_account_ids: list[str] = Field(default_factory=list)
    keyword: str = ""
    status: str = "all"
    group_id: str = "all"
    health: str = "all"
    # “更多筛选”：rt_missing / no_rescue / high_failure / idle / auto_paused / egress_* / source:x / plan:x
    flag: str = "all"


class AccountSelectionPreviewRequest(BaseModel):
    selection: AccountSelectionScope


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    refresh: bool = True
    return_items: bool = True
    # 入池前验证 refresh_token 与额度，不可用的账号不留在号池。
    verify_on_import: bool = False
    # 导入完整备份：保留备份里的管理 ID 与账号全部字段。
    restore: bool = False
    # None 不改动分组；"" 归入未分组；其它值必须是已存在的账号组。
    target_group_id: str | None = None


class Sub2APIFileImportRequest(BaseModel):
    """导入本地 Sub2API 导出文件：content 为文件的 JSON 文本。"""

    content: str = Field(min_length=2, max_length=20_000_000)
    refresh: bool = True
    return_items: bool = False
    target_group_id: str | None = None


class LoginImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)
    verify: bool = True


class LoginImportStopRequest(BaseModel):
    job_id: str = ""


class AccountReactivateRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)


class AccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    selection: AccountSelectionScope | None = None


class AccountImportCleanupRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    remove: bool = False


class AccountOperationRequest(BaseModel):
    """按账号操作的通用请求：account_ids / selection 优先，access_tokens 为旧接口兼容。"""

    access_tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    selection: AccountSelectionScope | None = None


class AccountEgressSwitchRequest(AccountOperationRequest):
    target: str = ""


AccountRefreshRequest = AccountOperationRequest
AccountClearAutoPauseRequest = AccountOperationRequest


class AccountTestRequest(BaseModel):
    id: str = ""
    access_token: str = ""
    # probe 只做远程信息探测（不消耗额度）；chat / image 发起真实对话或画图。
    mode: Literal["probe", "chat", "image"] = "probe"
    model: str = Field(default="", max_length=120)
    prompt: str = Field(default="", max_length=8000)


class AccountExportRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    selection: AccountSelectionScope | None = None
    format: Literal["json", "zip", "cpa", "sub2api", "agent_identity", "txt", "backup"] = "json"


# 账号状态只允许这四个值；任意字符串会写进账号记录并让调度/过滤失灵。
AccountBackendStatus = Literal["正常", "限流", "异常", "禁用"]


class AccountUpdateRequest(BaseModel):
    # 传 id 时：用稳定管理 ID 定位账号，access_token / refresh_token 表示新凭据（空串不覆盖）。
    # 不传 id 时（旧接口）：access_token 是定位账号的当前 token。
    id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    type: str | None = None
    source_type: str | None = None
    status: AccountBackendStatus | None = None
    quota: int | None = None
    # 账号页编辑表单会随更新一并提交 proxy，所以这里保留；出口切换另有 /api/accounts/egress/switch。
    proxy: str | None = None
    group_id: str | None = None


class AccountBatchUpdateRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    selection: AccountSelectionScope | None = None
    status: AccountBackendStatus | None = None


class AccountGroupBindRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    selection: AccountSelectionScope | None = None
    group_id: str = ""


class AccountGroupRequest(BaseModel):
    id: str = ""
    name: str = ""
    proxy: str = ""
    proxy_group_id: str = ""
    enabled: bool = True
    notes: str = ""
    create_only: bool = False


class CPAPoolCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    secret_key: str = ""


class CPAPoolUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    secret_key: str | None = None


class CPAImportRequest(BaseModel):
    names: list[str] = Field(default_factory=list)
    target_group_id: str | None = None


class Sub2APIServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""
    api_key: str = ""
    group_id: str = ""
    verify_tls: bool = True


class Sub2APIServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    api_key: str | None = None
    group_id: str | None = None
    verify_tls: bool | None = None


class Sub2APIImportGroupBinding(BaseModel):
    remote_group_id: str = ""
    name: str = ""
    account_ids: list[str] = Field(default_factory=list)


class Sub2APIImportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    group_bindings: list[Sub2APIImportGroupBinding] = Field(default_factory=list)
    create_account_groups: bool = True
    target_group_id: str | None = None


class OAuthLoginStartRequest(BaseModel):
    """起始 OAuth 桥。email_hint 可选，仅用于让 OpenAI 登录页预填邮箱。"""
    email_hint: str = ""


class OAuthLoginFinishRequest(BaseModel):
    """提交 callback。callback 既可以是完整 URL 也可以只填 code。"""
    session_id: str = ""
    callback: str = ""
    target_group_id: str | None = None
    # Codex 登录绑定时指定目标账号（稳定管理 ID）；OAuth 导入忽略该字段。
    account_id: str = ""


def _account_payload_token(item: dict[str, Any]) -> str:
    return str(item.get("access_token") or item.get("accessToken") or "").strip()


def _unique_tokens(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))


def _download_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_export_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (clean or fallback)[:80]


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _slug_id(value: object) -> str:
    raw = _clean_text(value).lower()
    chars: list[str] = []
    for char in raw:
        if char.isalnum() or char in {"-", "_"}:
            chars.append(char)
        elif char.isspace():
            chars.append("-")
    return "".join(chars).strip("-_")


def _config_dict_list(key: str) -> list[dict[str, Any]]:
    raw = config.get_internal().get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _account_group_id(value: object) -> str:
    return _slug_id(value)


def _account_group_proxy_reference(proxy: object, proxy_group_id: object = "") -> str:
    raw = _clean_text(proxy)
    if raw.lower() == "global":
        return ""
    if raw:
        return raw
    legacy_group_id = _clean_text(proxy_group_id)
    return f"group:{legacy_group_id}" if legacy_group_id else ""


def _proxy_group_id_from_reference(proxy: object) -> str:
    raw = _clean_text(proxy)
    if raw.lower().startswith("group:"):
        return _clean_text(raw.split(":", 1)[1])
    return ""


def _account_group_payload(groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    accounts = account_service.list_accounts()
    counts: dict[str, int] = {}
    for account in accounts:
        group_id = _clean_text(account.get("group_id"))
        if group_id:
            counts[group_id] = counts.get(group_id, 0) + 1
    normalized_groups = []
    for group in groups if groups is not None else _config_dict_list("account_groups"):
        group_id = _account_group_id(group.get("id"))
        if not group_id:
            continue
        proxy = _account_group_proxy_reference(group.get("proxy"), group.get("proxy_group_id"))
        normalized_groups.append(
            {
                "id": group_id,
                "name": _clean_text(group.get("name")) or group_id,
                "proxy": proxy,
                "proxy_group_id": _proxy_group_id_from_reference(proxy),
                "enabled": bool(group.get("enabled", True)),
                "notes": _clean_text(group.get("notes")),
                "account_count": counts.get(group_id, 0),
            }
        )
    return config.mask_public({
        "groups": normalized_groups,
        "proxy_groups": _config_dict_list("proxy_groups"),
    })


def _upsert_account_group(body: AccountGroupRequest) -> dict[str, Any]:
    group_id = _account_group_id(body.id or body.name)
    if not group_id:
        raise ValueError("account group id is required")
    groups = _config_dict_list("account_groups")
    exists = any(_account_group_id(group.get("id")) == group_id for group in groups)
    if body.create_only and exists:
        raise ValueError("account group already exists")
    proxy = _account_group_proxy_reference(body.proxy, body.proxy_group_id)
    item = {
        "id": group_id,
        "name": body.name.strip() or group_id,
        "proxy": proxy,
        "proxy_group_id": _proxy_group_id_from_reference(proxy),
        "enabled": body.enabled,
        "notes": body.notes.strip(),
    }
    next_groups = [group for group in groups if _account_group_id(group.get("id")) != group_id]
    next_groups.append(item)
    updated = config.update({"account_groups": next_groups})
    return {"group": config.mask_public(item), **_account_group_payload(updated.get("account_groups", []))}


def _truthy_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = _clean_text(value).lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "none", "null", ""}:
        return False
    return default


def _int_value(value: object) -> int:
    try:
        return int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _account_status_category(account: dict[str, Any]) -> Literal["normal", "limited", "abnormal", "disabled"]:
    status = _clean_text(account.get("status"))
    status_key = status.lower()
    reason_code = _clean_text(account.get("status_reason_code")).lower()
    error_kind = _clean_text(account.get("last_error_kind")).lower()

    if (
        status in {"禁用"}
        or status_key in {"disabled", "auto_disabled"}
        or reason_code == "disabled"
        or _truthy_value(account.get("auto_disabled"))
        or (account.get("enabled") is not None and not _truthy_value(account.get("enabled"), True))
    ):
        return "disabled"

    if status in {"限流"} or status_key in {"limited", "rate_limited", "cooling", "backoff"}:
        return "limited"
    if status in {"异常"} or status_key in {"abnormal", "invalid", "error", "incomplete"}:
        return "abnormal"

    limited_reason_codes = {
        "pro_cooldown",
        "video_cooldown",
        "lane_backoff",
        "lane_degraded",
        "image_generation_unavailable",
        "image_degraded_to_fast",
        "image_quota_exhausted",
        "text_pending",
    }
    limited_error_kinds = {
        "quota_exhausted",
        "media_pending",
        "media_generation_unavailable",
        "media_degraded",
        "lane_degraded",
        "text_pending",
    }
    if reason_code in limited_reason_codes or error_kind in limited_error_kinds:
        return "limited"

    abnormal_reason_codes = {
        "snlm0e_refresh_failed",
        "account_invalid",
        "parse_failure",
        "upstream_error",
    }
    abnormal_error_kinds = {"auth_invalid", "parse_failure", "upstream_error"}
    if (
        reason_code in abnormal_reason_codes
        or error_kind in abnormal_error_kinds
        or _int_value(account.get("invalid_count")) > 0
        or _clean_text(account.get("last_refresh_error"))
        or _clean_text(account.get("last_token_refresh_error"))
    ):
        return "abnormal"

    lane_backoff_summary = account.get("lane_backoff_summary")
    if isinstance(lane_backoff_summary, dict) and _truthy_value(lane_backoff_summary.get("active")):
        return "limited"

    return "normal"


def _account_status_label(
        category: Literal["normal", "limited", "abnormal", "disabled"],
) -> str:
    return {
        "normal": "正常",
        "limited": "限流",
        "abnormal": "异常",
        "disabled": "禁用",
    }[category]


def _account_status_payload(account: dict[str, Any]) -> dict[str, str]:
    category = _account_status_category(account)
    return {
        "status_category": category,
        "status_label": _account_status_label(category),
    }


def _account_display_context() -> dict[str, Any]:
    try:
        groups = _config_dict_list("account_groups")
    except Exception:  # noqa: BLE001 - 展示用，出错不影响账号列表
        groups = []
    return account_view.egress_context(groups)


def _account_for_api(account: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    # Browser session material is retained only for server-side checkout and
    # future explicit payment operations.  Listing accounts must not broadcast
    # it to the admin SPA or SSE consumers.  Credential / rescue / quota / egress
    # display fields come from services.account_view (booleans and timestamps only).
    item = dict(account)
    try:
        scheduling = account_service.scheduling_view(item)
    except Exception:  # noqa: BLE001 - 调度视图只是展示，出错不能影响账号列表
        scheduling = None
    try:
        display = account_view.account_display_fields(
            item,
            schedule=scheduling,
            context=context if context is not None else _account_display_context(),
        )
    except Exception:  # noqa: BLE001 - 同上
        display = {}
    has_refresh_token = bool(_clean_text(item.get("refresh_token")))
    codex_bound = bool(_clean_text(item.get("codex_access_token")))
    for key in account_view.SECRET_ACCOUNT_FIELDS:
        item.pop(key, None)
    management_id = _clean_text(item.pop("management_id", "")).lower()
    if management_id:
        item["id"] = management_id
        item["has_refresh_token"] = has_refresh_token
        item["codex_bound"] = codex_bound
    return {
        **item,
        **display,
        **_account_status_payload(item),
        **({"scheduling": scheduling} if scheduling is not None else {}),
        "availability": account_view.account_availability(scheduling, display.get("egress")),
    }


def _accounts_for_api(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    context = _account_display_context()
    return [_account_for_api(item, context) for item in items if isinstance(item, dict)]


def _checkout_final_url(account: dict[str, Any]) -> str:
    final_url = _clean_text(account.get("checkout_final_url"))
    if final_url:
        return final_url
    if _clean_text(account.get("checkout_final_kind")):
        return _clean_text(account.get("checkout_url"))
    return ""


def _checkout_expiry_epoch(account: dict[str, Any]) -> float | None:
    for key in ("checkout_qr_expires_at_unix", "checkout_qr_expires_at"):
        value = account.get(key)
        if value in (None, ""):
            continue
        try:
            epoch = float(value)
        except (TypeError, ValueError):
            raw = _clean_text(value)
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            epoch = parsed.timestamp()
        if epoch > 1_000_000_000_000:
            epoch /= 1000
        if epoch > 0:
            return epoch
    return None


def _has_unexpired_checkout_link(account: dict[str, Any], *, now: float | None = None) -> bool:
    if _clean_text(account.get("checkout_link_status")).lower() != "ready":
        return False
    final_url = _checkout_final_url(account)
    if not is_final_checkout_url(final_url, _clean_text(account.get("checkout_channel"))):
        return False
    expires_at = _checkout_expiry_epoch(account)
    return expires_at is not None and expires_at > (time.time() if now is None else now)


def _availability_code(
        account: dict[str, Any],
        context: dict[str, Any] | None,
        cache: dict[int, str] | None = None,
) -> str:
    """调度状态 code：只读本地 scheduling_view / egress_view，不做远程请求；cache 让一次遍历内每个账号只算一次。"""
    key = id(account)
    if cache is not None and key in cache:
        return cache[key]
    try:
        scheduling = account_service.scheduling_view(account)
    except Exception:  # noqa: BLE001 - 调度视图只是展示，出错不能影响账号列表
        scheduling = None
    try:
        egress = account_view.egress_view(account, context if context is not None else _account_display_context())
    except Exception:  # noqa: BLE001
        egress = None
    code = account_view.account_availability(scheduling, egress)["code"]
    if cache is not None:
        cache[key] = code
    return code


def _status_matches_filter(
        account: dict[str, Any],
        status_filter: str,
        context: dict[str, Any] | None = None,
        cache: dict[int, str] | None = None,
) -> bool:
    status_filter = status_filter.strip().lower()
    if not status_filter or status_filter == "all":
        return True
    if status_filter.startswith("sched:"):
        # 按调度状态筛：冷静期 / 放量 / 今日已满 / 失败冷却 / 自动暂停 / 待核验 / 并发已满 / 出口切换中 …
        wanted = status_filter.split(":", 1)[1]
        code = _availability_code(account, context, cache)
        return code == wanted or (wanted == "ramp" and code == "ramp_full")
    if status_filter == "valid_checkout":
        return _has_unexpired_checkout_link(account)
    if status_filter in {"normal", "limited", "abnormal", "disabled"}:
        return _account_status_category(account) == status_filter
    status = _clean_text(account.get("status"))
    status_map = {
        "normal": "\u6b63\u5e38",
        "limited": "\u9650\u6d41",
        "abnormal": "\u5f02\u5e38",
        "disabled": "\u7981\u7528",
    }
    expected = status_map.get(status_filter)
    return status == expected if expected else status.lower() == status_filter


def _account_matches_keyword(account: dict[str, Any], keyword: str) -> bool:
    needle = keyword.strip().lower()
    if not needle:
        return True
    fields = (
        account.get("access_token"),
        account.get("management_id"),
        account.get("email"),
        account.get("user_id"),
        account.get("type"),
        account.get("source_type"),
        account.get("status"),
        account.get("proxy"),
        account.get("group_id"),
    )
    return any(needle in _clean_text(value).lower() for value in fields)


def _account_matches_group(account: dict[str, Any], group_id: str) -> bool:
    group_id = group_id.strip()
    if not group_id or group_id == "all":
        return True
    current = _clean_text(account.get("group_id"))
    if group_id == "__ungrouped__":
        return not current
    return current == group_id


HIGH_FAILURE_MIN_CALLS = 5
HIGH_FAILURE_RATE = 0.4
IDLE_ACCOUNT_SECS = 24 * 3600
_EGRESS_FLAGS = {"egress_direct", "egress_switching", "egress_down"}


def _account_matches_flag(account: dict[str, Any], flag: str, context: dict[str, Any] | None = None) -> bool:
    """“更多筛选”：来源 / 套餐 / 凭据风险 / 表现 / 出口。值形如 source:web、plan:free、rt_missing。"""
    flag = flag.strip().lower()
    if not flag or flag == "all":
        return True
    kind, _, value = flag.partition(":")
    if kind == "source":
        return account_service._normalize_source_type(account.get("source_type")) == value
    if kind == "plan":
        plan = _clean_text(account.get("type")).lower().replace("-", "").replace("_", "")
        return (plan or "unknown") == value
    rt_ok = bool(_clean_text(account.get("refresh_token"))) and not account.get("refresh_token_invalid_at")
    if flag == "rt_missing":
        return not rt_ok
    if flag == "no_rescue":
        # RT 不可用，又没有 Outlook 收码凭据：AT 失效后救不回来
        mail = (context or {}).get("mail")
        return not rt_ok and not account_view._has_outlook_credentials(account, mail)
    if flag == "high_failure":
        success = max(0, _int_value(account.get("success")))
        failure = max(0, _int_value(account.get("fail")))
        total = success + failure
        return total >= HIGH_FAILURE_MIN_CALLS and failure / total >= HIGH_FAILURE_RATE
    if flag == "idle":
        last_used = account_view.timestamp_seconds(account.get("last_used_at"))
        return not last_used or time.time() - last_used >= IDLE_ACCOUNT_SECS
    if flag == "auto_paused":
        return account_service._auto_pause_active(account)
    if flag in _EGRESS_FLAGS:
        try:
            egress = account_view.egress_view(account, context if context is not None else _account_display_context())
        except Exception:  # noqa: BLE001
            return False
        if flag == "egress_switching":
            return bool(egress.get("switch_to"))
        if egress.get("switch_to"):
            return False
        if flag == "egress_direct":
            return egress.get("mode") != "slot"
        return egress.get("mode") == "slot" and not egress.get("usable", True)
    return True


def _needs_display_context(status: str, flag: str) -> bool:
    status = status.strip().lower()
    flag = flag.strip().lower()
    return status.startswith("sched:") or flag == "no_rescue" or flag in _EGRESS_FLAGS


def _filtered_accounts(
        items: list[dict[str, Any]],
        *,
        keyword: str,
        status: str,
        group_id: str,
        health: str,
        health_by_token: dict[str, dict[str, Any]],
        flag: str = "all",
        context: dict[str, Any] | None = None,
        availability_cache: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    if context is None and _needs_display_context(status, flag):
        context = _account_display_context()
    return [
        item for item in items
        if _account_matches_keyword(item, keyword)
        and _account_matches_group(item, group_id)
        and health_matches_filter(
            str((health_by_token.get(str(item.get("access_token") or "")) or {}).get("health_level") or ""),
            health,
        )
        and _account_matches_flag(item, flag, context)
        and _status_matches_filter(item, status, context, availability_cache)
    ]


def _accounts_page(
        *,
        page: int,
        page_size: int,
        keyword: str,
        status: str,
        group_id: str,
        health: str = "all",
        flag: str = "all",
) -> dict[str, Any]:
    items = account_service.list_accounts()
    health_by_token, _pool = _health_report(items)
    context = _account_display_context()
    cache: dict[int, str] = {}
    # 调度状态汇总：除“状态”外的其它条件（关键字 / 账号组 / 健康度 / 更多筛选）都生效，
    # 这样点任一芯片后的结果数 == 芯片上的数字；一次遍历，调度视图只读本地内存。
    scoped = _filtered_accounts(
        items,
        keyword=keyword,
        status="all",
        group_id=group_id,
        health=health,
        health_by_token=health_by_token,
        flag=flag,
        context=context,
    )
    availability_summary: dict[str, int] = {}
    for item in scoped:
        code = _availability_code(item, context, cache)
        availability_summary[code] = availability_summary.get(code, 0) + 1
    filtered = [item for item in scoped if _status_matches_filter(item, status, context, cache)]
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, 500))
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "items": [
            {**_account_for_api(item, context), **(health_by_token.get(str(item.get("access_token") or "")) or {})}
            for item in filtered[start:end]
        ],
        "total": len(filtered),
        "all_total": len(items),
        "availability_summary": availability_summary,
        "page": safe_page,
        "page_size": safe_page_size,
    }


# ---------- 稳定管理 ID 与账号选择 ----------

def _clean_ids(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(_clean_text(item).lower() for item in values if _clean_text(item)))


def _account_management_id(account: dict[str, Any] | None) -> str:
    return _clean_text((account or {}).get("management_id")).lower()


def _account_targets(
        account_ids: list[str] | None,
        legacy_tokens: list[str] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """把 ID 与旧版 access_token 统一解析成 [(当前 access_token, 管理 ID)]，并返回找不到的 ID。

    旧版 token 即使已不存在也原样保留（管理 ID 为空），让各接口沿用原有的“未找到”处理。
    """
    tokens, missing_ids = account_service.resolve_account_ids(_clean_ids(account_ids))
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        account = account_service.get_account(token)
        if account is None:
            continue
        current = _clean_text(account.get("access_token")) or token
        if current in seen:
            continue
        seen.add(current)
        targets.append((current, _account_management_id(account)))
    for token in _unique_tokens(legacy_tokens or []):
        account = account_service.get_account(token)
        current = _clean_text((account or {}).get("access_token")) or token
        if current in seen:
            continue
        seen.add(current)
        targets.append((current, _account_management_id(account)))
    return targets, missing_ids


def _account_selection_members(scope: AccountSelectionScope) -> list[tuple[str, str]]:
    items = account_service.list_accounts()
    if scope.mode == "filter":
        health_by_token: dict[str, dict[str, Any]] = {}
        if _clean_text(scope.health).lower() not in {"", "all"}:
            health_by_token, _pool = _health_report(items)
        items = _filtered_accounts(
            items,
            keyword=scope.keyword,
            status=scope.status,
            group_id=scope.group_id,
            health=scope.health,
            health_by_token=health_by_token,
            flag=scope.flag,
        )
    members: list[tuple[str, str]] = []
    seen: set[str] = set()
    for account in items:
        account_id = _account_management_id(account)
        token = _clean_text(account.get("access_token"))
        if not account_id or not token or account_id in seen:
            continue
        seen.add(account_id)
        members.append((token, account_id))
    return members


def _account_selection_targets(
        selection: AccountSelectionScope | None,
        account_ids: list[str] | None = None,
        legacy_tokens: list[str] | None = None,
        *,
        default_all: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """解析一次批量操作的目标：[(access_token, 管理 ID)] 与找不到的 ID。"""
    if selection is None or selection.mode == "explicit":
        explicit_ids = [*(selection.account_ids if selection is not None else []), *(account_ids or [])]
        if default_all and not explicit_ids and not _unique_tokens(legacy_tokens or []):
            return _account_selection_members(AccountSelectionScope(mode="all")), []
        return _account_targets(explicit_ids, legacy_tokens)
    excluded = set(_clean_ids(selection.excluded_account_ids))
    members = [
        (token, account_id)
        for token, account_id in _account_selection_members(selection)
        if account_id not in excluded
    ]
    return members, []


# 后台账号操作任务的强引用：事件循环只持有任务的弱引用，长任务可能在执行中被回收。
_ACCOUNT_OPERATION_TASKS: set[asyncio.Task] = set()


def _schedule_account_operation(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _ACCOUNT_OPERATION_TASKS.add(task)
    task.add_done_callback(_ACCOUNT_OPERATION_TASKS.discard)
    return task


def _ensure_remote_import_idle(entry: dict | None, label: str) -> None:
    """该连接上有进行中的导入任务时不允许改/删配置（导入线程还在用它的凭据）。不存在的连接交给调用方返回 404。"""
    if entry is not None and import_job_is_active(entry.get("import_job")):
        raise HTTPException(status_code=409, detail={"error": f"{label} import job is active"})


def _account_missing_error(account_id: str = "") -> dict[str, str]:
    """单个账号不存在的错误项：只带稳定管理 ID，不带 token 的任何片段。"""
    clean_id = _clean_text(account_id)
    return {"id": clean_id, "code": "account_not_found", "message": "account not found"}


def _account_not_found_errors(account_ids: list[str]) -> list[dict[str, str]]:
    return [
        {"id": account_id, "code": "account_not_found", "message": "account not found"}
        for account_id in dict.fromkeys(_clean_text(item) for item in account_ids if _clean_text(item))
    ]


def _account_selection_preview(selection: AccountSelectionScope) -> dict[str, Any]:
    if selection.mode == "explicit":
        targets, missing_ids = _account_targets(selection.account_ids)
        return {
            "matching_count": len(targets),
            "selected_count": len(targets),
            "excluded_account_ids": [],
            "account_ids": [account_id for _token, account_id in targets],
            "errors": _account_not_found_errors(missing_ids),
        }
    matching_ids = [account_id for _token, account_id in _account_selection_members(selection)]
    matching_set = set(matching_ids)
    excluded = [account_id for account_id in _clean_ids(selection.excluded_account_ids) if account_id in matching_set]
    excluded_set = set(excluded)
    selected = [account_id for account_id in matching_ids if account_id not in excluded_set]
    return {
        "matching_count": len(matching_ids),
        "selected_count": len(selected),
        "excluded_account_ids": excluded,
        "account_ids": selected,
        "errors": [],
    }


def _target_account_group_id(value: str | None) -> str | None:
    """None 表示不改分组；"" 表示未分组；其它必须是已存在的账号组。"""
    if value is None:
        return None
    raw = _clean_text(value)
    if raw in {"", "__ungrouped__"}:
        return ""
    group_id = _account_group_id(raw)
    if not any(group.get("id") == group_id for group in _account_group_payload()["groups"]):
        raise HTTPException(status_code=400, detail={"error": "account group not found"})
    return group_id


def _health_report(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Health never blocks listing: on any evaluation error return no health data."""
    try:
        return account_health_report(items, status_category=_account_status_category)
    except Exception:
        return {}, {}


def _log_account_test(
    account_id: str,
    mode: str,
    model: str,
    *,
    ok: bool,
    started: float,
    status_code: int = 200,
    error: object = "",
) -> None:
    """账号测试写账号类系统日志：只含管理 ID、模式、模型、耗时和结果，不含 prompt 与任何 token。"""
    try:
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "账号测试",
            {
                "id": _clean_text(account_id),
                "mode": mode,
                "model": _clean_text(model),
                "ok": ok,
                "status_code": status_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": sanitize_diagnostic_text(error, limit=300),
            },
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响测试结果
        pass


def _test_account(access_token: str) -> dict[str, Any]:
    """Probe one account with a remote info fetch (no image quota is spent)."""
    started = time.perf_counter()
    try:
        account = account_service.fetch_remote_info(access_token, "account_batch_test", remove_invalid=False)
    except Exception as exc:
        return {
            "ok": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error": sanitize_diagnostic_text(exc, sensitive_values=[access_token], limit=300) or exc.__class__.__name__,
            "item": None,
        }
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not account:
        return {"ok": False, "duration_ms": duration_ms, "error": "账号不存在或已被移除", "item": None}
    item = _account_for_api(account)
    category = item.get("status_category")
    ok = category == "normal"
    if ok:
        message = "无限额度" if account_service.is_unlimited_image_quota_account(account) else (
            "额度未知" if account.get("image_quota_unknown") else f"剩余额度 {int(account.get('quota') or 0)}"
        )
    else:
        message = _clean_text(account.get("last_refresh_error") or account.get("last_error")) or str(
            item.get("status_label") or account.get("status") or "不可用"
        )
    return {"ok": ok, "duration_ms": duration_ms, "message": message, "item": item}


def _import_abnormal_tokens(access_tokens: list[str]) -> list[str]:
    tokens = _unique_tokens(access_tokens)
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        account = account_service.get_account(token)
        if not account or not _status_matches_filter(account, "abnormal"):
            continue
        current_token = _clean_text(account.get("access_token")) or token
        if current_token and current_token not in seen:
            seen.add(current_token)
            result.append(current_token)
    return result


def _account_zip_bytes(items: list[dict[str, str]]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            raw_name = item.get("email") or item.get("account_id") or f"account-{index:03d}"
            base_name = _safe_export_name(raw_name, f"account-{index:03d}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(name)
            archive.writestr(
                f"{name}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
    return buf.getvalue()


def _redact(text: object, secrets: list[object]) -> str:
    message = _clean_text(text)
    for secret in secrets:
        value = _clean_text(secret)
        if value:
            message = message.replace(value, "***")
    return message[:300]


def _single_account(account_id: str, access_token: str) -> dict[str, Any] | None:
    """按稳定管理 ID（优先）或旧版 access_token 取单个账号。"""
    clean_id = _clean_text(account_id).lower()
    if clean_id:
        return account_service.get_account_by_id(clean_id)
    clean_token = _clean_text(access_token)
    return account_service.get_account(clean_token) if clean_token else None


def _force_refresh_access_tokens(tokens: list[str], progress_id: str) -> dict[str, Any]:
    """逐个账号强制用 refresh_token 换新 access_token；不拉取账号信息也不查询额度。"""
    account_service.ensure_refresh_progress(progress_id, len(tokens))
    refreshed = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for token in tokens:
        errors_before = len(errors)
        before = account_service.get_account(token) or {}
        label = anonymize_token(token)
        management_id = _clean_text(before.get("management_id"))
        secrets = [before.get("access_token"), before.get("refresh_token"), before.get("id_token")]
        try:
            if not before:
                errors.append({"id": management_id, "token": label, "error": "account not found"})
            elif _clean_text(before.get("source_type")) == "chatgpt_web" or not _clean_text(before.get("refresh_token")):
                skipped += 1
            else:
                account_service.refresh_access_token(token, force=True, event="manual_refresh_access_token")
                after = account_service.get_account(token) or {}
                if after.get("last_token_refresh_at") != before.get("last_token_refresh_at"):
                    refreshed += 1
                else:
                    errors.append({
                        "id": management_id,
                        "token": label,
                        "error": _redact(after.get("last_token_refresh_error"), secrets) or "refresh failed",
                    })
        except Exception as exc:
            errors.append({"id": management_id, "token": label, "error": _redact(exc, secrets) or "refresh failed"})
        account_service.update_refresh_progress(
            progress_id, token, error=errors[-1]["error"] if len(errors) > errors_before else "",
        )
    result = {"refreshed": refreshed, "skipped": skipped, "errors": errors, "items": account_service.list_accounts()}
    account_service.finish_refresh_progress(progress_id, result)
    return result


def _run_account_capability_test(account: dict[str, Any], mode: str, model: str, prompt: str) -> dict[str, Any]:
    result = account_test_service.execute({
        "account_id": _account_management_id(account),
        "mode": mode,
        "model": model,
        "prompt": prompt,
    })
    log_service.add(
        LOG_TYPE_ACCOUNT,
        "账号真实测试",
        {
            "id": _account_management_id(account),
            "mode": mode,
            "model": model,
            "status": result.get("status"),
            "duration_ms": result.get("duration_ms"),
            "quota_deducted": result.get("quota_deducted"),
            "error": result.get("error_message") or "",
        },
    )
    return result


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/users")
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users")
    async def create_user_key(
            body: UserKeyCreateRequest,
            include_items: bool = Query(default=False),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            item, raw_key = auth_service.create_key(role="user", name=body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        # key 为本项目历史字段，raw_key 与 yukkcat/chatgpt2api 对齐；两者同值。
        payload: dict[str, object] = {"item": item, "key": raw_key, "raw_key": raw_key}
        if include_items:
            payload["items"] = auth_service.list_keys(role="user")
        return payload

    @router.post("/api/auth/users/{key_id}")
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "key": body.key,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        try:
            item = auth_service.update_key(key_id, updates, role="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"item": item, "items": auth_service.list_keys(role="user")}

    @router.delete("/api/auth/users/{key_id}")
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not auth_service.delete_key(key_id, role="user"):
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"items": auth_service.list_keys(role="user")}

    @router.get("/api/accounts")
    async def get_accounts(
            page: int = Query(default=1, ge=1),
            page_size: int = Query(default=500, ge=1, le=500),
            keyword: str = "",
            status: str = "all",
            group_id: str = "all",
            health: str = "all",
            flag: str = "all",
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return _accounts_page(
            page=page,
            page_size=page_size,
            keyword=keyword,
            status=status,
            group_id=group_id,
            health=health,
            flag=flag,
        )

    @router.get("/api/accounts/health")
    async def get_accounts_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        by_token, pool = _health_report(account_service.list_accounts())
        counts: dict[str, int] = {}
        for item in by_token.values():
            level = str(item.get("health_level") or "")
            counts[level] = counts.get(level, 0) + 1
        return {"pool": pool, "counts": counts}

    @router.post("/api/accounts/selection-preview")
    async def preview_account_selection(
            body: AccountSelectionPreviewRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(_account_selection_preview, body.selection)

    @router.post("/api/accounts/test")
    async def test_account(body: AccountTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not _clean_text(body.id) and not _clean_text(body.access_token):
            raise HTTPException(status_code=400, detail={"error": "id or access_token is required"})
        account = _single_account(body.id, body.access_token)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        if body.mode == "probe":
            access_token = _clean_text(account.get("access_token")) or _clean_text(body.access_token)
            return await run_in_threadpool(_test_account, access_token)
        model = _clean_text(body.model)
        prompt = _clean_text(body.prompt)
        if not model or not prompt:
            raise HTTPException(status_code=400, detail={"error": "model and prompt are required"})
        return await run_in_threadpool(_run_account_capability_test, account, body.mode, model, prompt)

    @router.post("/api/accounts/{account_id}/test")
    async def test_account_by_id(
            account_id: str,
            body: AccountTestRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        started = time.perf_counter()
        try:
            result = await test_account(body.model_copy(update={"id": account_id}), authorization)
        except HTTPException as exc:
            _log_account_test(account_id, body.mode, body.model, ok=False, started=started, status_code=exc.status_code)
            raise
        if body.mode == "probe":
            # chat / image 模式的真实测试已在 _run_account_capability_test 里记过日志，这里只补 probe。
            _log_account_test(
                account_id, body.mode, body.model, ok=bool(isinstance(result, dict) and result.get("ok")),
                started=started, error=(result.get("error") if isinstance(result, dict) else ""),
            )
        return result

    @router.get("/api/accounts/{account_id}/access-token")
    async def get_account_access_token(account_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account = account_service.get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        access_token = _clean_text(account.get("access_token"))
        if not access_token:
            raise HTTPException(status_code=404, detail={"error": "access token not found"})
        log_service.add(LOG_TYPE_ACCOUNT, "查看账号凭据", {"id": _account_management_id(account), "kind": "access_token"})
        return JSONResponse({"access_token": access_token}, headers={"Cache-Control": "no-store"})

    @router.get("/api/accounts/{account_id}/refresh-token")
    async def get_account_refresh_token(account_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account = account_service.get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        refresh_token = _clean_text(account.get("refresh_token"))
        if not refresh_token:
            raise HTTPException(status_code=404, detail={"error": "refresh token not found"})
        log_service.add(LOG_TYPE_ACCOUNT, "查看账号凭据", {"id": _account_management_id(account), "kind": "refresh_token"})
        return JSONResponse({"refresh_token": refresh_token}, headers={"Cache-Control": "no-store"})

    @router.get("/api/account-groups")
    async def list_account_groups(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return _account_group_payload()

    @router.post("/api/account-groups")
    async def save_account_group(body: AccountGroupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return _upsert_account_group(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.delete("/api/account-groups/{group_id}")
    async def delete_account_group(group_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        normalized = _account_group_id(group_id)
        groups = _config_dict_list("account_groups")
        next_groups = [group for group in groups if _account_group_id(group.get("id")) != normalized]
        if len(next_groups) == len(groups):
            raise HTTPException(status_code=404, detail={"error": "account group not found"})
        updated = config.update({"account_groups": next_groups})
        for account in account_service.list_accounts():
            if _clean_text(account.get("group_id")) == normalized:
                account_service.update_account(account.get("access_token", ""), {"group_id": ""}, quiet=True)
        return {
            "deleted": normalized,
            **_account_group_payload(updated.get("account_groups", [])),
            "items": _accounts_for_api(account_service.list_accounts()),
        }

    @router.post("/api/accounts")
    async def create_accounts(body: AccountCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        target_group_id = _target_account_group_id(body.target_group_id)
        group_fields: dict[str, Any] = {"group_id": target_group_id} if target_group_id is not None else {}
        account_payloads = [{**item, **group_fields} for item in body.accounts if isinstance(item, dict)]
        payload_tokens = [_account_payload_token(item) for item in account_payloads]
        tokens = _unique_tokens([*body.tokens, *payload_tokens])
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        payload_token_set = set(_unique_tokens(payload_tokens))
        extra_tokens = [token for token in tokens if token not in payload_token_set]
        if body.verify_on_import:
            verify_payloads = [
                *account_payloads,
                *({"access_token": token, "source_type": "web", **group_fields} for token in extra_tokens),
            ]
            verified = await run_in_threadpool(verify_and_import, verify_payloads)
            items = [item for token in verified["tokens"] if (item := account_service.get_account(token)) is not None]
            return {
                "added": verified["added"],
                "skipped": 0,
                "verified": verified["verified"],
                "rejected": verified["rejected"],
                "refreshed": verified["added"],
                "errors": [],
                "items": _accounts_for_api(items) if body.return_items else [],
            }
        if account_payloads:
            if body.restore:
                result = account_service.add_account_items(
                    account_payloads, return_items=body.return_items, restore=True,
                )
            else:
                result = account_service.add_account_items(account_payloads, return_items=body.return_items)
            if extra_tokens:
                if target_group_id is None:
                    extra_result = account_service.add_accounts(extra_tokens, return_items=body.return_items)
                else:
                    extra_result = account_service.add_account_items(
                        [{"access_token": token, **group_fields} for token in extra_tokens],
                        return_items=body.return_items,
                    )
                result["added"] = int(result.get("added") or 0) + int(extra_result.get("added") or 0)
                result["skipped"] = int(result.get("skipped") or 0) + int(extra_result.get("skipped") or 0)
        elif target_group_id is None:
            result = account_service.add_accounts(tokens, return_items=body.return_items)
        else:
            result = account_service.add_account_items(
                [{"access_token": token, **group_fields} for token in tokens],
                return_items=body.return_items,
            )
        if not body.refresh:
            return {
                **result,
                "refreshed": 0,
                "errors": [],
                "items": _accounts_for_api(result.get("items", [])) if body.return_items else [],
            }
        refresh_result = account_service.refresh_accounts(
            tokens,
            remove_invalid=False,
        )
        return {
            **result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": _accounts_for_api(refresh_result.get("items", result.get("items", []))) if body.return_items else [],
        }

    @router.delete("/api/accounts")
    async def delete_accounts(body: AccountDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.tokens)
        if not targets:
            raise HTTPException(status_code=400, detail={
                "error": "tokens is required",
                "errors": _account_not_found_errors(missing_ids),
            })
        result = account_service.delete_accounts([token for token, _id in targets], return_items=False)
        return {
            **result,
            "target_ids": [account_id for _token, account_id in targets if account_id],
            "errors": _account_not_found_errors(missing_ids),
        }

    @router.post("/api/accounts/import-cleanup")
    async def cleanup_imported_abnormal_accounts(
            body: AccountImportCleanupRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        targets, missing_ids = _account_targets(body.account_ids, body.access_tokens)
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        abnormal_tokens = _import_abnormal_tokens([token for token, _id in targets])
        removed = 0
        if body.remove and abnormal_tokens:
            removed = int(account_service.delete_accounts(abnormal_tokens, return_items=False).get("removed") or 0)
        return {
            "checked": len(targets),
            "abnormal": len(abnormal_tokens),
            "removed": removed,
            "errors": _account_not_found_errors(missing_ids),
        }

    @router.post("/api/accounts/import-sub2api-file")
    async def import_sub2api_file(body: Sub2APIFileImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        target_group_id = _target_account_group_id(body.target_group_id)
        try:
            document = json.loads(body.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": "不是有效的 JSON 文件"}) from exc
        payloads = parse_sub2api_export(document)
        if not payloads:
            raise HTTPException(status_code=400, detail={"error": "文件中没有找到 access_token"})
        if target_group_id is not None:
            payloads = [{**item, "group_id": target_group_id} for item in payloads]
        result = await run_in_threadpool(account_service.add_account_items, payloads, body.return_items)
        tokens = _unique_tokens([_account_payload_token(item) for item in payloads])
        refreshed = 0
        errors: list[Any] = []
        if body.refresh:
            refresh_result = await run_in_threadpool(account_service.refresh_accounts, tokens, None, False)
            refreshed = int(refresh_result.get("refreshed") or 0)
            errors = list(refresh_result.get("errors") or [])
        return {
            "parsed": len(payloads),
            "added": int(result.get("added") or 0),
            "skipped": int(result.get("skipped") or 0),
            "refreshed": refreshed,
            "errors": errors,
            "items": _accounts_for_api(result.get("items", [])) if body.return_items else [],
        }

    @router.post("/api/accounts/login-import")
    async def start_login_import(body: LoginImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            job = login_import_service.start(body.text, verify=body.verify)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"job": job}

    @router.get("/api/accounts/login-import")
    async def get_login_import(job_id: str = "", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"job": login_import_service.get(job_id)}

    @router.post("/api/accounts/login-import/stop")
    async def stop_login_import(body: LoginImportStopRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"job": login_import_service.stop(body.job_id)}

    @router.post("/api/accounts/reactivate")
    async def reactivate_accounts(body: AccountReactivateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        emails = [str(item or "").strip() for item in body.emails if str(item or "").strip()]
        if not emails:
            raise HTTPException(status_code=400, detail={"error": "emails is required"})
        return await run_in_threadpool(login_import_service.reactivate, emails)

    @router.post("/api/accounts/clear-auto-pause")
    async def clear_accounts_auto_pause(
            body: AccountClearAutoPauseRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        items = [item for token, _id in targets if (item := account_service.clear_auto_pause(token)) is not None]
        return {"cleared": len(items), "items": _accounts_for_api(items), "errors": _account_not_found_errors(missing_ids)}

    @router.post("/api/accounts/egress/switch")
    async def switch_accounts_egress(body: AccountEgressSwitchRequest, authorization: str | None = Header(default=None)):
        """按账号 ID / 选择范围申请换出口（对接 services/egress_slots.request_switch）。"""
        require_admin(authorization)
        from services import egress_slots

        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        try:
            result = await run_in_threadpool(egress_slots.request_switch, [token for token, _id in targets], body.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {**result, "errors": _account_not_found_errors(missing_ids)}

    @router.post("/api/accounts/egress/cancel-switch")
    async def cancel_accounts_egress_switch(body: AccountOperationRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        from services import egress_slots

        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        result = await run_in_threadpool(egress_slots.cancel_switch, [token for token, _id in targets])
        return {**result, "errors": _account_not_found_errors(missing_ids)}

    @router.post("/api/accounts/sync")
    @router.post("/api/accounts/refresh")
    async def refresh_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        """同步账号信息与额度。/refresh 为旧路径，/sync 为同义新路径；未指定账号时同步全部。"""
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection, body.account_ids, body.access_tokens, default_all=True,
        )
        access_tokens = [token for token, _id in targets]
        if not access_tokens:
            raise HTTPException(status_code=400, detail={
                "error": "access_tokens is required",
                "errors": _account_not_found_errors(missing_ids),
            })

        progress_id = str(uuid.uuid4())
        # 进度记录在返回前建好（前端立刻轮询不会 404），选中但已不存在的账号写进事件流。
        account_service.ensure_refresh_progress(progress_id, len(access_tokens))
        account_service.report_missing_accounts(progress_id, missing_ids)

        async def _do_refresh():
            try:
                await run_in_threadpool(account_service.refresh_accounts, access_tokens, progress_id)
            except Exception as e:
                account_service.finish_refresh_progress(
                    progress_id, error=sanitize_diagnostic_text(e, limit=300) or "refresh failed",
                )

        _schedule_account_operation(_do_refresh())

        return {
            "progress_id": progress_id,
            "target_ids": [account_id for _token, account_id in targets if account_id],
            "errors": _account_not_found_errors(missing_ids),
        }

    @router.post("/api/accounts/refresh-access-token")
    async def refresh_access_tokens(body: AccountOperationRequest, authorization: str | None = Header(default=None)):
        """强制用 refresh_token 换新 access_token，不拉取账号信息也不查询额度。进度走 /refresh/progress。"""
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        access_tokens = [token for token, _id in targets]
        if not access_tokens:
            raise HTTPException(status_code=400, detail={
                "error": "no valid accounts selected",
                "errors": _account_not_found_errors(missing_ids),
            })

        progress_id = str(uuid.uuid4())
        account_service.ensure_refresh_progress(progress_id, len(access_tokens))
        account_service.report_missing_accounts(progress_id, missing_ids)

        async def _do_refresh_access_tokens():
            try:
                await run_in_threadpool(_force_refresh_access_tokens, access_tokens, progress_id)
            except Exception:
                account_service.finish_refresh_progress(progress_id, error="access token refresh failed")

        _schedule_account_operation(_do_refresh_access_tokens())
        return {
            "progress_id": progress_id,
            "target_ids": [account_id for _token, account_id in targets if account_id],
            "errors": _account_not_found_errors(missing_ids),
        }

    def _progress_for_api(progress: dict[str, Any]) -> dict[str, Any]:
        result = progress.get("result") if isinstance(progress.get("result"), dict) else None
        if result is not None:
            progress = dict(progress)
            progress["result"] = {**result, "items": _accounts_for_api(result.get("items", []))}
        return progress

    @router.get("/api/accounts/refresh/progress/{progress_id}")
    async def get_refresh_progress(progress_id: str, authorization: str | None = Header(default=None)):
        """旧接口，保持原有字段（另带 events）；新代码请用 /api/accounts/operations/{id}。"""
        require_admin(authorization)
        progress = account_service.get_refresh_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return _progress_for_api(progress)

    @router.get("/api/accounts/operations/{progress_id}")
    async def get_account_operation_progress(
        progress_id: str,
        after: int = 0,
        authorization: str | None = Header(default=None),
    ):
        """账号操作统一进度：旧字段 + status(running/success/failed) / percent / events（after=上次 last_sequence 增量取）。"""
        require_admin(authorization)
        progress = account_service.get_operation_progress(progress_id, after_sequence=max(0, int(after)))
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return _progress_for_api(progress)

    @router.post("/api/accounts/export")
    async def export_accounts(body: AccountExportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection, body.account_ids, body.access_tokens, default_all=True,
        )
        if missing_ids:
            raise HTTPException(status_code=400, detail={
                "error": "one or more accounts were not found",
                "errors": _account_not_found_errors(missing_ids),
            })
        access_tokens = _unique_tokens([token for token, _id in targets])
        timestamp = _download_timestamp()

        if body.format == "txt":
            lines = [
                _clean_text(account.get("access_token"))
                for account in account_service.build_backup_records(access_tokens)
                if _clean_text(account.get("access_token"))
            ]
            if not lines:
                raise HTTPException(status_code=400, detail={"error": "没有可导出的账号"})
            return Response(
                "\n".join(lines) + "\n",
                media_type="text/plain; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="access-tokens-{timestamp}.txt"',
                },
            )

        if body.format == "backup":
            records = account_service.build_backup_records(access_tokens)
            if not records:
                raise HTTPException(status_code=400, detail={"error": "没有可导出的账号"})
            payload = {
                "format": BACKUP_FORMAT,
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "accounts": records,
            }
            return Response(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                media_type="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="accounts-backup-{timestamp}.json"',
                },
            )

        if body.format == "agent_identity":
            selected = set(access_tokens)
            accounts = [
                account
                for account in account_service.list_accounts()
                if not selected or _account_payload_token(account) in selected
            ]
            auth_items: list[dict[str, Any]] = []
            errors: list[str] = []
            for account in accounts:
                try:
                    auth_items.append(ensure_openai_agent_identity(account))
                except Exception as exc:
                    label = _clean_text(account.get("email")) or "unknown account"
                    errors.append(f"{label}: {str(exc)[:200]}")
            if not auth_items:
                detail = "；".join(errors[:3]) or "没有可导出的 Agent Identity"
                raise HTTPException(status_code=400, detail={"error": detail})
            if len(auth_items) == 1:
                return Response(
                    json.dumps(auth_items[0], ensure_ascii=False, indent=2) + "\n",
                    media_type="application/json",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": 'attachment; filename="auth.json"',
                    },
                )
            archive = io.BytesIO()
            used_names: set[str] = set()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, auth_json in enumerate(auth_items, start=1):
                    identity = auth_json.get("agent_identity") if isinstance(auth_json, dict) else {}
                    identity = identity if isinstance(identity, dict) else {}
                    base_name = _safe_export_name(
                        _clean_text(identity.get("email")),
                        f"account-{index}",
                    )
                    name = base_name
                    suffix = 2
                    while name in used_names:
                        name = f"{base_name}-{suffix}"
                        suffix += 1
                    used_names.add(name)
                    bundle.writestr(
                        f"{name}/auth.json",
                        json.dumps(auth_json, ensure_ascii=False, indent=2) + "\n",
                    )
            return Response(
                archive.getvalue(),
                media_type="application/zip",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="openai-agent-identities-{timestamp}.zip"',
                },
            )

        items = account_service.build_export_items(access_tokens)
        if not items:
            raise HTTPException(
                status_code=400,
                detail={"error": "没有可导出的完整账号，需要同时有 access_token、refresh_token 和 id_token"},
            )

        if body.format in {"zip", "cpa"}:
            content = _account_zip_bytes(items)
            return Response(
                content,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="codex-accounts-cpa-{timestamp}.zip"'},
            )

        if body.format == "sub2api":
            payload = {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "proxies": [],
                "accounts": [build_openai_oauth_export_account(item) for item in items],
            }
            return Response(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="openai-accounts-sub2api-{timestamp}.json"'},
            )

        payload: dict[str, str] | list[dict[str, str]] = items[0] if len(items) == 1 else items
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.json"'},
        )

    @router.get("/api/accounts/agent-identities")
    async def list_agent_identities(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        items = openai_agent_identity_store.summary()
        return {"total": len(items), "items": items}

    @router.post("/api/accounts/update")
    async def update_account(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account_id = _clean_text(body.id).lower()
        new_access_token = ""
        new_refresh_token = ""
        if account_id:
            current = account_service.get_account_by_id(account_id)
            access_token = _clean_text((current or {}).get("access_token"))
            new_access_token = _clean_text(body.access_token)
            new_refresh_token = _clean_text(body.refresh_token)
        else:
            access_token = _clean_text(body.access_token)
            if not access_token:
                raise HTTPException(status_code=400, detail={"error": "id is required"})
            current = account_service.get_account(access_token)
        if current is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        updates = {
            key: value
            for key, value in {
                "type": body.type,
                "source_type": body.source_type,
                "status": body.status,
                "quota": body.quota,
                "proxy": body.proxy,
                "group_id": body.group_id,
            }.items()
            if value is not None
        }
        credentials_changed = bool(new_access_token or new_refresh_token)
        if not updates and not credentials_changed:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        if credentials_changed:
            try:
                renewed = await run_in_threadpool(
                    lambda: account_service.update_credentials(
                        account_id, access_token=new_access_token, refresh_token=new_refresh_token,
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
            if renewed is None:
                raise HTTPException(status_code=404, detail={"error": "account not found"})
            access_token = _clean_text(renewed.get("access_token")) or access_token
            account = renewed
        if updates:
            account = account_service.update_account(access_token, updates)
        elif not credentials_changed:
            account = current
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": _account_for_api(account), "items": _accounts_for_api(account_service.list_accounts())}

    @router.post("/api/accounts/batch-update")
    async def batch_update_accounts(body: AccountBatchUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        if not targets and not missing_ids:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        updates = {key: value for key, value in {"status": body.status}.items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        updated = 0
        errors: list[Any] = list(_account_not_found_errors(missing_ids))
        for token, _account_id in targets:
            account = account_service.update_account(token, updates, quiet=True)
            if account is None:
                errors.append(_account_missing_error(_account_id))
            else:
                updated += 1
        return {"updated": updated, "errors": errors, "items": _accounts_for_api(account_service.list_accounts())}

    @router.post("/api/accounts/group")
    async def bind_accounts_group(body: AccountGroupBindRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(body.selection, body.account_ids, body.access_tokens)
        if not targets and not missing_ids:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        group_id = "" if body.group_id.strip() == "__ungrouped__" else _account_group_id(body.group_id)
        if group_id and not any(group.get("id") == group_id for group in _account_group_payload()["groups"]):
            raise HTTPException(status_code=404, detail={"error": "account group not found"})
        updated = 0
        errors: list[Any] = list(_account_not_found_errors(missing_ids))
        for token, _account_id in targets:
            account = account_service.update_account(token, {"group_id": group_id}, quiet=True)
            if account is None:
                errors.append(_account_missing_error(_account_id))
            else:
                updated += 1
        return {
            "updated": updated,
            "errors": errors,
            "group_id": group_id,
            **_account_group_payload(),
            "items": _accounts_for_api(account_service.list_accounts()),
        }

    @router.post("/api/accounts/oauth/start")
    async def start_oauth_login(
            body: OAuthLoginStartRequest,
            authorization: str | None = Header(default=None),
    ):
        """登记一次 PKCE 会话，返回可让用户浏览器打开的 authorize URL。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(oauth_login_service.start, body.email_hint)
        except OAuthLoginError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/accounts/oauth/finish")
    async def finish_oauth_login(
            body: OAuthLoginFinishRequest,
            authorization: str | None = Header(default=None),
    ):
        """收用户从浏览器抓回的 callback URL / code，换出 token 三件套并落盘。"""
        require_admin(authorization)
        target_group_id = _target_account_group_id(body.target_group_id)
        # 回调地址里带着一次性授权 code，会话 ID 也是凭据：日志只记是否存在和长度，失败只记异常类名。
        print(
            "[oauth-login] finish called: "
            f"session_present={bool(body.session_id)}, callback_present={bool(body.callback)}, "
            f"callback_length={len(body.callback or '')}",
            flush=True,
        )
        try:
            tokens = await run_in_threadpool(oauth_login_service.finish, body.session_id, body.callback)
        except OAuthLoginError as exc:
            print(f"[oauth-login] finish rejected: {exc.__class__.__name__}", flush=True)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        payload = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "source_type": "oauth_login",
        }
        if target_group_id is not None:
            payload["group_id"] = target_group_id
        add_result = await run_in_threadpool(account_service.add_account_items, [payload])
        refresh_result = await run_in_threadpool(
            account_service.refresh_accounts, [tokens["access_token"]]
        )
        return {
            **add_result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": _accounts_for_api(refresh_result.get("items", add_result.get("items", []))),
        }

    @router.post("/api/accounts/codex-oauth/start")
    async def start_codex_oauth_login(
            body: OAuthLoginStartRequest,
            authorization: str | None = Header(default=None),
    ):
        """生成 Codex 登录链接；登录后浏览器会跳到 localhost:1455 的回调地址（打不开是正常的）。"""
        require_admin(authorization)
        return await run_in_threadpool(codex_oauth_service.start, body.email_hint)

    @router.post("/api/accounts/codex-oauth/finish")
    async def finish_codex_oauth_login(
            body: OAuthLoginFinishRequest,
            authorization: str | None = Header(default=None),
    ):
        """用回调地址换 Codex token 并绑定到已有账号；不新建账号、不改原有 token，响应不含 token。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                codex_oauth_service.bind, body.session_id, body.callback, _clean_text(body.account_id),
            )
        except OAuthLoginError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/accounts/remote-imports/active")
    async def get_active_remote_import(authorization: str | None = Header(default=None)):
        """全局唯一进行中的远程 CPA / Sub2API 导入任务；离开页面后可据此恢复进度抽屉。"""
        require_admin(authorization)
        return {"active": find_active_remote_import()}

    @router.get("/api/cpa/pools")
    async def list_cpa_pools(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools")
    async def create_cpa_pool(body: CPAPoolCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        if not body.secret_key.strip():
            raise HTTPException(status_code=400, detail={"error": "secret_key is required"})
        pool = cpa_config.add_pool(name=body.name, base_url=body.base_url, secret_key=body.secret_key)
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools/{pool_id}")
    async def update_cpa_pool(pool_id: str, body: CPAPoolUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        _ensure_remote_import_idle(cpa_config.get_pool(pool_id), "CPA")
        pool = cpa_config.update_pool(pool_id, body.model_dump(exclude_none=True))
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.delete("/api/cpa/pools/{pool_id}")
    async def delete_cpa_pool(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        _ensure_remote_import_idle(cpa_config.get_pool(pool_id), "CPA")
        if not cpa_config.delete_pool(pool_id):
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.get("/api/cpa/pools/{pool_id}/files")
    async def cpa_pool_files(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool_id": pool_id, "files": await run_in_threadpool(list_remote_files, pool)}

    @router.post("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import(pool_id: str, body: CPAImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        target_group_id = _target_account_group_id(body.target_group_id)
        try:
            job = cpa_import_service.start_import(pool, body.names, target_group_id=target_group_id)
        except RemoteImportConflictError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import_progress(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"import_job": pool.get("import_job")}

    @router.get("/api/sub2api/servers")
    async def list_sub2api_servers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers")
    async def create_sub2api_server(body: Sub2APIServerCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        has_login = body.email.strip() and body.password.strip()
        has_api_key = bool(body.api_key.strip())
        if not has_login and not has_api_key:
            raise HTTPException(status_code=400, detail={"error": "email+password or api_key is required"})
        server = sub2api_config.add_server(
            name=body.name,
            base_url=body.base_url,
            email=body.email,
            password=body.password,
            api_key=body.api_key,
            group_id=body.group_id,
            verify_tls=body.verify_tls,
        )
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers/{server_id}")
    async def update_sub2api_server(server_id: str, body: Sub2APIServerUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        _ensure_remote_import_idle(sub2api_config.get_server(server_id), "Sub2API")
        server = sub2api_config.update_server(server_id, body.model_dump(exclude_none=True))
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.delete("/api/sub2api/servers/{server_id}")
    async def delete_sub2api_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        _ensure_remote_import_idle(sub2api_config.get_server(server_id), "Sub2API")
        if not sub2api_config.delete_server(server_id):
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.get("/api/sub2api/servers/{server_id}/groups")
    async def sub2api_server_groups(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            groups = await run_in_threadpool(sub2api_list_remote_groups, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "groups": groups}

    @router.get("/api/sub2api/servers/{server_id}/accounts")
    async def sub2api_server_accounts(
        server_id: str,
        group_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        if group_id is not None:
            server = {**server, "group_id": group_id}
        try:
            accounts = await run_in_threadpool(sub2api_list_remote_accounts, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "accounts": accounts}

    @router.post("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import(server_id: str, body: Sub2APIImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        target_group_id = _target_account_group_id(body.target_group_id)
        try:
            job = sub2api_import_service.start_import(
                server,
                body.account_ids,
                group_bindings=[binding.model_dump() for binding in body.group_bindings],
                create_account_groups=body.create_account_groups,
                target_group_id=target_group_id,
            )
        except RemoteImportConflictError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import_progress(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"import_job": server.get("import_job")}

    # 放在最后注册：/api/accounts/{account_id} 会匹配 login-import 等单段固定 GET 路径，必须排在它们之后。
    @router.get("/api/accounts/{account_id}")
    async def get_account_detail(account_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account = _single_account(account_id, "")
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": _account_for_api(account)}

    return router

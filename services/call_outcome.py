"""调用结果归类：把一条调用日志归为成功/部分成功/文本/限流/失败，并统计出图张数与换号次数。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

入参既可以是日志条目（含 ``detail``），也可以是平铺的 detail 字典；
日志列表、实时监控、概览统计共用同一套口径，避免各处各算各的。
"""
from __future__ import annotations

from typing import Any, Mapping

from services.image_failure import is_rate_limit_failure_code, normalize_failure_code

OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial_success"
OUTCOME_TEXT_REVIEW = "text_review"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"

_SUCCESS_STATUSES = {"success", "completed", "complete", "done"}
_FAILED_STATUSES = {"failed", "error", "fail"}
_TEXT_REPLY_CODES = {"upstream_text_reply", "text_reply"}
_IMAGE_BUSINESS_ENDPOINTS = ("/images/generations", "/images/edits")

# 写入日志 detail 的归类字段（日志列表/详情直接展示，旧日志缺失时前端会自行推导）。
OUTCOME_DETAIL_KEYS = (
    "outcome",
    "image_requested_count",
    "image_succeeded_count",
    "image_failed_count",
    "image_result_status",
    "attempt_count",
    "switch_count",
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _int(value: object) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _detail(item: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = item.get("detail")
    return detail if isinstance(detail, Mapping) else item


def _value(item: Mapping[str, Any], key: str, default: object = "") -> object:
    detail = item.get("detail")
    if isinstance(detail, Mapping):
        value = detail.get(key)
        if value not in (None, ""):
            return value
    value = item.get(key)
    return default if value in (None, "") else value


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    normalized = _clean(value).lower()
    if normalized in {"1", "true", "yes", "on", "是"}:
        return True
    if normalized in {"0", "false", "no", "off", "否"}:
        return False
    return None


def call_attempts(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempts = _value(item, "image_attempts", [])
    if not isinstance(attempts, list):
        return []
    normalized: list[dict[str, Any]] = []
    for value in attempts:
        if not isinstance(value, Mapping):
            continue
        attempt = dict(value)
        attempt["slot"] = max(1, _int(attempt.get("slot")))
        attempt["attempt"] = max(1, _int(attempt.get("attempt")))
        if "switched_account" in attempt:
            attempt["switched_account"] = _optional_bool(attempt.get("switched_account"))
        normalized.append(attempt)
    return normalized


def call_switch_count(item: Mapping[str, Any]) -> int:
    """换号次数：取各次尝试里 switched_account 的计数与显式字段中的较大者。"""
    from_attempts = sum(1 for attempt in call_attempts(item) if attempt.get("switched_account") is True)
    explicit = max(
        _int(_value(item, "switch_count")),
        _int(_value(item, "image_account_switch_count")),
    )
    return max(from_attempts, explicit)


def _result_url_count(item: Mapping[str, Any]) -> int:
    detail = _detail(item)
    urls = detail.get("urls")
    if isinstance(urls, list):
        return len({_clean(url) for url in urls if _clean(url)})
    return 0


def _is_image_call(item: Mapping[str, Any]) -> bool:
    endpoint = _clean(_value(item, "endpoint")).lower().rstrip("/")
    if endpoint.endswith(_IMAGE_BUSINESS_ENDPOINTS):
        return True
    if _optional_bool(_value(item, "image_request")) is True:
        return True
    model = _clean(_value(item, "model")).lower()
    return endpoint.endswith(("/chat/completions", "/responses")) and "image" in model


def call_image_counts(item: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """返回 (请求张数, 成功张数, 失败张数, 出图结果状态)；非生图调用全部为 0。"""
    attempts = call_attempts(item)
    request_meta = _value(item, "request_meta", {})
    request_meta = request_meta if isinstance(request_meta, Mapping) else {}
    result_images = _value(item, "result_images", [])
    result_image_count = len(result_images) if isinstance(result_images, list) else 0
    result_count = max(
        _int(_value(item, "result_image_count")),
        result_image_count,
        _int(_value(item, "result_data_count")),
        _result_url_count(item),
    )
    image_call = _is_image_call(item) or bool(attempts) or result_count > 0
    if not image_call:
        return 0, 0, 0, ""

    succeeded_slots = {
        _int(attempt.get("slot"))
        for attempt in attempts
        if _clean(attempt.get("status")).lower() == "success" and _int(attempt.get("slot")) > 0
    }
    requested = max(
        _int(request_meta.get("n")),
        max((_int(attempt.get("slot")) for attempt in attempts), default=0),
    )
    succeeded = max(len(succeeded_slots), result_count)
    requested = max(requested, succeeded)
    failed = max(0, requested - succeeded)
    if succeeded > 0 and failed > 0:
        status = OUTCOME_PARTIAL
    elif succeeded > 0:
        status = OUTCOME_SUCCESS
    elif requested > 0:
        status = OUTCOME_FAILED
    else:
        status = ""
    return requested, succeeded, failed, status


def _is_text_reply(item: Mapping[str, Any], error_code: str) -> bool:
    if normalize_failure_code(error_code) in _TEXT_REPLY_CODES:
        return True
    status_code = _int(_value(item, "status_code"))
    has_upstream_text = bool(
        _clean(_value(item, "upstream_message_preview")) or _clean(_value(item, "raw_upstream_message"))
    )
    if status_code == 400 and has_upstream_text and not _result_url_count(item):
        return True
    attempts = call_attempts(item)
    return bool(attempts) and all(
        _clean(attempt.get("status")).lower() in {"message", "failed"} for attempt in attempts
    ) and any(_clean(attempt.get("status")).lower() == "message" for attempt in attempts)


def call_outcome(item: Mapping[str, Any]) -> str:
    status = _clean(_value(item, "status")).lower()
    error_code = _clean(_value(item, "error_code", _value(item, "failure_code"))).lower()
    status_code = _int(_value(item, "status_code"))
    requested, succeeded, failed, result_status = call_image_counts(item)
    failure_flag = status in _FAILED_STATUSES or bool(_clean(_value(item, "error")))

    if status == OUTCOME_PARTIAL or result_status == OUTCOME_PARTIAL or (
        requested > 0 and succeeded > 0 and failed > 0
    ):
        return OUTCOME_PARTIAL
    if status == OUTCOME_TEXT_REVIEW:
        return OUTCOME_TEXT_REVIEW
    if failure_flag and _is_text_reply(item, error_code):
        return OUTCOME_TEXT_REVIEW
    if status in _SUCCESS_STATUSES and not failure_flag:
        return OUTCOME_SUCCESS
    if (
        status_code == 429
        or status in {"limited", "rate_limited", "限流"}
        or is_rate_limit_failure_code(error_code)
        or is_rate_limit_failure_code(normalize_failure_code(error_code))
    ):
        return OUTCOME_RATE_LIMITED
    if failure_flag or status_code >= 400:
        return OUTCOME_FAILED
    return OUTCOME_UNKNOWN


def build_outcome_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """生成要写入日志 detail 的归类字段。"""
    requested, succeeded, failed, result_status = call_image_counts(item)
    fields: dict[str, Any] = {
        "outcome": call_outcome(item),
        "attempt_count": len(call_attempts(item)),
        "switch_count": call_switch_count(item),
    }
    if requested or succeeded or failed:
        fields.update({
            "image_requested_count": requested,
            "image_succeeded_count": succeeded,
            "image_failed_count": failed,
            "image_result_status": result_status,
        })
    return fields


def apply_outcome_fields(detail: dict[str, Any]) -> dict[str, Any]:
    """就地补全 detail 的归类字段（不覆盖已有显式值），返回同一个字典。"""
    for key, value in build_outcome_fields(detail).items():
        if key in {"attempt_count", "switch_count"} and not value:
            continue
        detail.setdefault(key, value)
    return detail


def dashboard_outcome(item: Mapping[str, Any]) -> str:
    """概览口径：success / final_failed / excluded（文本回复不计入成败）。

    状态无法判定的记录沿用旧口径按成功计（只有显式失败/报错才算失败）。
    """
    outcome = call_outcome(item)
    if outcome in {OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_UNKNOWN}:
        return "success"
    if outcome == OUTCOME_TEXT_REVIEW:
        return "excluded"
    return "final_failed"

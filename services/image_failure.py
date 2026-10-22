"""Structured classification of GPT image generation failures.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only) ``services/image_failure.py``
and adapted to this project: every failure resolves to one ``ImageFailure``
that decides the public error projection, whether the request may move to
another account, and whether the account itself should be charged a failure.

The legacy pipeline here still carries most failures as free text, so
``classify_image_error_text`` maps those texts onto the same failure codes in
one place instead of scattering keyword checks across the protocol layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from curl_cffi.requests import exceptions as curl_exceptions

from utils.helper import UpstreamHTTPError


@dataclass(frozen=True)
class FailurePolicy:
    scope: str
    capability: str | None
    retryable: bool
    status_code: int
    error_type: str
    verify_account: bool = False


@dataclass(frozen=True)
class ImageFailure:
    code: str
    scope: str
    capability: str | None
    retryable: bool
    retry_after: int | None
    status_code: int
    error_type: str
    verify_account: bool = False
    raw_detail: Any = field(default=None, compare=False, repr=False)
    public_detail: str = field(default="", compare=False, repr=False)

    @property
    def outcome(self) -> str:
        return "text" if self.status_code == 400 else "failure"

    @property
    def switch_account(self) -> bool:
        """Whether another account could plausibly succeed where this one failed."""
        return self.code in ACCOUNT_SWITCH_CODES

    @property
    def account_failure(self) -> bool:
        """Whether the failure should be charged to the account's fail counter."""
        return self.code not in NON_ACCOUNT_FAILURE_CODES

    def with_raw_detail(self, raw_detail: Any) -> "ImageFailure":
        return replace(self, raw_detail=raw_detail)

    def with_public_detail(self, public_detail: Any) -> "ImageFailure":
        return replace(self, public_detail=_safe_public_text(public_detail))

    def diagnostic_fields(self) -> dict[str, Any]:
        return {
            "failure_code": self.code,
            "failure_scope": self.scope,
            "failure_capability": self.capability,
            "failure_retryable": self.retryable,
            "failure_account_failure": self.account_failure,
            "failure_switch_account": self.switch_account,
            "failure_retry_after": self.retry_after,
            "status_code": self.status_code,
            "error_type": self.error_type,
        }


FAILURE_POLICIES: dict[str, FailurePolicy] = {
    "upstream_error": FailurePolicy("transient", None, False, 502, "server_error", verify_account=True),
    "conversation_not_ready": FailurePolicy("transient", "image_generation", True, 409, "server_error"),
    "internal_error": FailurePolicy("internal", None, False, 500, "server_error"),
    "upstream_unavailable": FailurePolicy("transient", None, True, 502, "server_error", verify_account=True),
    "upstream_connection_failed": FailurePolicy("transient", None, True, 502, "server_error", verify_account=True),
    "upstream_connection_timeout": FailurePolicy("transient", None, True, 504, "server_error", verify_account=True),
    "upstream_rate_limited": FailurePolicy(
        "transient", "image_generation", False, 429, "rate_limit_error", verify_account=True,
    ),
    "image_poll_timeout": FailurePolicy("transient", "image_generation", False, 502, "server_error", verify_account=True),
    "image_stream_timeout": FailurePolicy("transient", "image_generation", False, 502, "server_error", verify_account=True),
    "image_stream_interrupted": FailurePolicy(
        "transient", "image_generation", False, 502, "server_error", verify_account=True,
    ),
    "image_tool_error": FailurePolicy("account", "image_generation", False, 502, "server_error", verify_account=True),
    "upstream_error_reply": FailurePolicy("transient", "image_generation", False, 502, "server_error"),
    "image_quota_exhausted": FailurePolicy(
        "account", "image_generation", False, 429, "insufficient_quota", verify_account=True,
    ),
    "file_upload_throttled": FailurePolicy("account", "file_upload", True, 429, "rate_limit_error", verify_account=True),
    "auth_invalid": FailurePolicy("account", "auth", False, 401, "authentication_error", verify_account=True),
    "content_policy_violation": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "invalid_image_input": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "upstream_text_reply": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "no_image_generated": FailurePolicy("request", None, False, 502, "server_error"),
    "unsupported_model": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "image_download_failed": FailurePolicy("delivery", None, False, 502, "server_error"),
    "task_interrupted": FailurePolicy("request", None, False, 503, "server_error"),
    "no_available_account": FailurePolicy("transient", None, False, 503, "server_error"),
    "account_busy": FailurePolicy("transient", None, False, 503, "server_error"),
    # Request-scoped scheduling results: waiting for an account / egress ran past the
    # request deadline, or too many requests were already queued.
    "proxy_unavailable": FailurePolicy("request", None, False, 503, "server_error"),
    "account_busy_timeout": FailurePolicy("request", None, True, 503, "server_error"),
    "account_queue_full": FailurePolicy("request", None, True, 503, "server_error"),
    "insufficient_quota": FailurePolicy("account", "image_generation", False, 429, "insufficient_quota"),
}


FAILURE_CODE_ALIASES = {
    "connection_failed": "upstream_connection_failed",
    "connection_timeout": "upstream_connection_timeout",
    "invalid_access_token": "auth_invalid",
    "moderation_blocked": "content_policy_violation",
    "quota_exhausted": "image_quota_exhausted",
    "rate_limit_exceeded": "upstream_rate_limited",
    "safety_blocked": "content_policy_violation",
    "token_invalid": "auth_invalid",
    "token_invalidated": "auth_invalid",
    "token_revoked": "auth_invalid",
    "unsupported_image_model": "unsupported_model",
    "upstream_timeout": "image_poll_timeout",
}

# Failures another account can plausibly fix. Request-scoped failures (policy,
# text replies), long poll timeouts and transport errors (handled by the
# fallback egress) deliberately stay on the current account.
ACCOUNT_SWITCH_CODES = frozenset({
    "upstream_error_reply",
    "image_quota_exhausted",
    "insufficient_quota",
    "upstream_rate_limited",
    "file_upload_throttled",
    "image_tool_error",
    "upstream_unavailable",
})

RATE_LIMIT_FAILURE_CODES = frozenset({
    "429",
    "file_upload_throttled",
    "image_quota_exhausted",
    "insufficient_quota",
    "limited",
    "quota_exhausted",
    "rate_limit",
    "rate_limit_exceeded",
    "rate_limited",
    "upstream_rate_limited",
    "限流",
})

TEXT_REVIEW_FAILURE_CODES = frozenset(
    code for code, policy in FAILURE_POLICIES.items() if policy.status_code == 400
)

# Failures that say nothing about the account's health. ``upstream_error_reply``
# only deprioritizes the account; every HTTP-400 "text result" code (content
# policy, invalid input, upstream text reply, ...) is caused by the request, so a
# rejected prompt must never feed the account's failure streak / auto-pause.
NON_ACCOUNT_FAILURE_CODES = frozenset({
    "upstream_error_reply",
    "image_download_failed",
    "account_busy_timeout",
    "account_queue_full",
    "proxy_unavailable",
}) | TEXT_REVIEW_FAILURE_CODES

FAILED_STATUSES = frozenset({"error", "fail", "failed", "limited", "rate_limited", "限流"})


def normalize_failure_code(code: Any) -> str:
    normalized = str(code or "").strip().lower()
    return FAILURE_CODE_ALIASES.get(normalized, normalized)


def is_known_failure_code(code: Any) -> bool:
    return normalize_failure_code(code) in FAILURE_POLICIES


def is_rate_limit_failure_code(value: Any) -> bool:
    return str(value or "").strip().lower() in RATE_LIMIT_FAILURE_CODES


def image_failure(
    code: str | None,
    *,
    retry_after: int | None = None,
    raw_detail: Any = None,
) -> ImageFailure:
    normalized = normalize_failure_code(code or "upstream_error")
    if normalized not in FAILURE_POLICIES:
        normalized = "upstream_error"
    policy = FAILURE_POLICIES[normalized]
    return ImageFailure(
        code=normalized,
        scope=policy.scope,
        capability=policy.capability,
        retryable=policy.retryable,
        retry_after=retry_after,
        status_code=policy.status_code,
        error_type=policy.error_type,
        verify_account=policy.verify_account,
        raw_detail=raw_detail,
    )


def _is_structured_text_payload(text: str) -> bool:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
    try:
        json.loads(candidate)
    except (TypeError, ValueError):
        return False
    return True


def _is_structured_failure_code(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in (
        FAILURE_POLICIES.keys()
        | FAILURE_CODE_ALIASES.keys()
        | RATE_LIMIT_FAILURE_CODES
        | FAILED_STATUSES
    )


def _safe_public_text(value: Any) -> str:
    if isinstance(value, Mapping):
        error = value.get("error")
        candidates = [
            error.get("message") if isinstance(error, Mapping) else None,
            value.get("message"),
            value.get("detail"),
            value.get("error_description"),
            error if isinstance(error, str) else None,
        ]
        for candidate in candidates:
            if text := _safe_public_text(candidate):
                return text
        return ""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if _is_structured_text_payload(text) or _is_structured_failure_code(text):
        return ""
    return text


# --- upstream error replies --------------------------------------------------

_ERROR_REPLY_MAX_CHARS = 300
_ERROR_REPLY_ERROR_RE = re.compile(
    r"(?:发生|出现|遇到)了?[^，。,.!?！？]{0,6}(?:错误|问题|故障)|出错"
    r"|\berror\b|went wrong|technical (?:issue|problem|difficult)",
    re.IGNORECASE,
)
_ERROR_REPLY_IMAGE_RE = re.compile(r"图片|图像|生成|\bimages?\b|generat", re.IGNORECASE)


def is_upstream_error_reply_text(text: Any) -> bool:
    """Short assistant text reporting that the upstream itself failed to make the image.

    e.g. "由于我这边发生了错误，我未能生成图片。" — transient and unrelated to the
    prompt, so it should switch accounts instead of being returned as text.
    """
    detail = _safe_public_text(text)
    return bool(
        detail
        and len(detail) <= _ERROR_REPLY_MAX_CHARS
        and _ERROR_REPLY_ERROR_RE.search(detail)
        and _ERROR_REPLY_IMAGE_RE.search(detail)
    )


def upstream_error_reply_failure(failure: ImageFailure, text: Any = None) -> ImageFailure:
    """Reclassify an ``upstream_text_reply`` that is really an upstream-side error."""
    if failure.code != "upstream_text_reply":
        return failure
    detail = failure.public_detail or _safe_public_text(text) or _safe_public_text(failure.raw_detail)
    if not is_upstream_error_reply_text(detail):
        return failure
    return image_failure(
        "upstream_error_reply",
        retry_after=failure.retry_after,
        raw_detail=failure.raw_detail if failure.raw_detail is not None else detail,
    ).with_public_detail(detail)


# --- free-text classification -------------------------------------------------

REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}')


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。
    """
    if not message:
        return False
    return bool(REFERENCED_IMAGE_IDS_RE.search(message) or TOOL_PARAMS_JSON_RE.search(message))


def is_content_policy_image_message(message: str) -> bool:
    text = str(message or "")
    lower = text.lower()
    return (
        "content policy" in lower
        or "policy violation" in lower
        or "content_policy_violation" in lower
        or "safety" in lower and "policy" in lower
        or "防护限制" in text
        or "可能违反" in text
        or "违反" in text
        or "裸露" in text
        or "色情" in text
        or "情色" in text
    )


def is_upstream_text_reply_error(message: str) -> bool:
    lower = str(message or "").lower()
    return (
        is_model_text_reply_instead_of_image(message)
        or "upstream completed without generating images" in lower
        or "no image result found" in lower
        or "returned a text description" in lower
        or "content_policy_violation" in lower
        or "防护限制" in message
        or "违反" in message
        or "裸露" in message
        or "色情" in message
        or "情色" in message
    )


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


# 上游只用文字说明"生图次数到上限"、没有结构化错误码时的识别（简繁中文 / 英文）。
# 只匹配"达到 ... 图像/图片 ... 上限/限制"这类明确说法，避免把普通回复误判成额度耗尽。
# Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
_IMAGE_QUOTA_TEXT_PATTERNS = (
    re.compile(r"(已達|已达|達到|达到|用完|用盡|用尽).{0,30}(圖像|图像|圖片|图片|影像|生圖|生图).{0,20}(上限|限制|額度|额度)"),
    re.compile(r"(hit|reached|exceeded|used up).{0,60}\bimage\b.{0,40}\b(limit|quota)", re.IGNORECASE),
    re.compile(r"(hit|reached|exceeded|used up).{0,40}\b(limit|quota)\b.{0,40}\bimage", re.IGNORECASE),
)


def text_says_image_quota_exhausted(raw_detail: Any) -> bool:
    text = raw_detail if isinstance(raw_detail, str) else str(raw_detail or "")
    return any(pattern.search(text) for pattern in _IMAGE_QUOTA_TEXT_PATTERNS)


def is_image_quota_error(message: str) -> bool:
    lower = str(message or "").lower()
    return "image_account_selection:quota_exhausted" in lower or "insufficient_quota" in lower


def is_rate_limit_error(message: str) -> bool:
    lower = str(message or "").lower()
    return any(
        marker in lower
        for marker in (
            "status=429",
            "status_code=429",
            "http 429",
            "rate limit exceeded",
            "rate_limit_exceeded",
            "rate_limited",
            "quota exceeded",
            "quota_exceeded",
            "image quota",
            "too many requests",
        )
    )


def is_local_image_busy_error(message: str) -> bool:
    lower = str(message or "").lower()
    return (
        "no account in the pool" in lower
        or "no available image quota" in lower
        or "account concurrency" in lower
        or "server busy" in lower
        or "local busy" in lower
        or "rate-limit status" in lower
    )


def is_image_poll_timeout_error(message: str) -> bool:
    lower = str(message or "").lower()
    return (
        "生图超时" in lower
        or "imagepolltimeouterror" in lower
        or "image_poll_timeout" in lower
        or "poll timeout" in lower
        or "image_poll_timeout_secs" in lower
    )


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (35)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
        or "upstream image connection failed" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
        or "upstream connection timed out" in text
    )


def is_stream_transport_error(message: str) -> bool:
    """检测上游流式响应传输错误，这类错误通常来自 HTTP/2/SSE/代理长连接。"""
    text = str(message or "").lower()
    return (
        "curl: (92)" in text
        or "http/2 stream" in text
        or "internal_error" in text
        or "stream was not closed cleanly" in text
        or "stream reset" in text
        or "response ended prematurely" in text
        or "sse stream exceeded" in text
        or "upstream image stream interrupted" in text
    )


def should_refresh_image_account(message: object) -> bool:
    text = str(message or "")
    return (
        is_token_invalid_error(text)
        or is_image_quota_error(text)
        or text_says_image_quota_exhausted(text)
        or is_rate_limit_error(text)
    )


def classify_image_message_text(message: str) -> ImageFailure:
    """Classify a terminal upstream message that came back instead of an image."""
    text = str(message or "")
    if text_says_image_quota_exhausted(text):
        return image_failure("image_quota_exhausted", raw_detail=text)
    if is_content_policy_image_message(text):
        return image_failure("content_policy_violation", raw_detail=text).with_public_detail(text)
    if is_model_text_reply_instead_of_image(text):
        return image_failure("upstream_text_reply", raw_detail=text).with_public_detail(text)
    if is_upstream_error_reply_text(text):
        return image_failure("upstream_error_reply", raw_detail=text).with_public_detail(text)
    failure = image_failure("no_image_generated", raw_detail=text)
    # Historically a terminal message is a 400 text review result, not a 502.
    return replace(failure, status_code=400, error_type="invalid_request_error")


def classify_image_error_text(message: Any, *, code: str | None = None) -> ImageFailure | None:
    """Map a legacy free-text image error onto a failure code.

    Returns ``None`` when the text carries no recognizable signal, so callers
    can keep their own fallback. The order mirrors the historical public
    message projection so that user-visible messages do not change.
    """
    text = str(message or "").strip()
    normalized_code = normalize_failure_code(code)
    if normalized_code in {"upstream_text_reply", "no_image_generated", "upstream_error_reply"}:
        return image_failure(normalized_code, raw_detail=text).with_public_detail(text)
    if not text:
        return None
    lower = text.lower()
    if "image_account_selection:quota_exhausted" in lower:
        return image_failure("image_quota_exhausted", raw_detail=text)
    if "image_account_selection:unavailable" in lower:
        return image_failure("no_available_account", raw_detail=text)
    if is_image_quota_error(text) or text_says_image_quota_exhausted(text):
        return image_failure("image_quota_exhausted", raw_detail=text)
    if is_local_image_busy_error(text):
        return image_failure("account_busy", raw_detail=text)
    if "unsupported image model" in lower:
        return image_failure("unsupported_model", raw_detail=text)
    if is_image_poll_timeout_error(text):
        return image_failure("image_poll_timeout", raw_detail=text)
    if is_stream_transport_error(text):
        return image_failure("image_stream_interrupted", raw_detail=text)
    if is_connection_timeout_error(text):
        return image_failure("upstream_connection_timeout", raw_detail=text)
    if is_tls_connection_error(text):
        return image_failure("upstream_connection_failed", raw_detail=text)
    if is_upstream_text_reply_error(text):
        return image_failure("upstream_text_reply", raw_detail=text).with_public_detail(text)
    if is_upstream_error_reply_text(text):
        return image_failure("upstream_error_reply", raw_detail=text).with_public_detail(text)
    if is_token_invalid_error(text):
        return image_failure("auth_invalid", raw_detail=text)
    if is_rate_limit_error(text):
        return image_failure("upstream_rate_limited", raw_detail=text)
    if normalized_code in FAILURE_POLICIES:
        return image_failure(normalized_code, raw_detail=text)
    return None


# Friendly message key (config.image_error_messages) for each failure code.
FRIENDLY_MESSAGE_KEYS = {
    "image_quota_exhausted": "quota",
    "insufficient_quota": "quota",
    "no_available_account": "no_account",
    "account_busy": "local_busy",
    "account_busy_timeout": "local_busy",
    "account_queue_full": "local_busy",
    "unsupported_model": "unsupported_model",
    "image_poll_timeout": "poll_timeout",
    "image_stream_interrupted": "stream_interrupted",
    "upstream_connection_timeout": "connection_timeout",
    "upstream_connection_failed": "connection_failed",
    "upstream_text_reply": "text_reply",
    "no_image_generated": "text_reply",
    "upstream_error_reply": "text_reply",
}


def friendly_message_key(failure: ImageFailure | None) -> str:
    if failure is None:
        return ""
    return FRIENDLY_MESSAGE_KEYS.get(failure.code, "")


# --- structured upstream payloads ---------------------------------------------

def _structured_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key in ("code", "error_code", "failure_code", "type", "error"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    codes.add(candidate.strip().lower())
            pending.extend(child for child in current.values() if isinstance(child, (Mapping, list, tuple)))
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return codes


QUOTA_CODES = {"insufficient_quota", "quota_exhausted", "image_quota_exhausted"}
AUTH_CODES = {"invalid_access_token", "token_invalid", "token_invalidated", "token_revoked"}
POLICY_CODES = {"content_policy_violation", "moderation_blocked", "safety_blocked"}


def _failure_priority(code: str) -> int:
    normalized = normalize_failure_code(code)
    if normalized == "auth_invalid":
        return 8
    if normalized in {"image_quota_exhausted", "insufficient_quota"}:
        return 7
    if normalized in {"file_upload_throttled", "upstream_rate_limited"}:
        return 6
    if normalized == "image_download_failed":
        return 5
    if normalized in TEXT_REVIEW_FAILURE_CODES and normalized != "upstream_text_reply":
        return 4
    if normalized in {"image_tool_error", "upstream_error_reply"}:
        return 3
    if normalized == "upstream_text_reply":
        return 2
    if normalized in {
        "image_poll_timeout",
        "image_stream_interrupted",
        "image_stream_timeout",
        "task_interrupted",
        "upstream_connection_failed",
        "upstream_connection_timeout",
        "upstream_unavailable",
    }:
        return 1
    return 0


def image_failure_priority(failure: ImageFailure) -> int:
    """Return the shared ordering used when several image failures compete."""
    return _failure_priority(failure.code)


def merge_failures(current: ImageFailure | None, candidate: ImageFailure | None) -> ImageFailure | None:
    """Keep the most informative failure across several attempts."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    if candidate.code == current.code or _failure_priority(candidate.code) > _failure_priority(current.code):
        winner, other = candidate, current
    else:
        winner, other = current, candidate
    if not winner.raw_detail and other.raw_detail:
        winner = winner.with_raw_detail(other.raw_detail)
    if not winner.public_detail and other.public_detail:
        winner = winner.with_public_detail(other.public_detail)
    return winner


def _classify_structured_failure_codes(
    codes: Any,
    *,
    retry_after: int | None = None,
    raw_detail: Any = None,
) -> ImageFailure | None:
    normalized_codes = {
        str(code or "").strip().lower()
        for code in (codes if isinstance(codes, (set, frozenset, list, tuple)) else (codes,))
        if str(code or "").strip()
    }
    known_codes: set[str] = set()
    if normalized_codes.intersection(POLICY_CODES):
        known_codes.add("content_policy_violation")
    if normalized_codes.intersection(AUTH_CODES):
        known_codes.add("auth_invalid")
    if normalized_codes.intersection(QUOTA_CODES):
        known_codes.add("image_quota_exhausted")
    known_codes.update(
        FAILURE_CODE_ALIASES.get(code, code)
        for code in normalized_codes
        if code not in POLICY_CODES | AUTH_CODES | QUOTA_CODES
        and FAILURE_CODE_ALIASES.get(code, code) in FAILURE_POLICIES
    )
    if any(is_rate_limit_failure_code(code) for code in normalized_codes):
        known_codes.add("upstream_rate_limited")
    if known_codes:
        selected_code = max(known_codes, key=lambda code: (_failure_priority(code), code))
        return image_failure(selected_code, retry_after=retry_after, raw_detail=raw_detail)
    return None


def classify_upstream_http_error(exc: UpstreamHTTPError) -> ImageFailure:
    codes = _structured_codes(exc.body)
    context_path = str(exc.context or "").strip().lower().split("?", 1)[0].rstrip("/")
    status_code = int(exc.status_code)
    retry_after = exc.retry_after
    credential_scope = str(getattr(exc, "credential_scope", "account") or "account")
    structured_failure = _classify_structured_failure_codes(codes, retry_after=retry_after, raw_detail=exc.body)

    # The real HTTP status owns the text/failure boundary. Structured fields
    # may refine the reason, but must never turn a non-400 response into text
    # or turn a real 400 response into an account failure.
    if status_code == 400:
        if structured_failure is not None and structured_failure.status_code == 400:
            return structured_failure
        return image_failure("invalid_image_input", raw_detail=exc.body)
    if credential_scope != "account":
        # CDN signed URLs / public pages: a 401/403/5xx says nothing about the
        # account credential, so never classify it as auth_invalid / account failure.
        if credential_scope == "signed_asset" and "download" in context_path:
            failure = image_failure("image_download_failed", retry_after=retry_after, raw_detail=exc.body)
        elif status_code == 429:
            failure = image_failure("upstream_rate_limited", retry_after=retry_after, raw_detail=exc.body)
        elif status_code in {408, 504}:
            failure = image_failure("upstream_connection_timeout", retry_after=retry_after, raw_detail=exc.body)
        elif status_code in {403, 423} or status_code >= 500:
            failure = image_failure("upstream_unavailable", retry_after=retry_after, raw_detail=exc.body)
        else:
            failure = image_failure("upstream_error", retry_after=retry_after, raw_detail=exc.body)
        return replace(
            failure,
            status_code=status_code,
            scope="delivery" if credential_scope == "signed_asset" else failure.scope,
            verify_account=False,
        )
    if status_code == 401:
        return image_failure("auth_invalid", retry_after=retry_after, raw_detail=exc.body)
    if structured_failure is not None and structured_failure.code == "conversation_not_ready" and status_code in {404, 409, 423}:
        return replace(structured_failure, status_code=status_code)
    if status_code == 429:
        is_file_upload = context_path in {"/backend-api/files", "image_upload"} or (
            context_path.startswith("/backend-api/files/") and context_path.endswith("/uploaded")
        )
        if is_file_upload:
            return image_failure("file_upload_throttled", retry_after=retry_after, raw_detail=exc.body)
    if structured_failure is not None and structured_failure.status_code == status_code:
        return structured_failure
    if status_code in {403, 423}:
        return replace(image_failure("upstream_unavailable", retry_after=retry_after, raw_detail=exc.body), status_code=status_code)
    if status_code == 429:
        return image_failure("upstream_rate_limited", retry_after=retry_after, raw_detail=exc.body)
    if status_code in {408, 504}:
        return image_failure("upstream_connection_timeout", retry_after=retry_after, raw_detail=exc.body)
    if status_code >= 500:
        return replace(image_failure("upstream_unavailable", retry_after=retry_after, raw_detail=exc.body), status_code=status_code)
    return replace(image_failure("upstream_error", retry_after=retry_after, raw_detail=exc.body), status_code=status_code)


def classify_image_exception(exc: BaseException, *, code: str | None = None) -> ImageFailure:
    """Resolve any exception raised during an image attempt to one failure."""
    failure = getattr(exc, "failure", None)
    if isinstance(failure, ImageFailure):
        return failure
    if isinstance(exc, UpstreamHTTPError):
        return classify_upstream_http_error(exc)
    structured_code = code or getattr(exc, "code", None)
    if normalize_failure_code(structured_code) == "upstream_error":
        # The generic default code carries no signal; let the text decide.
        text_failure = classify_image_error_text(str(exc))
        if text_failure is not None:
            return text_failure
    if isinstance(structured_code, str) and structured_code.strip() and is_known_failure_code(structured_code):
        resolved = image_failure(structured_code, raw_detail=str(exc))
        if resolved.code in {"upstream_text_reply", "no_image_generated", "content_policy_violation"}:
            resolved = resolved.with_public_detail(str(exc))
        return upstream_error_reply_failure(resolved, str(exc))
    text_failure = classify_image_error_text(str(exc))
    if text_failure is not None:
        return text_failure
    if isinstance(exc, (TimeoutError, curl_exceptions.Timeout)):
        return image_failure("upstream_connection_timeout", raw_detail=str(exc))
    if isinstance(
        exc,
        (
            ConnectionError,
            curl_exceptions.ConnectionError,
            curl_exceptions.ProxyError,
            curl_exceptions.SSLError,
            curl_exceptions.RequestException,
        ),
    ):
        return image_failure("upstream_connection_failed", raw_detail=str(exc))
    return image_failure("upstream_error", raw_detail=str(exc))


# --- structured message facts (role/status/end_turn/is_error/codes) ------------
# Ported from yukkcat/chatgpt2api (AGPL-3.0-only) ``services/image_failure.py``.

def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _message(value: Any) -> Mapping[str, Any]:
    item = _mapping(value)
    nested = item.get("message")
    if isinstance(nested, Mapping):
        return nested
    nested_value = _mapping(item.get("v"))
    nested = nested_value.get("message")
    if isinstance(nested, Mapping):
        return nested
    return item


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    content_map = _mapping(content)
    parts = content_map.get("parts")
    if isinstance(parts, list):
        parts_text = "".join(part for part in parts if isinstance(part, str)).strip()
        if parts_text:
            return parts_text
    return str(content_map.get("text") or "").strip()


TERMINAL_MESSAGE_STATUSES = frozenset({
    "complete",
    "completed",
    "done",
    "finished",
    "finished_successfully",
    "finished_partial_completion",
    "success",
    "succeeded",
})


def is_terminal_message_status(value: Any) -> bool:
    return str(value or "").strip().lower() in TERMINAL_MESSAGE_STATUSES


def _message_has_image_output(message: Mapping[str, Any]) -> bool:
    author = _mapping(message.get("author"))
    metadata = _mapping(message.get("metadata"))
    if str(author.get("role") or "").strip().lower() != "tool":
        return False
    if str(metadata.get("async_task_type") or "").strip().lower() != "image_gen":
        return False

    def has_pointer(value: Any) -> bool:
        if isinstance(value, Mapping):
            pointer = value.get("asset_pointer")
            if isinstance(pointer, str) and pointer.startswith(("file-service://", "sediment://")):
                return True
            return any(has_pointer(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_pointer(item) for item in value)
        return False

    return has_pointer(message.get("content"))


def classify_message_facts(
    *,
    role: str = "",
    content_type: str = "",
    status: str = "",
    end_turn: bool = False,
    is_error: bool = False,
    blocked: bool = False,
    has_image_output: bool = False,
    has_text: bool = False,
    turn_use_case: str = "",
    async_task_type: str = "",
    message_type: str = "",
    codes: Any = (),
    raw_detail: Any = None,
) -> ImageFailure | None:
    """Classify one upstream message from structured facts (not from free text)."""
    if has_image_output:
        return None
    if blocked:
        return image_failure("content_policy_violation", raw_detail=raw_detail)
    normalized_role = str(role or "").strip().lower()
    normalized_content_type = str(content_type or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    structured_codes = (
        tuple(codes)
        if isinstance(codes, (set, frozenset, list, tuple))
        else (codes,)
    )
    structured_failure = _classify_structured_failure_codes(
        (*structured_codes, normalized_status),
        raw_detail=raw_detail,
    )
    if structured_failure is not None:
        return structured_failure
    if normalized_role == "assistant" and normalized_content_type == "text" and (
        end_turn or is_terminal_message_status(normalized_status)
    ) and has_text:
        if text_says_image_quota_exhausted(raw_detail):
            return image_failure("image_quota_exhausted", raw_detail=raw_detail)
        return image_failure("upstream_text_reply", raw_detail=raw_detail).with_public_detail(raw_detail)
    if normalized_role == "tool" and normalized_content_type == "system_error":
        if text_says_image_quota_exhausted(raw_detail):
            return image_failure("image_quota_exhausted", raw_detail=raw_detail)
        return image_failure("image_tool_error", raw_detail=raw_detail)
    if is_error or normalized_status in FAILED_STATUSES:
        code = "upstream_rate_limited" if is_rate_limit_failure_code(normalized_status) else "upstream_error"
        return image_failure(code, raw_detail=raw_detail)
    return None


def classify_upstream_message(value: Any) -> ImageFailure | None:
    outer = _mapping(value)
    event_type = str(outer.get("type") or "").strip().lower()
    if event_type in {"response.failed", "response.incomplete"}:
        response = _mapping(outer.get("response"))
        failure = _classify_structured_failure_codes(
            _structured_codes(outer),
            raw_detail=outer,
        ) or image_failure("image_tool_error", raw_detail=outer)
        if event_type == "response.failed":
            failure = failure.with_public_detail(response.get("error") or outer.get("error"))
        return failure
    moderation = _mapping(outer.get("moderation_response"))
    message = _message(value)
    author = _mapping(message.get("author"))
    metadata = _mapping(message.get("metadata"))
    content = _mapping(message.get("content"))
    raw_detail = _message_text(message)
    return classify_message_facts(
        role=str(author.get("role") or "").strip().lower(),
        content_type=str(content.get("content_type") or "").strip().lower(),
        status=str(message.get("status") or metadata.get("status") or "").strip().lower(),
        end_turn=message.get("end_turn") is True,
        is_error=(
            message.get("is_error") is True
            or metadata.get("is_error") is True
            or bool(message.get("error"))
            or bool(metadata.get("error"))
        ),
        blocked=(
            message.get("blocked") is True
            or metadata.get("blocked") is True
            or (outer.get("type") == "moderation" and moderation.get("blocked") is True)
        ),
        has_image_output=_message_has_image_output(message),
        has_text=bool(raw_detail),
        codes=_structured_codes(message),
        raw_detail=raw_detail or value,
    )


def merge_message_failure(
    current: ImageFailure | None,
    candidate: ImageFailure | None,
) -> ImageFailure | None:
    """Merge failures from several messages of one turn by priority."""
    return merge_failures(current, candidate)


def classify_task_failure(task: Any) -> ImageFailure | None:
    image_message = _mapping(task).get("image_gen_message")
    if not isinstance(image_message, Mapping):
        return None
    failure = classify_upstream_message(image_message)
    if failure is None or failure.code != "image_tool_error":
        return failure
    message = _message(image_message)
    role = str(_mapping(message.get("author")).get("role") or "").strip().lower()
    content_type = str(_mapping(message.get("content")).get("content_type") or "").strip().lower()
    has_explicit_tool_code = any(
        FAILURE_CODE_ALIASES.get(code, code) == "image_tool_error"
        for code in _structured_codes(message)
    )
    if (role == "tool" and content_type == "system_error") or has_explicit_tool_code:
        return failure
    # Task probes often expose only status=failed/is_error: real failure evidence,
    # but not a specific image-tool classification.
    return image_failure("upstream_error", raw_detail=failure.raw_detail)


def _current_conversation_turn(data: Any) -> list[Mapping[str, Any]]:
    """Messages after the last user message of the conversation document."""
    data_map = _mapping(data)
    mapping = _mapping(data_map.get("mapping"))
    messages: list[Mapping[str, Any]] = []
    current_node = str(data_map.get("current_node") or "").strip()
    if current_node and current_node in mapping:
        lineage: list[Mapping[str, Any]] = []
        visited: set[str] = set()
        node_id = current_node
        while node_id and node_id not in visited:
            visited.add(node_id)
            node = _mapping(mapping.get(node_id))
            if not node:
                break
            message = _message(node)
            if message:
                lineage.append(message)
            node_id = str(node.get("parent") or "").strip()
        messages = list(reversed(lineage))
    else:
        ordered: list[tuple[float, str, Mapping[str, Any]]] = []
        for raw_node_id, node in mapping.items():
            message = _message(node)
            if not message:
                continue
            try:
                create_time = float(message.get("create_time") or 0.0)
            except (TypeError, ValueError):
                create_time = 0.0
            ordered.append((create_time, str(raw_node_id), message))
        ordered.sort(key=lambda item: (item[0], item[1]))
        messages = [message for _t, _n, message in ordered]
    last_user_index = -1
    for message_index, message in enumerate(messages):
        if str(_mapping(message.get("author")).get("role") or "").strip().lower() == "user":
            last_user_index = message_index
    return messages[last_user_index + 1:]


def terminal_assistant_text(data: Any) -> str:
    for message in reversed(_current_conversation_turn(data)):
        role = str(_mapping(message.get("author")).get("role") or "").strip().lower()
        content_type = str(_mapping(message.get("content")).get("content_type") or "").strip().lower()
        status = str(message.get("status") or _mapping(message.get("metadata")).get("status") or "").strip().lower()
        if (
            role == "assistant"
            and content_type in {"text", "code"}
            and (message.get("end_turn") is True or is_terminal_message_status(status))
        ):
            return _message_text(message)
    return ""


def classify_conversation_failure(data: Any) -> ImageFailure | None:
    """Failure of the current turn of a conversation document, None when it has an image."""
    current_turn = _current_conversation_turn(data)
    if any(_message_has_image_output(message) for message in current_turn):
        return None
    failure: ImageFailure | None = None
    for message in current_turn:
        failure = merge_message_failure(failure, classify_upstream_message(message))
    return failure


def extract_message_facts(value: Any) -> dict[str, Any]:
    """Collect role/content_type/status/end_turn/is_error/codes from an SSE event or message."""
    facts: dict[str, Any] = {}

    def add_codes(candidate_value: Any) -> None:
        candidates = _structured_codes(candidate_value)
        if isinstance(candidate_value, str) and candidate_value.strip():
            candidates.add(candidate_value.strip().lower())
        if candidates:
            facts["codes"] = set(facts.get("codes") or ()).union(candidates)

    def record_content(content: Mapping[str, Any]) -> None:
        if content.get("content_type") not in (None, ""):
            facts["content_type"] = str(content.get("content_type") or "").strip().lower()
        parts = content.get("parts")
        if isinstance(parts, list) and any(isinstance(part, str) and bool(part.strip()) for part in parts):
            facts["has_text"] = True
        if isinstance(content.get("text"), str) and str(content.get("text") or "").strip():
            facts["has_text"] = True

    def record_metadata(metadata: Mapping[str, Any]) -> None:
        if metadata.get("is_error") is True or bool(metadata.get("error")):
            facts["is_error"] = True
        if metadata.get("blocked") is True:
            facts["blocked"] = True
        if metadata.get("status") not in (None, ""):
            facts["status"] = str(metadata.get("status") or "").strip().lower()
        for name in ("turn_use_case", "async_task_type", "message_type"):
            if metadata.get(name) not in (None, ""):
                facts[name] = str(metadata.get(name) or "").strip().lower()
        add_codes(metadata)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            message = item.get("message")
            if isinstance(message, Mapping):
                visit(message)
            author = _mapping(item.get("author"))
            content = _mapping(item.get("content"))
            metadata = _mapping(item.get("metadata"))
            if author or content:
                message_id = item.get("id") or item.get("message_id")
                if isinstance(message_id, str) and message_id.strip():
                    facts["message_id"] = message_id.strip()
            if author.get("role") not in (None, ""):
                facts["role"] = str(author.get("role") or "").strip().lower()
            record_content(content)
            record_metadata(metadata)
            if item.get("status") not in (None, ""):
                facts["status"] = str(item.get("status") or "").strip().lower()
            if isinstance(item.get("end_turn"), bool):
                facts["end_turn"] = item["end_turn"]
            if item.get("is_error") is True or bool(item.get("error")):
                facts["is_error"] = True
            if item.get("blocked") is True:
                facts["blocked"] = True
            add_codes({
                key: item.get(key)
                for key in ("code", "error_code", "failure_code")
                if item.get(key) not in (None, "")
            })
            for name in ("turn_use_case", "async_task_type", "message_type"):
                candidate = item.get(name)
                if candidate in (None, ""):
                    candidate = metadata.get(name)
                if candidate not in (None, ""):
                    facts[name] = str(candidate).strip().lower()
            path = str(item.get("p") or "").strip().lower()
            patch_value = item.get("v")
            if path.endswith("/message/metadata") and isinstance(patch_value, Mapping):
                record_metadata(patch_value)
            elif path.endswith("/message/content") and isinstance(patch_value, Mapping):
                record_content(patch_value)
            elif path.endswith("/message/author/role") and isinstance(patch_value, str):
                facts["role"] = patch_value.strip().lower()
            elif path.endswith(("/message/id", "/message/message_id")) and isinstance(patch_value, str):
                facts["message_id"] = patch_value.strip()
            elif path.endswith("/message/content/content_type") and isinstance(patch_value, str):
                facts["content_type"] = patch_value.strip().lower()
            elif path.endswith("/message/content/text") and isinstance(patch_value, str) and patch_value.strip():
                facts["has_text"] = True
            elif path.endswith("/message/content/parts") and isinstance(patch_value, list):
                if any(isinstance(part, str) and part.strip() for part in patch_value):
                    facts["has_text"] = True
            elif "/message/content/parts/" in path and isinstance(patch_value, str) and patch_value.strip():
                facts["has_text"] = True
            elif path.endswith(("/message/status", "/message/metadata/status")) and isinstance(patch_value, str):
                facts["status"] = patch_value.strip().lower()
            elif path.endswith("/message/end_turn") and isinstance(patch_value, bool):
                facts["end_turn"] = patch_value
            elif path.endswith(("/message/is_error", "/message/metadata/is_error")) and patch_value is True:
                facts["is_error"] = True
            elif path.endswith(("/message/error", "/message/metadata/error")) and bool(patch_value):
                facts["is_error"] = True
                add_codes(patch_value)
            elif path.endswith(("/message/blocked", "/message/metadata/blocked")) and patch_value is True:
                facts["blocked"] = True
            elif "/message/" in path and path.rsplit("/", 1)[-1] in {"code", "error_code", "failure_code", "type"}:
                add_codes(patch_value)
            else:
                for name in ("turn_use_case", "async_task_type", "message_type"):
                    if path.endswith(f"/message/metadata/{name}") and isinstance(patch_value, str):
                        facts[name] = patch_value.strip().lower()
                        break
            for key, child in item.items():
                if key not in {"message", "author", "content", "metadata"} and isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return facts


def structured_upstream_codes(value: Any) -> set[str]:
    """Lower-cased structured error codes found anywhere in an upstream payload."""
    return _structured_codes(value)

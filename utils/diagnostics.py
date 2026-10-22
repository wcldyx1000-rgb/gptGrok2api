from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


EXCEPTION_DIAGNOSTIC_ATTRS: tuple[tuple[str, str], ...] = (
    ("code", "error_code"),
    ("raw_error", "raw_error"),
    ("upstream_error", "upstream_error"),
    ("upstream_error_type", "upstream_error_type"),
    ("upstream_request_id", "upstream_request_id"),
    ("can_resume_poll", "can_resume_poll"),
    ("raw_upstream_message", "raw_upstream_message"),
    ("raw_upstream_message_len", "raw_upstream_message_len"),
    ("raw_upstream_message_truncated", "raw_upstream_message_truncated"),
    ("upstream_message_preview", "upstream_message_preview"),
    ("upstream_message_len", "upstream_message_len"),
    ("upstream_message_truncated", "upstream_message_truncated"),
    ("tool_invoked", "tool_invoked"),
    ("terminal_message", "terminal_message"),
    ("blocked", "blocked"),
    ("poll_attempts", "poll_attempts"),
    ("poll_timeout_secs", "poll_timeout_secs"),
    ("stream_timeout_secs", "stream_timeout_secs"),
    ("stream_timeout_followup", "stream_timeout_followup"),
    ("last_task_error", "last_task_error"),
    ("last_conversation_snapshot", "last_conversation_snapshot"),
)

_DIAGNOSTIC_SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "authorization",
    "proxy_authorization",
    "proxyauthorization",
    "password",
    "proxy_password",
    "proxypassword",
    "secret_key",
    "secretkey",
    "api_key",
    "apikey",
    "codex_access_token",
    "codex_refresh_token",
    "outlook_password",
    "outlook_refresh_token",
}

_DIAGNOSTIC_PROXY_KEYS = {
    "proxy",
    "proxy_url",
    "proxyurl",
}

_NON_SECRET_PROXY_VALUES = {
    "default",
    "direct",
    "inherit",
    "system",
}


def sanitize_diagnostic_text(
    value: object,
    *,
    sensitive_values: Iterable[object] = (),
    proxy_values: Iterable[object] = (),
    limit: int = 0,
) -> str:
    """Remove credentials from diagnostics before storage or projection.

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    text = str(value or "").strip()
    if not text:
        return ""
    proxies = sorted(
        {
            proxy
            for item in proxy_values
            if (proxy := str(item or "").strip())
            and proxy.casefold() not in _NON_SECRET_PROXY_VALUES
        },
        key=len,
        reverse=True,
    )
    secrets = sorted(
        {str(item or "").strip() for item in sensitive_values if str(item or "").strip()},
        key=len,
        reverse=True,
    )
    for proxy in proxies:
        text = text.replace(proxy, "[proxy]")
    for secret in secrets:
        text = text.replace(secret, "[credential]")
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@",
        r"\1***@",
        text,
    )
    text = re.sub(
        r"(?i)(?<![a-z0-9])((?:\[[0-9a-f:.]+\]|localhost|"
        r"\d{1,3}(?:\.\d{1,3}){3}|"
        r"(?=[a-z0-9.-]*[a-z])[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?):\d{1,5}):"
        r"[^:\s,;]+:[^\s,;]+",
        r"\1:***:***",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:Proxy-)?Authorization\s*:\s*(?:Basic|Bearer))\s+"
        r"[A-Za-z0-9._~+/=-]+",
        r"\1 [credential]",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [credential]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:(?:proxy[_-]?)?(?:user(?:name)?|pass(?:word)?)|"
        r"access_token|refresh_token|id_token)=)[^&#\s]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)((?<![a-z0-9_])[\"']?(?:access[_-]?token|refresh[_-]?token|"
        r"id[_-]?token|password)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'&}]+",
        r"\1[credential]",
        text,
    )
    return text if limit <= 0 or len(text) <= limit else f"{text[:limit]}..."


def _collect_diagnostic_values(
    value: object,
    sensitive_values: list[object],
    proxy_values: list[object],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in _DIAGNOSTIC_SECRET_KEYS and item not in (None, ""):
                sensitive_values.append(item)
            elif normalized_key in _DIAGNOSTIC_PROXY_KEYS and item not in (None, ""):
                proxy_values.append(item)
            _collect_diagnostic_values(item, sensitive_values, proxy_values)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_diagnostic_values(item, sensitive_values, proxy_values)


def _scrub_diagnostic_value(
    value: object,
    sensitive_values: list[object],
    proxy_values: list[object],
) -> object:
    if isinstance(value, dict):
        result: dict[object, object] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in _DIAGNOSTIC_SECRET_KEYS and item not in (None, ""):
                result[key] = "[credential]"
            else:
                result[key] = _scrub_diagnostic_value(item, sensitive_values, proxy_values)
        return result
    if isinstance(value, list):
        return [_scrub_diagnostic_value(item, sensitive_values, proxy_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_diagnostic_value(item, sensitive_values, proxy_values) for item in value)
    if isinstance(value, str):
        return sanitize_diagnostic_text(value, sensitive_values=sensitive_values, proxy_values=proxy_values)
    return value


def scrub_diagnostic_value(
    value: object,
    *,
    sensitive_values: Iterable[object] = (),
    proxy_values: Iterable[object] = (),
) -> object:
    """递归脱敏：凭据字段整体替换为 [credential]，字符串里出现的已知凭据/代理密码被擦除。

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    collected_sensitive = list(sensitive_values)
    collected_proxy = list(proxy_values)
    _collect_diagnostic_values(value, collected_sensitive, collected_proxy)
    return _scrub_diagnostic_value(value, collected_sensitive, collected_proxy)


def scrub_account_error_fields(
    account: dict,
    extra_secrets: Iterable[object] = (),
    fields: Iterable[str] = (
        "last_remote_check_error",
        "last_refresh_error",
        "last_token_refresh_error",
        "last_relogin_error",
        "relogin_reason",
    ),
) -> None:
    """把账号错误字段里掺入的本账号凭据（AT/RT/ID Token/Outlook 凭据）和代理密码擦掉（原地修改）。"""
    secrets = [
        account.get(key)
        for key in ("access_token", "refresh_token", "id_token", "outlook_refresh_token", "outlook_password")
    ]
    secrets.extend(extra_secrets)
    secrets = [str(value).strip() for value in secrets if str(value or "").strip()]
    proxies = [account.get("proxy")]
    for key in fields:
        value = account.get(key)
        if isinstance(value, str) and value:
            account[key] = sanitize_diagnostic_text(value, sensitive_values=secrets, proxy_values=proxies)


_IMPORT_SECRET_KEYS = {
    "accesstoken", "apikey", "authorization", "idtoken", "password", "proxyauthorization",
    "proxypassword", "proxyuser", "proxyusername", "refreshtoken", "secretkey", "token",
}
_IMPORT_PROXY_KEYS = {"proxy", "proxyurl"}
_IMPORT_CREDENTIAL_CONTAINERS = {"credentials", "credential", "tokens", "auth"}


def _import_key(value: object) -> str:
    return str(value or "").strip().casefold().replace("_", "").replace("-", "")


def _append_import_text(values: list[str], value: object) -> None:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_import_text(values, item)
        return
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def _collect_import_values(value: object, sensitive_values: list[str], proxy_values: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _import_key(key)
            if normalized_key in _IMPORT_SECRET_KEYS:
                _append_import_text(sensitive_values, item)
            elif normalized_key in _IMPORT_PROXY_KEYS:
                _append_import_text(proxy_values, item)
            if isinstance(item, (Mapping, list, tuple)):
                _collect_import_values(item, sensitive_values, proxy_values)
            elif normalized_key in _IMPORT_CREDENTIAL_CONTAINERS and isinstance(item, str):
                try:
                    decoded = json.loads(item)
                except (TypeError, ValueError):
                    continue
                _collect_import_values(decoded, sensitive_values, proxy_values)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_import_values(item, sensitive_values, proxy_values)


def collect_import_diagnostic_values(*sources: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """收集远程导入（CPA / Sub2API）过程中已知的凭据与代理，用来擦导入错误文本。

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    sensitive_values: list[str] = []
    proxy_values: list[str] = []
    for source in sources:
        _collect_import_values(source, sensitive_values, proxy_values)
    return tuple(sensitive_values), tuple(proxy_values)


def sanitize_import_errors(
    errors: object,
    *,
    sensitive_values: Iterable[object] = (),
    proxy_values: Iterable[object] = (),
) -> list[dict]:
    """导入任务的错误列表逐条脱敏（name / error 两个字段）。"""
    if not isinstance(errors, list):
        return []
    sensitive = list(sensitive_values)
    proxies = list(proxy_values)
    result: list[dict] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        cleaned = dict(item)
        for key in ("name", "error"):
            if isinstance(cleaned.get(key), str):
                cleaned[key] = sanitize_diagnostic_text(cleaned[key], sensitive_values=sensitive, proxy_values=proxies)
        result.append(cleaned)
    return result


def diagnostic_excerpt(value: object, limit: int = 1000) -> str:
    """Return a bounded diagnostic string for logs and upstream error details."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "...[truncated]"


def exception_diagnostic_fields(
    exc: Exception,
    *,
    include_status_code: bool = False,
    string_limit: int = 4000,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    attrs = EXCEPTION_DIAGNOSTIC_ATTRS
    if include_status_code:
        attrs = (("status_code", "status_code"), *attrs)
    for attr, key in attrs:
        if not hasattr(exc, attr):
            continue
        value = getattr(exc, attr)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            value = diagnostic_excerpt(value, string_limit)
        fields[key] = value
    if "raw_upstream_message" not in fields and hasattr(exc, "last_assistant_text"):
        value = getattr(exc, "last_assistant_text")
        if value not in (None, ""):
            fields["raw_upstream_message"] = (
                diagnostic_excerpt(value, string_limit)
                if isinstance(value, str)
                else value
            )
    followup = fields.get("stream_timeout_followup")
    if isinstance(followup, dict) and "diagnosis" not in fields:
        fields["diagnosis"] = followup
    return fields

from __future__ import annotations

import copy
from dataclasses import dataclass
import os
import re
import threading
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

from services.json_file import read_json_object, write_json_file
from services.storage.base import StorageBackend
from utils.locale_env import normalize_locale_fields

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_STATE_FILE = DATA_DIR / "backup_state.json"

DEFAULT_BACKUP_INCLUDE = {
    "config": True,
    "register": True,
    "cpa": True,
    "sub2api": True,
    "logs": True,
    "dashboard_metrics": True,
    "image_tasks": True,
    "editable_files": True,
    "accounts_snapshot": True,
    "auth_keys_snapshot": True,
    "images": False,
}

DEFAULT_IMAGE_STORAGE = {
    "enabled": False,
    "mode": "local",
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_root_path": "chatgpt2api/images",
    "public_base_url": "",
}

DEFAULT_GENBOX_PUSH = {
    "enabled": False,
    "base_url": "",
    "source_id": "",
    "push_key": "",
    "timeout_secs": 20,
    "auto_push_after_studio": False,
}

IMAGE_UPSCALE_ENGINES = ("pillow_lanczos", "sharp_lanczos3")

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

DEFAULT_IMAGE_ERROR_MESSAGES = {
    "fallback": "图片生成请求失败，请稍后重试。",
    "quota": "图片账号额度已用完，请稍后再试或联系管理员。",
    "no_account": "当前图片账号暂不可用，可能是账号池、并发或上游波动，请稍后重试。",
    "local_busy": "当前没有可用的图片账号或账号并发已满，请稍后重试。",
    "unsupported_model": "当前模型不支持图片生成，请检查 model 参数。",
    "poll_timeout": "图片任务暂未返回结果，可能仍在排队或上游处理较慢，请重试。",
    "stream_interrupted": "图片生成连接中断，可能是上游服务繁忙或网络波动，请重试。",
    "connection_failed": "连接上游图片服务失败，可能是网络或代理波动，请重试。",
    "connection_timeout": "连接上游图片服务超时，请稍后重试。",
    "token_invalid": "图片生成账号状态异常，请稍后重试。",
    "text_reply": "上游返回了文本说明，未生成图片。请调整提示词或重试。",
}

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    # 未单独配置地区环境的出口（含直连）使用的默认时区 / 语言；留空沿用 Asia/Shanghai + zh-CN。
    "timezone": "",
    "language": "",
    # 代理组节点连续失败达到阈值后临时停用，冷却后自动恢复（不改动代理配置）。
    "node_fail_threshold": 1,
    "node_cooldown_sec": 600,
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
        # FlareSolverr 按出口复用浏览器会话：会话上限、空闲回收秒数；凭证用掉这个比例的有效期后台提前换新。
        "max_sessions": 4,
        "session_idle_sec": 600,
        "early_refresh_ratio": 0.8,
    },
}

DEFAULT_THIRD_PARTY_APPS = {
    "infinite_canvas": {
        "enabled": False,
        "url": "https://canvas.best",
    },
}


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


# ---- 敏感配置脱敏 ----------------------------------------------------------
# 返回给前端的配置不得含明文凭据：敏感字段置空并附带 has_<字段> 布尔；
# 保存时字段为空表示沿用已存值，只有显式 clear_<字段>=true 才清除。
# 内部服务（备份 / WebDAV / 代理选择等）仍通过 get_backup_settings() 等内部方法拿明文。
MASKED_PLACEHOLDER = "********"
PROXY_PASSWORD_MASK = "***"

# 配置段 -> 需要脱敏的字段（genbox_push.push_key、cf_cookies 另有专用实现）
SECRET_SETTING_FIELDS: dict[str, tuple[str, ...]] = {
    "backup": ("secret_access_key", "passphrase"),
    "image_storage": ("webdav_password",),
    "ai_review": ("api_key",),
}

# 顶层历史/误写入的敏感键：永远不对外返回。
_TOP_LEVEL_SECRET_KEYS = ("webdav_password", "webdav_secret", "api_key", "secret_access_key", "passphrase")

_PROXY_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^/\s:@,;]*):(?P<pw>[^/\s,;]*)@(?P<host>[^/\s,;?#@]+)"
)


def mask_proxy_credentials(text: str) -> str:
    """socks5h://user:pass@host:1080 -> socks5h://user:***@host:1080（支持多行 / 逗号分隔的列表）。"""
    if "@" not in text or "://" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        if match.group("pw") == PROXY_PASSWORD_MASK:
            return match.group(0)
        return f"{match.group('scheme')}{match.group('user')}:{PROXY_PASSWORD_MASK}@{match.group('host')}"

    return _PROXY_CREDENTIAL_RE.sub(_replace, text)


def _walk_strings(value: object, transform):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_walk_strings(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: _walk_strings(item, transform) for key, item in value.items()}
    return value


def _collect_proxy_credentials(value: object, mapping: dict[str, str]) -> None:
    if isinstance(value, str):
        if "@" in value and "://" in value:
            for match in _PROXY_CREDENTIAL_RE.finditer(value):
                original = match.group(0)
                masked = mask_proxy_credentials(original)
                if masked != original:
                    mapping.setdefault(masked, original)
    elif isinstance(value, list):
        for item in value:
            _collect_proxy_credentials(item, mapping)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_proxy_credentials(item, mapping)


def restore_masked_proxy_credentials(value: object, stored: object) -> object:
    """把提交内容里被打码的 `user:***@host` 还原为已保存的真实凭据；找不到对应项时原样保留。"""
    mapping: dict[str, str] = {}
    _collect_proxy_credentials(stored, mapping)
    if not mapping:
        return value
    marker = f":{PROXY_PASSWORD_MASK}@"

    def _restore(text: str) -> str:
        if marker not in text:
            return text
        return _PROXY_CREDENTIAL_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)

    return _walk_strings(value, _restore)


def _mask_secret_sections(data: dict[str, object], raw_sections: dict[str, object]) -> None:
    """data 中的敏感段置空并附带 has_*；raw_sections 提供未脱敏的来源（用于判断是否已配置）。"""
    for section, fields in SECRET_SETTING_FIELDS.items():
        source = raw_sections.get(section)
        if not isinstance(source, dict):
            source = {}
        public = dict(data.get(section) if isinstance(data.get(section), dict) else source)
        for field in fields:
            public[f"has_{field}"] = bool(str(source.get(field) or "").strip())
            public[field] = ""
            public.pop(f"clear_{field}", None)
        data[section] = public


def _merge_secret_section(incoming: object, stored: object, fields: tuple[str, ...]) -> object:
    """保存时合并敏感字段：空值/占位符沿用已存值，clear_<字段> 为真才清除。"""
    if not isinstance(incoming, dict):
        return incoming
    merged = dict(incoming)
    previous = stored if isinstance(stored, dict) else {}
    for field in fields:
        clear = _normalize_bool(merged.pop(f"clear_{field}", False), False)
        merged.pop(f"has_{field}", None)
        if clear:
            merged[field] = ""
            continue
        value = str(merged.get(field) or "").strip()
        if not value or value == MASKED_PLACEHOLDER:
            merged[field] = str(previous.get(field) or "")
    return merged


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _clamp_finite_float(value: object, default: float, minimum: float, maximum: float) -> float:
    """限定在 [minimum, maximum]；非数字 / NaN / inf 回落默认值。"""
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return min(maximum, max(minimum, number))


def _normalize_ratio(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if normalized != normalized:  # NaN
        return default
    return min(maximum, max(minimum, normalized))


def _normalize_backup_include(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    normalized = dict(DEFAULT_BACKUP_INCLUDE)
    for key in normalized:
        normalized[key] = _normalize_bool(source.get(key), normalized[key])
    return normalized


MAX_BACKUP_INTERVAL_MINUTES = 525_600  # 一年


def _normalize_backup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "provider": "cloudflare_r2",
        "account_id": str(source.get("account_id") or "").strip(),
        "access_key_id": str(source.get("access_key_id") or "").strip(),
        "secret_access_key": str(source.get("secret_access_key") or "").strip(),
        "bucket": str(source.get("bucket") or "").strip(),
        "prefix": str(source.get("prefix") or "backups").strip().strip("/") or "backups",
        "interval_minutes": min(MAX_BACKUP_INTERVAL_MINUTES, _normalize_positive_int(source.get("interval_minutes"), 360, 1)),
        "rotation_keep": _normalize_positive_int(source.get("rotation_keep"), 10, 0),
        "encrypt": _normalize_bool(source.get("encrypt"), False),
        "passphrase": str(source.get("passphrase") or "").strip(),
        "include": _normalize_backup_include(source.get("include")),
    }


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "last_started_at": str(source.get("last_started_at") or "").strip() or None,
        "last_finished_at": str(source.get("last_finished_at") or "").strip() or None,
        "last_status": str(source.get("last_status") or "idle").strip() or "idle",
        "last_error": str(source.get("last_error") or "").strip() or None,
        "last_object_key": str(source.get("last_object_key") or "").strip() or None,
    }


def _normalize_image_storage_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    mode = str(source.get("mode") or "local").strip().lower()
    if mode not in {"local", "webdav", "both"}:
        mode = "local"
    enabled = _normalize_bool(source.get("enabled"), False)
    if not enabled:
        mode = "local"
    root_path = str(source.get("webdav_root_path") or DEFAULT_IMAGE_STORAGE["webdav_root_path"]).strip().strip("/")
    return {
        "enabled": enabled,
        "mode": mode,
        "webdav_url": str(source.get("webdav_url") or "").strip().rstrip("/"),
        "webdav_username": str(source.get("webdav_username") or "").strip(),
        "webdav_password": str(source.get("webdav_password") or "").strip(),
        "webdav_root_path": root_path or str(DEFAULT_IMAGE_STORAGE["webdav_root_path"]),
        "public_base_url": str(source.get("public_base_url") or "").strip().rstrip("/"),
    }


def _normalize_genbox_base_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    if parsed.path.rstrip("/") not in {"", "/api/sync/push"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _normalize_genbox_push_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    try:
        timeout = int(source.get("timeout_secs") or DEFAULT_GENBOX_PUSH["timeout_secs"])
    except (TypeError, ValueError):
        timeout = int(DEFAULT_GENBOX_PUSH["timeout_secs"])
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "base_url": _normalize_genbox_base_url(source.get("base_url")),
        "source_id": str(source.get("source_id") or "").strip(),
        "push_key": str(source.get("push_key") or "").strip(),
        "timeout_secs": max(5, min(120, timeout)),
        "auto_push_after_studio": _normalize_bool(source.get("auto_push_after_studio"), False),
    }


def _validate_genbox_push_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    if not _normalize_genbox_base_url(settings.get("base_url")):
        raise ValueError("启用 GenBox 推送后必须填写有效的 HTTP(S) 地址")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(settings.get("source_id") or "").strip()):
        raise ValueError("GenBox 来源 ID 格式无效")
    if not str(settings.get("push_key") or "").strip():
        raise ValueError("启用 GenBox 推送后必须填写 Push Key")


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


def _normalize_status_codes(value: object) -> list[int]:
    items = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            status = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= status <= 599 and status not in normalized:
            normalized.append(status)
    if not normalized:
        return list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])
    return normalized


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    user_agent = str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip()
    browser = str(clearance_source.get("browser") or default_clearance["browser"]).strip()

    existing_clearance_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_cf_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_clearance_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_cf_clearance

    try:
        locale_timezone, locale_language = normalize_locale_fields(source.get("timezone"), source.get("language"))
    except ValueError:
        locale_timezone, locale_language = "", ""

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "skip_ssl_verify": _normalize_bool(
            source.get("skip_ssl_verify"),
            bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"]),
        ),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "timezone": locale_timezone,
        "language": locale_language,
        "node_fail_threshold": min(
            20,
            _normalize_positive_int(source.get("node_fail_threshold"), int(DEFAULT_PROXY_RUNTIME["node_fail_threshold"]), 1),
        ),
        "node_cooldown_sec": min(
            86400,
            _normalize_positive_int(source.get("node_cooldown_sec"), int(DEFAULT_PROXY_RUNTIME["node_cooldown_sec"]), 10),
        ),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or str(default_clearance["user_agent"]),
            "browser": browser or str(default_clearance["browser"]),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(
                clearance_source.get("timeout_sec"),
                int(default_clearance["timeout_sec"]),
                1,
            ),
            "refresh_interval": _normalize_positive_int(
                clearance_source.get("refresh_interval"),
                int(default_clearance["refresh_interval"]),
                60,
            ),
            "warm_up_on_start": _normalize_bool(
                clearance_source.get("warm_up_on_start"),
                bool(default_clearance["warm_up_on_start"]),
            ),
            "max_sessions": min(
                16,
                _normalize_positive_int(clearance_source.get("max_sessions"), int(default_clearance["max_sessions"]), 1),
            ),
            "session_idle_sec": _normalize_positive_int(
                clearance_source.get("session_idle_sec"),
                int(default_clearance["session_idle_sec"]),
                30,
            ),
            "early_refresh_ratio": _normalize_ratio(
                clearance_source.get("early_refresh_ratio"),
                float(default_clearance["early_refresh_ratio"]),
                0.1,
                0.95,
            ),
        },
    }


def _normalize_third_party_apps_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    canvas_source = source.get("infinite_canvas") if isinstance(source.get("infinite_canvas"), dict) else {}
    return {
        "infinite_canvas": {
            "enabled": _normalize_bool(canvas_source.get("enabled"), False),
            "url": str(canvas_source.get("url") or DEFAULT_THIRD_PARTY_APPS["infinite_canvas"]["url"]).strip(),
        },
    }


def _legacy_basic_from_settings(value: object, settings: dict[str, object]) -> dict[str, object]:
    source = dict(value) if isinstance(value, dict) else {}
    source["proxy"] = str(settings.get("proxy") or "").strip()
    source["base_url"] = str(settings.get("base_url") or "").strip().rstrip("/")
    # image_expire_hours 是历史遗留字段，实际含义为“天”，仅为兼容旧版本回写，权威配置是 image_retention_hours。
    try:
        hours = int(settings.get("image_retention_hours") or DEFAULT_RETENTION_HOURS)
        source["image_expire_hours"] = max(1, -(-hours // 24))
    except (TypeError, ValueError):
        source["image_expire_hours"] = max(1, DEFAULT_RETENTION_HOURS // 24)
    return source


DEFAULT_RETENTION_HOURS = 720  # 30 天，与改为按小时之前的默认值一致
DEFAULT_IMAGE_MIN_FREE_MB = 500
MAX_RETENTION_HOURS = 24 * 3650


def _clamp_retention_hours(value: object, default: int = DEFAULT_RETENTION_HOURS) -> int:
    try:
        return min(MAX_RETENTION_HOURS, max(1, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _migrate_retention_settings(data: dict[str, object]) -> None:
    """旧版按天保留期迁移为按小时：新键存在时以新键为准，旧键换算（天 x 24）后移除。"""
    basic = data.get("basic")
    legacy_image_days = data.get("image_retention_days")
    if legacy_image_days is None and isinstance(basic, dict):
        legacy_image_days = basic.get("image_expire_hours")
    for name, legacy_days in (
        ("image_retention_hours", legacy_image_days),
        ("log_retention_hours", data.get("log_retention_days")),
    ):
        if data.get(name) in (None, ""):
            if legacy_days not in (None, ""):
                try:
                    data[name] = _clamp_retention_hours(float(legacy_days) * 24)
                except (TypeError, ValueError):
                    pass
        else:
            data[name] = _clamp_retention_hours(data[name])
    data.pop("image_retention_days", None)
    data.pop("log_retention_days", None)


def _promote_legacy_basic_settings(data: dict[str, object]) -> dict[str, object]:
    next_data = dict(data or {})
    _migrate_retention_settings(next_data)
    basic = next_data.get("basic")
    if not isinstance(basic, dict):
        return next_data
    if "proxy" not in next_data and "proxy" in basic:
        next_data["proxy"] = str(basic.get("proxy") or "").strip()
    if "base_url" not in next_data and "base_url" in basic:
        next_data["base_url"] = str(basic.get("base_url") or "").strip().rstrip("/")
    return next_data


def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    if not str(settings.get("webdav_url") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
    if not str(settings.get("webdav_password") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


# 设置页可写的顶层键；也是 revision 的跟踪范围（后台任务写 proxy_groups 等其它键不会让设置页误报冲突）。
SETTINGS_UPDATE_KEYS = {
    "proxy",
    "fallback_proxy",
    "proxy_runtime",
    "base_url",
    "refresh_account_interval_minute",
    "image_retention_hours",
    "log_retention_hours",
    "image_min_free_mb",
    # 旧版按天的键仍被接受，由 config 迁移为小时（天 x 24）。
    "image_retention_days",
    "log_retention_days",
    "image_poll_timeout_secs",
    "image_stream_timeout_secs",
    "console_request_timeout_secs",
    "image_queue_limit",
    "account_processing_concurrency",
    "image_poll_interval_secs",
    "image_poll_initial_wait_secs",
    "image_account_concurrency",
    "image_account_cooldown_secs",
    "image_preflight_fresh_secs",
    "image_stream_post_tool_wait_secs",
    "image_poll_adaptive",
    "image_warm_enabled",
    "chat_sentinel_vm_enabled",
    "chat_temporary_mode",
    "image_stream_direct_resolve",
    "image_drop_extra_variants",
    "image_upscale_enabled",
    "image_account_retry_count",
    "image_account_retry_enabled",
    "image_max_account_attempts",
    "account_auto_pause_strikes",
    "account_auto_pause_hours",
    "new_account_cooldown_hours",
    "new_account_ramp_days",
    "new_account_ramp_daily_images",
    "text_account_concurrency",
    "account_failure_cooldown_secs",
    "account_relogin_enabled",
    "account_relogin_interval_secs",
    "account_relogin_concurrency",
    "image_parallel_generation",
    "image_remove_conversation_after_result",
    "image_error_friendly_enabled",
    "image_error_messages",
    "image_settle_enabled",
    "image_check_before_hit_enabled",
    "image_settle_secs",
    "image_timeout_retry_secs",
    "auto_remove_invalid_accounts",
    "auto_remove_rate_limited_accounts",
    "log_levels",
    "global_system_prompt",
    "sensitive_words",
    "ai_review",
    "image_generation",
    "quota_limits",
    "runtime_capacity",
    "image_storage",
    "genbox_push",
    "image_upscale_engine",
    "backup",
    "chat_completion_cache",
    "third_party_apps",
}


class SettingsRevisionConflictError(Exception):
    """乐观并发：客户端提交的 revision 与当前不一致。"""

    def __init__(self, current_revision: int, expected_revision: int):
        super().__init__("设置已在别处被修改，请重新加载后再保存")
        self.current_revision = current_revision
        self.expected_revision = expected_revision


REVISION_KEY = "_revision"


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = _promote_legacy_basic_settings(self._load())
        self._loaded_mtime_ns = self._config_mtime_ns()
        self._storage_backend: StorageBackend | None = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return read_json_object(self.path, name="config.json")

    def _config_mtime_ns(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return 0

    def reload_if_changed(self) -> None:
        with self._lock:
            current_mtime_ns = self._config_mtime_ns()
            if current_mtime_ns and current_mtime_ns != self._loaded_mtime_ns:
                self.data = _promote_legacy_basic_settings(self._load())
                self._loaded_mtime_ns = current_mtime_ns

    @property
    def revision(self) -> int:
        """设置单调版本号（持久化在 config.json 的内部字段 `_revision`，每次保存 +1）。"""
        with self._lock:
            self.reload_if_changed()
            try:
                return max(0, int(self.data.get(REVISION_KEY) or 0))
            except (TypeError, ValueError):
                return 0

    def _save(self) -> None:
        write_json_file(self.path, self.data)
        self._loaded_mtime_ns = self._config_mtime_ns()

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int:
        """账号刷新间隔（分钟），范围 1–10080（一周）。"""
        self.reload_if_changed()
        try:
            return min(10080, max(1, int(self.data.get("refresh_account_interval_minute", 5))))
        except (TypeError, ValueError):
            return 5

    @property
    def image_retention_hours(self) -> int:
        self.reload_if_changed()
        return _clamp_retention_hours(self.data.get("image_retention_hours"))

    @property
    def log_retention_hours(self) -> int:
        self.reload_if_changed()
        return _clamp_retention_hours(self.data.get("log_retention_hours"))

    @property
    def image_min_free_mb(self) -> int:
        self.reload_if_changed()
        try:
            return min(10_000_000, max(0, int(self.data.get("image_min_free_mb", DEFAULT_IMAGE_MIN_FREE_MB))))
        except (TypeError, ValueError):
            return DEFAULT_IMAGE_MIN_FREE_MB

    @property
    def image_poll_timeout_secs(self) -> int:
        self.reload_if_changed()
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def image_stream_timeout_secs(self) -> int:
        self.reload_if_changed()
        try:
            return max(1, int(self.data.get("image_stream_timeout_secs", 300)))
        except (TypeError, ValueError):
            return 300

    @property
    def image_request_timeout_secs(self) -> int:
        """单次图片请求的总等待上限：流超时 + 轮询超时 + 30 秒余量。"""
        return self.image_stream_timeout_secs + self.image_poll_timeout_secs + 30

    @property
    def console_request_timeout_secs(self) -> int:
        """管理台 API 请求的等待上限（秒），前端 axios 读取；范围 30–3600。"""
        self.reload_if_changed()
        try:
            return min(3600, max(30, int(self.data.get("console_request_timeout_secs", 600))))
        except (TypeError, ValueError):
            return 600

    @property
    def image_queue_limit(self) -> int:
        """后台图片任务同时在途（排队 + 运行）的上限；0 表示自动按队列容量，满时返回 429。"""
        self.reload_if_changed()
        try:
            return min(10000, max(0, int(self.data.get("image_queue_limit", 0))))
        except (TypeError, ValueError):
            return 0

    @property
    def account_processing_concurrency(self) -> int:
        """账号批量刷新 / 检测等批处理的并发线程数。"""
        self.reload_if_changed()
        try:
            return min(100, max(1, int(self.data.get("account_processing_concurrency", 30))))
        except (TypeError, ValueError):
            return 30

    @property
    def image_poll_interval_secs(self) -> float:
        return _clamp_finite_float(self.data.get("image_poll_interval_secs", 2.0), 2.0, 0.5, 60.0)

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """首次轮询前的等待秒数，给会话文档留出落库时间；默认 1 秒，
        之后由 image_poll_adaptive 按 429 情况自动拉长间隔。"""
        return _clamp_finite_float(self.data.get("image_poll_initial_wait_secs", 1.0), 1.0, 0.0, 60.0)

    @property
    def image_account_concurrency(self) -> int:
        try:
            return min(3, max(1, int(self.data.get("image_account_concurrency", 1))))
        except (TypeError, ValueError):
            return 1

    @property
    def image_account_cooldown_secs(self) -> int:
        """Rest time for one account after an image request ends before it may
        take another one; 0 disables the cooldown."""
        try:
            return min(600, max(0, int(self.data.get("image_account_cooldown_secs", 0))))
        except (TypeError, ValueError):
            return 0

    @property
    def image_preflight_fresh_secs(self) -> int:
        """How long a successful remote account check makes the image preflight
        redundant; 0 always runs the preflight."""
        try:
            return min(3600, max(0, int(self.data.get("image_preflight_fresh_secs", 600))))
        except (TypeError, ValueError):
            return 600

    @property
    def image_stream_post_tool_wait_secs(self) -> float:
        """生图指令写完（end_turn=false）后继续读同一条流等图片 ID 的最长秒数；
        超时平滑转回轮询。0 表示与旧行为一致：在该处断开流直接轮询。"""
        try:
            return min(120.0, max(0.0, float(self.data.get("image_stream_post_tool_wait_secs", 45.0))))
        except (TypeError, ValueError):
            return 45.0

    @property
    def image_poll_adaptive(self) -> bool:
        """轮询间隔动态调整：按账号与出口计倍率，未被限流时缩短，遇到 429 翻倍（1–16 秒）。"""
        return _normalize_bool(self.data.get("image_poll_adaptive"), True)

    @property
    def image_warm_enabled(self) -> bool:
        """空闲账号预热：有生图流量时提前准备首页和 sentinel 令牌。"""
        return _normalize_bool(self.data.get("image_warm_enabled"), True)

    @property
    def chat_temporary_mode(self) -> bool:
        """文字对话是否使用官网“临时对话”（不留历史、不用于训练）；默认开启。

        关闭后不再发送 history_and_training_disabled。只影响文字对话，生图始终走原有请求。
        """
        return _normalize_bool(self.data.get("chat_temporary_mode"), True)

    @property
    def chat_sentinel_vm_enabled(self) -> bool:
        """对话 / 生图的 chat-requirements 改由官方 Sentinel SDK 在 Node VM 中生成（需要 Node.js）。

        默认关闭：SDK 的 proof 使用 FNV 算法，与线上验证的 SHA3 PoW 是否一致尚未实测；
        开启后上游拒绝时会自动退回 Python PoW。
        """
        return _normalize_bool(self.data.get("chat_sentinel_vm_enabled"), False)

    @property
    def image_stream_direct_resolve(self) -> bool:
        """SSE 已给出图片 ID 时直接解析下载地址，不再等待二次确认；解析不到时才回退轮询。"""
        return _normalize_bool(self.data.get("image_stream_direct_resolve"), True)

    @property
    def image_drop_extra_variants(self) -> bool:
        """上游"二选一"并行生成时同一会话只交付第一张图，避免返回张数超过 n。"""
        return _normalize_bool(self.data.get("image_drop_extra_variants"), True)

    @property
    def image_upscale_enabled(self) -> bool:
        """上游返回的图片小于请求尺寸时，用 Lanczos 放大到请求尺寸。"""
        return _normalize_bool(self.data.get("image_upscale_enabled"), False)

    @property
    def image_upscale_engine(self) -> str:
        """放大引擎：pillow_lanczos（默认）或 sharp_lanczos3（需要 Node sharp，不可用时自动回退 Pillow）。"""
        value = str(self.data.get("image_upscale_engine") or "pillow_lanczos").strip().lower()
        return value if value in IMAGE_UPSCALE_ENGINES else "pillow_lanczos"

    @property
    def image_account_retry_enabled(self) -> bool:
        """Whether one image may move to another account after an account-switchable failure."""
        return _normalize_bool(self.data.get("image_account_retry_enabled"), True)

    @property
    def image_max_account_attempts(self) -> int:
        """Max accounts one image may try (1..10, default 4).

        Falls back to the legacy image_account_retry_count (+1) when the new key was never saved.
        """
        raw = self.data.get("image_max_account_attempts")
        if raw in (None, ""):
            legacy = self.data.get("image_account_retry_count")
            if legacy not in (None, ""):
                try:
                    return min(10, max(1, int(legacy) + 1))
                except (TypeError, ValueError):
                    pass
            return 4
        try:
            return min(10, max(1, int(raw)))
        except (TypeError, ValueError):
            return 4

    @property
    def image_account_retry_count(self) -> int:
        """How many times one image may move to another account (attempts - 1); 0 when switching is off."""
        if not self.image_account_retry_enabled:
            return 0
        return self.image_max_account_attempts - 1

    @property
    def account_auto_pause_strikes(self) -> int:
        """Strikes (runs of 3 consecutive image failures) within 6 hours that
        auto-pause an account; 0 disables auto pause."""
        try:
            return min(20, max(0, int(self.data.get("account_auto_pause_strikes", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def account_auto_pause_hours(self) -> int:
        """How long an auto-paused account stays deprioritized."""
        try:
            return min(72, max(1, int(self.data.get("account_auto_pause_hours", 6))))
        except (TypeError, ValueError):
            return 6

    @staticmethod
    def _clamped_int(value: object, default: int, low: int, high: int) -> int:
        try:
            return min(high, max(low, int(value)))
        except (TypeError, ValueError):
            return default

    @property
    def new_account_cooldown_hours(self) -> int:
        """新入池账号的冷静期（小时）：期内不接图片和对话；0 关闭。仅对带 new_account_joined_at 的新号生效。"""
        return self._clamped_int(self.data.get("new_account_cooldown_hours", 12), 12, 0, 168)

    @property
    def new_account_ramp_days(self) -> int:
        """冷静期结束后的放量期（天）：期内每日图片数受限；0 关闭。"""
        return self._clamped_int(self.data.get("new_account_ramp_days", 3), 3, 0, 30)

    @property
    def new_account_ramp_daily_images(self) -> int:
        """放量期内单账号每天（北京时间）最多出图张数。"""
        return self._clamped_int(self.data.get("new_account_ramp_daily_images", 5), 5, 1, 200)

    @property
    def text_account_concurrency(self) -> int:
        """单个账号同时承载的对话/搜索请求数。"""
        return self._clamped_int(self.data.get("text_account_concurrency", 3), 3, 1, 20)

    @property
    def account_failure_cooldown_secs(self) -> int:
        """账号上游连续失败（3 次）后的短时冷却秒数；0 关闭。与长时的自动暂停不同。"""
        return self._clamped_int(self.data.get("account_failure_cooldown_secs", 300), 300, 0, 3600)

    @property
    def account_relogin_enabled(self) -> bool:
        """refresh_token 作废后用账号的 Outlook 凭据自动重新登录；默认关闭。"""
        return _normalize_bool(self.data.get("account_relogin_enabled"), True)

    @property
    def account_relogin_interval_secs(self) -> int:
        """自动重新登录的扫描间隔。"""
        try:
            return min(3600, max(60, int(self.data.get("account_relogin_interval_secs", 300))))
        except (TypeError, ValueError):
            return 300

    @property
    def account_relogin_concurrency(self) -> int:
        """同时重新登录的账号数；登录要发验证码，默认一次一个。"""
        try:
            return min(4, max(1, int(self.data.get("account_relogin_concurrency", 1))))
        except (TypeError, ValueError):
            return 1

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_remove_conversation_after_result(self) -> bool:
        self.reload_if_changed()
        return _normalize_bool(self.data.get("image_remove_conversation_after_result"), False)

    @property
    def image_error_friendly_enabled(self) -> bool:
        self.reload_if_changed()
        value = self.data.get("image_error_friendly_enabled", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_image_error_messages(self) -> dict[str, str]:
        self.reload_if_changed()
        value = self.data.get("image_error_messages")
        source = value if isinstance(value, dict) else {}
        messages: dict[str, str] = {}
        for key, default in DEFAULT_IMAGE_ERROR_MESSAGES.items():
            custom = str(source.get(key) or "").strip()
            messages[key] = custom or default
        return messages

    @property
    def image_settle_enabled(self) -> bool:
        """图片二次确认机制：找到 file_ids 后等待一段时间再次确认。"""
        value = self.data.get("image_settle_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_check_before_hit_enabled(self) -> bool:
        """先check再hit：通过轮询确认 file_ids 存在后再返回，而非仅依赖 SSE 事件。"""
        value = self.data.get("image_check_before_hit_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs(self) -> float:
        """二次确认等待时间（秒）。"""
        try:
            return max(0.5, float(self.data.get("image_settle_secs", 1.5)))
        except (TypeError, ValueError):
            return 1.5

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def log_levels(self) -> list[str]:
        levels = self.data.get("log_levels")
        if not isinstance(levels, list):
            return []
        allowed = {"debug", "info", "warning", "error"}
        return [level for item in levels if (level := str(item or "").strip().lower()) in allowed]

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        """Deprecated shim: expiry is owned by the retention coordinator.

        Delegates to ImageStorageService via image_service so index, WebDAV state,
        tombstones, thumbnails and tags stay consistent. Nothing calls this on the save path.
        """
        from services.image_service import cleanup_image_retention

        return int(cleanup_image_retention(self.image_retention_hours).get("removed") or 0)

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        """对外（前端 / API 响应）使用的配置：敏感字段已脱敏。内部需要明文请用 get_internal() 或专用 getter。"""
        with self._lock:
            return self._sanitize_public(self.get_internal())

    def _sanitize_public(self, data: dict[str, object]) -> dict[str, object]:
        data = dict(data)
        data.pop(REVISION_KEY, None)
        for key in _TOP_LEVEL_SECRET_KEYS:
            data.pop(key, None)
        _mask_secret_sections(data, self.data)
        basic = data.get("basic")
        if isinstance(basic, dict):
            data["basic"] = {
                key: value for key, value in basic.items()
                if str(key).lower() not in {"api_key", "password", "secret", "token", "webdav_password"}
            }
        return _walk_strings(data, mask_proxy_credentials)  # type: ignore[return-value]

    @staticmethod
    def mask_public(value):
        """响应体兜底脱敏：把任意结构里的 `scheme://user:pass@host` 打码为 `user:***@host`。"""
        return _walk_strings(value, mask_proxy_credentials)

    def restore_masked(self, value):
        """请求体还原：把 `user:***@host` 还原为已保存的真实代理凭据（找不到则原样保留）。"""
        with self._lock:
            self.reload_if_changed()
            return restore_masked_proxy_credentials(value, self.data)

    def get_internal(self) -> dict[str, object]:
        """内部使用：包含明文凭据，禁止直接返回给前端或写入日志。"""
        with self._lock:
            self.reload_if_changed()
            data = dict(self.data)
            data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
            data["image_retention_hours"] = self.image_retention_hours
            data["log_retention_hours"] = self.log_retention_hours
            data["image_min_free_mb"] = self.image_min_free_mb
            data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
            data["image_stream_timeout_secs"] = self.image_stream_timeout_secs
            data["console_request_timeout_secs"] = self.console_request_timeout_secs
            data["image_queue_limit"] = self.image_queue_limit
            data["account_processing_concurrency"] = self.account_processing_concurrency
            data["image_poll_interval_secs"] = self.image_poll_interval_secs
            data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
            data["image_account_concurrency"] = self.image_account_concurrency
            data["image_account_cooldown_secs"] = self.image_account_cooldown_secs
            data["image_preflight_fresh_secs"] = self.image_preflight_fresh_secs
            data["image_stream_post_tool_wait_secs"] = self.image_stream_post_tool_wait_secs
            data["image_poll_adaptive"] = self.image_poll_adaptive
            data["image_warm_enabled"] = self.image_warm_enabled
            data["chat_sentinel_vm_enabled"] = self.chat_sentinel_vm_enabled
            data["chat_temporary_mode"] = self.chat_temporary_mode
            data["image_stream_direct_resolve"] = self.image_stream_direct_resolve
            data["image_drop_extra_variants"] = self.image_drop_extra_variants
            data["image_upscale_enabled"] = self.image_upscale_enabled
            data["image_upscale_engine"] = self.image_upscale_engine
            data["image_account_retry_enabled"] = self.image_account_retry_enabled
            data["image_max_account_attempts"] = self.image_max_account_attempts
            data["image_account_retry_count"] = self.image_account_retry_count
            data["account_auto_pause_strikes"] = self.account_auto_pause_strikes
            data["account_auto_pause_hours"] = self.account_auto_pause_hours
            data["new_account_cooldown_hours"] = self.new_account_cooldown_hours
            data["new_account_ramp_days"] = self.new_account_ramp_days
            data["new_account_ramp_daily_images"] = self.new_account_ramp_daily_images
            data["text_account_concurrency"] = self.text_account_concurrency
            data["account_failure_cooldown_secs"] = self.account_failure_cooldown_secs
            data["account_relogin_enabled"] = self.account_relogin_enabled
            data["account_relogin_interval_secs"] = self.account_relogin_interval_secs
            data["account_relogin_concurrency"] = self.account_relogin_concurrency
            data["image_parallel_generation"] = self.image_parallel_generation
            data["image_remove_conversation_after_result"] = self.image_remove_conversation_after_result
            data["image_error_friendly_enabled"] = self.image_error_friendly_enabled
            data["image_error_messages"] = self.get_image_error_messages()
            data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
            data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
            data["log_levels"] = self.log_levels
            data["sensitive_words"] = self.sensitive_words
            data["ai_review"] = self.ai_review
            data["global_system_prompt"] = self.global_system_prompt
            data["backup"] = self.get_backup_settings()
            data["image_storage"] = self.get_image_storage_settings()
            data["genbox_push"] = self.get_public_genbox_push_settings()
            data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
            data["proxy_runtime"] = self.get_public_proxy_runtime_settings()
            data["fallback_proxy"] = self.get_proxy_fallback_settings()
            data["third_party_apps"] = self.get_third_party_apps_settings()
            data["basic"] = _legacy_basic_from_settings(data.get("basic"), data)
            data.pop("auth-key", None)
            return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_direct_locale(self) -> dict[str, str]:
        """直连出口（以及自定义代理 URL）使用的时区 / 语言；两项都有效才算已配置，否则返回空字典。"""
        raw = self.data.get("direct_locale")
        if not isinstance(raw, dict):
            return {}
        try:
            timezone_value, language_value = normalize_locale_fields(raw.get("timezone"), raw.get("language"))
        except ValueError:
            return {}
        if not timezone_value or not language_value:
            return {}
        return {"timezone": timezone_value, "language": language_value}

    def get_proxy_fallback_settings(self) -> str:
        return str(self.data.get("fallback_proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        runtime = _walk_strings(copy.deepcopy(self.get_proxy_runtime_settings()), mask_proxy_credentials)
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
        return runtime

    def get_third_party_apps_settings(self) -> dict[str, object]:
        return _normalize_third_party_apps_settings(self.data.get("third_party_apps"))

    def update(self, data: dict[str, object], *, expected_revision: int | None = None) -> dict[str, object]:
        with self._lock:
            self.reload_if_changed()
            if expected_revision is not None and expected_revision != self.revision:
                raise SettingsRevisionConflictError(self.revision, expected_revision)
            next_data = _promote_legacy_basic_settings(self.data)
            incoming_data = _promote_legacy_basic_settings(dict(data or {}))
            incoming_data.pop(REVISION_KEY, None)
            # 前端回传的是脱敏后的配置：代理 `user:***@host` 还原为已存凭据，敏感字段留空沿用已存值。
            incoming_data = restore_masked_proxy_credentials(incoming_data, self.data)  # type: ignore[assignment]
            for secret_section, secret_fields in SECRET_SETTING_FIELDS.items():
                if secret_section in incoming_data:
                    incoming_data[secret_section] = _merge_secret_section(
                        incoming_data[secret_section], self.data.get(secret_section), secret_fields
                    )
            for secret_key in _TOP_LEVEL_SECRET_KEYS:
                incoming_data.pop(secret_key, None)
            next_data.update(incoming_data)
            next_data = _promote_legacy_basic_settings(next_data)
            if "backup" in next_data:
                next_data["backup"] = _normalize_backup_settings(next_data.get("backup"))
            if "image_storage" in next_data:
                next_data["image_storage"] = _normalize_image_storage_settings(next_data.get("image_storage"))
                _validate_image_storage_settings(next_data["image_storage"])
            if "genbox_push" in next_data:
                incoming_genbox = next_data.get("genbox_push")
                incoming_genbox = dict(incoming_genbox) if isinstance(incoming_genbox, dict) else {}
                clear_push_key = _normalize_bool(incoming_genbox.get("clear_push_key"), False)
                if clear_push_key:
                    incoming_genbox["push_key"] = ""
                elif not str(incoming_genbox.get("push_key") or "").strip():
                    # 前端读到的 push_key 是脱敏空串；留空表示沿用已保存的密钥。
                    incoming_genbox["push_key"] = self.get_genbox_push_settings().get("push_key") or ""
                next_data["genbox_push"] = _normalize_genbox_push_settings(incoming_genbox)
                _validate_genbox_push_settings(next_data["genbox_push"])
            if "image_upscale_engine" in next_data and str(next_data.get("image_upscale_engine") or "").strip().lower() not in IMAGE_UPSCALE_ENGINES:
                raise ValueError("图片放大引擎无效")
            if "chat_completion_cache" in next_data:
                next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                    next_data.get("chat_completion_cache")
                )
            if "third_party_apps" in next_data:
                next_data["third_party_apps"] = _normalize_third_party_apps_settings(next_data.get("third_party_apps"))
            if "proxy_runtime" in next_data:
                incoming_runtime = next_data.get("proxy_runtime")
                if isinstance(incoming_runtime, dict):
                    previous_clearance = self.get_proxy_runtime_settings().get("clearance")
                    if isinstance(previous_clearance, dict):
                        incoming_runtime = dict(incoming_runtime)
                        incoming_runtime["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
                        incoming_runtime["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
                next_data["proxy_runtime"] = _normalize_proxy_runtime_settings(incoming_runtime)
            next_data["basic"] = _legacy_basic_from_settings(next_data.get("basic"), next_data)
            next_data.pop("backup_state", None)
            # 只有设置页管理的键发生变化才推进 revision；后台任务写 proxy_groups 等不影响设置页的并发判断。
            settings_changed = any(next_data.get(key) != self.data.get(key) for key in SETTINGS_UPDATE_KEYS)
            next_data[REVISION_KEY] = self.revision + (1 if settings_changed else 0)
            self.data = next_data
            self._save()
        return self.get()

    def get_backup_settings(self) -> dict[str, object]:
        """内部使用：包含明文 R2 密钥与加密口令，禁止直接返回给前端或写入日志。"""
        return _normalize_backup_settings(self.data.get("backup"))

    def get_image_storage_settings(self) -> dict[str, object]:
        """内部使用：包含明文 WebDAV 密码，禁止直接返回给前端或写入日志。"""
        return _normalize_image_storage_settings(self.data.get("image_storage"))

    def get_public_backup_settings(self) -> dict[str, object]:
        settings = dict(self.get_backup_settings())
        for field in SECRET_SETTING_FIELDS["backup"]:
            settings[f"has_{field}"] = bool(str(settings.get(field) or "").strip())
            settings[field] = ""
        return settings

    def get_public_image_storage_settings(self) -> dict[str, object]:
        settings = dict(self.get_image_storage_settings())
        for field in SECRET_SETTING_FIELDS["image_storage"]:
            settings[f"has_{field}"] = bool(str(settings.get(field) or "").strip())
            settings[field] = ""
        return settings

    def get_genbox_push_settings(self) -> dict[str, object]:
        """内部使用：包含明文 push_key，禁止直接返回给前端或写入日志。"""
        return _normalize_genbox_push_settings(self.data.get("genbox_push"))

    def get_public_genbox_push_settings(self) -> dict[str, object]:
        settings = self.get_genbox_push_settings()
        has_push_key = bool(str(settings.get("push_key") or "").strip())
        settings["push_key"] = ""
        settings["has_push_key"] = has_push_key
        return settings

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


def load_backup_state() -> dict[str, object]:
    return _normalize_backup_state(read_json_object(BACKUP_STATE_FILE, name="backup_state.json"))


def save_backup_state(state: dict[str, object]) -> dict[str, object]:
    normalized = _normalize_backup_state(state)
    write_json_file(BACKUP_STATE_FILE, normalized)
    return normalized


config = ConfigStore(CONFIG_FILE)

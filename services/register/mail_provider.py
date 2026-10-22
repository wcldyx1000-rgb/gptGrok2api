from __future__ import annotations

import hashlib
import imaplib
import random
import re
import os
import secrets
import sqlite3
import string
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, message_from_string, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from curl_cffi import requests


from services.config import DATA_DIR
from services.json_file import read_json_file, write_json_file
from services.proxy_service import proxy_settings
from utils.sqlite_runtime import sqlite_connection_guard

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()

OUTLOOK_TOKEN_USED_FILE = DATA_DIR / "outlook_token_used.json"
_outlook_token_state_lock = Lock()
# in_use 超过该秒数视为陈旧（注册进程崩溃残留），可被重新领用
OUTLOOK_IN_USE_STALE_SECONDS = 3600
# family_used：同一原邮箱的某个 +别名已经注册过（OpenAI 把别名当同一个人），原邮箱及其余别名不再领用。
OUTLOOK_RECORDED_STATES = {"used", "family_used", "in_use", "login_required", "token_invalid", "failed"}
OUTLOOK_UNAVAILABLE_STATES = {"used", "family_used", "login_required", "token_invalid", "failed"}
OUTLOOK_FAMILY_BLOCKING_STATES = {"used", "family_used"}
# 瞬时失败（网络抖动/CF/限流等）把邮箱放回池：每次放回后至少冷却这么久，连续放回超过上限再标 failed，避免空转。
OUTLOOK_TRANSIENT_COOLDOWN_SECONDS = 10.0
OUTLOOK_TRANSIENT_COOLDOWN_MAX_SECONDS = 60.0
OUTLOOK_TRANSIENT_RELEASE_LIMIT = 3
OUTLOOK_TRANSIENT_FORGET_SECONDS = 1800.0
# 池里暂时只剩“冷却中/同家族别名占用中”的邮箱时，create_mailbox 最多等多久再放弃。
OUTLOOK_CLAIM_WAIT_SECONDS = 60.0
OUTLOOK_CLAIM_POLL_SECONDS = 2.0
OUTLOOK_BUSY_STATES = {"in_use"}
OUTLOOK_RETRYABLE_STATES = {"failed"}
OUTLOOK_INVALID_STATES = {"login_required", "token_invalid"}
OUTLOOK_CREDENTIAL_FATAL_STATES = OUTLOOK_INVALID_STATES
OUTLOOK_REFRESHED_CREDENTIAL_RESET_STATES = OUTLOOK_RETRYABLE_STATES | OUTLOOK_INVALID_STATES


def _outlook_token_state_db_file():
    # Derive this from the legacy path so tests and custom DATA_DIR deployments
    # that override OUTLOOK_TOKEN_USED_FILE keep their state isolated.
    return OUTLOOK_TOKEN_USED_FILE.with_suffix(".db")


def _outlook_state_connection() -> sqlite3.Connection:
    path = _outlook_token_state_db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            connection.close()
            raise
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS mailbox_states (
            email TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            lease_token TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS mailbox_states_state_idx
            ON mailbox_states(state, updated_at);
        CREATE TABLE IF NOT EXISTS mailbox_state_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(mailbox_states)").fetchall()}
    if "use_count" not in columns:
        try:
            connection.execute("ALTER TABLE mailbox_states ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                connection.close()
                raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def _legacy_outlook_token_state() -> dict[str, dict[str, Any]]:
    data = read_json_file(
        OUTLOOK_TOKEN_USED_FILE,
        name="outlook_token_used.json",
        default_factory=dict,
        expected_types=(dict, list),
    )
    state: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            key = str(item).strip().lower()
            if key:
                state[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            email = str(key).strip().lower()
            if not email:
                continue
            if isinstance(value, dict):
                state[email] = {
                    "state": str(value.get("state") or "used").strip() or "used",
                    "reason": str(value.get("reason") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                }
            else:
                state[email] = {
                    "state": str(value or "used").strip() or "used",
                    "reason": "",
                    "updated_at": "",
                }
    return state


def _ensure_outlook_state_migrated(connection: sqlite3.Connection) -> None:
    migrated = connection.execute(
        "SELECT value FROM mailbox_state_meta WHERE key = 'legacy_json_imported'"
    ).fetchone()
    if migrated is not None:
        return
    legacy = _legacy_outlook_token_state()
    now = datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(
        """
        INSERT OR IGNORE INTO mailbox_states(email, state, reason, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                email,
                str(entry.get("state") or "used"),
                str(entry.get("reason") or ""),
                str(entry.get("updated_at") or now),
            )
            for email, entry in legacy.items()
        ],
    )
    connection.execute(
        "INSERT OR REPLACE INTO mailbox_state_meta(key, value) VALUES('legacy_json_imported', ?)",
        (now,),
    )
    connection.commit()


def _load_ddg_aliases() -> set[str]:
    data = read_json_file(
        DDG_ALIASES_FILE,
        name="ddg_aliases.json",
        default_factory=list,
        expected_types=list,
    )
    return {str(item).strip().lower() for item in data if str(item).strip()} if isinstance(data, list) else set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    write_json_file(DDG_ALIASES_FILE, sorted(aliases))


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


def _load_outlook_token_state() -> dict[str, dict[str, Any]]:
    """Return the cross-process mailbox state snapshot.

    The legacy JSON file is imported once into SQLite on first access.
    """
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        rows = connection.execute(
            "SELECT email, state, reason, updated_at, lease_token, use_count FROM mailbox_states"
        ).fetchall()
    return {
        str(row["email"]): {
            "state": str(row["state"]),
            "reason": str(row["reason"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "lease_token": str(row["lease_token"] or ""),
            "use_count": int(row["use_count"] or 0),
        }
        for row in rows
    }


def _save_outlook_token_state(state: dict[str, dict[str, Any]]) -> None:
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM mailbox_states")
        connection.executemany(
            """
            INSERT INTO mailbox_states(email, state, reason, updated_at, lease_token, use_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(email).strip().lower(),
                    str(entry.get("state") or "used"),
                    str(entry.get("reason") or ""),
                    str(entry.get("updated_at") or datetime.now(timezone.utc).isoformat()),
                    str(entry.get("lease_token") or ""),
                    int(entry.get("use_count") or 0),
                )
                for email, entry in state.items()
                if str(email).strip()
            ],
        )
        connection.commit()


_outlook_transient_lock = Lock()
# email -> [连续瞬时放回次数, 冷却到期(monotonic), 最近一次放回(monotonic)]；仅进程内，重启即清零。
_outlook_transient_state: dict[str, list[float]] = {}


def _outlook_transient_cooling(email: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _outlook_transient_lock:
        record = _outlook_transient_state.get(str(email or "").strip().lower())
        return bool(record and record[1] > current)


def _outlook_note_transient_release(email: str, *, cooldown: float | None = None) -> int:
    """记录一次瞬时放回，返回该邮箱连续放回次数；长时间没出问题的记录会被遗忘。"""
    key = str(email or "").strip().lower()
    now = time.monotonic()
    with _outlook_transient_lock:
        record = _outlook_transient_state.get(key)
        count = int(record[0]) if record and now - record[2] < OUTLOOK_TRANSIENT_FORGET_SECONDS else 0
        count += 1
        wait = cooldown if cooldown is not None else min(
            OUTLOOK_TRANSIENT_COOLDOWN_MAX_SECONDS, OUTLOOK_TRANSIENT_COOLDOWN_SECONDS * count
        )
        _outlook_transient_state[key] = [float(count), now + max(0.0, wait), now]
        return count


def _outlook_clear_transient(email: str) -> None:
    with _outlook_transient_lock:
        _outlook_transient_state.pop(str(email or "").strip().lower(), None)


def _outlook_family_key(credential: dict[str, Any]) -> str:
    """同一原邮箱的 +别名属于同一家族；没有别名关系时就是自己。"""
    return str(credential.get("login_email") or credential.get("alias_of") or credential.get("email") or "").strip().lower()


def _outlook_busy_families(store: dict[str, dict[str, Any]], pool: list[dict[str, Any]]) -> set[str]:
    """有邮箱正被领用（in_use 且未陈旧）的家族：同家族别名不并发领用。"""
    busy: set[str] = set()
    for item in pool:
        key = str(item.get("email") or "").strip().lower()
        entry = store.get(key) if key else None
        if isinstance(entry, dict) and str(entry.get("state") or "") == "in_use" and not _outlook_entry_available(entry):
            busy.add(_outlook_family_key(item))
    return busy


def _claim_outlook_credential_ex(pool: list[dict[str, str]]) -> tuple[dict[str, str] | None, str, bool]:
    """原子领用一个可用邮箱；第三个返回值表示“暂时没有，但只是被冷却/同家族占用挡住，稍后可能有”。"""
    now = datetime.now(timezone.utc)
    lease_token = secrets.token_urlsafe(24)
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT email, state, reason, updated_at, lease_token FROM mailbox_states"
        ).fetchall()
        store = {
            str(row["email"]): {
                "state": str(row["state"]),
                "reason": str(row["reason"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "lease_token": str(row["lease_token"] or ""),
            }
            for row in rows
        }
        busy_families = _outlook_busy_families(store, pool)
        credential = None
        waitable = False
        for item in pool:
            if not _outlook_credential_available(store, item):
                continue
            if _outlook_family_key(item) in busy_families and _is_outlook_family_member(item, pool):
                waitable = True
                continue
            if _outlook_transient_cooling(str(item.get("email") or "")):
                waitable = True
                continue
            credential = item
            break
        if credential is None:
            connection.rollback()
            return None, "", waitable
        email = str(credential.get("email") or "").strip().lower()
        connection.execute(
            """
            INSERT INTO mailbox_states(email, state, reason, updated_at, lease_token)
            VALUES (?, 'in_use', '', ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                state = 'in_use', reason = '', updated_at = excluded.updated_at,
                lease_token = excluded.lease_token
            """,
            (email, now.isoformat(), lease_token),
        )
        connection.commit()
    return credential, lease_token, False


def _is_outlook_family_member(credential: dict[str, Any], pool: list[dict[str, Any]]) -> bool:
    """只有“别名展开”产生的家族才做并发互斥：条目带 alias_of，或它是某个别名的原邮箱。"""
    if str(credential.get("alias_of") or "").strip():
        return True
    key = str(credential.get("email") or "").strip().lower()
    return any(
        str(item.get("alias_of") or "").strip().lower() == key
        for item in pool
        if item is not credential
    )


def _claim_outlook_credential(pool: list[dict[str, str]]) -> tuple[dict[str, str] | None, str]:
    """Atomically reserve one available credential across worker processes."""
    credential, lease_token, _waitable = _claim_outlook_credential_ex(pool)
    return credential, lease_token


def _outlook_entry_available(entry: dict[str, Any] | None) -> bool:
    """该邮箱当前是否可领用：未记录、或 in_use 已陈旧、或非终态时可用。"""
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if current == "in_use":
        updated_at = str(entry.get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds()
            return age >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def _outlook_credential_state(store: dict[str, dict[str, Any]], credential: dict[str, Any]) -> str:
    """返回地址自身状态；如果原登录邮箱 token 已失效，则别名也继承该致命状态。"""
    key = str(credential.get("email") or "").strip().lower()
    entry = store.get(key) if key else None
    state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
    if state:
        return state
    login_email = str(credential.get("login_email") or credential.get("alias_of") or "").strip().lower()
    if login_email and login_email != key:
        parent = store.get(login_email)
        parent_state = str(parent.get("state") or "") if isinstance(parent, dict) else ""
        if parent_state in OUTLOOK_CREDENTIAL_FATAL_STATES:
            return parent_state
        if parent_state in OUTLOOK_FAMILY_BLOCKING_STATES:
            return "family_used"
    return ""


def _outlook_credential_available(store: dict[str, dict[str, Any]], credential: dict[str, Any]) -> bool:
    key = str(credential.get("email") or "").strip().lower()
    entry = store.get(key) if key else None
    if not _outlook_entry_available(entry):
        return False
    state = _outlook_credential_state(store, credential)
    return state not in OUTLOOK_CREDENTIAL_FATAL_STATES and state != "family_used"


def _set_outlook_token_state(
    address: str,
    state: str,
    reason: str = "",
    *,
    lease_token: str = "",
) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    bump = 1 if str(state) == "used" else 0
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        if lease_token:
            cursor = connection.execute(
                """
                UPDATE mailbox_states
                SET state = ?, reason = ?, updated_at = ?, lease_token = '',
                    use_count = use_count + ?
                WHERE email = ? AND lease_token = ?
                """,
                (
                    str(state),
                    str(reason or ""),
                    datetime.now(timezone.utc).isoformat(),
                    bump,
                    target,
                    lease_token,
                ),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO mailbox_states(email, state, reason, updated_at, lease_token, use_count)
                VALUES (?, ?, ?, ?, '', ?)
                ON CONFLICT(email) DO UPDATE SET
                    state = excluded.state, reason = excluded.reason,
                    updated_at = excluded.updated_at, lease_token = '',
                    use_count = mailbox_states.use_count + excluded.use_count
                """,
                (
                    target,
                    str(state),
                    str(reason or ""),
                    datetime.now(timezone.utc).isoformat(),
                    bump,
                ),
            )
        connection.commit()
        return cursor.rowcount == 1


def _release_outlook_token_state(address: str, *, lease_token: str = "") -> bool:
    """把 in_use 释放回未使用（仅当当前确实是 in_use 时）。"""
    target = str(address or "").strip().lower()
    if not target:
        return False
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        if lease_token:
            cursor = connection.execute(
                "DELETE FROM mailbox_states WHERE email = ? AND state = 'in_use' AND lease_token = ?",
                (target, lease_token),
            )
        else:
            cursor = connection.execute(
                "DELETE FROM mailbox_states WHERE email = ? AND state = 'in_use'",
                (target,),
            )
        connection.commit()
        return cursor.rowcount == 1


def clear_outlook_token_states(addresses: list[str] | set[str], states: set[str] | None = None) -> int:
    """清除指定邮箱的状态记录。

    states 为空时清除任意状态；否则只清除指定状态。用于重新导入新凭据后释放旧失败标记，
    不应清除 used，避免已经成功消费的邮箱被误用。
    """
    targets = {str(item or "").strip().lower() for item in addresses}
    targets.discard("")
    if not targets:
        return 0
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in targets)
        params: list[str] = list(targets)
        sql = f"DELETE FROM mailbox_states WHERE email IN ({placeholders})"
        if states:
            state_placeholders = ",".join("?" for _ in states)
            sql += f" AND state IN ({state_placeholders})"
            params.extend(states)
        cursor = connection.execute(sql, params)
        connection.commit()
        return cursor.rowcount


def reset_outlook_token_pool_state(scope: str = "all") -> int:
    """重置邮箱池状态文件。

    scope=all 清空所有记录；
    scope=retryable/failed 仅释放 in_use 与 failed（保留 used 和凭据失效状态）；
    scope=invalid 仅释放 login_required/token_invalid，用于重新授权或重新导入 refresh_token 后手动恢复。
    返回被清除的条目数。
    """
    normalized = str(scope or "all").strip().lower()
    if normalized in {"failed", "retryable"}:
        target_states = OUTLOOK_RETRYABLE_STATES | OUTLOOK_BUSY_STATES
    elif normalized in {"invalid", "reauth"}:
        target_states = OUTLOOK_INVALID_STATES
    elif normalized in {"busy", "in_use"}:
        target_states = OUTLOOK_BUSY_STATES
    else:
        target_states = set()
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        if target_states:
            placeholders = ",".join("?" for _ in target_states)
            cursor = connection.execute(
                f"DELETE FROM mailbox_states WHERE state IN ({placeholders})",
                list(target_states),
            )
        else:
            cursor = connection.execute("DELETE FROM mailbox_states")
        connection.commit()
        return cursor.rowcount


# --- Outlook 邮箱池逐邮箱管理视图（Ported from chatgpt-register (AGPL-3.0-only).）---
# 可由管理员手动重置回 unused 的状态；in_use 只有租约过期（陈旧）后才允许，租约中的邮箱不可重置。
OUTLOOK_RESETTABLE_STATES = {"failed", "used", "family_used", "login_required", "token_invalid"}
_OUTLOOK_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OUTLOOK_SECRET_IN_TEXT_RE = re.compile(r"[A-Za-z0-9_\-./+=]{32,}")


def redact_outlook_reason(text: Any, limit: int = 160) -> str:
    """失败原因对外展示前脱敏：邮箱打码、疑似令牌/长串密钥替换，并截断。"""
    value = " ".join(str(text or "").split())
    value = _OUTLOOK_EMAIL_IN_TEXT_RE.sub(lambda match: _mask_outlook_email(match.group(0)), value)
    value = _OUTLOOK_SECRET_IN_TEXT_RE.sub("***", value)
    return value[:limit]


def _is_outlook_transient_failed_reason(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    # 连续多次瞬时失败后被停放的邮箱，原因前缀固定，里面的原始错误也是瞬时类。
    if text.startswith("连续") and "瞬时失败" in text:
        return True
    return classify_outlook_failure(text) == "transient"


def release_outlook_transient_failed() -> int:
    """把遗留的“因瞬时错误被标 failed”的邮箱放回池（删除状态记录即回到 unused）。

    只处理 failed，且原因文本按 classify_outlook_failure 归为 transient；
    账号被锁/凭据无效/登录流异常等确定性错误（token_invalid、login_required 或未知原因的 failed）不动。
    返回释放数量。状态库不存在时直接返回 0（不凭空创建）。
    """
    if not _outlook_token_state_db_file().exists():
        return 0
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT email, reason FROM mailbox_states WHERE state = 'failed'"
        ).fetchall()
        targets = [str(row["email"]) for row in rows if _is_outlook_transient_failed_reason(str(row["reason"] or ""))]
        released = 0
        for email in targets:
            released += connection.execute(
                "DELETE FROM mailbox_states WHERE email = ? AND state = 'failed'", (email,)
            ).rowcount
        connection.commit()
    for email in targets:
        _outlook_clear_transient(email)
    return released


def reset_outlook_mailbox_state(email: str) -> str:
    """把单个邮箱重置为 unused。返回 reset / missing / leased / not_resettable。

    读取与删除在同一个 BEGIN IMMEDIATE 事务里，并按读到的租约令牌做 CAS，
    租约中（未陈旧的 in_use）的邮箱不会被动。
    """
    target = str(email or "").strip().lower()
    if not target:
        return "missing"
    with sqlite_connection_guard, closing(_outlook_state_connection()) as connection:
        _ensure_outlook_state_migrated(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state, updated_at, lease_token FROM mailbox_states WHERE email = ?", (target,)
        ).fetchone()
        if row is None:
            connection.rollback()
            return "missing"
        state = str(row["state"])
        if state == "in_use":
            if not _outlook_entry_available({"state": state, "updated_at": str(row["updated_at"] or "")}):
                connection.rollback()
                return "leased"
        elif state not in OUTLOOK_RESETTABLE_STATES:
            connection.rollback()
            return "not_resettable"
        cursor = connection.execute(
            "DELETE FROM mailbox_states WHERE email = ? AND state = ? AND lease_token = ?",
            (target, state, str(row["lease_token"] or "")),
        )
        connection.commit()
        changed = cursor.rowcount == 1
    if changed:
        _outlook_clear_transient(target)
    return "reset" if changed else "missing"


def outlook_pool_entries(
    base_credentials: list[dict[str, str]],
    provider: dict | None = None,
) -> list[dict[str, Any]]:
    """逐邮箱投影：原邮箱 + 别名展开出的每个地址一行。

    绝不包含密码、refresh_token、client_id 明文，也不含租约令牌；只给 has_* 布尔和脱敏邮箱。
    内部键 `_address` 是未脱敏地址，调用方用完必须剥掉。
    """
    store = _load_outlook_token_state()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def build(credential: dict[str, str], *, is_alias: bool) -> None:
        address = str(credential.get("email") or "").strip()
        key = address.lower()
        if not address or key in seen:
            return
        seen.add(key)
        recorded = store.get(key)
        recorded = recorded if isinstance(recorded, dict) else None
        own_state = str(recorded.get("state") or "") if recorded else ""
        state = _outlook_credential_state(store, credential) or "unused"
        inherited = bool(state != "unused" and not own_state)
        stale = False
        leased_at = ""
        if state == "in_use":
            leased_at = str(recorded.get("updated_at") or "") if recorded else ""
            stale = _outlook_entry_available(recorded)
        parent = str(credential.get("alias_of") or "").strip()
        if state == "in_use":
            resettable = stale
        else:
            resettable = bool(own_state) and state in OUTLOOK_RESETTABLE_STATES
        rows.append(
            {
                "_address": address,
                "email": _mask_outlook_email(address),
                "alias_of": _mask_outlook_email(parent) if parent else "",
                "is_alias": is_alias,
                "state": "leased" if state == "in_use" else state,
                "inherited": inherited,
                "reason": redact_outlook_reason(recorded.get("reason") if recorded else ""),
                "updated_at": str(recorded.get("updated_at") or "") if recorded else "",
                "leased_at": leased_at,
                "lease_stale": stale,
                "cooling": _outlook_transient_cooling(address),
                "use_count": int(recorded.get("use_count") or 0) if recorded else 0,
                "has_password": bool(str(credential.get("password") or "").strip()),
                "has_client_id": bool(str(credential.get("client_id") or "").strip()),
                "has_refresh_token": bool(str(credential.get("refresh_token") or "").strip()),
                "resettable": resettable,
                "deletable": not is_alias,
            }
        )

    for credential in base_credentials:
        build(credential, is_alias=False)
        for item in expand_outlook_aliases([credential], provider):
            if str(item.get("alias_of") or "").strip():
                build(item, is_alias=True)
    return rows


def prune_outlook_unused_credentials(credentials: list[dict[str, str]], entry: dict | None = None) -> tuple[list[dict[str, str]], int]:
    """Return credentials with recorded state, plus the number pruned as unused."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
    kept: list[dict[str, str]] = []
    removed = 0
    for credential in credentials:
        expanded = expand_outlook_aliases([credential], entry)
        has_recorded = False
        for item in expanded:
            key = str(item.get("email") or "").strip().lower()
            state_entry = store.get(key) if key else None
            state = str(state_entry.get("state") or "") if isinstance(state_entry, dict) else ""
            if state in OUTLOOK_RECORDED_STATES:
                has_recorded = True
                break
        if has_recorded:
            kept.append(credential)
        else:
            removed += 1
    return kept, removed


def outlook_token_pool_stats(pool: list[dict[str, str]] | None = None) -> dict[str, int]:
    """统计邮箱池各状态数量。pool 为该 provider 当前导入的邮箱列表（用于算 unused）。"""
    store = _load_outlook_token_state()
    counts = {"unused": 0, "in_use": 0, "used": 0, "family_used": 0, "login_required": 0, "token_invalid": 0, "failed": 0}
    if pool:
        for credential in pool:
            state = _outlook_credential_state(store, credential)
            if state in counts:
                counts[state] += 1
            else:
                counts["unused"] += 1
    else:
        for entry in store.values():
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
    counts["available"] = counts["unused"]
    counts["busy"] = counts["in_use"]
    counts["retryable"] = counts["failed"]
    counts["invalid"] = counts["login_required"] + counts["token_invalid"]
    counts["abnormal"] = counts["retryable"] + counts["invalid"]
    return counts


def outlook_token_pool_failures(
    pool: list[dict[str, str]] | None = None,
    *,
    since: str = "",
) -> list[dict[str, str]]:
    """Return failed mailbox state entries in the configured pool since a batch started."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
    since_at: datetime | None = None
    if str(since or "").strip():
        try:
            parsed = datetime.fromisoformat(str(since).strip())
            since_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            since_at = None
    failures: list[dict[str, str]] = []
    for credential in pool or []:
        email = str(credential.get("email") or "").strip()
        entry = store.get(email.lower()) if email else None
        if not isinstance(entry, dict) or str(entry.get("state") or "") != "failed":
            continue
        updated_at = str(entry.get("updated_at") or "")
        if since_at is not None:
            try:
                parsed_updated = datetime.fromisoformat(updated_at)
                comparable = parsed_updated if parsed_updated.tzinfo else parsed_updated.replace(tzinfo=timezone.utc)
                if comparable < since_at:
                    continue
            except ValueError:
                continue
        failures.append(
            {
                "email": email,
                "reason": str(entry.get("reason") or ""),
                "updated_at": updated_at,
            }
        )
    failures.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return failures


ResultT = TypeVar("ResultT")
domain_lock = Lock()
provider_lock = Lock()
domain_index = 0
provider_index = 0
cloudmail_token_lock = Lock()
cloudmail_token_cache: dict[str, tuple[str, float]] = {}
gptmail_status_lock = Lock()
gptmail_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}

GPTMAIL_DEFAULT_API_BASE = "https://mail.chatgpt.org.uk"
GPTMAIL_PUBLIC_STATUS_CACHE_SECONDS = 60
GPTMAIL_CUSTOM_STATUS_CACHE_SECONDS = 30


def _config(mail_config: dict) -> dict:
    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 30),
        "wait_interval": float(mail_config.get("wait_interval") or 2),
        "user_agent": str(mail_config.get("user_agent") or "Mozilla/5.0"),
        "proxy": str(mail_config.get("proxy") or "").strip(),
    }


def _random_mailbox_name() -> str:
    return f"{''.join(random.choices(string.ascii_lowercase, k=5))}{''.join(random.choices(string.digits, k=random.randint(1, 3)))}{''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))}"


def _random_subdomain_label() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 10)))


def _next_domain(domains: list[str]) -> str:
    global domain_index
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    if len(domains) == 1:
        return domains[0]
    with domain_lock:
        value = domains[domain_index % len(domains)]
        domain_index = (domain_index + 1) % len(domains)
        return value


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _create_session(conf: dict):
    proxy = str(conf.get("proxy") or "").strip()
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate="chrome",
        verify=False,
        trust_env=False,
    )
    return requests.Session(**kwargs)


def _gptmail_proxy_hint(conf: dict) -> str:
    proxy = str(conf.get("proxy") or "").strip()
    return f"（当前注册代理：{proxy}）" if proxy else "（当前未配置注册代理，可能使用稳定代理运行时）"


def _is_proxy_tunnel_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "connect tunnel failed" in text
        or "curl: (56)" in text
        or "curl: (97)" in text
        or "curl: (35)" in text
        or "proxy connect" in text
    )


def _gptmail_api_base(entry: dict) -> str:
    value = str(entry.get("api_base") or "").strip()
    return (value or GPTMAIL_DEFAULT_API_BASE).rstrip("/")


def _gptmail_key_mode(entry: dict) -> str:
    mode = str(entry.get("key_mode") or entry.get("api_key_mode") or "").strip().lower()
    if mode in {"public", "custom"}:
        return mode
    return "custom" if str(entry.get("api_key") or "").strip() else "public"


def _gptmail_cache_key(api_base: str, key_mode: str, api_key: str = "", reveal_public_key: bool = False, proxy: str = "") -> str:
    digest = hashlib.sha256(f"{api_base}|{key_mode}|{api_key}|{int(reveal_public_key)}|{proxy}".encode()).hexdigest()[:16]
    return f"{api_base}|{key_mode}|{digest}"


def _gptmail_mask_key(value: str) -> str:
    key = str(value or "").strip()
    if len(key) <= 8:
        return "*" * len(key) if key else ""
    return f"{key[:5]}...{key[-4:]}"


def _gptmail_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _gptmail_status_cache_expiry(data: dict[str, Any], now: float, ttl: int) -> float:
    seconds_until_reset = _gptmail_int(data.get("seconds_until_reset"))
    if seconds_until_reset and seconds_until_reset > 0:
        return now + max(1, seconds_until_reset)
    reset_at = str(data.get("reset_at") or "").strip()
    if reset_at:
        try:
            reset_date = datetime.fromisoformat(reset_at[:-1] + "+00:00" if reset_at.endswith("Z") else reset_at)
            if reset_date.tzinfo is None:
                reset_date = reset_date.replace(tzinfo=timezone.utc)
            seconds_from_reset_at = int(reset_date.timestamp() - now)
            if seconds_from_reset_at > 0:
                return now + max(1, seconds_from_reset_at)
        except Exception:
            pass
    return now + ttl


def _gptmail_status_payload(entry: dict, conf: dict, *, reveal_public_key: bool = False) -> dict[str, Any]:
    api_base = _gptmail_api_base(entry)
    key_mode = _gptmail_key_mode(entry)
    api_key = str(entry.get("api_key") or "").strip()
    session = _create_session(conf)
    try:
        if key_mode == "public":
            headers = {"User-Agent": conf["user_agent"], "Accept": "application/json"}
            params = {"reveal": "1"} if reveal_public_key else None
            if reveal_public_key:
                headers["X-Public-Key-Reveal"] = "click"
            resp = session.request("GET", f"{api_base}/api/public-key-status", headers=headers, params=params, timeout=conf["request_timeout"], verify=False)
            if resp.status_code != 200:
                raise RuntimeError(f"GPTMail 公共 Key 状态请求失败: HTTP {resp.status_code}, body={resp.text[:300]}")
            body = resp.json()
            if not isinstance(body, dict) or not body.get("success"):
                raise RuntimeError(str((body or {}).get("error") or "GPTMail 公共 Key 状态返回异常"))
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            return {
                "ok": True,
                "key_mode": key_mode,
                "api_base": api_base,
                "source": "public-key-status",
                "is_active": bool(data.get("is_active", True)),
                "daily_limit": _gptmail_int(data.get("daily_limit")),
                "used_today": _gptmail_int(data.get("used_today")),
                "remaining_today": _gptmail_int(data.get("remaining_today")),
                "reset_at": data.get("reset_at") or "",
                "seconds_until_reset": _gptmail_int(data.get("seconds_until_reset")),
                "api_key": str(data.get("key") or "").strip(),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        if not api_key:
            raise RuntimeError("GPTMail 自定义模式需要配置 API Key")
        resp = session.request(
            "GET",
            f"{api_base}/api/stats",
            headers={"User-Agent": conf["user_agent"], "Accept": "application/json", "X-API-Key": api_key},
            timeout=conf["request_timeout"],
            verify=False,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"GPTMail 自定义 Key 状态请求失败: HTTP {resp.status_code}, body={resp.text[:300]}")
        body = resp.json()
        if not isinstance(body, dict) or not body.get("success"):
            raise RuntimeError(str((body or {}).get("error") or "GPTMail 自定义 Key 状态返回异常"))
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return {
            "ok": True,
            "key_mode": key_mode,
            "api_base": api_base,
            "source": "stats",
            "is_active": True,
            "daily_limit": _gptmail_int(usage.get("daily_limit")),
            "used_today": _gptmail_int(usage.get("used_today")),
            "remaining_today": _gptmail_int(usage.get("remaining_today")),
            "total_limit": _gptmail_int(usage.get("total_limit")),
            "total_usage": _gptmail_int(usage.get("total_usage")),
            "remaining_total": _gptmail_int(usage.get("remaining_total")),
            "reset_at": usage.get("reset_at") or body.get("reset_at") or "",
            "seconds_until_reset": _gptmail_int(usage.get("seconds_until_reset") or body.get("seconds_until_reset")),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except RuntimeError:
        raise
    except Exception as exc:
        if _is_proxy_tunnel_error(exc):
            raise RuntimeError(f"GPTMail 检测失败：代理 CONNECT 隧道返回 503，无法连接 {api_base}{_gptmail_proxy_hint(conf)}。请切换直连或更换注册代理后重试。原始错误: {exc}") from exc
        raise RuntimeError(f"GPTMail 检测失败{_gptmail_proxy_hint(conf)}: {exc}") from exc
    finally:
        session.close()


def _gptmail_cached_status(entry: dict, conf: dict, *, reveal_public_key: bool = False, force: bool = False) -> dict[str, Any]:
    api_base = _gptmail_api_base(entry)
    key_mode = _gptmail_key_mode(entry)
    api_key = str(entry.get("api_key") or "").strip()
    ttl = GPTMAIL_PUBLIC_STATUS_CACHE_SECONDS if key_mode == "public" else GPTMAIL_CUSTOM_STATUS_CACHE_SECONDS
    cache_key = _gptmail_cache_key(api_base, key_mode, api_key, reveal_public_key, str(conf.get("proxy") or "").strip())
    now = time.time()
    if not force:
        with gptmail_status_lock:
            cached = gptmail_status_cache.get(cache_key)
            if cached and now < cached[0]:
                return dict(cached[1])
    data = _gptmail_status_payload(entry, conf, reveal_public_key=reveal_public_key)
    expires_at = _gptmail_status_cache_expiry(data, now, ttl)
    with gptmail_status_lock:
        gptmail_status_cache[cache_key] = (expires_at, dict(data))
    return data


def _gptmail_api_key(entry: dict, conf: dict) -> str:
    if _gptmail_key_mode(entry) == "custom":
        api_key = str(entry.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("GPTMail 自定义模式需要配置 API Key")
        return api_key
    status = _gptmail_cached_status(entry, conf, reveal_public_key=True)
    api_key = str(status.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("GPTMail 公共 Key 获取失败")
    return api_key


def gptmail_status(mail_config: dict, entry: dict | None = None, *, force: bool = False) -> dict[str, Any]:
    provider_entry = dict(entry or {})
    if not provider_entry:
        provider_entry = next((dict(item) for item in _entries(mail_config) if item.get("type") == "gptmail"), {})
    if not provider_entry:
        raise RuntimeError("未找到 GPTMail 邮箱来源")
    provider_entry["type"] = "gptmail"
    conf = _config(mail_config)
    reveal_public_key = _gptmail_key_mode(provider_entry) == "public"
    data = _gptmail_cached_status(provider_entry, conf, reveal_public_key=reveal_public_key, force=force)
    public_key = str(data.pop("api_key", "") or "").strip()
    key_hint = _gptmail_mask_key(public_key if data.get("key_mode") == "public" else str(provider_entry.get("api_key") or ""))
    return {**data, "key_hint": key_hint, "local_compose": bool(provider_entry.get("local_compose")), "default_domain": str(provider_entry.get("default_domain") or "").strip()}


def refresh_gptmail_public_key(mail_config: dict, entry: dict | None = None, *, force: bool = True) -> dict[str, Any]:
    provider_entry = dict(entry or {})
    if not provider_entry:
        provider_entry = next((dict(item) for item in _entries(mail_config) if item.get("type") == "gptmail"), {})
    if not provider_entry:
        raise RuntimeError("未找到 GPTMail 邮箱来源")
    provider_entry["type"] = "gptmail"
    if _gptmail_key_mode(provider_entry) != "public":
        raise RuntimeError("只有 GPTMail 公共 Key 模式需要自动刷新 Key")
    conf = _config(mail_config)
    data = _gptmail_cached_status(provider_entry, conf, reveal_public_key=True, force=force)
    public_key = str(data.pop("api_key", "") or "").strip()
    if not public_key:
        raise RuntimeError("GPTMail 公共 Key 获取失败")
    return {
        **data,
        "key_hint": _gptmail_mask_key(public_key),
        "local_compose": bool(provider_entry.get("local_compose")),
        "default_domain": str(provider_entry.get("default_domain") or "").strip(),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(data: dict[str, Any]) -> tuple[str, str]:
    text_content = str(data.get("text_content") or data.get("text") or data.get("body") or data.get("content") or "")
    html_content = str(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html") or "")
    if text_content or html_content:
        return text_content, html_content
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            html.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _extract_raw_mail_headers(data: dict[str, Any]) -> tuple[str, str]:
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return "", ""
    return str(parsed.get("Subject") or ""), str(parsed.get("From") or "")


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                out.extend(_extract_text_candidates(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_text_candidates(item))
        return out
    return []


def _message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in (
        "to",
        "toEmail",
        "mailTo",
        "receiver",
        "receivers",
        "address",
        "email",
        "envelope_to",
        "delivered_to",
        "x_forwarded_to",
        "x_original_to",
    ):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    return not target or not candidates or any(target in str(item).strip().lower() for item in candidates if str(item).strip())


def _plain_email_text(value: Any) -> str:
    if isinstance(value, list):
        value = "\n".join(str(item or "") for item in value)
    text = str(value or "")
    text = re.sub(r"(?is)<(?:style|script)\b[^>]*>.*?</(?:style|script)>", " ", text)
    return re.sub(r"<[^>]+>", " ", text)


def _extract_labeled_code(content: str) -> str | None:
    token = r"([A-Z0-9]{3}-[A-Z0-9]{3}|\d{6})"
    for pattern in (
        rf"(?:verification|security|login|confirm(?:ation)?)\s*(?:email\s*)?code\s*(?:is\s*)?[:=-]?\s*{token}",
        rf"(?:code|验证码)\s*(?:is\s*)?[:=-]?\s*{token}",
    ):
        match = re.search(pattern, content, re.I)
        if match and match.group(1) != "177010":
            return match.group(1).upper()
    return None


def _extract_code(
    message: dict[str, Any],
    *,
    expected_keyword: str = "",
    require_body_context: bool = False,
    allow_subject_code: bool | None = None,
) -> str | None:
    subject = str(message.get("subject") or "")
    text_content = _plain_email_text(message.get("text_content"))
    html_content = _plain_email_text(message.get("html_content"))
    body = f"{text_content}\n{html_content}".strip()
    content = f"{subject}\n{body}".strip()
    if not content:
        return None

    if allow_subject_code is None:
        allow_subject_code = not require_body_context
    if allow_subject_code:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI\b", subject, re.I)
        if match:
            return match.group(1).upper()

    body_code = _extract_labeled_code(body)
    if body_code:
        return body_code

    keyword = str(expected_keyword or "").strip()
    if keyword and body:
        token = r"([A-Z0-9]{3}-[A-Z0-9]{3}|\d{6})"
        keyword_pattern = re.escape(keyword)
        for pattern in (
            rf"{keyword_pattern}.{{0,180}}?(?:verification|security|login|confirm|code).{{0,80}}?{token}",
            rf"(?:verification|security|login|confirm|code).{{0,80}}?{token}.{{0,180}}?{keyword_pattern}",
        ):
            match = re.search(pattern, body, re.I | re.S)
            if match:
                return match.group(1).upper()

    if require_body_context:
        candidates = {
            match.group(1).upper()
            for match in re.finditer(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])", body, re.I)
        }
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    match = re.search(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])", content, re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


def _message_tracking_ref(message: dict[str, Any]) -> str:
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"
    received_at = message.get("received_at")
    received_value = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    content = "\n".join(str(message.get(key) or "") for key in ("subject", "sender", "text_content", "html_content"))
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


def _message_before_code_boundary(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    boundary = mailbox.get("_code_not_before")
    received_at = message.get("received_at")
    if not isinstance(boundary, datetime) or not isinstance(received_at, datetime):
        return False
    if not received_at.tzinfo:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at < boundary


def _received_at_timestamp(item: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        parsed = _parse_received_at(value)
        if parsed is not None:
            return parsed.timestamp()
    return 0.0


def _message_before_received_after(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    """mailbox["_received_after"]（ISO 字符串）之前收到的邮件视为旧邮件。"""
    received_after = mailbox.get("_received_after")
    received_at = message.get("received_at")
    if not received_after or not isinstance(received_at, datetime):
        return False
    try:
        threshold = received_after if isinstance(received_after, datetime) else datetime.fromisoformat(str(received_after))
    except ValueError:
        return False
    if threshold.tzinfo is None:
        threshold = threshold.replace(tzinfo=timezone.utc)
    current = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
    return current < threshold


GPTMAIL_REQUEST_ATTEMPTS = 4
GPTMAIL_CREATE_MIN_INTERVAL_SECONDS = 2.5
_gptmail_create_lock = Lock()
_gptmail_next_create: dict[str, float] = {}


def _gptmail_rate_limited(status_code: int, body: str) -> bool:
    """HTTP 429 或 Cloudflare 的 Error 1015（被限速）。"""
    text = str(body or "").lower()
    return status_code == 429 or "error 1015" in text or ("1015" in text and "rate" in text) or "rate limit" in text


def _gptmail_throttle_create(api_base: str) -> None:
    """创建邮箱串行化并保持最小间隔（同一个 api_base 的所有线程共用）。"""
    key = str(api_base or "").rstrip("/")
    with _gptmail_create_lock:
        wait = _gptmail_next_create.get(key, 0.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _gptmail_next_create[key] = time.monotonic() + GPTMAIL_CREATE_MIN_INTERVAL_SECONDS


def is_mailbox_creation_rate_limited(error: Exception | str | None) -> bool:
    """邮箱来源创建被限流（GPTMail 429/1015 等）：应换下一个来源，而不是整个任务失败。"""
    text = str(error or "")
    lowered = text.lower()
    return (
        "GPTMail 被限流" in text
        or ("gptmail" in lowered and ("http 429" in lowered or "1015" in lowered))
        or "error 1015" in lowered
    )


class BaseMailProvider:
    name = "unknown"
    # 每次轮询最多扫描最近多少封邮件：最新一封可能是欢迎/安全提醒而不是验证码。
    recent_scan_limit = 5

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        """最近 N 封邮件（最新在前）。默认只有最新一封；有列表接口的 provider 应覆盖。"""
        message = self.fetch_latest_message(mailbox)
        return [message] if message else []

    def _cached_detail(self, key: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """邮件详情不可变：同一封只取一次，轮询扫描多封时不会放大请求量（对限流敏感的公共站很重要）。"""
        cache = self.__dict__.setdefault("_detail_cache_store", {})
        if key in cache:
            return cache[key]
        value = loader()
        if len(cache) >= 128:
            cache.pop(next(iter(cache)))
        cache[key] = value
        return value

    def wait_for(self, mailbox: dict[str, Any], on_message: Callable[[dict[str, Any]], ResultT | None]) -> ResultT | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            for message in self.fetch_recent_messages(mailbox):
                if not message:
                    continue
                result = on_message(message)
                if result is not None:
                    return result
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        tried_value = mailbox.setdefault("_tried_otp_codes", [])
        if not isinstance(tried_value, list):
            tried_value = []
            mailbox["_tried_otp_codes"] = tried_value
        tried_codes = {str(item) for item in tried_value}

        def extract_unseen_code(message: dict[str, Any]) -> str | None:
            if _message_before_code_boundary(mailbox, message) or _message_before_received_after(mailbox, message):
                return None
            ref = _message_tracking_ref(message)
            if ref in seen_refs:
                return None
            code = _extract_code(
                message,
                expected_keyword=str(getattr(self, "code_keyword", "") or ""),
                require_body_context=bool(getattr(self, "require_code_context", False)),
                allow_subject_code=bool(message.get("_trusted_code_subject"))
                or not bool(getattr(self, "require_code_context", False)),
            )
            if not code:
                return None
            seen_value.append(ref)
            seen_refs.add(ref)
            if code in tried_codes:
                # 同一个码已经试过（重发/重复投递）：不再返回，继续等新码。
                return None
            tried_value.append(code)
            tried_codes.add(code)
            return code

        return self.wait_for(mailbox, extract_unseen_code)

    def close(self) -> None:
        pass


class CloudflareTempMailProvider(BaseMailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_password = str(entry["admin_password"]).strip()
        self.domain = entry.get("domain") or []
        self.label = str(entry.get("label") or "CF临时邮箱").strip()
        self.code_keyword = str(entry.get("keyword") or "").strip()
        self.require_code_context = bool(self.code_keyword)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"Content-Type": "application/json", "User-Agent": self.conf["user_agent"], **(headers or {})}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudflareTempMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/admin/new_address", headers={"x-admin-auth": self.admin_password}, payload={"enablePrefix": True, "name": username or _random_mailbox_name(), "domain": _next_domain(self.domain)})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError("CloudflareTempMail 缺少 address 或 jwt")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "token": token,
            "label": self.label,
        }

    def get_existing_mailbox(self, email: str) -> dict[str, Any]:
        """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
        data = self._request("POST", "/admin/get_address", headers={"x-admin-auth": self.admin_password}, payload={"address": email})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError(f"CloudflareTempMail 无法获取已有邮箱 {email} 的 JWT")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    @staticmethod
    def _list_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("results", "data", "messages", "hydra:member"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _message_detail(self, token: str, message_id: str) -> dict[str, Any]:
        encoded_id = quote(str(message_id or ""), safe="")
        if not encoded_id:
            return {}
        for path in (f"/api/mail/{encoded_id}", f"/api/mails/{encoded_id}"):
            try:
                response = self.session.request(
                    "GET",
                    f"{self.api_base}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": self.conf["user_agent"],
                    },
                    timeout=self.conf["request_timeout"],
                    verify=False,
                )
                if not 200 <= response.status_code < 300:
                    continue
                data = response.json()
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            nested = data.get("data")
            return dict(nested) if isinstance(nested, dict) else data
        return {}

    @staticmethod
    def _merge_message_summary(item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        merged = dict(item)
        for key, value in detail.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return merged

    def _normalize_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        message_id = str(item.get("id") or item.get("_id") or item.get("msgid") or "")
        has_content = any(
            value
            for value in (
                item.get("raw"),
                item.get("text"),
                item.get("text_content"),
                item.get("html"),
                item.get("html_content"),
                item.get("body"),
                item.get("content"),
            )
        )
        detail = {} if has_content else self._message_detail(str(mailbox.get("token") or ""), message_id)
        merged = self._merge_message_summary(item, detail)
        text_content, html_content = _extract_content(merged)
        sender = merged.get("from") or merged.get("sender") or ""
        raw_subject, raw_sender = _extract_raw_mail_headers(merged)
        subject = str(merged.get("subject") or "")
        trusted_subject = False
        if not subject and raw_subject:
            subject = raw_subject
            trusted_subject = True
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        if not sender and raw_sender:
            sender = raw_sender
        return {
            "provider": self.name,
            "mailbox": str(mailbox.get("address") or ""),
            "message_id": str(merged.get("id") or merged.get("_id") or merged.get("msgid") or message_id),
            "subject": subject,
            "sender": str(sender),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                merged.get("createdAt")
                or merged.get("created_at")
                or merged.get("receivedAt")
                or merged.get("date")
                or merged.get("timestamp")
            ),
            "raw": merged,
            "_trusted_code_subject": trusted_subject,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/mails",
            headers={"Authorization": f"Bearer {mailbox['token']}"},
            params={"limit": 10, "offset": 0},
        )
        target = str(mailbox.get("address") or "")
        messages = [item for item in self._list_items(data) if _message_matches_email(item, target)]
        return [self._normalize_message(mailbox, item) for item in messages]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        for message in messages:
            if _extract_code(
                message,
                expected_keyword=self.code_keyword,
                require_body_context=self.require_code_context,
                allow_subject_code=bool(message.get("_trusted_code_subject")),
            ):
                return message
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class DDGMailProvider(BaseMailProvider):
    name = "ddg_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.ddg_token = str(entry["ddg_token"]).strip()
        self.cf_api_base = str(entry.get("api_base") or entry.get("cf_api_base") or "").rstrip("/")
        self.cf_inbox_jwt = str(entry.get("cf_inbox_jwt") or "").strip()
        self.cf_admin_password = str(entry.get("admin_password") or "").strip()
        self.cf_api_key = str(entry.get("cf_api_key") or "").strip()
        self.cf_auth_mode = str(entry.get("cf_auth_mode") or "none").strip().lower()
        self.cf_domain = entry.get("cf_domain") or []
        self.cf_create_path = str(entry.get("cf_create_path") or "/api/new_address").strip()
        self.cf_messages_path = str(entry.get("cf_messages_path") or "/api/mails").strip()
        self.session = _create_session(conf)

    def _cf_build_headers(self, content_type: bool = False) -> dict:
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.cf_api_key:
            if self.cf_auth_mode == "x-api-key":
                headers["X-API-Key"] = self.cf_api_key
            elif self.cf_auth_mode != "none":
                headers["Authorization"] = f"Bearer {self.cf_api_key}"
        return headers

    def _cf_request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict:
        merged_headers = {**self._cf_build_headers(True), **(headers or {}), "User-Agent": self.conf["user_agent"]}
        if self.cf_admin_password and method.upper() in ("POST",):
            merged_headers["x-admin-auth"] = self.cf_admin_password
        if self.cf_api_key and self.cf_auth_mode == "query-key":
            params = {**(params or {}), "key": self.cf_api_key}
        resp = self.session.request(method.upper(), f"{self.cf_api_base}{path}", headers=merged_headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DDGMail CF请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _ddg_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = self.session.request(method.upper(), f"https://quack.duckduckgo.com{path}", headers={"Authorization": f"Bearer {self.ddg_token}", "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"DDG API请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return resp.json()

    def _cf_list_payload(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "hydra:member", "data", "messages"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    return value["messages"]
        return []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        ddg_data = self._ddg_request("POST", "/api/email/addresses", payload={})
        ddg_address_part = str(ddg_data.get("address") or "").strip()
        if not ddg_address_part:
            raise RuntimeError("DDG API 返回无 address 字段")
        ddg_address = f"{ddg_address_part}@duck.com"

        if _is_ddg_alias_duplicate(ddg_address):
            raise RuntimeError(f"[{self.label}] DDG日上限已达，别名 {ddg_address} 已存在，自动切换邮箱提供商")

        _record_ddg_alias(ddg_address)

        if not self.cf_inbox_jwt:
            raise RuntimeError("DDGMail 需要 cf_inbox_jwt（DDG 转发目标的固定收件箱 JWT），请在邮箱配置中填写 CF Inbox JWT")

        return {"provider": self.name, "provider_ref": self.provider_ref, "address": ddg_address, "token": self.cf_inbox_jwt, "label": self.label}

    def _parse_raw_recipient(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        match = re.search(r"^To:\s*(.+?)$", raw_text, re.MULTILINE | re.IGNORECASE)
        if match:
            addr = match.group(1).strip()
            addr = re.sub(r"\s*<[^>]*>", "", addr)
            return addr.strip().lower()
        try:
            parsed = message_from_string(raw_text, policy=policy.default)
            return str(parsed.get("To") or "").strip().lower()
        except Exception:
            return ""

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        target_address = str(mailbox.get("address") or "").strip().lower()
        data = self._cf_request("GET", self.cf_messages_path, headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0})
        raw_list = self._cf_list_payload(data)
        messages = [item for item in raw_list if isinstance(item, dict)]
        result: list[dict[str, Any]] = []
        for item in messages:
            message_id = str(item.get("id") or item.get("msgid") or item.get("_id") or "")
            raw_text = str(item.get("raw") or "")
            raw_recipient = self._parse_raw_recipient(raw_text)
            if target_address and raw_recipient and target_address not in raw_recipient:
                continue
            text_content, html_content = _extract_content(item)
            subject = str(item.get("subject") or "")
            sender = item.get("from") or item.get("sender") or item.get("source") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
            if raw_text and (not subject or not sender or subject == sender == ""):
                try:
                    parsed = message_from_string(raw_text, policy=policy.default)
                    if not subject:
                        subject = str(parsed.get("Subject") or "")
                    if not sender:
                        sender = str(parsed.get("From") or "")
                except Exception:
                    pass
            result.append({"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": subject, "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item})
            if len(result) >= self.recent_scan_limit:
                break
        return result

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class _NonRetryableCloudMailGenError(RuntimeError):
    pass


class CloudMailGenProvider(BaseMailProvider):
    name = "cloudmail_gen"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_email = str(entry.get("admin_email") or "").strip()
        self.admin_password = str(entry.get("admin_password") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.session = _create_session(conf)

    def _clear_token_cache(self) -> None:
        with cloudmail_token_lock:
            cloudmail_token_cache.pop(self._cache_key(), None)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        last_error = ""
        attempts = 3
        for attempt in range(attempts):
            try:
                resp = self.session.request(
                    method.upper(),
                    f"{self.api_base}{path}",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": self.conf["user_agent"],
                        **(headers or {}),
                    },
                    params=params,
                    json=payload,
                    timeout=self.conf["request_timeout"],
                    verify=False,
                )
                if resp.status_code in expected:
                    return {} if resp.status_code == 204 else resp.json()
                message = f"CloudMailGen 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}"
                if not self._is_retryable_status(int(resp.status_code)):
                    raise _NonRetryableCloudMailGenError(message)
                last_error = message
            except _NonRetryableCloudMailGenError as error:
                raise RuntimeError(str(error)) from error
            except Exception as error:
                last_error = f"CloudMailGen 请求异常: {method} {path}, error={error}"
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(last_error or f"CloudMailGen 请求失败: {method} {path}")

    def _cache_key(self) -> str:
        return f"{self.api_base}|{self.admin_email}"

    @staticmethod
    def _is_success_payload(data: Any) -> bool:
        return isinstance(data, dict) and data.get("code") == 200

    def _fetch_email_list(self, token: str, address: str) -> dict:
        data = self._request(
            "POST",
            "/api/public/emailList",
            headers={"Authorization": token},
            payload={"toEmail": address, "size": 20, "timeSort": "desc"},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        return data

    def _get_token(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMailGen 缺少 admin_email 或 admin_password")
        cache_key = self._cache_key()
        now = time.time()
        with cloudmail_token_lock:
            cached = cloudmail_token_cache.get(cache_key)
            if cached and now < cached[1] - 300:
                return cached[0]
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
        )
        token = ""
        if isinstance(data, dict) and data.get("code") == 200:
            token = str((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError(f"CloudMailGen genToken 返回异常: {data}")
        with cloudmail_token_lock:
            cloudmail_token_cache[cache_key] = (token, now + 24 * 3600)
        return token

    def _resolve_address(self, username: str | None = None) -> str:
        domain = _next_domain(self.domain)
        if self.subdomain:
            domain = f"{random.choice(self.subdomain)}.{domain}"
        if username:
            local_part = username
        elif self.email_prefix:
            local_part = f"{self.email_prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
        else:
            local_part = _random_mailbox_name()
        return f"{local_part}@{domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.domain:
            raise RuntimeError("CloudMailGen 需要至少配置一个 domain")
        address = self._resolve_address(username)
        token = self._get_token()
        self._request(
            "POST",
            "/api/public/addUser",
            headers={"Authorization": token},
            payload={"list": [{"email": address}]},
        )
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def _normalize_message(self, address: str, item: dict[str, Any]) -> dict[str, Any]:
        text_content, html_content = _extract_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or item.get("emailId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("sender") or item.get("sendEmail") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt") or item.get("created_at") or item.get("createTime") or item.get("receivedAt") or item.get("date") or item.get("timestamp")
            ),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("CloudMailGen 缺少 address")
        token = self._get_token()
        data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            self._clear_token_cache()
            token = self._get_token()
            data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        items = data.get("data") or []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        return [self._normalize_message(address, item) for item in messages[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(BaseMailProvider):
    name = "tempmail_lol"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _resolve_domain(domain: str) -> tuple[str, bool]:
        text = str(domain or "").strip().lower()
        if text.startswith("*.") and len(text) > 2:
            return f"{_random_subdomain_label()}.{text[2:]}", True
        return text, False

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"https://api.tempmail.lol/v2{path}", params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"TempMail.lol 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.domain:
            domain, force_random_prefix = self._resolve_domain(random.choice(self.domain))
            payload["domain"] = domain
            if force_random_prefix:
                payload["prefix"] = _random_mailbox_name()
        if username and "prefix" not in payload:
            payload["prefix"] = username
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def _normalize_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        text_content, html_content = _extract_content(item)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("token") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from") or item.get("from_address") or ""), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("created_at") or item.get("createdAt") or item.get("date") or item.get("received_at") or item.get("timestamp")), "raw": item}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request("GET", "/inbox", params={"token": mailbox["token"]})
        items = data.get("emails") or data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        messages.sort(key=lambda value: (_received_at_timestamp(value, "created_at", "createdAt", "date", "received_at", "timestamp"), str(value.get("id") or value.get("token") or "")), reverse=True)
        return [self._normalize_message(mailbox, item) for item in messages[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class DuckMailProvider(BaseMailProvider):
    name = "duckmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", use_api_key: bool = False, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.api_key if use_api_key else token}"} if use_api_key or token else {}
        resp = self.session.request(method.upper(), f"https://api.duckmail.sbs{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DuckMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("hydra:member") or data.get("member") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username or _random_mailbox_name()}@{self.default_domain}"
        payload = {"address": address, "password": password}
        account = self._request("POST", "/accounts", use_api_key=True, payload=payload)
        token_data = self._request("POST", "/token", use_api_key=True, payload=payload)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": str(token_data.get("token") or ""), "password": password, "account_id": str(account.get("id") or "")}

    def _load_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        message_id = str(item.get("id") or item.get("@id") or "").replace("/messages/", "")
        if message_id:
            token = str(mailbox.get("token") or "")
            item = self._cached_detail(f"duckmail:{message_id}", lambda: self._request("GET", f"/messages/{message_id}", token=token))
        sender = item.get("from") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("name") or ""
        html_content = item.get("html") or ""
        if isinstance(html_content, list):
            html_content = "".join(str(value) for value in html_content)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": str(item.get("text") or item.get("text_content") or ""), "html_content": str(html_content), "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")), "raw": item}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"page": 1})
        items = [item for item in self._items(data) if isinstance(item, dict)]
        return [self._load_message(mailbox, item) for item in items[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class GptMailProvider(BaseMailProvider):
    name = "gptmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = _gptmail_api_base(entry)
        self.key_mode = _gptmail_key_mode(entry)
        self.api_key = _gptmail_api_key(entry, conf)
        self.default_domain = str(entry.get("default_domain") or "").strip()
        self.local_compose = bool(entry.get("local_compose"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key})

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        query = dict(params or {})
        last_error = ""
        for attempt in range(GPTMAIL_REQUEST_ATTEMPTS):
            resp = self.session.request(method.upper(), f"{self.api_base}{path}", params=query, json=payload, timeout=self.conf["request_timeout"], verify=False)
            if resp.status_code == 200:
                data = resp.json()
                return data["data"] if isinstance(data, dict) and "data" in data else data
            body = str(resp.text or "")[:300]
            last_error = f"GPTMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={body}"
            if not _gptmail_rate_limited(int(resp.status_code), body):
                raise RuntimeError(last_error)
            if attempt < GPTMAIL_REQUEST_ATTEMPTS - 1:
                # 429 / Cloudflare 1015：指数退避（遵守 Retry-After）后重试，公共站对突发请求很敏感。
                time.sleep(_retry_after_seconds(resp, min(12.0, 1.5 * (2 ** attempt)) + random.uniform(0.2, 0.8)))
        raise RuntimeError(f"GPTMail 被限流（已退避重试 {GPTMAIL_REQUEST_ATTEMPTS} 次）: {last_error}")

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if self.local_compose:
            if not self.default_domain:
                raise RuntimeError("GPTMail 本地拼接模式需要配置默认域名")
            prefix = username or _random_mailbox_name()
            return {"provider": self.name, "provider_ref": self.provider_ref, "address": f"{prefix}@{self.default_domain}"}
        payload = {key: value for key, value in {"prefix": username, "domain": self.default_domain}.items() if value}
        # 创建邮箱串行且保持最小间隔，避免并发线程一起打爆公共站。
        _gptmail_throttle_create(self.api_base)
        data = self._request("POST" if payload else "GET", "/api/generate-email", payload=payload or None)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": str(data["email"])}

    def _normalize_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from_address") or ""), "text_content": str(item.get("content") or ""), "html_content": str(item.get("html_content") or ""), "received_at": _parse_received_at(item.get("timestamp") or item.get("created_at")), "raw": item}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/emails", params={"email": mailbox["address"]})
        emails = data if isinstance(data, list) else data.get("emails") or []
        emails = [item for item in emails if isinstance(item, dict)]
        emails.sort(key=lambda value: (float(value.get("timestamp") or 0), str(value.get("id") or "")), reverse=True)
        messages: list[dict[str, Any]] = []
        for item in emails[: self.recent_scan_limit]:
            if item.get("id"):
                item_id = str(item["id"])
                item = self._cached_detail(f"gptmail:{item_id}", lambda item_id=item_id: self._request("GET", f"/api/email/{item_id}"))
            messages.append(self._normalize_message(mailbox, item))
        return messages

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class DoneMailProvider(BaseMailProvider):
    name = "donemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        api_base = str(entry["api_base"]).rstrip("/")
        for suffix in ("/api/overview", "/api/view-mails", "/api"):
            if api_base.endswith(suffix):
                api_base = api_base[: -len(suffix)].rstrip("/")
                break
        self.api_base = api_base
        self.admin_key = str(entry.get("admin_key") or entry.get("admin_password") or entry.get("api_key") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain") or entry.get("default_domain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.message_limit = max(1, min(50, int(entry.get("message_limit") or 20)))
        self.session = _create_session(conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Admin-Key": self.admin_key,
        })

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        if not self.admin_key:
            raise RuntimeError("DoneMail 缺少 X-Admin-Key")
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            params=params,
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"DoneMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("ok") is False:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            message = error.get("message") or error.get("code") or "DoneMail 返回失败"
            raise RuntimeError(f"DoneMail 请求失败: {message}")
        return data

    @staticmethod
    def _items(data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("data") or data.get("mails") or data.get("messages") or data.get("items") or []
            return items if isinstance(items, list) else []
        return []

    def _resolve_address(self, username: str | None = None) -> str:
        if username and "@" in username:
            return username.strip()
        if not self.domain:
            raise RuntimeError("DoneMail 需要至少配置一个 domain")
        local_part = username or (f"{self.email_prefix}_{_random_mailbox_name()}" if self.email_prefix else _random_mailbox_name())
        return f"{local_part}@{_next_domain(self.domain)}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        address = self._resolve_address(username)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def get_existing_mailbox(self, email: str) -> dict[str, Any]:
        address = str(email or "").strip()
        if not address:
            raise RuntimeError("DoneMail 缺少 email")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def _normalize_message(self, address: str, item: dict[str, Any]) -> dict[str, Any]:
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(sender),
            "text_content": text_content or str(item.get("preview") or ""),
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedAt") or item.get("received_at") or item.get("createdAt") or item.get("created_at") or item.get("date") or item.get("timestamp")),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("DoneMail 缺少 address")
        data = self._request("GET", "/api/mails", params={"limit": self.message_limit, "to": address})
        messages = [item for item in self._items(data) if isinstance(item, dict) and _message_matches_email(item, address)]
        messages.sort(
            key=lambda value: (
                _received_at_timestamp(value, "receivedAt", "received_at", "createdAt", "created_at", "date", "timestamp"),
                str(value.get("id") or value.get("_id") or ""),
            ),
            reverse=True,
        )
        return [self._normalize_message(address, item) for item in messages[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class ICloudPrivacyMailError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "icloud_api_error")
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)


class ICloudPrivacyMailProvider(BaseMailProvider):
    name = "icloud_api"
    _retryable_status_codes = {408, 429, 500, 502, 503, 504}

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.internal = str(entry.get("type") or "").strip().lower() == "icloud_local"
        self.api_base = str(entry.get("api_base") or "").strip().rstrip("/")
        if self.internal and not self.api_base:
            self.api_base = str(os.getenv("ICLOUD_PRIVACY_MAIL_BASE_URL") or "http://127.0.0.1:8787").strip().rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.project = str(entry.get("project") or "openai").strip().lower() or "openai"
        self.purpose = str(entry.get("purpose") or "register").strip() or "register"
        self.keyword = str(entry.get("keyword") or "OpenAI").strip() or "OpenAI"
        wait_ms = 12000 if entry.get("wait_ms") is None else int(entry.get("wait_ms"))
        self.wait_ms = max(0, min(30000, wait_ms))
        use_proxy = entry.get("use_proxy", False)
        self.use_proxy = (
            use_proxy
            if isinstance(use_proxy, bool)
            else str(use_proxy).strip().lower() in {"1", "true", "yes", "on"}
        )
        session_conf = {**conf, "proxy": str(conf.get("proxy") or "").strip() if self.use_proxy else "direct"}
        self.session = _create_session(session_conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _payload_error(data: dict[str, Any], status_code: int = 0) -> ICloudPrivacyMailError:
        code = str(data.get("code") or "icloud_api_error").strip() or "icloud_api_error"
        message = str(data.get("message") or code).strip() or code
        retryable = bool(data.get("retryable")) or status_code in ICloudPrivacyMailProvider._retryable_status_codes
        return ICloudPrivacyMailError(
            code,
            f"iCloud Privacy Mail: {message}",
            retryable=retryable,
            status_code=status_code,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            resp = self.session.request(
                method.upper(),
                url,
                headers=headers,
                json=payload,
                timeout=timeout or self.conf["request_timeout"],
                verify=False,
            )
        except Exception as exc:
            raise ICloudPrivacyMailError(
                "network_error",
                f"iCloud Privacy Mail 请求失败（{type(exc).__name__}）",
                retryable=True,
            ) from exc

        try:
            data = resp.json()
        except Exception as exc:
            retryable = resp.status_code in self._retryable_status_codes
            raise ICloudPrivacyMailError(
                "invalid_response",
                f"iCloud Privacy Mail 返回非 JSON 响应（HTTP {resp.status_code}）",
                retryable=retryable,
                status_code=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise ICloudPrivacyMailError("invalid_response", "iCloud Privacy Mail 返回结构不是对象")
        if not 200 <= resp.status_code < 300:
            raise self._payload_error(data, resp.status_code)
        return data

    @staticmethod
    def _code_url(mailbox: dict[str, Any], *, after: str, keyword: str, wait_ms: int) -> str:
        raw_url = str(mailbox.get("api_url") or "").strip()
        parts = urlsplit(raw_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ICloudPrivacyMailError("invalid_code_url", "iCloud Privacy Mail 未返回有效的单邮箱 API 地址")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if after:
            query["after"] = after
        if keyword:
            query["keyword"] = keyword
        else:
            query.pop("keyword", None)
        if wait_ms > 0:
            query["wait_ms"] = str(wait_ms)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.api_base:
            raise RuntimeError("iCloud Privacy Mail 缺少 API Base")
        if not self.internal and not self.api_key:
            raise RuntimeError("iCloud Privacy Mail 缺少 API Key")
        headers = {"X-ChatGPT2API-Internal": "icloud-privacy-mail"} if self.internal else {"Authorization": f"Bearer {self.api_key}"}
        data = self._request_json(
            "POST",
            f"{self.api_base}/api/v1/mailboxes/claim",
            headers=headers,
            payload={"project": self.project, "purpose": self.purpose, "count": 1},
        )
        if data.get("success") is not True:
            raise self._payload_error(data)
        item = data.get("mailbox") if isinstance(data.get("mailbox"), dict) else {}
        address = str(item.get("email") or "").strip()
        api_url = str(item.get("api_url") or "").strip()
        if not address or not api_url:
            raise ICloudPrivacyMailError("invalid_response", "iCloud Privacy Mail 领取响应缺少 email 或 api_url")
        if item.get("api_active") is False:
            raise ICloudPrivacyMailError("api_disabled", "iCloud Privacy Mail 返回的邮箱 API 已停用")
        if item.get("icloud_active") is False:
            raise ICloudPrivacyMailError("icloud_inactive", "iCloud Privacy Mail 返回的邮箱 iCloud 状态不可用")
        self._code_url({"api_url": api_url}, after="", keyword="", wait_ms=0)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "api_url": api_url,
            "mailbox_id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "supports_passwordless_login": True,
            "_icloud_claim_project": self.project,
            "_icloud_claim_base": self.api_base,
            "_icloud_claim_internal": self.internal,
        }

    def update_claim(self, mailbox: dict[str, Any], claimed: bool) -> None:
        if not self.internal:
            return
        address = str(mailbox.get("address") or "").strip()
        if not address:
            return
        self._request_json(
            "POST",
            f"{self.api_base}/api/v1/mailboxes/claim-status",
            headers={"X-ChatGPT2API-Internal": "icloud-privacy-mail"},
            payload={"project": self.project, "emails": [address], "claimed": bool(claimed)},
        )

    def sync_existing_claims(self, emails: list[str], claimed: bool = True) -> dict[str, Any]:
        unique = list(dict.fromkeys(str(email or "").strip().lower() for email in emails if str(email or "").strip()))
        if not unique:
            return {"updated": 0, "missing": []}
        return self._request_json(
            "POST",
            f"{self.api_base}/api/v1/mailboxes/claim-status",
            headers={"X-ChatGPT2API-Internal": "icloud-privacy-mail"},
            payload={"project": self.project, "emails": unique[:500], "claimed": bool(claimed)},
        )

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        boundary = mailbox.get("_code_not_before")
        received_after = _parse_received_at(mailbox.get("_received_after"))
        if isinstance(boundary, datetime):
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
            if received_after and received_after > boundary:
                boundary = received_after
            after = (boundary - timedelta(seconds=10)).isoformat()
        elif received_after:
            after = (received_after - timedelta(seconds=10)).isoformat()
        else:
            after = ""
        keyword_override = mailbox.get("_icloud_keyword")
        keyword = self.keyword if keyword_override is None else str(keyword_override).strip()
        wait_override = mailbox.get("_icloud_request_wait_ms")
        wait_ms = int(self.wait_ms if wait_override is None else wait_override)
        url = self._code_url(mailbox, after=after, keyword=keyword, wait_ms=wait_ms)
        timeout = max(float(self.conf["request_timeout"]), wait_ms / 1000 + 5)
        try:
            data = self._request_json("GET", url, timeout=timeout)
        except ICloudPrivacyMailError as exc:
            if exc.retryable:
                return None
            raise
        if data.get("success") is not True:
            error = self._payload_error(data)
            if error.code == "no_code" or error.retryable:
                return None
            raise error
        code = _extract_code(
            {
                "subject": str(data.get("subject") or ""),
                "text_content": str(data.get("code") or ""),
                "html_content": "",
            }
        )
        if not code:
            raise ICloudPrivacyMailError("invalid_response", "iCloud Privacy Mail 成功响应缺少有效验证码")
        return {
            "provider": self.name,
            "mailbox": str(mailbox.get("address") or data.get("email") or ""),
            "message_id": str(data.get("message_id") or ""),
            "subject": str(data.get("subject") or f"{self.keyword or 'Account'} verification code"),
            "sender": "iCloud Privacy Mail",
            "text_content": code,
            "html_content": "",
            "received_at": _parse_received_at(data.get("received_at")),
            "raw": data,
        }

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            mailbox["_icloud_request_wait_ms"] = min(self.wait_ms, remaining_ms)
            try:
                message = self.fetch_latest_message(mailbox)
            finally:
                mailbox.pop("_icloud_request_wait_ms", None)
            if message:
                code = _extract_code(message)
                if code:
                    return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(0.2, self.conf["wait_interval"]), remaining))
        return None

    def close(self) -> None:
        self.session.close()


class MoEmailProvider(BaseMailProvider):
    name = "moemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.expiry_time = int(entry.get("expiry_time") or 0)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"MoEmail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"MoEmail {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/api/emails/generate", payload={"name": username or _random_mailbox_name(), "expiryTime": self.expiry_time, "domain": _next_domain(self.domain)}, expected=(200, 201))
        address = str(data.get("email") or "").strip()
        email_id = str(data.get("id") or data.get("email_id") or "").strip()
        if not address or not email_id:
            raise RuntimeError("MoEmail 缺少 email 或 id")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "email_id": email_id}

    def _load_message(self, mailbox: dict[str, Any], email_id: str, item: dict[str, Any]) -> dict[str, Any]:
        message_id = str(item.get("id") or item.get("message_id") or item.get("_id") or "").strip()
        if message_id:
            detail = self._cached_detail(f"moemail:{email_id}:{message_id}", lambda: self._request("GET", f"/api/emails/{email_id}/{message_id}"))
        else:
            detail = {"message": item}
        message = detail.get("message") if isinstance(detail.get("message"), dict) else detail
        text_content, html_content = _extract_content(message)
        sender = message.get("from") or message.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(message.get("subject") or item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(message.get("createdAt") or message.get("created_at") or message.get("receivedAt") or message.get("date") or message.get("timestamp") or item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": detail}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        email_id = str(mailbox.get("email_id") or "").strip()
        if not email_id:
            raise RuntimeError("MoEmail 缺少 email_id")
        data = self._request("GET", f"/api/emails/{email_id}")
        items = data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        ordered = sorted(
            enumerate(messages),
            key=lambda pair: (_received_at_timestamp(pair[1], "createdAt", "created_at", "receivedAt", "date", "timestamp"), pair[0]),
            reverse=True,
        )
        return [self._load_message(mailbox, email_id, item) for _, item in ordered[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class InbucketMailProvider(BaseMailProvider):
    name = "inbucket"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.random_subdomain = bool(entry.get("random_subdomain", True))
        self.session = _create_session(conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"Inbucket 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        content_type = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def _resolve_domain(self) -> str:
        if self.domain:
            return _next_domain(self.domain)
        raise RuntimeError("Inbucket 需要至少配置一个 domain")

    def _mailbox_name(self, address: str) -> str:
        local_part, _, _ = str(address or "").partition("@")
        return local_part.strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = username or _random_mailbox_name()
        base_domain = self._resolve_domain()
        domain = f"{_random_subdomain_label()}.{base_domain}" if self.random_subdomain else base_domain
        address = f"{local_part}@{domain}"
        mailbox_name = self._mailbox_name(address)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "base_domain": base_domain,
            "mailbox_name": mailbox_name,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        mailbox_name = str(mailbox.get("mailbox_name") or self._mailbox_name(str(mailbox.get("address") or ""))).strip()
        if not mailbox_name:
            raise RuntimeError("Inbucket 缺少 mailbox_name")
        data = self._request("GET", f"/api/v1/mailbox/{mailbox_name}")
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        items.sort(
            key=lambda value: (_received_at_timestamp(value, "date"), str(value.get("id") or "")),
            reverse=True,
        )
        address = str(mailbox.get("address") or "").strip()
        messages: list[dict[str, Any]] = []
        for item in items:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            detail = self._cached_detail(
                f"inbucket:{mailbox_name}:{message_id}",
                lambda message_id=message_id: self._request("GET", f"/api/v1/mailbox/{mailbox_name}/{message_id}"),
            )
            if not isinstance(detail, dict):
                continue
            header = detail.get("header") if isinstance(detail.get("header"), dict) else {}
            body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
            normalized = {
                "provider": self.name,
                "mailbox": mailbox_name,
                "message_id": message_id,
                "subject": str(detail.get("subject") or item.get("subject") or ""),
                "sender": str(detail.get("from") or item.get("from") or ""),
                "text_content": str(body.get("text") or ""),
                "html_content": str(body.get("html") or ""),
                "received_at": _parse_received_at(detail.get("date") or item.get("date")),
                "to": header.get("To") if isinstance(header, dict) else None,
                "raw": detail,
            }
            if _message_matches_email(normalized, address):
                messages.append(normalized)
                if len(messages) >= self.recent_scan_limit:
                    break
        return messages

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


class YydsMailProvider(BaseMailProvider):
    name = "yyds_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.subdomain = str(entry.get("subdomain") or "").strip()
        self.wildcard = bool(entry.get("wildcard"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"YYDSMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"YYDSMail 请求失败: {data.get('errorCode') or data.get('error')}")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("items") or data.get("messages") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {"localPart": username or _random_mailbox_name()}
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        if self.subdomain:
            payload["subdomain"] = self.subdomain
        data = self._request("POST", "/accounts/wildcard" if self.wildcard else "/accounts", payload=payload)
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or data.get("temp_token") or data.get("tempToken") or data.get("access_token") or "").strip()
        if not address or not token:
            raise RuntimeError("YYDSMail 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token, "account_id": str(data.get("id") or "")}

    def _load_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if message_id:
            token = str(mailbox.get("token") or "")
            address = mailbox["address"]
            item = self._cached_detail(f"yyds:{message_id}", lambda: self._request("GET", f"/messages/{message_id}", token=token, params={"address": address}))
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        messages.sort(key=lambda value: (_received_at_timestamp(value, "createdAt", "created_at", "receivedAt", "date", "timestamp"), str(value.get("id") or "")), reverse=True)
        return [self._load_message(mailbox, item) for item in messages[: self.recent_scan_limit]]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def close(self) -> None:
        self.session.close()


OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"


def _is_outlook_scope_denied(error: Exception | str) -> bool:
    text = str(error or "").lower()
    return (
        "aadsts70000" in text
        or ("scope" in text and ("unauthorized" in text or "expired" in text or "grant" in text))
    )


def _is_outlook_request_loop(error: Exception | str) -> bool:
    text = str(error or "").lower()
    return "aadsts50196" in text or "client request loop" in text


def _is_outlook_graph_unavailable(error: Exception | str) -> bool:
    return _is_outlook_scope_denied(error) or _is_outlook_request_loop(error)


def _is_outlook_transient_fetch_error(error: Exception | str) -> bool:
    if isinstance(error, (TimeoutError, imaplib.IMAP4.abort, OutlookTokenTransportError)):
        return True
    if isinstance(error, OSError) and not isinstance(error, imaplib.IMAP4.error):
        return True
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection closed",
            "connection aborted",
            "socket error",
            "eof",
            "authenticated but not connected",
            "connect tunnel failed",
            "curl: (7)",
            "curl: (28)",
            "curl: (35)",
            "curl: (52)",
            "curl: (55)",
            "curl: (56)",
            "curl: (97)",
        )
    )


class OutlookTokenError(RuntimeError):
    """refresh_token 换取 access_token 失败（凭据失效/权限不对），与“读邮件失败”区分。"""


class OutlookTokenRateLimitError(OutlookTokenError):
    """Microsoft OAuth 临时限流，不代表 refresh_token 已失效。"""


class OutlookTokenTransportError(OutlookTokenError):
    """刷新令牌时网络/代理/TLS 传输失败：与凭据是否有效无关，属瞬时错误。"""


def _clean_outlook_value(value: str) -> str:
    return str(value or "").replace("﻿", "").replace(" ", " ").strip()


def _mask_outlook_email(email: str) -> str:
    local, sep, domain = str(email or "").partition("@")
    if not sep:
        return "***"
    masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
    return f"{masked}@{domain}"


def _add_outlook_parse_issue(issues: list[dict[str, Any]], line_no: int, reason: str, email: str = "") -> None:
    if len(issues) >= 5:
        return
    issue: dict[str, Any] = {"line": line_no, "reason": reason}
    if email:
        issue["email"] = _mask_outlook_email(email)
    issues.append(issue)


def _parse_outlook_credentials_with_report(text: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """解析邮箱池文本，每行格式：email----password----client_id----refresh_token。"""
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    report: dict[str, Any] = {
        "raw_lines": 0,
        "non_empty": 0,
        "valid": 0,
        "duplicates": 0,
        "invalid": 0,
        "skipped": 0,
        "issues": [],
    }
    issues = report["issues"]
    for line_no, raw_line in enumerate(str(text or "").splitlines(), start=1):
        report["raw_lines"] += 1
        line = _clean_outlook_value(raw_line)
        if not line:
            continue
        report["non_empty"] += 1
        if "----" not in line:
            report["invalid"] += 1
            _add_outlook_parse_issue(issues, line_no, "缺少 ---- 分隔符")
            continue
        parts = [_clean_outlook_value(part) for part in line.split("----", 3)]
        if len(parts) != 4:
            report["invalid"] += 1
            _add_outlook_parse_issue(issues, line_no, "字段不足")
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email:
            report["invalid"] += 1
            _add_outlook_parse_issue(issues, line_no, "邮箱格式不正确", email)
            continue
        if not client_id:
            report["invalid"] += 1
            _add_outlook_parse_issue(issues, line_no, "缺少 client_id", email)
            continue
        if not refresh_token:
            report["invalid"] += 1
            _add_outlook_parse_issue(issues, line_no, "缺少 refresh_token", email)
            continue
        key = email.lower()
        if key in seen:
            report["duplicates"] += 1
            _add_outlook_parse_issue(issues, line_no, "重复邮箱，已合并", email)
            continue
        seen.add(key)
        credentials.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    report["valid"] = len(credentials)
    report["skipped"] = int(report["duplicates"]) + int(report["invalid"])
    return credentials, report


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    return _parse_outlook_credentials_with_report(text)[0]


def inspect_outlook_credentials(text: str) -> dict[str, Any]:
    return _parse_outlook_credentials_with_report(text)[1]


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _normalize_int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def outlook_alias_supported(email: str) -> bool:
    _, sep, domain = str(email or "").strip().lower().partition("@")
    if not sep:
        return False
    return (
        domain == "outlook.com"
        or domain == "hotmail.com"
        or domain == "live.com"
        or domain == "msn.com"
        or domain.startswith("hotmail.")
        or domain.startswith("outlook.")
    )


def outlook_alias_address(email: str, tag: str) -> str:
    local, sep, domain = str(email or "").strip().partition("@")
    if not sep:
        return email
    base_local = local.split("+", 1)[0]
    return f"{base_local}+{tag}@{domain}"


def outlook_alias_tag(prefix: str, index: int) -> str:
    clean_prefix = re.sub(r"[^A-Za-z0-9._-]+", "", str(prefix or "").strip()) or "c2api"
    return f"{clean_prefix}{index}"


def expand_outlook_aliases(credentials: list[dict[str, str]], entry: dict | None = None) -> list[dict[str, str]]:
    source = entry if isinstance(entry, dict) else {}
    enabled = _normalize_bool(source.get("alias_enabled"), False)
    per_email = _normalize_int(source.get("alias_per_email"), 0, 0, 200)
    include_original = _normalize_bool(source.get("alias_include_original"), True)
    prefix = str(source.get("alias_prefix") or "c2api").strip() or "c2api"
    if not enabled or per_email <= 0:
        return credentials

    expanded: list[dict[str, str]] = []
    seen: set[str] = set()
    for credential in credentials:
        original = str(credential.get("login_email") or credential.get("email") or "").strip()
        if include_original and credential.get("email"):
            key = str(credential["email"]).strip().lower()
            if key not in seen:
                expanded.append(dict(credential))
                seen.add(key)
        if not outlook_alias_supported(original):
            continue
        for index in range(1, per_email + 1):
            alias_email = outlook_alias_address(original, outlook_alias_tag(prefix, index))
            key = alias_email.lower()
            if key in seen:
                continue
            expanded.append({
                **credential,
                "email": alias_email,
                "login_email": original,
                "alias_of": original,
            })
            seen.add(key)
    return expanded


def _is_outlook_token_rate_limited(status_code: int, detail: str) -> bool:
    text = str(detail or "").lower()
    return (
        status_code == 429
        or "aadsts90055" in text
        or "excessive request rate" in text
        or "aadsts50196" in text
        or "client request loop" in text
    )


def _retry_after_seconds(resp: Any, fallback: float) -> float:
    value = ""
    try:
        value = str(resp.headers.get("Retry-After") or "").strip()
    except Exception:
        value = ""
    if value:
        try:
            return max(0.5, min(30.0, float(value)))
        except ValueError:
            pass
    return fallback


def _normalize_outlook_pool(value: Any, entry: dict | None = None) -> list[dict[str, str]]:
    """邮箱池既支持纯文本，也支持对象列表；按 provider 配置展开 Outlook 加号别名。"""
    source = entry if isinstance(entry, dict) else {}
    items: list[dict[str, str]] = []
    if isinstance(value, str):
        items = parse_outlook_credentials(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                items.extend(parse_outlook_credentials(item))
            elif isinstance(item, dict):
                email = _clean_outlook_value(item.get("email") or item.get("address") or "")
                client_id = _clean_outlook_value(item.get("client_id") or "")
                refresh_token = _clean_outlook_value(item.get("refresh_token") or "")
                if "@" in email and client_id and refresh_token:
                    login_email = _clean_outlook_value(item.get("login_email") or item.get("alias_of") or email)
                    payload = {
                        "email": email,
                        "password": _clean_outlook_value(item.get("password") or ""),
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                    }
                    if login_email and login_email != email:
                        payload["login_email"] = login_email
                        payload["alias_of"] = _clean_outlook_value(item.get("alias_of") or login_email)
                    items.append(payload)
    return expand_outlook_aliases(items, source)


class OutlookTokenProvider(BaseMailProvider):
    """使用 refresh_token 读取 Outlook/Hotmail 邮箱验证码。

    邮箱池在应用配置里维护（mailboxes 字段，每行 email----password----client_id----refresh_token），
    create_mailbox() 从池中取下一个未使用的邮箱，wait_for_code() 用 refresh_token 换取 access_token
    后通过 Graph/IMAP 读取最新邮件。
    """

    name = "outlook_token"

    def __init__(self, entry: dict, conf: dict):
        # 微软 token / Graph 走公开 SOCKS 会 curl 97/35 直接断连：Outlook 读信始终直连，
        # 不受 mail.api_use_register_proxy 影响（该开关只对 GPTMail 等 HTTP 邮箱 API 生效）。
        # "direct" 是显式直连引用，不会再回落到全局/运行时代理。
        conf = {**conf, "proxy": "direct"}
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = _normalize_outlook_pool(entry.get("mailboxes") or entry.get("pool"), entry)
        self.mode = str(entry.get("mode") or "auto").strip().lower() or "auto"
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "auto"
        self.imap_host = str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip() or OUTLOOK_DEFAULT_IMAP_HOST
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.session = _create_session(conf)
        self._imap_connection: imaplib.IMAP4_SSL | None = None
        self._imap_mailbox_key = ""

    def close(self) -> None:
        self._close_imap_connection()
        self.session.close()

    def _close_imap_connection(self) -> None:
        imap = self._imap_connection
        self._imap_connection = None
        self._imap_mailbox_key = ""
        if imap is None:
            return
        try:
            imap.logout()
        except Exception:
            pass

    def _rebuild_direct_session(self) -> None:
        """隧道类错误后丢弃旧会话，按直连重建（不带任何代理）。"""
        self.conf = {**self.conf, "proxy": "direct"}
        try:
            self.session.close()
        except Exception:
            pass
        self.session = _create_session(self.conf)

    def _exchange_refresh_token(self, client_id: str, refresh_token: str, scope: str) -> str:
        max_attempts = 3
        last_detail = ""
        last_status = 0
        tunnel_retried = False
        for attempt in range(max_attempts):
            try:
                resp = self.session.post(
                    OUTLOOK_TOKEN_URL,
                    data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scope},
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.conf["user_agent"]},
                    timeout=self.conf["request_timeout"],
                    verify=False,
                )
            except Exception as error:
                last_detail = str(error)[:300]
                if _is_proxy_tunnel_error(error) and not tunnel_retried:
                    tunnel_retried = True
                    self._rebuild_direct_session()
                    time.sleep(0.4)
                    continue
                # 传输层失败不是凭据问题：分类后上报，由调用方按瞬时错误处理，不让原始异常冒泡。
                raise OutlookTokenTransportError(f"OutlookToken 刷新请求失败: {last_detail}") from error
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.status_code == 200:
                access_token = str(data.get("access_token") or "").strip()
                if not access_token:
                    raise OutlookTokenError("OutlookToken 刷新响应缺少 access_token")
                return access_token

            detail = str(data.get("error_description") or data.get("error") or resp.text[:300])
            last_detail = detail
            last_status = int(resp.status_code)
            if _is_outlook_request_loop(detail):
                raise OutlookTokenRateLimitError(
                    f"OutlookToken 刷新被 Microsoft 暂时限制: HTTP {last_status}, {detail}"
                )
            if _is_outlook_token_rate_limited(last_status, detail) and attempt < max_attempts - 1:
                delay = _retry_after_seconds(resp, 1.5 * (attempt + 1) + random.uniform(0.5, 1.5))
                time.sleep(delay)
                continue
            if _is_outlook_token_rate_limited(last_status, detail):
                raise OutlookTokenRateLimitError(f"OutlookToken 刷新被 Microsoft 限流: HTTP {last_status}, {detail}")
            raise OutlookTokenError(f"OutlookToken 刷新失败: HTTP {last_status}, {detail}")
        raise OutlookTokenRateLimitError(f"OutlookToken 刷新被 Microsoft 限流: HTTP {last_status}, {last_detail}")

    def _access_token(self, mailbox: dict[str, Any], client_id: str, refresh_token: str, scope: str) -> str:
        """缓存 access_token 复用：避免 wait_for_code 轮询时每次都换 token 触发限流。"""
        cache = mailbox.get("_outlook_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_outlook_token_cache"] = cache
        cached = cache.get(scope)
        if isinstance(cached, tuple) and len(cached) == 2 and time.monotonic() < cached[1]:
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, refresh_token, scope)
        cache[scope] = (token, time.monotonic() + 600)
        return token

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError("OutlookToken 邮箱池为空，请在邮箱配置中导入 email----password----client_id----refresh_token")
        credential, lease_token, waitable = _claim_outlook_credential_ex(self.pool)
        deadline = time.monotonic() + max(0.0, float(OUTLOOK_CLAIM_WAIT_SECONDS))
        # 只是被“瞬时失败冷却 / 同家族别名正在注册”挡住时，等一会儿再领，而不是直接判池子用尽。
        while credential is None and waitable and time.monotonic() < deadline:
            time.sleep(max(0.01, min(float(OUTLOOK_CLAIM_POLL_SECONDS), deadline - time.monotonic())))
            credential, lease_token, waitable = _claim_outlook_credential_ex(self.pool)
        if credential is None:
            raise RuntimeError(f"[{self.label}] OutlookToken 邮箱池暂无可用邮箱（共 {len(self.pool)} 个，已用尽或全部占用/失效），请导入新邮箱或重置池状态")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": credential["email"],
            "login_email": credential.get("login_email") or credential["email"],
            "alias_of": credential.get("alias_of", ""),
            "label": self.label,
            "password": credential.get("password", ""),
            "client_id": credential["client_id"],
            "refresh_token": credential["refresh_token"],
            "_outlook_lease_token": lease_token,
        }

    def _read_graph(self, access_token: str) -> list[dict[str, Any]]:
        request_kwargs = {
            "headers": {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            "params": {"$top": self.message_limit, "$orderby": "receivedDateTime desc", "$select": "subject,receivedDateTime,from,toRecipients,ccRecipients,body,bodyPreview"},
            "timeout": self.conf["request_timeout"],
            "verify": False,
        }
        try:
            resp = self.session.get(OUTLOOK_GRAPH_MESSAGES_URL, **request_kwargs)
        except Exception as error:
            if not _is_proxy_tunnel_error(error):
                raise
            # 隧道类错误：清掉代理按直连重建会话，只重试一次。
            self._rebuild_direct_session()
            time.sleep(0.4)
            resp = self.session.get(OUTLOOK_GRAPH_MESSAGES_URL, **request_kwargs)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else resp.text[:300]
            raise RuntimeError(f"OutlookToken Graph 失败: HTTP {resp.status_code}, {detail}")
        items = data.get("value") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _graph_sender(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        if isinstance(sender, dict):
            address = sender.get("emailAddress") or {}
            if isinstance(address, dict):
                return str(address.get("address") or address.get("name") or "")
        return ""

    @staticmethod
    def _graph_recipients(message: dict[str, Any]) -> list[str]:
        recipients: list[str] = []
        for key in ("toRecipients", "ccRecipients"):
            values = message.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                address = item.get("emailAddress") if isinstance(item, dict) and isinstance(item.get("emailAddress"), dict) else {}
                value = str(address.get("address") or address.get("name") or "").strip()
                if value:
                    recipients.append(value)
        return recipients

    def _normalize_graph_item(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        content_type = str(body.get("contentType") or "").lower()
        content = str(body.get("content") or "")
        text_content = content if content_type != "html" else str(item.get("bodyPreview") or "")
        html_content = content if content_type == "html" else ""
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": self._graph_sender(item),
            "to": self._graph_recipients(item),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedDateTime")),
            "raw": item,
        }

    def _graph_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件（Graph 已按 receivedDateTime desc 排序，最新在前）。"""
        return [self._normalize_graph_item(mailbox, item) for item in self._read_graph(access_token)]

    def _imap_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件，最新在前。"""
        login_email = str(mailbox.get("login_email") or mailbox["address"]).strip()
        mailbox_key = login_email.lower()
        if self._imap_connection is not None and self._imap_mailbox_key != mailbox_key:
            self._close_imap_connection()
        if self._imap_connection is None:
            auth_string = f"user={login_email}\x01auth=Bearer {access_token}\x01\x01"
            imap = imaplib.IMAP4_SSL(
                self.imap_host,
                timeout=max(1.0, float(self.conf["request_timeout"])),
            )
            try:
                imap.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
                status, _ = imap.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError("OutlookToken IMAP select INBOX 失败")
            except Exception:
                try:
                    imap.logout()
                except Exception:
                    pass
                raise
            self._imap_connection = imap
            self._imap_mailbox_key = mailbox_key
        imap = self._imap_connection
        try:
            status, data = imap.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-self.message_limit :]
            messages: list[dict[str, Any]] = []
            for uid in reversed(uids):  # 最新在前
                status, fetched = imap.uid("fetch", uid, "(INTERNALDATE RFC822)")
                if status != "OK":
                    continue
                raw_payload = b""
                internal_received = None
                for part in fetched:
                    if not (isinstance(part, tuple) and isinstance(part[1], bytes)):
                        continue
                    meta = part[0].decode("utf-8", "replace") if isinstance(part[0], bytes) else str(part[0])
                    match = re.search(r'INTERNALDATE "([^"]+)"', meta)
                    if match:
                        try:
                            parsed = imaplib.Internaldate2tuple(b'INTERNALDATE "' + match.group(1).encode() + b'"')
                            if parsed:
                                internal_received = datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
                        except Exception:
                            internal_received = None
                    raw_payload = part[1]
                    break
                if raw_payload:
                    messages.append(self._parse_imap_message(mailbox, raw_payload, internal_received))
            return messages
        except Exception:
            self._close_imap_connection()
            raise

    def _parse_imap_message(self, mailbox: dict[str, Any], raw: bytes, internal_received: datetime | None = None) -> dict[str, Any]:
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = internal_received or _parse_received_at(parsedate_to_datetime(str(message.get("Date") or "")))
        except Exception:
            received = internal_received
        plain: list[str] = []
        html: list[str] = []
        for part in (message.walk() if message.is_multipart() else [message]):
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if not payload:
                continue
            if part.get_content_type() == "text/html":
                html.append(str(payload))
            else:
                plain.append(str(payload))

        def _decode(value: str | None) -> str:
            if not value:
                return ""
            try:
                return str(make_header(decode_header(value)))
            except Exception:
                return value

        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": _decode(str(message.get("Message-ID") or "")),
            "subject": _decode(str(message.get("Subject") or "")),
            "sender": _decode(str(message.get("From") or "")),
            "to": _decode(str(message.get("To") or "")),
            "delivered_to": _decode(str(message.get("Delivered-To") or "")),
            "x_forwarded_to": _decode(str(message.get("X-Forwarded-To") or "")),
            "x_original_to": _decode(str(message.get("X-Original-To") or "")),
            "text_content": "\n".join(plain).strip(),
            "html_content": "\n".join(html).strip(),
            "received_at": received,
            "raw": None,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        """拉取最近 N 封邮件（最新在前），供 wait_for_code 逐封扫描验证码。"""
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("OutlookToken mailbox 缺少 client_id 或 refresh_token")
        errors: list[str] = []
        graph_unavailable = bool(mailbox.get("_outlook_graph_unavailable"))
        if self.mode in {"graph", "auto"} and not graph_unavailable:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_GRAPH_SCOPE)
                return self._graph_messages(mailbox, access_token)
            except Exception as error:
                if _is_outlook_graph_unavailable(error):
                    graph_unavailable = True
                    mailbox["_outlook_graph_unavailable"] = True
                elif self.mode == "graph":
                    raise
                # Older Outlook refresh tokens are commonly IMAP-only. Graph
                # scope rejection and temporary request-loop protection are
                # compatibility branches, not proof that the token is invalid.
                if not graph_unavailable:
                    errors.append(f"graph: {error}")
        should_try_imap = self.mode in {"imap", "auto"} or (
            self.mode == "graph" and graph_unavailable
        )
        if should_try_imap:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_IMAP_SCOPE)
                return self._imap_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "imap":
                    raise
                errors.append(f"imap: {error}")
                if self.mode == "graph":
                    raise RuntimeError("; ".join(errors)) from error
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        """轮询时遍历最近 N 封邮件，逐封提取验证码，避免最新一封是广告/安全提醒时错过验证码。"""
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        tried_value = mailbox.setdefault("_tried_otp_codes", [])
        if not isinstance(tried_value, list):
            tried_value = []
            mailbox["_tried_otp_codes"] = tried_value
        tried_codes = {str(item) for item in tried_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        target_address = str(mailbox.get("address") or "").strip()
        last_transient_error: Exception | None = None
        transient_failures = 0
        successful_reads = 0
        while time.monotonic() < deadline:
            try:
                messages = self.fetch_recent_messages(mailbox)
                successful_reads += 1
                last_transient_error = None
                transient_failures = 0
            except Exception as error:
                if not _is_outlook_transient_fetch_error(error):
                    raise
                last_transient_error = error
                transient_failures += 1
                base_interval = max(0.2, self.conf["wait_interval"])
                time.sleep(min(15.0, base_interval * transient_failures))
                continue
            for message in messages:
                if _message_before_code_boundary(mailbox, message):
                    continue
                if target_address and not _message_matches_email(message, target_address):
                    continue
                received_after = mailbox.get("_received_after")
                received_at = message.get("received_at")
                if received_after and received_at:
                    try:
                        threshold = datetime.fromisoformat(str(received_after))
                        if threshold.tzinfo is None:
                            threshold = threshold.replace(tzinfo=timezone.utc)
                        current = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
                        if current < threshold:
                            continue
                    except Exception:
                        pass
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                code = _extract_code(message)
                if code:
                    seen_value.append(ref)
                    seen_refs.add(ref)
                    if code in tried_codes:
                        continue
                    tried_value.append(code)
                    tried_codes.add(code)
                    return code
                seen_refs.add(ref)
            time.sleep(max(0.2, self.conf["wait_interval"]))
        if last_transient_error is not None and successful_reads == 0:
            raise OutlookTokenTransportError(
                f"OutlookToken 邮箱查询持续失败: {last_transient_error}"
            ) from last_transient_error
        return None


class DomainMailProvider(BaseMailProvider):
    """自建域名：Cloudflare catch-all 转发到一个收信邮箱，IMAP 收码（实现见 domain_mail.py）。"""

    name = "custom_domain"

    def __init__(self, entry: dict, conf: dict):
        super().__init__({**conf, "proxy": ""}, str(entry.get("provider_ref") or ""))
        from services.register import domain_mail

        self._domain = domain_mail
        self.label = str(entry.get("label") or self.provider_ref)
        self.domains = domain_mail.parse_domains(entry.get("domains"))
        self.daily_limit = _normalize_int(entry.get("daily_limit_per_domain"), 10, 0, 1000)
        self.imap_host = str(entry.get("imap_host") or "").strip()
        self.imap_port = _normalize_int(entry.get("imap_port"), 993, 1, 65535)
        self.imap_user = str(entry.get("imap_user") or entry.get("forward_to") or "").strip()
        self.imap_password = str(entry.get("imap_password") or "").replace(" ", "").strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        address = self._domain.issue_address(self.domains, self.daily_limit)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "label": self.label}

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        messages = self._domain.fetch_imap_messages(
            self.imap_host,
            self.imap_user,
            self.imap_password,
            port=self.imap_port,
            limit=20,
            timeout=float(self.conf.get("request_timeout") or 20),
        )
        address = str(mailbox.get("address") or "")
        result = []
        for message in messages:
            if self._domain.message_addressed_to(message, address):
                result.append({**message, "provider": self.name, "mailbox": address})
        return result

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None


def _entries(mail_config: dict) -> list[dict]:
    result: list[dict] = []
    counters: dict[str, int] = {}
    for item in mail_config["providers"]:
        idx = len(result) + 1
        t = item.get("type", "")
        cnt = counters.get(t, 0) + 1
        counters[t] = cnt
        label = f"DDG-{cnt}" if t == "ddg_mail" else f"{t}#{idx}"
        stable_id = str(item.get("id") or item.get("provider_id") or "").strip()
        provider_ref = f"{item['type']}:{stable_id}" if stable_id else f"{item['type']}#{idx}"
        result.append({**item, "provider_ref": provider_ref, "label": label})
    return result


def _enabled_entries(mail_config: dict) -> list[dict]:
    items = [item for item in _entries(mail_config) if item.get("enable")]
    if not items:
        raise RuntimeError("mail.providers 没有启用的 provider")
    return items


def _next_entry(mail_config: dict, excluded_provider_refs: set[str] | None = None) -> dict:
    global provider_index
    excluded = {str(item or "").strip() for item in (excluded_provider_refs or set()) if str(item or "").strip()}
    items = [item for item in _enabled_entries(mail_config) if str(item.get("provider_ref") or "").strip() not in excluded]
    if not items:
        raise RuntimeError("没有剩余可用的邮箱提供商")
    if len(items) == 1:
        return dict(items[0])
    with provider_lock:
        value = dict(items[provider_index % len(items)])
        provider_index = (provider_index + 1) % len(items)
        return value


def _create_provider(
    mail_config: dict,
    provider: str = "",
    provider_ref: str = "",
    excluded_provider_refs: set[str] | None = None,
) -> BaseMailProvider:
    entry = next((dict(item) for item in _entries(mail_config) if provider_ref and item["provider_ref"] == provider_ref), None)
    entry = (
        entry
        or next((dict(item) for item in _enabled_entries(mail_config) if provider and item["type"] == provider), None)
        or _next_entry(mail_config, excluded_provider_refs)
    )
    conf = _config(mail_config)
    if entry["type"] == "cloudmail_gen":
        return CloudMailGenProvider(entry, conf)
    if entry["type"] == "cloudflare_temp_email":
        return CloudflareTempMailProvider(entry, conf)
    if entry["type"] == "ddg_mail":
        return DDGMailProvider(entry, conf)
    if entry["type"] == "tempmail_lol":
        return TempMailLolProvider(entry, conf)
    if entry["type"] == "duckmail":
        return DuckMailProvider(entry, conf)
    if entry["type"] == "gptmail":
        return GptMailProvider(entry, conf)
    if entry["type"] in {"donemail", "done_mail"}:
        return DoneMailProvider(entry, conf)
    if entry["type"] in {"icloud_api", "icloud_local"}:
        return ICloudPrivacyMailProvider(entry, conf)
    if entry["type"] == "moemail":
        return MoEmailProvider(entry, conf)
    if entry["type"] == "inbucket":
        return InbucketMailProvider(entry, conf)
    if entry["type"] == "yyds_mail":
        return YydsMailProvider(entry, conf)
    if entry["type"] == "outlook_token":
        return OutlookTokenProvider(entry, conf)
    if entry["type"] == "custom_domain":
        return DomainMailProvider(entry, conf)
    raise RuntimeError(f"不支持的 mail.provider: {entry['type']}")


def create_mailbox(
    mail_config: dict,
    username: str | None = None,
    excluded_provider_refs: set[str] | None = None,
) -> dict:
    excluded = {str(item or "").strip() for item in (excluded_provider_refs or set()) if str(item or "").strip()}
    enabled = [item for item in _enabled_entries(mail_config) if str(item.get("provider_ref") or "").strip() not in excluded]
    if not enabled:
        raise RuntimeError("没有剩余可用的邮箱提供商")
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config, excluded_provider_refs=excluded)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            mailbox = provider.create_mailbox(username)
            mailbox["_code_not_before"] = datetime.now(timezone.utc)
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
            # 到达每日上限的来源跳过，换下一个已启用的来源。
            # 到达每日上限 / 被限流的来源跳过，换下一个已启用的来源。
            if (
                "DDG日上限已达" not in last_error
                and type(error).__name__ != "DomainMailExhausted"
                and not is_mailbox_creation_rate_limited(error)
            ):
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法创建邮箱")


def sync_icloud_claims(mail_config: dict, project: str, emails: list[str]) -> dict[str, Any]:
    """把已有 GPT/Grok 账号邮箱同步到当前系统 iCloud 邮箱标签。"""
    entries = [
        dict(item)
        for item in _entries(mail_config)
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "icloud_local"
    ]
    if not entries:
        return {"updated": 0, "missing": [], "skipped": True}
    entry = entries[0]
    entry["project"] = str(project or "openai").strip().lower() or "openai"
    provider = ICloudPrivacyMailProvider(entry, _config(mail_config))
    try:
        updated = 0
        missing: list[str] = []
        for offset in range(0, len(emails), 500):
            result = provider.sync_existing_claims(emails[offset:offset + 500])
            updated += int(result.get("updated") or 0)
            missing.extend(str(item) for item in (result.get("missing") or []))
        return {"updated": updated, "missing": missing}
    finally:
        provider.close()


def wait_for_code(mail_config: dict, mailbox: dict, *, wait_timeout: float | None = None) -> str | None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        if wait_timeout is not None:
            provider.conf = {**provider.conf, "wait_timeout": max(1.0, float(wait_timeout))}
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def _icloud_mailbox_result_claimed(*, success: bool, error: Exception | str | None) -> bool:
    if success:
        return True
    reason = str(error or "").strip().lower()
    return any(
        marker in reason
        for marker in (
            "account_deactivated",
            "deleted or deactivated",
            "openaiemailalreadyregistered",
            "already registered",
            "existing account found",
            "account already exists",
            "already exists",
            "already in use",
            "已存在 gpt 账号",
        )
    )


def mark_mailbox_result(mailbox: dict, *, success: bool, error: Exception | str | None = None) -> None:
    """注册流程结束后更新邮箱池状态。

    iCloud 邮箱按 GPT/Grok 项目独立更新注册标签；Outlook Token 邮箱继续记录
    used、token_invalid、login_required 或 failed 状态。
    """
    if str(mailbox.get("provider") or "") == ICloudPrivacyMailProvider.name and bool(mailbox.get("_icloud_claim_internal")):
        try:
            provider = ICloudPrivacyMailProvider(
                {
                    "type": "icloud_local",
                    "api_base": mailbox.get("_icloud_claim_base"),
                    "project": mailbox.get("_icloud_claim_project") or "openai",
                    "purpose": "register",
                    "keyword": "xAI" if str(mailbox.get("_icloud_claim_project")) == "grok" else "OpenAI",
                },
                {"request_timeout": 15, "wait_timeout": 15, "wait_interval": 1, "user_agent": "chatgpt2api", "proxy": ""},
            )
            try:
                provider.update_claim(
                    mailbox,
                    _icloud_mailbox_result_claimed(success=success, error=error),
                )
            finally:
                provider.close()
        except Exception:
            pass
        return
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return
    lease = str(mailbox.get("_outlook_lease_token") or "")
    if success:
        _outlook_clear_transient(address)
        _set_outlook_token_state(address, "used", lease_token=lease)
        mark_outlook_family_used(mailbox)
        return
    reason = str(error or "").strip()
    category = classify_outlook_failure(error)
    if category == "transient":
        count = _outlook_note_transient_release(
            address,
            cooldown=OUTLOOK_TRANSIENT_COOLDOWN_MAX_SECONDS if _is_outlook_throttle_text(error) else None,
        )
        if count > OUTLOOK_TRANSIENT_RELEASE_LIMIT:
            # 同一邮箱连续多次瞬时失败：不再放回，交给人工看一眼，避免空转烧请求。
            _outlook_clear_transient(address)
            _set_outlook_token_state(
                address,
                "failed",
                f"连续 {count} 次瞬时失败：{reason}"[:300],
                lease_token=lease,
            )
        else:
            _release_outlook_token_state(address, lease_token=lease)
        return
    if category == "token_invalid":
        _set_outlook_token_state(address, "token_invalid", reason[:300], lease_token=lease)
        login_email = str(mailbox.get("login_email") or mailbox.get("alias_of") or "").strip()
        if login_email and login_email.lower() != address.lower():
            _set_outlook_token_state(login_email, "token_invalid", reason[:300])
    elif category == "login_required":
        _set_outlook_token_state(address, "login_required", reason[:300], lease_token=lease)
        login_email = str(mailbox.get("login_email") or mailbox.get("alias_of") or "").strip()
        if login_email and login_email.lower() != address.lower():
            _set_outlook_token_state(login_email, "login_required", reason[:300])
    else:
        _set_outlook_token_state(address, "failed", reason[:300], lease_token=lease)


_OUTLOOK_TRANSIENT_MARKERS = (
    "cloudflare",
    "cf_challenge",
    "等待注册验证码超时",
    "验证码超时",
    "验证码投递超时",
    "未收到验证码",
    "邮箱未收到任何邮件",
    "sentinel_so_token",
    "sentinel_req",
    "connect tunnel",
    "curl:",
    "timeout",
    "timed out",
    "proxy",
    "ssl",
    "connection",
    "temporarily",
    "rate_limit_exceeded",
    "too many requests",
    "unsupported_country",
    "territory not supported",
    "invalid_state",
    "sign-in session is no longer valid",
    "passwordless_send_otp_http_409",
    "login_continue_http_409",
    "platform_authorize_http_400",
    "platform_authorize_http_403",
    "platform_authorize_error",
    "signup_switched_to_login",
    "error 1015",
)
_OUTLOOK_TRANSIENT_STATUS_RE = re.compile(r"(?<!\d)(429|502|503|504)(?!\d)")
# 凭据本身的确定性问题（账号被锁/停用/不存在/密码或授权失效）：不是重试能解决的。
_OUTLOOK_TOKEN_FATAL_MARKERS = (
    "aadsts50053",
    "aadsts50057",
    "aadsts50034",
    "aadsts50126",
    "aadsts70000",
    "aadsts70008",
    "aadsts700082",
    "aadsts65001",
    "invalid_grant",
    "account is locked",
    "account has been locked",
    "账号被锁",
)


def _is_outlook_throttle_text(error: Exception | str | None) -> bool:
    text = str(error or "")
    return (
        isinstance(error, OutlookTokenRateLimitError)
        or "AADSTS90055" in text
        or "Microsoft 限流" in text
        or "Microsoft 暂时限制" in text
    )


def classify_outlook_failure(error: Exception | str | None) -> str:
    """把注册失败归类：transient（放回池）/ token_invalid / login_required / failed。

    只有确定性错误（账号被锁、凭据无效、登录流异常）才会落成不可再用的状态；
    网络、Cloudflare、限流、5xx、invalid_state 这类瞬时错误放回池里重试，不烧邮箱。
    """
    if isinstance(error, OutlookTokenTransportError):
        return "transient"
    if isinstance(error, OutlookTokenRateLimitError):
        return "transient"
    text = str(error or "")
    lowered = text.lower()
    if isinstance(error, OutlookTokenError):
        return "token_invalid"
    if "登录流" in text or "login flow" in lowered or "login_required" in lowered:
        return "login_required"
    if any(marker in lowered for marker in _OUTLOOK_TOKEN_FATAL_MARKERS):
        return "token_invalid"
    if "OutlookToken 刷新失败: HTTP" in text:
        return "token_invalid"
    if _is_outlook_throttle_text(text) or "aadsts50196" in lowered:
        return "transient"
    if any(marker in lowered for marker in _OUTLOOK_TRANSIENT_MARKERS) or _OUTLOOK_TRANSIENT_STATUS_RE.search(lowered):
        return "transient"
    if "access_token" in text:
        return "token_invalid"
    return "failed"


def mark_outlook_family_used(mailbox: dict) -> None:
    """家族里已经有号了：把原邮箱记成 family_used，其余别名跟着不可领用。

    仅别名展开产生的邮箱（带 alias_of/login_email）生效；原邮箱已有更强状态时不覆盖。
    """
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    address = str(mailbox.get("address") or "").strip().lower()
    base = str(mailbox.get("login_email") or mailbox.get("alias_of") or "").strip().lower()
    if not base or base == address:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        current = str((store.get(base) or {}).get("state") or "")
        if current in OUTLOOK_UNAVAILABLE_STATES or current == "in_use":
            return
    _set_outlook_token_state(base, "family_used", f"别名 {address} 已注册")


def release_mailbox(mailbox: dict) -> None:
    """把 outlook_token 邮箱从 in_use 释放回未使用（用于流程主动放弃且未消费验证码时）。"""
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    _release_outlook_token_state(
        str(mailbox.get("address") or ""),
        lease_token=str(mailbox.get("_outlook_lease_token") or ""),
    )


def get_existing_mailbox(mail_config: dict, email: str) -> dict:
    """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            if hasattr(provider, "get_existing_mailbox"):
                mailbox = provider.get_existing_mailbox(email)
                return mailbox
            else:
                raise RuntimeError(f"邮箱提供商 {provider.name} 不支持查询已有邮箱")
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法查询已有邮箱")

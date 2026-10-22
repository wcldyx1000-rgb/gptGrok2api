from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from threading import Condition, Lock, Thread
from typing import Any

from services.account_processing import account_processing_slot, account_processing_worker_count
from services.account_credentials import oauth_session_kwargs, project_upstream_credential_availability
from services.account_scheduler import WeightedFairScheduler
from services.account_usage import ImageUsageTracker
from services.checkout_url import is_final_checkout_url, is_final_pix_url, is_final_upi_url
from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend, StorageMutation, StorageRevisionConflictError
from utils.diagnostics import sanitize_diagnostic_text, scrub_account_error_fields
from utils.helper import anonymize_token


class ImageAccountSelectionError(RuntimeError):
    """图片账号调度失败。

    这是“本次请求为什么没拿到账号”的错误，不等同于账号持久状态。
    账号是否限流/异常仍由远程配额确认或鉴权结果决定。
    """

    # 控制流只认两个结果：
    #   quota_exhausted -> 远程确认额度耗尽，告诉客户端别重试（429）
    #   unavailable     -> 其它一切（没号/全忙/预检失败/上游波动），可重试（503）
    DEFAULTS: dict[str, tuple[int, str, str]] = {
        "quota_exhausted": (429, "insufficient_quota", "insufficient_quota"),
        "unavailable": (503, "server_error", "no_available_account"),
        # 请求级结果（不代表账号池状态）：等号超过请求截止 / 排队人数已满。
        "deadline_exceeded": (503, "server_error", "account_busy_timeout"),
        "queue_full": (503, "server_error", "account_queue_full"),
    }

    def __init__(self, kind: str, message: str = "") -> None:
        defaults = self.DEFAULTS.get(kind, self.DEFAULTS["unavailable"])
        self.kind = kind if kind in self.DEFAULTS else "unavailable"
        self.status_code, self.error_type, self.code = defaults
        detail = message or self.kind.replace("_", " ")
        super().__init__(f"image_account_selection:{self.kind}; {detail}")


class OAuthRefreshError(RuntimeError):
    """RT 换 AT 失败的结构化错误：保留上游状态码、错误码和（截断的）描述。

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """

    def __init__(self, status_code: int, error_code: str = "", description: str = "") -> None:
        self.status_code = int(status_code or 0)
        self.error_code = str(error_code or "").strip()
        self.description = str(description or "").strip()[:300]
        details = [f"oauth_refresh_http_{self.status_code}"]
        if self.error_code:
            details.append(self.error_code)
        if self.description and self.description.casefold() != self.error_code.casefold():
            details.append(self.description)
        super().__init__(": ".join(details))


class TerminalRefreshTokenError(OAuthRefreshError):
    """RT 已被吊销、过期或复用：再试也救不回来，账号需要重新登录。"""

    def __init__(self, status_code: int, error_code: str = "", description: str = "") -> None:
        super().__init__(status_code, error_code, description)
        try:
            from services.image_failure import image_failure
            self.failure = image_failure("auth_invalid", raw_detail=str(self))
        except Exception:  # pragma: no cover - 分类模块不可用时不影响刷新流程
            pass


class RefreshCredentialsChangedError(RuntimeError):
    """刷新期间账号凭据已被别处替换（并发轮换、手动更新、账号被删）：本次结果作废，不覆盖新凭据。"""

    def __init__(self) -> None:
        message = "OAuth refresh credentials changed during refresh."
        try:
            from services.image_failure import image_failure
            self.failure = image_failure("upstream_unavailable", raw_detail=message)
        except Exception:  # pragma: no cover
            pass
        super().__init__(message)


# (access_token, refresh_token, last_token_refresh_at)：凭据“代”。
_CredentialGeneration = tuple[str, str, str]


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_SECONDS = 3 * 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS = 6 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE = 3
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    _POOL_HEALTH_REFRESH_BATCH_SIZE = 10
    _IMAGE_FAILURE_REFRESH_COOLDOWN_SECONDS = 30
    _IMAGE_FAILURE_REFRESH_MAX_CONCURRENT = 2
    # No account weighs more than this multiple of a typical account, so one
    # account reporting a huge quota cannot absorb the whole pool's traffic.
    _IMAGE_SCHEDULE_MAX_WEIGHT_RATIO = 4.0
    _AUTO_PAUSE_FAILURE_STREAK = 3
    _AUTO_PAUSE_STRIKE_WINDOW_SECONDS = 6 * 60 * 60
    # 连续失败多少次触发一次短时冷却（也是自动暂停的一次“计次”）。
    UPSTREAM_FAILURE_COOLDOWN_STREAK = 3
    # 待核验标记超过这个时间仍未出结果（核验线程丢了/进程重启）就视为过期，账号重新参与调度。
    _PENDING_AUTH_STALE_SECONDS = 600
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()

    @staticmethod
    def ready_pix_checkout_url(account: dict | None) -> str:
        """Return the persisted Pix instructions URL only for a ready account."""
        if not isinstance(account, dict):
            return ""
        if str(account.get("checkout_link_status") or "").strip().lower() != "ready":
            return ""
        for key in ("checkout_final_url", "checkout_hosted_instructions_url", "checkout_url"):
            value = str(account.get(key) or "").strip()
            if is_final_pix_url(value):
                return value
        return ""

    @staticmethod
    def ready_upi_checkout_url(account: dict | None) -> str:
        """Return the persisted UPI instructions URL only for a ready account."""
        if not isinstance(account, dict):
            return ""
        if str(account.get("checkout_link_status") or "").strip().lower() != "ready":
            return ""
        for key in ("checkout_final_url", "checkout_hosted_instructions_url", "checkout_url"):
            value = str(account.get(key) or "").strip()
            if is_final_upi_url(value):
                return value
        return ""

    @classmethod
    def ready_checkout_url(cls, account: dict | None, checkout_channel: str = "") -> str:
        """Return a complete persisted instructions URL for the requested channel."""
        channel = str(checkout_channel or "").strip().lower()
        if channel == "pix":
            return cls.ready_pix_checkout_url(account)
        if channel == "upi":
            return cls.ready_upi_checkout_url(account)
        if not isinstance(account, dict):
            return ""
        if str(account.get("checkout_link_status") or "").strip().lower() != "ready":
            return ""
        for key in ("checkout_final_url", "checkout_hosted_instructions_url", "checkout_url"):
            value = str(account.get(key) or "").strip()
            if is_final_checkout_url(value):
                return value
        return ""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        # 按凭据代单飞：同一代凭据的并发刷新只发一次 OAuth 请求，不同账号互不阻塞。
        self._oauth_refresh_flights_lock = Lock()
        self._oauth_refresh_flights: dict[_CredentialGeneration, Future] = {}
        self._limited_release_lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        # 对话/搜索账号并发槽位（运行时状态，按 access_token 计数，轮换时迁移）。
        self._text_slot_condition = Condition(self._lock)
        self._text_inflight: dict[str, int] = {}
        # 上游连续失败（对话/搜索）计数与短时冷却截止（time.monotonic）；图片失败走 _image_failure_streaks。
        self._upstream_failure_streaks: dict[str, int] = {}
        self._upstream_cooldown_until: dict[str, float] = {}
        self._device_id_lock = Lock()
        self._auth_verify_lock = Lock()
        self._auth_verify_active: set[str] = set()
        # Runtime-only rotation state, keyed by access token (migrated on rotation).
        self._image_scheduler = WeightedFairScheduler()
        self._text_scheduler = WeightedFairScheduler()
        # Sliding-window image usage per account and pool selection pressure,
        # read by services.account_health. Guarded by self._lock.
        self._image_usage = ImageUsageTracker()
        # Consecutive image failures per account; a run of
        # _AUTO_PAUSE_FAILURE_STREAK counts as one auto-pause strike.
        self._image_failure_streaks: dict[str, int] = {}
        # 上次成功持久化时的账号内容（深拷贝），_save_accounts 只写与之不同的账号。
        self._persisted_accounts: dict[str, dict] = {}
        self._accounts_revision: str | None = None
        self._accounts_revision_checked_at = 0.0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}
        self._image_waiters = 0
        # monotonic time when each account last finished an image request; drives
        # image_account_cooldown_secs.
        self._image_released_at: dict[str, float] = {}
        self._image_failure_refresh_lock = Lock()
        self._image_failure_refresh_active: set[str] = set()
        self._image_failure_refresh_started_at: dict[str, float] = {}
        self._token_aliases: dict[str, str] = {}
        self._cumulative_total = self._load_cumulative_total()
        self._backfill_management_ids()

    def _get_cumulative_file(self) -> Path:
        storage_path = getattr(self.storage, "file_path", None)
        if isinstance(storage_path, Path):
            return storage_path.with_name(".cumulative_total")
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        try:
            payload = str(token or "").split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            import base64
            import json
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    # ---- 稳定管理 ID ----
    # 账号以 access_token 为存储键，而 token 会轮换；management_id 持久化在账号记录里，
    # 首次出现时由 access_token 的哈希派生（多进程各自补齐得到相同结果），之后随记录走、
    # 不再随 token 变化。对外 API 以 `id` 暴露。

    @staticmethod
    def _management_id_for_token(access_token: str, salt: int = 0) -> str:
        raw = str(access_token or "") if not salt else f"{access_token}#{salt}"
        return f"acct_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"

    @classmethod
    def _normalize_management_id(cls, value: object, access_token: str) -> str:
        candidate = str(value or "").strip().lower()
        suffix = candidate[5:] if candidate.startswith("acct_") else ""
        if len(suffix) == 24 and all(char in "0123456789abcdef" for char in suffix):
            return candidate
        return cls._management_id_for_token(access_token)

    @classmethod
    def _unique_management_id(cls, access_token: str, preferred: object, used_ids: set[str]) -> str:
        candidate = cls._normalize_management_id(preferred, access_token)
        salt = 0
        while candidate in used_ids:
            salt += 1
            candidate = cls._management_id_for_token(access_token, salt)
        return candidate

    @classmethod
    def _dedupe_management_ids(cls, accounts: dict[str, dict]) -> None:
        """保证一批账号的 management_id 互不相同；冲突的后来者换新 ID。"""
        used: set[str] = set()
        for token, account in accounts.items():
            management_id = cls._unique_management_id(token, account.get("management_id"), used)
            if management_id != account.get("management_id"):
                account["management_id"] = management_id
            used.add(management_id)

    _STORAGE_MUTATION_MAX_ATTEMPTS = 3

    def _supports_account_mutations(self) -> bool:
        return bool(getattr(self.storage, "supports_account_mutations", False))

    def _read_accounts_snapshot(self) -> tuple[dict[str, dict], str | None]:
        load_snapshot = getattr(self.storage, "load_accounts_snapshot", None)
        if callable(load_snapshot):
            snapshot = load_snapshot()
            items, revision = snapshot.items, snapshot.revision
        else:
            items, revision = self.storage.load_accounts(), None
        accounts = {
            normalized["access_token"]: normalized
            for item in items
            if (normalized := self._normalize_account(item)) is not None
        }
        # 记录磁盘上还没有（或冲突的）management_id，调用方据此把补齐结果落盘。
        raw_ids = {
            str(item.get("access_token") or item.get("accessToken") or ""): str(item.get("management_id") or "").strip().lower()
            for item in items
            if isinstance(item, dict)
        }
        self._dedupe_management_ids(accounts)
        self._ids_to_backfill = {
            token for token, account in accounts.items()
            if raw_ids.get(token) != account.get("management_id")
        }
        return accounts, revision

    def _load_accounts(self) -> dict[str, dict]:
        accounts, revision = self._read_accounts_snapshot()
        self._persisted_accounts = self._baseline_without_backfill(accounts)
        self._accounts_revision = revision
        self._accounts_revision_checked_at = time.monotonic()
        return accounts

    def _baseline_without_backfill(self, accounts: dict[str, dict]) -> dict[str, dict]:
        """持久化基线：尚未写盘的 management_id 不计入，这样下一次保存会把它落盘。"""
        baseline = deepcopy(accounts)
        for token in getattr(self, "_ids_to_backfill", set()):
            if token in baseline:
                baseline[token].pop("management_id", None)
        return baseline

    def _backfill_management_ids(self) -> None:
        """启动时把为旧账号补齐的 management_id 写回存储（失败不影响启动）。"""
        if not getattr(self, "_ids_to_backfill", None):
            return
        try:
            with self._lock:
                self._save_accounts()
        except Exception:
            pass

    def _account_mutation(self) -> StorageMutation:
        baseline = self._persisted_accounts
        return StorageMutation(
            upserts=tuple(
                account
                for token, account in self._accounts.items()
                if baseline.get(token) != account
            ),
            delete_keys=tuple(token for token in baseline if token not in self._accounts),
            expected_revision=self._accounts_revision,
        )

    def _advance_persisted_baseline(self, mutation: StorageMutation) -> None:
        for token in mutation.delete_keys:
            self._persisted_accounts.pop(token, None)
        for account in mutation.upserts:
            self._persisted_accounts[str(account.get("access_token") or "")] = deepcopy(account)

    def _merge_remote_accounts_locked(self) -> None:
        """另一个写入者修改过存储：以远端为准，再叠加本进程尚未持久化的变更。"""
        remote, revision = self._read_accounts_snapshot()
        baseline = self._persisted_accounts
        merged = dict(remote)
        for token, account in self._accounts.items():
            if baseline.get(token) != account:
                merged[token] = account
        for token in baseline:
            if token not in self._accounts:
                merged.pop(token, None)
        self._accounts = merged
        self._persisted_accounts = self._baseline_without_backfill(remote)
        self._accounts_revision = revision
        self._accounts_revision_checked_at = time.monotonic()

    def _save_accounts(self) -> None:
        """持久化账号变更（调用方持有 self._lock）。

        只写与上次持久化基线不同的账号；没有变化时不触碰存储。支持修订号的后端
        做乐观并发检查，冲突时合并远端数据后重试。
        """
        for attempt in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            mutation = self._account_mutation()
            if mutation.empty:
                return
            if not self._supports_account_mutations():
                self.storage.save_accounts(list(self._accounts.values()))
                self._advance_persisted_baseline(mutation)
                return
            try:
                result = self.storage.mutate_accounts(mutation)
            except StorageRevisionConflictError:
                if attempt + 1 >= self._STORAGE_MUTATION_MAX_ATTEMPTS:
                    raise
                self._merge_remote_accounts_locked()
                continue
            self._advance_persisted_baseline(mutation)
            self._accounts_revision = result.revision
            self._accounts_revision_checked_at = time.monotonic()
            return

    def sync_accounts_from_storage(self, *, max_age_seconds: float = 15.0) -> bool:
        """其它进程改过账号时重新加载；修订号未变则只做一次轻量查询。返回是否重新加载。"""
        if not self._supports_account_mutations():
            return False
        if time.monotonic() - self._accounts_revision_checked_at < max_age_seconds:
            return False
        try:
            revision = self.storage.load_accounts_revision()
        except Exception:
            return False
        with self._lock:
            self._accounts_revision_checked_at = time.monotonic()
            if revision is None or revision == self._accounts_revision:
                return False
            self._merge_remote_accounts_locked()
            self._image_slot_condition.notify_all()
            return True

    @classmethod
    def _credentials_usable(cls, account: dict) -> bool:
        """AT 已失效（过期/被拒）且没有可用 RT 的账号既不能执行也无法恢复，不参与调度。"""
        availability = project_upstream_credential_availability(
            str(account.get("access_token") or ""),
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=str(account.get("last_remote_check_result") or "").strip().lower() == "invalid",
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )
        return availability.status != "unavailable"

    @classmethod
    def _auth_verification_pending(cls, account: dict, now: datetime | None = None) -> bool:
        """账号处于“AT 失效待核验”：401 之后核验结果出来之前，暂不接新请求。"""
        if str(account.get("last_remote_check_result") or "") != "pending":
            return False
        since = cls._parse_time(account.get("pending_auth_at") or account.get("last_remote_check_attempt_at"))
        if since is None:
            return True
        age = ((now or datetime.now(timezone.utc)) - since).total_seconds()
        return age <= cls._PENDING_AUTH_STALE_SECONDS

    @classmethod
    def _is_image_account_available(cls, account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if cls._new_account_blocks_image(account):
            return False
        if cls._auth_verification_pending(account) or not cls._credentials_usable(account):
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        # quota 是展示/预估值，不能作为持久调度开关。
        # 只有远程确认后写入的“限流”状态才代表图片额度耗尽；否则 quota=0 也要允许进入预检，
        # 避免本地扣减或额度重置不同步时把账号锁死在候选池外。
        return account.get("status") == "正常" or int(account.get("quota") or 0) > 0

    @classmethod
    def _is_unlimited_image_quota_account(cls, account: dict) -> bool:
        if not isinstance(account, dict) or not bool(account.get("image_quota_unknown")):
            return False
        account_type = (cls._normalize_account_type(account.get("type")) or "").lower()
        return account_type in {"pro", "prolite"}

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    # codex 生图模型的选号条件：做过 Codex 登录的账号（任意套餐），或原有的
    # codex 来源 + Plus/Team/Pro 账号。只用于选号，不改变 "codex" 来源本身的含义。
    CODEX_IMAGE_SOURCE = "codex_image"
    CODEX_IMAGE_PLAN_TYPES = ("plus", "team", "pro")

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        if source_type == cls.CODEX_IMAGE_SOURCE:
            if account.get("codex_access_token"):
                return True
            return (
                cls._normalize_source_type(account.get("source_type")) == "codex"
                and cls._account_matches_any_plan_type(account, cls.CODEX_IMAGE_PLAN_TYPES)
            )
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        return str(value or "web").strip().lower() or "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key) or raw

    @staticmethod
    def _has_value(value: object) -> bool:
        return value is not None and str(value).strip() != ""

    @staticmethod
    def _bool_value(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        raw = str(value or "").strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "none", "null", ""}:
            return False
        return default

    @classmethod
    def _quota_value(cls, value: object, default: int = 0) -> int:
        if not cls._has_value(value):
            return max(0, int(default or 0))
        try:
            return max(0, int(float(str(value).strip())))
        except (TypeError, ValueError):
            return max(0, int(default or 0))

    @classmethod
    def _extract_image_quota_from_limits(cls, limits_progress: object) -> tuple[int | None, str | None, bool | None]:
        if not isinstance(limits_progress, list):
            return None, None, None
        if not limits_progress:
            return None, None, None

        for item in limits_progress:
            if not isinstance(item, dict):
                continue
            feature = str(
                item.get("feature_name")
                or item.get("feature")
                or item.get("name")
                or item.get("type")
                or ""
            ).strip().lower()
            if feature in {"image_gen", "image_generation", "image", "images"}:
                restore_at = str(
                    item.get("reset_after")
                    or item.get("restore_at")
                    or item.get("reset_at")
                    or ""
                ).strip() or None
                return cls._quota_value(item.get("remaining"), 0), restore_at, False

        return None, None, True

    def _search_account_type(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("plan_type", "account_plan", "account_type", "subscription_type", "type"):
                plan = self._normalize_account_type(payload.get(key))
                if plan:
                    return plan
            for value in payload.values():
                plan = self._search_account_type(value)
                if plan:
                    return plan
        elif isinstance(payload, list):
            for value in payload:
                plan = self._search_account_type(value)
                if plan:
                    return plan
        return None

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        normalized["management_id"] = self._normalize_management_id(normalized.get("management_id"), access_token)
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        limits_progress = normalized.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        derived_quota, derived_restore_at, derived_unknown = self._extract_image_quota_from_limits(limits_progress)
        has_explicit_quota = self._has_value(normalized.get("quota"))
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        if not has_explicit_quota and derived_quota is not None:
            normalized["quota"] = derived_quota
        normalized["quota"] = self._quota_value(normalized.get("quota"), 0)
        if derived_unknown is not None and not self._has_value(normalized.get("image_quota_unknown")):
            normalized["image_quota_unknown"] = derived_unknown
        normalized["image_quota_unknown"] = self._bool_value(normalized.get("image_quota_unknown"), False)
        if (
            normalized["source_type"] == "codex"
            and normalized["quota"] == 0
            and not limits_progress
            and normalized.get("status") not in {"限流", "异常", "禁用"}
            and not normalized.get("last_token_refresh_at")
            and not normalized.get("last_used_at")
        ):
            normalized["image_quota_unknown"] = True
        normalized["limits_progress"] = limits_progress
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        if derived_restore_at and not normalized.get("restore_at"):
            normalized["restore_at"] = derived_restore_at
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_remote_checked_at"] = normalized.get("last_remote_checked_at") or None
        normalized["last_remote_check_attempt_at"] = normalized.get("last_remote_check_attempt_at") or None
        normalized["last_remote_check_error"] = normalized.get("last_remote_check_error") or None
        normalized["last_remote_check_error_at"] = normalized.get("last_remote_check_error_at") or None
        normalized["last_remote_check_event"] = normalized.get("last_remote_check_event") or None
        normalized["last_remote_check_result"] = normalized.get("last_remote_check_result") or None
        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        return normalized

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountService._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str) -> int | None:
        exp = cls._jwt_exp(access_token)
        if exp <= 0:
            return None
        return exp - int(time.time())

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        try:
            iat = int(cls._decode_jwt_payload(access_token).get("iat") or 0)
        except (TypeError, ValueError):
            return None
        if iat <= 0:
            return None
        return datetime.fromtimestamp(iat, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    @staticmethod
    def _credential_generation(access_token: str, account: dict | None) -> _CredentialGeneration:
        item = account or {}
        return (
            str(item.get("access_token") or access_token or "").strip(),
            str(item.get("refresh_token") or "").strip(),
            str(item.get("last_token_refresh_at") or "").strip(),
        )

    def _credential_snapshot(self, access_token: str) -> tuple[str, str, dict | None]:
        resolved, account = self._get_account_for_token(access_token)
        if not account:
            return resolved, "", None
        active_token = str(account.get("access_token") or resolved or access_token).strip()
        return active_token, str(account.get("refresh_token") or "").strip(), account

    @staticmethod
    def _credential_error_text(error: object, account: dict | None) -> str:
        """账号错误字段落盘/写日志前擦掉本账号的 AT/RT/ID Token、Outlook 凭据和代理密码。"""
        item = account or {}
        return sanitize_diagnostic_text(
            error,
            sensitive_values=(
                item.get("access_token"),
                item.get("refresh_token"),
                item.get("id_token"),
                item.get("outlook_refresh_token"),
                item.get("outlook_password"),
                # 太短的密码会把正常文字也擦掉，只擦足够长的。
                *(
                    secret
                    for secret in (item.get("password"), item.get("totp_secret"))
                    if len(str(secret or "").strip()) >= 6
                ),
            ),
            proxy_values=(item.get("proxy"),),
            limit=2000,
        )

    def _record_token_refresh_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_credentials: _CredentialGeneration | None = None,
        terminal: bool = False,
    ) -> bool:
        """记录一次 RT 换 AT 失败。

        expected_credentials 给出时只在账号凭据还是发起刷新时的那一代才写入，避免旧代的失败
        盖在刚轮换好的新凭据上；terminal=True 同时写 refresh_token_invalid_at。保存失败会回滚内存。
        返回是否真的写入。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return False
            if expected_credentials is not None and self._credential_generation(resolved, current) != tuple(expected_credentials):
                return False
            next_item = dict(current)
            message = self._credential_error_text(error or "refresh token failed", current)
            next_item["last_token_refresh_error"] = message
            next_item["last_token_refresh_error_at"] = now
            if terminal:
                next_item["refresh_token_invalid_at"] = now
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[resolved] = account
            try:
                self._save_accounts()
            except Exception as exc:  # noqa: BLE001 - 保存失败回滚内存，不让半写状态留在池里
                self._accounts[resolved] = current
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "刷新错误保存失败",
                    {"source": event, "token": anonymize_token(resolved), "error": self._credential_error_text(exc, current)},
                )
                return False
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {"source": event, "token": anonymize_token(resolved), "error": message, "terminal": bool(terminal)},
        )
        return True

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _recent_refresh_token_keepalive_error(self, account: dict, now: datetime) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (now - last_error_at).total_seconds() < self._REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS

    def _refresh_token_keepalive_anchor(self, account: dict) -> datetime | None:
        return (
            self._parse_time(account.get("last_token_refresh_at"))
            or self._token_issued_at(str(account.get("access_token") or ""))
            or self._parse_time(account.get("created_at"))
        )

    def _refresh_token_keepalive_due_at(self, account: dict, now: datetime) -> datetime | None:
        # ChatGPT web sessions are issued for the ChatGPT browser client.  They
        # must not be refreshed with the legacy Platform OAuth client below.
        if self._normalize_source_type(account.get("source_type")) == "chatgpt_web":
            return None
        if not str(account.get("refresh_token") or "").strip():
            return None
        if account.get("status") == "禁用":
            return None
        if self._recent_refresh_token_keepalive_error(account, now):
            return None
        anchor = self._refresh_token_keepalive_anchor(account)
        if anchor is None:
            return now
        due_at = anchor + timedelta(seconds=self._REFRESH_TOKEN_KEEPALIVE_SECONDS)
        return due_at if due_at <= now else None

    @staticmethod
    def _oauth_refresh_error_fields(data: object) -> tuple[str, str]:
        """从 OAuth 错误响应里取 (error_code, description)；兼容 {error: "x"}、{error: {code, message}}。"""
        if not isinstance(data, dict):
            return "", ""
        error = data.get("error")
        nested = error if isinstance(error, dict) else {}
        code = str(
            nested.get("code")
            or data.get("code")
            or (error if isinstance(error, str) else "")
            or ""
        ).strip()
        description = str(
            data.get("error_description")
            or nested.get("message")
            or data.get("message")
            or nested.get("description")
            or ""
        ).strip()
        return code, description

    _TERMINAL_REFRESH_ERROR_CODES = frozenset({
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_invalidated",
        "refresh_token_expired",
        "refresh_token_reused",
        "token_revoked",
    })
    _TERMINAL_REFRESH_MESSAGE_FRAGMENTS = ("session has ended", "token has been revoked")
    _OAUTH_REFRESH_STATUS_RE = re.compile(r"oauth_refresh_http_(\d{3})")

    @classmethod
    def _is_terminal_refresh_error(cls, status_code: int, error_code: str, description: str) -> bool:
        """RT 是否已不可恢复。408/429/5xx 永不判终态；其余看错误码，或 4xx 加会话已结束类描述。

        单纯的 401（没有 invalid_grant 之类的错误码）可能只是偶发，不能据此判终态。
        """
        status_code = int(status_code or 0)
        if status_code in {408, 429} or status_code >= 500:
            return False
        if str(error_code or "").strip().casefold() in cls._TERMINAL_REFRESH_ERROR_CODES:
            return True
        normalized_description = str(description or "").strip().casefold()
        return 400 <= status_code < 500 and any(
            fragment in normalized_description for fragment in cls._TERMINAL_REFRESH_MESSAGE_FRAGMENTS
        )

    @classmethod
    def _is_terminal_refresh_text(cls, error: object) -> bool:
        """兼容只有文本的刷新错误（旧版本落盘的 last_token_refresh_error、被 mock 的异常）。"""
        text = str(error or "")
        match = cls._OAUTH_REFRESH_STATUS_RE.search(text)
        status = int(match.group(1)) if match else 0
        lowered = text.lower()
        code = next((item for item in cls._TERMINAL_REFRESH_ERROR_CODES if item in lowered), "")
        return cls._is_terminal_refresh_error(status, code, text)

    @classmethod
    def _refresh_exception_is_terminal(cls, exc: BaseException) -> bool:
        if isinstance(exc, TerminalRefreshTokenError):
            return True
        if isinstance(exc, OAuthRefreshError):
            return False
        return cls._is_terminal_refresh_text(exc)

    def _request_access_token_refresh(self, refresh_token: str, account: dict | None = None) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        # UA、sec-ch-ua* 与 TLS 目标同源，避免 UA 写 Chrome/145 而 curl_cffi 补出 Chrome110 的 Client Hints。
        session = requests.Session(**proxy_settings.build_session_kwargs(
            account=account,
            **oauth_session_kwargs(self._OAUTH_USER_AGENT, "chrome110"),
            verify=True,
        ))
        try:
            with account_processing_slot():
                response = session.post(
                    self._OAUTH_TOKEN_URL,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": self._OAUTH_USER_AGENT,
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._OAUTH_CLIENT_ID,
                    },
                    timeout=60,
                )
            raw_text = self._safe_response_text(response)
            try:
                data = response.json() if raw_text else {}
            except Exception:
                data = {}
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
                error_code, description = self._oauth_refresh_error_fields(data)
                description = description or raw_text
                error_type = (
                    TerminalRefreshTokenError
                    if self._is_terminal_refresh_error(status_code, error_code, description)
                    else OAuthRefreshError
                )
                raise error_type(status_code, error_code, description)
            return {
                "access_token": str(data.get("access_token") or "").strip(),
                "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
                "id_token": str(data.get("id_token") or "").strip(),
            }
        finally:
            session.close()

    def _migrate_token_state_locked(self, old_token: str, new_token: str) -> None:
        """access_token 轮换：旧键迁移到新键（账号记录、别名、在途计数、调度状态）。管理 ID 随记录不变。"""
        self._accounts.pop(old_token, None)
        self._token_aliases[old_token] = new_token
        old_inflight = int(self._image_inflight.pop(old_token, 0))
        if old_inflight:
            self._image_inflight[new_token] = int(self._image_inflight.get(new_token, 0)) + old_inflight
        old_released_at = self._image_released_at.pop(old_token, None)
        if old_released_at is not None:
            self._image_released_at[new_token] = old_released_at
        self._image_scheduler.rename(old_token, new_token)
        self._text_scheduler.rename(old_token, new_token)
        old_streak = self._image_failure_streaks.pop(old_token, None)
        if old_streak is not None:
            self._image_failure_streaks[new_token] = old_streak
        old_text = int(self._text_inflight.pop(old_token, 0))
        if old_text:
            self._text_inflight[new_token] = int(self._text_inflight.get(new_token, 0)) + old_text
        old_text_streak = self._upstream_failure_streaks.pop(old_token, None)
        if old_text_streak is not None:
            self._upstream_failure_streaks[new_token] = old_text_streak
        old_cooldown = self._upstream_cooldown_until.pop(old_token, None)
        if old_cooldown is not None:
            self._upstream_cooldown_until[new_token] = old_cooldown

    def _clear_credential_failure_state_locked(self, access_token: str, next_item: dict) -> None:
        """新凭据装上后，清掉旧凭据留下的失败痕迹（调用方持有 self._lock）。

        包括：需重登标记与重登失败计数、RT 失效标记、刷新/远程核验错误、异常状态、待核验标记、
        自动暂停与 strike、连续失败计数和短时冷却。apply_relogin_tokens 与 update_credentials 共用。
        """
        for key in ("relogin_needed_at", "relogin_reason", "last_relogin_error", "refresh_token_invalid_at"):
            next_item.pop(key, None)
        next_item["relogin_attempts"] = 0
        for key in (
            "last_token_refresh_error",
            "last_token_refresh_error_at",
            "last_invalid_at",
            "last_refresh_error",
            "last_refresh_error_at",
        ):
            next_item[key] = None
        next_item["invalid_count"] = 0
        if next_item.get("last_remote_check_result") in {"pending", "invalid"}:
            next_item["last_remote_check_result"] = None
            next_item["last_remote_check_error"] = None
            next_item["last_remote_check_error_at"] = None
        self._clear_pending_auth_fields(next_item)
        if next_item.get("status") == "异常":
            next_item["status"] = "正常"
            next_item["image_quota_unknown"] = True
        self._clear_auto_pause_fields(next_item)
        self._image_failure_streaks.pop(access_token, None)
        self._upstream_failure_streaks.pop(access_token, None)
        self._upstream_cooldown_until.pop(access_token, None)

    def update_credentials(self, account_id: str, *, access_token: str = "", refresh_token: str = "") -> dict | None:
        """手动改某账号的 AT / RT（按稳定管理 ID 定位）。空串表示不修改。

        AT 变化时账号记录迁移到新键但管理 ID 不变；新 AT 已属于另一个账号则抛 ValueError。
        凭据真的变了就按 apply_relogin_tokens 的口径清掉旧凭据留下的失败状态（需重登标记、异常状态、
        自动暂停、待核验标记、失败冷却）。保存失败回滚内存。返回更新后的账号副本，账号不存在返回 None。
        """
        management_id = str(account_id or "").strip().lower()
        new_access = str(access_token or "").strip()
        new_refresh = str(refresh_token or "").strip()
        if not management_id:
            return None
        with self._image_slot_condition:
            current_token = next(
                (
                    token for token, account in self._accounts.items()
                    if str(account.get("management_id") or "").strip().lower() == management_id
                ),
                "",
            )
            if not current_token:
                return None
            current = self._accounts[current_token]
            target_token = new_access or current_token
            if target_token != current_token and target_token in self._accounts:
                raise ValueError("access_token already belongs to another account")
            rotated = target_token != current_token
            refresh_changed = bool(new_refresh) and new_refresh != str(current.get("refresh_token") or "")
            next_item = dict(current)
            next_item["access_token"] = target_token
            if refresh_changed:
                next_item["refresh_token"] = new_refresh
            if rotated or refresh_changed:
                self._clear_credential_failure_state_locked(current_token, next_item)
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if rotated:
                self._accounts.pop(current_token, None)
            self._accounts[target_token] = account
            try:
                self._save_accounts()
            except Exception:
                if rotated:
                    self._accounts.pop(target_token, None)
                self._accounts[current_token] = current
                raise
            if rotated:
                self._migrate_token_state_locked(current_token, target_token)
            self._image_slot_condition.notify_all()
            result = dict(account)
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "手动更新账号凭据",
            {
                "token": anonymize_token(target_token),
                "access_token_changed": rotated,
                "refresh_token_changed": refresh_changed,
            },
        )
        return result

    def _apply_refreshed_tokens(
        self,
        old_access_token: str,
        token_data: dict,
        event: str,
        *,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """把刷新得到的新凭据写回账号。

        expected_credentials 给出时（RT 换 AT 路径）只在账号凭据仍是发起刷新的那一代才写，
        否则抛 RefreshCredentialsChangedError——手改或并发轮换出的新凭据不会被旧结果覆盖。
        保存失败回滚内存并向上抛。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                if expected_credentials is not None:
                    raise RefreshCredentialsChangedError()
                return old_token
            if expected_credentials is not None and self._credential_generation(old_token, current) != tuple(expected_credentials):
                raise RefreshCredentialsChangedError()
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token
            if new_token != old_token and new_token in self._accounts:
                # 新 AT 已经是池里另一个账号的了，写进去会覆盖别人。
                raise RefreshCredentialsChangedError()

            next_item = dict(current)
            next_item["access_token"] = new_token
            if token_data.get("refresh_token"):
                next_item["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            if token_data.get("id_token"):
                next_item["id_token"] = str(token_data.get("id_token") or "").strip()
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["refresh_token_invalid_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            if rotated:
                self._accounts.pop(old_token, None)
            self._accounts[new_token] = account
            try:
                self._save_accounts()
            except Exception:
                if rotated:
                    self._accounts.pop(new_token, None)
                self._accounts[old_token] = current
                raise
            if rotated:
                self._migrate_token_state_locked(old_token, new_token)
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def _refresh_access_token_owner(
        self,
        active_token: str,
        refresh_token: str,
        account: dict,
        *,
        event: str,
    ) -> str:
        """单飞里真正发 OAuth 请求的那一个线程。失败时按结构化判定写错误、必要时标记需重登。"""
        expected = self._credential_generation(active_token, account)
        try:
            token_data = self._request_access_token_refresh(refresh_token, account)
        except Exception as exc:
            _token, _refresh, current = self._credential_snapshot(active_token)
            if current is None or self._credential_generation(_token, current) != expected:
                # 请求期间凭据已被换代：这次失败说明不了新凭据的好坏。
                raise RefreshCredentialsChangedError() from exc
            terminal = self._refresh_exception_is_terminal(exc)
            recorded = self._record_token_refresh_error(
                active_token,
                event,
                str(exc or ""),
                expected_credentials=expected,
                terminal=terminal,
            )
            if not recorded:
                raise RefreshCredentialsChangedError() from exc
            if terminal:
                self.mark_account_relogin_needed(active_token, str(exc or ""))
                self._wake_relogin_service()
            raise
        return self._apply_refreshed_tokens(active_token, token_data, event, expected_credentials=expected)

    def _maintain_access_token(
        self,
        access_token: str,
        *,
        force: bool = False,
        event: str = "refresh_access_token",
        raise_on_error: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """按凭据代单飞的 AT 维护。

        同一代凭据（AT, RT, last_token_refresh_at）的并发调用只发一次 OAuth 请求，其余等结果；
        不同账号的刷新互不阻塞。凭据在途中换代时抛 RefreshCredentialsChangedError（传了
        expected_credentials 或 raise_on_error 时抛给调用方，否则按最新凭据重试一次）。
        默认（raise_on_error=False）不抛刷新错误，返回当前可用的 AT，与旧 refresh_access_token 一致。
        """
        if not access_token:
            return ""
        expected = tuple(expected_credentials) if expected_credentials is not None else None
        for attempt in range(2):
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                if raise_on_error or expected is not None:
                    raise RefreshCredentialsChangedError()
                return access_token
            active_token = str(account.get("access_token") or resolved_token or access_token)
            generation = self._credential_generation(active_token, account)
            if expected is not None and generation != expected:
                raise RefreshCredentialsChangedError()
            if self._normalize_source_type(account.get("source_type")) == "chatgpt_web":
                return active_token
            if not self._token_needs_refresh(active_token, force=force):
                return active_token
            refresh_token = str(account.get("refresh_token") or "").strip()
            if not refresh_token:
                return active_token
            if not force and (account.get("refresh_token_invalid_at") or self._recent_token_refresh_error(account)):
                return active_token

            with self._oauth_refresh_flights_lock:
                future = self._oauth_refresh_flights.get(generation)
                owner = future is None
                if future is None:
                    future = Future()
                    self._oauth_refresh_flights[generation] = future
            if owner:
                try:
                    result = self._refresh_access_token_owner(active_token, refresh_token, account, event=event)
                except BaseException as exc:  # noqa: BLE001 - 异常交给所有等待者
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            try:
                return future.result()
            except RefreshCredentialsChangedError:
                if expected is not None or raise_on_error:
                    raise
                latest_token, latest = self._get_account_for_token(active_token)
                if latest is None:
                    return access_token
                if attempt == 0 and self._credential_generation(latest_token, latest) != generation:
                    # 别处刚换了代：用最新凭据再评估一次（可能已经刷新好了）。
                    access_token = latest_token
                    continue
                return str(latest.get("access_token") or latest_token or active_token)
            except Exception:
                if raise_on_error:
                    raise
                latest_token, latest = self._get_account_for_token(active_token)
                return str((latest or {}).get("access_token") or latest_token or active_token)
            finally:
                if owner:
                    with self._oauth_refresh_flights_lock:
                        if self._oauth_refresh_flights.get(generation) is future:
                            self._oauth_refresh_flights.pop(generation, None)
        return access_token

    def refresh_access_token(self, access_token: str, *, force: bool = False, event: str = "refresh_access_token") -> str:
        """兼容入口：不抛刷新错误，返回当前可用的 AT。需要抛错/校验凭据代请用 ensure/force_refresh_access_token。"""
        return self._maintain_access_token(access_token, force=force, event=event)

    def ensure_access_token(
        self,
        access_token: str,
        *,
        event: str = "ensure_access_token",
        raise_on_error: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """AT 临期才用 RT 续期；返回可用的 AT。"""
        return self._maintain_access_token(
            access_token,
            force=False,
            event=event,
            raise_on_error=raise_on_error,
            expected_credentials=expected_credentials,
        )

    def force_refresh_access_token(
        self,
        access_token: str,
        *,
        event: str = "force_refresh_access_token",
        raise_on_error: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """显式恢复/管理操作：强制走一次 RT 换 AT（同一代凭据的并发强刷合并为一次）。"""
        return self._maintain_access_token(
            access_token,
            force=True,
            event=event,
            raise_on_error=raise_on_error,
            expected_credentials=expected_credentials,
        )

    def list_expiring_access_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for account in self._accounts.values()
                if self._normalize_source_type(account.get("source_type")) != "chatgpt_web"
                if str(account.get("refresh_token") or "").strip()
                and (token := str(account.get("access_token") or "").strip())
                and self._token_needs_refresh(token)
            ]

    def list_refresh_token_keepalive_tokens(self) -> list[str]:
        now = datetime.now(timezone.utc)
        due_items: list[tuple[datetime, str]] = []
        with self._lock:
            for account in self._accounts.values():
                due_at = self._refresh_token_keepalive_due_at(account, now)
                token = str(account.get("access_token") or "").strip()
                if due_at is not None and token:
                    due_items.append((due_at, token))
        due_items.sort(key=lambda item: item[0])
        return [token for _, token in due_items[: self._REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE]]

    def keepalive_refresh_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        for access_token in access_tokens:
            before = self.resolve_access_token(access_token)
            after = self.refresh_access_token(before, force=True, event="refresh_token_keepalive")
            account = self.get_account(after)
            if account and str(account.get("last_token_refresh_error") or "").strip():
                errors.append({
                    "token": anonymize_token(before),
                    "error": str(account.get("last_token_refresh_error") or "refresh token failed"),
                })
                continue
            if account:
                refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def image_queue_limit(self, ready_count: int) -> int:
        """等号排队上限：设置为 0 时取当前容量（可用账号数 x 单账号并发），相当于最多排一轮。

        Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
        """
        configured = int(config.image_queue_limit or 0)
        if configured > 0:
            return configured
        return max(1, ready_count) * max(1, int(config.image_account_concurrency or 1))

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            deadline_monotonic: float | None = None,
    ) -> str:
        wait_started = time.monotonic()
        waiting = False
        try:
            with self._image_slot_condition:
                while True:
                    remaining = (
                        float(deadline_monotonic) - time.monotonic()
                        if deadline_monotonic is not None
                        else None
                    )
                    if remaining is not None and remaining <= 0:
                        raise ImageAccountSelectionError(
                            "deadline_exceeded",
                            "image request deadline exceeded while waiting for an account slot",
                        )
                    ready_tokens = self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                    if not ready_tokens:
                        if not excluded_tokens:
                            self._image_usage.record_unavailable()
                        raise self._no_ready_candidate_error(plan_type, source_type, plan_types, excluded_tokens)
                    cooldown = float(config.image_account_cooldown_secs or 0)
                    now = time.monotonic()
                    cooldown_wait: float | None = None
                    tokens = []
                    for token in self._list_available_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types):
                        released_at = self._image_released_at.get(token) if cooldown > 0 else None
                        if released_at is not None:
                            # Pace each account like a person would: no new image request
                            # until the cooldown after its last one ended.
                            ready_in = released_at + cooldown - now
                            if ready_in > 0:
                                cooldown_wait = ready_in if cooldown_wait is None else min(cooldown_wait, ready_in)
                                continue
                        tokens.append(token)
                    if tokens:
                        pool_tokens = self._list_ready_candidate_tokens(None, plan_type, source_type, plan_types)
                        pool = (plan_type or "", source_type or "", tuple(sorted(plan_types or ())))
                        access_token = self._pick_image_token_locked(
                            self._prefer_serviceable_locked(tokens),
                            pool_tokens,
                            pool=pool,
                        )
                        self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                        waited = time.monotonic() - wait_started
                        if waited >= 0.05:
                            self._image_usage.record_wait(waited)
                        self._image_usage.record_lease(access_token)
                        return access_token
                    if not waiting and cooldown_wait is None:
                        # 真的要排队等空闲槽位：排队的人已经够一轮容量就直接报繁忙，
                        # 上限按整个可用池算（换号重试排除了试过的号，不能让上限跟着变小）。
                        pool_size = len(self._list_ready_candidate_tokens(None, plan_type, source_type, plan_types))
                        limit = self.image_queue_limit(max(pool_size, len(ready_tokens)))
                        if self._image_waiters >= limit:
                            raise ImageAccountSelectionError(
                                "queue_full",
                                f"{self._image_waiters} image requests already waiting (limit {limit})",
                            )
                        self._image_waiters += 1
                        waiting = True
                    wait_timeout = 1.0 if cooldown_wait is None else min(1.0, cooldown_wait)
                    if remaining is not None:
                        wait_timeout = min(wait_timeout, remaining)
                    self._image_slot_condition.wait(timeout=max(wait_timeout, 0.001))
        finally:
            if waiting:
                with self._image_slot_condition:
                    self._image_waiters = max(0, self._image_waiters - 1)

    def _no_ready_candidate_error(
            self,
            plan_type: str | None,
            source_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
            excluded_tokens: set[str] | None,
    ) -> "ImageAccountSelectionError":
        """没有任何 ready 候选时区分两种成因。

        这是“初筛阶段”的判据：只看本地缓存状态，此时还没走远程预检，
        不存在“预检失败”这一维度。匹配账号全部为“限流”（限流只由远程确认写入）
        -> 额度耗尽（429）；否则一律归为可重试的 unavailable（503）。

        注意：get_available_access_token 里的 429 判据更严
        （额外要求 not saw_unavailable_failure），因为那是“预检阶段”，
        会有上游波动等可重试失败混入，不能仅凭限流就下终结性结论。
        """
        excluded = set(excluded_tokens or set())
        matched = 0
        limited = 0
        for item in self._accounts.values():
            token = item.get("access_token") or ""
            if not token or token in excluded:
                continue
            if not (
                self._account_matches_plan_type(item, plan_type)
                and self._account_matches_any_plan_type(item, plan_types)
                and self._account_matches_source_type(item, source_type)
            ):
                continue
            matched += 1
            if str(item.get("status") or "") == "限流":
                limited += 1
        if matched > 0 and limited == matched:
            return ImageAccountSelectionError(
                "quota_exhausted",
                "all matched image accounts are remote-confirmed quota exhausted",
            )
        return ImageAccountSelectionError(
            "unavailable",
            "no image account is ready for current model/status filters",
        )

    def release_image_slot(self, access_token: str) -> float | None:
        """Free one in-flight slot; returns how long the lease was held, if tracked."""
        if not access_token:
            return None
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            held = None
            try:
                held = self._image_usage.record_release(access_token)
            finally:
                # 用量统计出任何问题都不能让槽位泄漏。
                current_inflight = int(self._image_inflight.get(access_token, 0))
                if current_inflight <= 1:
                    self._image_inflight.pop(access_token, None)
                else:
                    self._image_inflight[access_token] = current_inflight - 1
                if access_token in self._accounts:
                    self._image_released_at[access_token] = time.monotonic() + self._image_rest_jitter()
                self._image_slot_condition.notify_all()
        return held

    # 出图后休息时间的随机浮动范围（相对 image_account_cooldown_secs 的比例），避免所有账号节拍整齐划一。
    IMAGE_REST_JITTER = (0.7, 1.3)

    @classmethod
    def _image_rest_jitter(cls) -> float:
        """休息结束时刻的偏移秒数：休息 = cooldown * U(0.7, 1.3)，以“释放时刻前移/后移”实现。"""
        cooldown = float(config.image_account_cooldown_secs or 0)
        if cooldown <= 0:
            return 0.0
        low, high = cls.IMAGE_REST_JITTER
        return cooldown * (random.uniform(low, high) - 1.0)

    def _image_schedule_weights_locked(self, tokens: list[str]) -> dict[str, float]:
        """Estimate each pool member's remaining image quota as a scheduling weight.

        Known quota is used as-is. Accounts whose quota upstream does not report
        count as a typical account (median of known quotas); unlimited plans
        count as the largest known quota. Every weight is capped at a multiple
        of the typical account. Accounts drained to zero by local accounting
        rank last until a remote check confirms their quota again.
        """
        known_by_token: dict[str, float] = {}
        for token in tokens:
            account = self._accounts.get(token)
            if account and not account.get("image_quota_unknown"):
                quota = int(account.get("quota") or 0)
                if quota > 0:
                    known_by_token[token] = float(quota)
        known = sorted(known_by_token.values())
        typical = known[len(known) // 2] if known else 1.0
        ceiling = typical * self._IMAGE_SCHEDULE_MAX_WEIGHT_RATIO
        largest = min(known[-1], ceiling) if known else typical
        weights: dict[str, float] = {}
        for token in tokens:
            weight = known_by_token.get(token)
            if weight is None:
                account = self._accounts.get(token) or {}
                if self._is_unlimited_image_quota_account(account):
                    weight = largest
                elif self._quota_estimated_empty(account):
                    weight = typical / 10.0
                else:
                    weight = typical
            weights[token] = max(min(weight, ceiling), 0.01)
        return weights

    @staticmethod
    def _warm_preferred_tokens(tokens: list[str]) -> set[str]:
        try:
            from services.image_warm_pool import image_warm_pool
        except Exception:
            return set()
        has_fresh = getattr(image_warm_pool, "has_fresh", None)
        if not callable(has_fresh):
            return set()
        try:
            return {token for token in tokens if has_fresh(token)}
        except Exception:
            return set()

    def _pick_image_token_locked(self, tokens: list[str], pool_tokens: list[str], *, pool: tuple) -> str:
        if not tokens:
            return ""
        members = list(dict.fromkeys([*pool_tokens, *tokens]))
        weights = self._image_schedule_weights_locked(members)
        candidates = [(token, weights[token], int(self._image_inflight.get(token, 0))) for token in tokens]
        if len(self._image_scheduler) > 2 * len(self._accounts) + 16:
            self._image_scheduler.prune(self._accounts)
        picked = self._image_scheduler.pick(
            candidates,
            members=members,
            pool=pool,
            preferred=self._warm_preferred_tokens(tokens),
        )
        return picked or tokens[0]

    def _penalize_image_account_locked(self, access_token: str) -> None:
        if access_token not in self._accounts:
            return
        tokens = self._list_ready_candidate_tokens()
        if access_token not in tokens:
            tokens.append(access_token)
        weight = self._image_schedule_weights_locked(tokens)[access_token]
        self._image_scheduler.penalize(access_token, weight)

    def penalize_image_account(self, access_token: str, *, reason: str = "") -> None:
        """Deprioritize an account for a while without counting a failure.

        Used for failures that are not the account's fault (e.g. an upstream
        error reply); the fail counter and preflight freshness stay untouched.
        """
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            self._penalize_image_account_locked(access_token)
            # Counts as use (density), not as an account failure.
            self._image_usage.record_attempt(access_token, success=False, account_failure=False, failure_code=reason)

    def idle_image_tokens(self) -> list[str]:
        """Normal accounts with no image request in flight."""
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and (token := item.get("access_token") or "")
                   and not int(self._image_inflight.get(token, 0))
            ]

    @classmethod
    def _auto_pause_active(cls, account: dict, now: datetime | None = None) -> bool:
        until = cls._parse_time(account.get("auto_paused_until"))
        return until is not None and until > (now or datetime.now(timezone.utc))

    def _prefer_not_auto_paused_locked(self, tokens: list[str]) -> list[str]:
        """Auto pause only lowers priority: paused accounts serve when nothing else can."""
        now = datetime.now(timezone.utc)
        healthy = [token for token in tokens if not self._auto_pause_active(self._accounts.get(token) or {}, now)]
        return healthy or tokens

    def _apply_image_failure_streak_locked(self, access_token: str, next_item: dict, now: datetime) -> bool:
        """Count a consecutive image failure; returns True when the account just got auto-paused."""
        streak = self._image_failure_streaks.get(access_token, 0) + 1
        if streak < self._AUTO_PAUSE_FAILURE_STREAK:
            self._image_failure_streaks[access_token] = streak
            return False
        self._image_failure_streaks.pop(access_token, None)
        # 一轮连续失败：先短时冷却（不论是否达到自动暂停阈值）。
        self._begin_failure_cooldown_locked(access_token)
        threshold = int(config.account_auto_pause_strikes or 0)
        if threshold <= 0 or next_item.get("status") == "禁用" or self._auto_pause_active(next_item, now):
            return False
        window_start = now - timedelta(seconds=self._AUTO_PAUSE_STRIKE_WINDOW_SECONDS)
        strikes = [
            value for value in (next_item.get("failure_strikes") or [])
            if (parsed := self._parse_time(value)) is not None and parsed > window_start
        ]
        strikes.append(now.isoformat())
        if len(strikes) < threshold:
            next_item["failure_strikes"] = strikes
            return False
        hours = int(config.account_auto_pause_hours or 6)
        next_item.pop("failure_strikes", None)
        next_item["auto_paused_at"] = now.isoformat()
        next_item["auto_paused_until"] = (now + timedelta(hours=hours)).isoformat()
        next_item["auto_pause_reason"] = (
            f"6 小时内 {len(strikes)} 次连续失败（每次连续 {self._AUTO_PAUSE_FAILURE_STREAK} 次），自动暂停 {hours} 小时"
        )
        return True

    @staticmethod
    def _clear_auto_pause_fields(next_item: dict) -> bool:
        changed = bool(next_item.get("auto_paused_until") or next_item.get("failure_strikes"))
        for key in ("auto_paused_at", "auto_paused_until", "auto_pause_reason", "failure_strikes"):
            next_item.pop(key, None)
        return changed

    def clear_auto_pause(self, access_token: str) -> dict | None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            self._image_failure_streaks.pop(access_token, None)
            self._upstream_failure_streaks.pop(access_token, None)
            self._upstream_cooldown_until.pop(access_token, None)
            next_item = dict(current)
            if not self._clear_auto_pause_fields(next_item):
                return dict(current)
            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)

    # ---- refresh_token 失效后的重新登录（供 account_relogin_service 使用） ----

    def mark_account_relogin_needed(self, access_token: str, reason: str) -> dict | None:
        """Flag an account whose refresh_token can no longer renew it."""
        if not access_token:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            if not next_item.get("relogin_needed_at"):
                next_item["relogin_needed_at"] = now
            next_item["relogin_reason"] = self._credential_error_text(reason or "refresh token invalid", current)[:500]
            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)

    def list_relogin_needed_accounts(self) -> list[dict]:
        """Accounts flagged by mark_account_relogin_needed (disabled ones excluded)."""
        with self._lock:
            return [
                dict(item)
                for item in self._accounts.values()
                if item.get("relogin_needed_at") and item.get("status") != "禁用"
            ]

    def record_relogin_failure(self, access_token: str, error: str) -> dict | None:
        """Record one failed relogin attempt; the caller owns retry/backoff policy."""
        if not access_token:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["relogin_attempts"] = int(next_item.get("relogin_attempts") or 0) + 1
            next_item["last_relogin_at"] = now
            next_item["last_relogin_error"] = self._credential_error_text(error or "relogin failed", current)[:500]
            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)

    def apply_relogin_tokens(self, access_token: str, token_data: dict, *, event: str = "relogin") -> str:
        """Install tokens obtained by a fresh login and put the account back in service.

        token_data carries access_token (required) and optionally refresh_token /
        id_token. Clears the relogin flag, the auto pause and an 异常 status left
        behind by the dead refresh_token. Returns the account's new access token.
        """
        if not access_token or not str((token_data or {}).get("access_token") or "").strip():
            return access_token
        new_token = self._apply_refreshed_tokens(access_token, token_data, event)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            new_token = self._resolve_access_token_locked(new_token)
            current = self._accounts.get(new_token)
            if current is None:
                return new_token
            next_item = dict(current)
            self._clear_credential_failure_state_locked(new_token, next_item)
            next_item["last_relogin_at"] = now
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[new_token] = account
                self._save_accounts()
            self._image_slot_condition.notify_all()
        log_service.add(LOG_TYPE_ACCOUNT, "账号重新登录成功", {"source": event, "token": anonymize_token(new_token)})
        return new_token

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
            deadline_monotonic: float | None = None,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        excluded_tokens 中的账号不会被选中（例如本张图已失败过的账号）；排除后无可用账号时
        抛出 ImageAccountSelectionError。
        """
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise ImageAccountSelectionError(
                "deadline_exceeded",
                "image request deadline exceeded before account selection",
            )
        max_attempts = 20  # 防止无限循环
        with self._lock:
            caller_excluded = {self._resolve_access_token_locked(token) for token in (excluded_tokens or ()) if token}
        attempted_tokens: set[str] = set(caller_excluded)
        # 控制流只保留两个出口，但最终是否能说“额度耗尽”必须谨慎：
        # 只要出现过非额度类失败，就说明不能断言全部账号都耗尽，应返回可重试的 unavailable。
        saw_remote_quota_exhausted = False
        saw_unavailable_failure = False
        for _attempt in range(max_attempts):
            try:
                access_token = self._acquire_next_candidate_token(
                    excluded_tokens=attempted_tokens,
                    plan_type=plan_type,
                    source_type=source_type,
                    plan_types=plan_types,
                    deadline_monotonic=deadline_monotonic,
                )
            except ImageAccountSelectionError as exc:
                if exc.kind in {"deadline_exceeded", "queue_full"}:
                    raise
                if attempted_tokens - caller_excluded:
                    break
                raise
            attempted_tokens.add(access_token)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.release_image_slot(access_token)
                raise ImageAccountSelectionError(
                    "deadline_exceeded",
                    "image request deadline exceeded before remote account validation",
                )
            try:
                if self._image_preflight_is_fresh(access_token):
                    account = self._lease_without_preflight(access_token)
                else:
                    account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception:
                # 预检失败（上游波动/网络/401 等）：这个号这次不可用，换下一个。
                # 401 已在 fetch_remote_info 内部走异常处理，这里不再二次分类。
                saw_unavailable_failure = True
                self.release_image_slot(access_token)
                continue
            # fetch_remote_info 内部可能因 token rotation 导致 access_token 变化，
            # 把新 token 也加入排除列表，防止重复尝试
            resolved = str((account or {}).get("access_token") or "")
            if resolved and resolved != access_token:
                attempted_tokens.add(resolved)
            if (
                    self._is_image_account_available(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                return str((account or {}).get("access_token") or access_token)
            if str((account or {}).get("status") or "") == "限流":
                saw_remote_quota_exhausted = True
            else:
                saw_unavailable_failure = True
            self.release_image_slot(access_token)
        if saw_remote_quota_exhausted and not saw_unavailable_failure:
            raise ImageAccountSelectionError(
                "quota_exhausted",
                f"all usable image accounts remote-confirmed quota exhausted after {len(attempted_tokens - caller_excluded)} attempts",
            )
        raise ImageAccountSelectionError(
            "unavailable",
            f"no image account available after {len(attempted_tokens - caller_excluded)} attempts",
        )

    @classmethod
    def _quota_estimated_empty(cls, account: dict) -> bool:
        """Local accounting drained the quota after the last remote check."""
        empty_at = cls._parse_time(account.get("last_quota_estimated_empty_at"))
        if empty_at is None:
            return False
        checked_at = cls._parse_time(account.get("last_remote_checked_at"))
        return checked_at is None or empty_at >= checked_at

    def _image_preflight_is_fresh(self, access_token: str) -> bool:
        """Whether a recent successful remote check makes another preflight redundant.

        Any image failure, remote check error or locally drained quota since that
        check sends the account back through the full preflight.
        """
        fresh_seconds = int(config.image_preflight_fresh_secs or 0)
        if fresh_seconds <= 0:
            return False
        with self._lock:
            account = self._accounts.get(self._resolve_access_token_locked(access_token))
            if not account or not self._is_image_account_available(account):
                return False
            if account.get("status") != "正常" or account.get("last_remote_check_result") != "ok":
                return False
            if self._quota_estimated_empty(account):
                return False
            checked_at = self._parse_time(account.get("last_remote_checked_at"))
            failed_at = self._parse_time(account.get("last_image_failure_at"))
        if checked_at is None or (failed_at is not None and failed_at >= checked_at):
            return False
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        return 0 <= age <= fresh_seconds

    def _lease_without_preflight(self, access_token: str) -> dict | None:
        """Local half of the preflight: renew an expiring access token only."""
        active_token = self.refresh_access_token(
            access_token,
            event="get_available_access_token:preflight",
        ) or access_token
        with self._lock:
            account = self._accounts.get(self._resolve_access_token_locked(active_token))
            return dict(account) if account else None

    # ── 新号冷静期与分步放量 ──────────────────────────────────────
    # 只管带 new_account_joined_at 的号（注册流程入池时写入）；缺字段的老账号视为早已过了冷静期，
    # 手动导入/迁移的老号不会被当成新号。
    @staticmethod
    def _beijing_day(moment: datetime) -> str:
        return (moment.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

    @classmethod
    def new_account_stage(cls, account: dict, now: datetime | None = None) -> dict[str, Any] | None:
        """新号当前阶段：cooldown（冷静期）/ ramp（放量期）；不是新号或已完全放开返回 None。

        冷静期 [joined, joined+cooldown_hours) 内不接图片也不接对话；之后的放量期
        [cooldown_end, cooldown_end+ramp_days) 内每个北京日最多出 new_account_ramp_daily_images 张。
        """
        joined = cls._parse_time(account.get("new_account_joined_at")) if isinstance(account, dict) else None
        if joined is None:
            return None
        now = now or datetime.now(timezone.utc)
        cooldown_end = joined + timedelta(hours=config.new_account_cooldown_hours)
        if now < cooldown_end:
            return {"stage": "cooldown", "until": cooldown_end.isoformat()}
        ramp_end = cooldown_end + timedelta(days=config.new_account_ramp_days)
        if now < ramp_end:
            today = cls._beijing_day(now)
            used = int(account.get("ramp_image_count") or 0) if account.get("ramp_image_day") == today else 0
            limit = config.new_account_ramp_daily_images
            return {"stage": "ramp", "until": ramp_end.isoformat(), "used_today": used,
                    "daily_limit": limit, "exhausted": used >= limit}
        return None

    @classmethod
    def _new_account_in_cooldown(cls, account: dict) -> bool:
        stage = cls.new_account_stage(account)
        return bool(stage) and stage["stage"] == "cooldown"

    @classmethod
    def _new_account_blocks_image(cls, account: dict) -> bool:
        stage = cls.new_account_stage(account)
        return bool(stage) and (stage["stage"] == "cooldown" or bool(stage.get("exhausted")))

    @classmethod
    def _count_new_account_image(cls, item: dict, now: datetime) -> None:
        stage = cls.new_account_stage(item, now)
        if not stage or stage["stage"] != "ramp":
            return
        today = cls._beijing_day(now)
        if item.get("ramp_image_day") != today:
            item["ramp_image_day"] = today
            item["ramp_image_count"] = 0
        item["ramp_image_count"] = int(item.get("ramp_image_count") or 0) + 1

    # ── 上游连续失败后的短时冷却 ──────────────────────────────────
    # 与自动暂停的关系：连续失败 3 次 = 一次“失败轮”。每一轮立刻让账号冷却
    # account_failure_cooldown_secs（短，默认 5 分钟，只降低优先级）；图片的失败轮同时记一次自动暂停
    # 计次，6 小时内攒够 account_auto_pause_strikes 次才进入长时的自动暂停。成功一次清零两者的连续计数。
    def _is_cooling_down_locked(self, access_token: str) -> bool:
        until = self._upstream_cooldown_until.get(access_token)
        if until is None:
            return False
        if until <= time.monotonic():
            self._upstream_cooldown_until.pop(access_token, None)
            return False
        return True

    def _begin_failure_cooldown_locked(self, access_token: str) -> bool:
        secs = int(config.account_failure_cooldown_secs or 0)
        if secs <= 0:
            return False
        self._upstream_cooldown_until[access_token] = time.monotonic() + secs
        return True

    def _prefer_serviceable_locked(self, tokens: list[str]) -> list[str]:
        """分层偏好：既没冷却也没自动暂停 > 仅冷却中 > 自动暂停；上一层为空才退到下一层，从不直接拒绝请求。"""
        now = datetime.now(timezone.utc)
        tier_ok = [
            t for t in tokens
            if not self._auto_pause_active(self._accounts.get(t) or {}, now) and not self._is_cooling_down_locked(t)
        ]
        if tier_ok:
            return tier_ok
        tier_cooling = [t for t in tokens if not self._auto_pause_active(self._accounts.get(t) or {}, now)]
        return tier_cooling or tokens

    def record_upstream_failure(self, access_token: str) -> bool:
        """记录一次对话/搜索的可换号上游失败；连续 3 次触发短时冷却，返回是否刚进入冷却。"""
        if not access_token:
            return False
        with self._lock:
            token = self._resolve_access_token_locked(access_token)
            if token not in self._accounts:
                return False
            streak = self._upstream_failure_streaks.get(token, 0) + 1
            if streak < self.UPSTREAM_FAILURE_COOLDOWN_STREAK:
                self._upstream_failure_streaks[token] = streak
                return False
            self._upstream_failure_streaks.pop(token, None)
            return self._begin_failure_cooldown_locked(token)

    def clear_upstream_failures(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            token = self._resolve_access_token_locked(access_token)
            self._upstream_failure_streaks.pop(token, None)
            self._upstream_cooldown_until.pop(token, None)

    # ── 对话/搜索账号并发槽位 ─────────────────────────────────────
    def _text_members_locked(self) -> list[str]:
        """能接对话的账号：非禁用/异常、凭据可用、不在新号冷静期、没有待核验（图片范围的待核验不影响对话）。"""
        now = datetime.now(timezone.utc)
        members = []
        for account in self._accounts.values():
            token = account.get("access_token") or ""
            if not token or account.get("status") in {"禁用", "异常"}:
                continue
            if self._new_account_in_cooldown(account) or not self._credentials_usable(account):
                continue
            if self._auth_verification_pending(account, now) and account.get("pending_auth_scope") != "image":
                continue
            members.append(token)
        return members

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        """不占槽位的取号（兼容旧调用，如后台任务/预选）；对话与搜索请使用 acquire_text_access_token。"""
        excluded = set(excluded_tokens or set())
        with self._lock:
            members = self._text_members_locked()
            candidates = self._prefer_serviceable_locked([token for token in members if token not in excluded])
            if not candidates:
                return ""
            # Text has its own rotation so image traffic does not skew it.
            access_token = self._text_scheduler.pick(
                [(token, 1.0, int(self._text_inflight.get(token, 0))) for token in candidates],
                members=members,
            ) or candidates[0]
        return self.refresh_access_token(access_token, event="get_text_access_token") or access_token

    def acquire_text_access_token(
            self,
            preferred_token: str = "",
            excluded_tokens: set[str] | None = None,
            timeout_secs: float = 120.0,
    ) -> str:
        """取一个对话账号并占用它的并发槽位（每账号最多 text_account_concurrency 个）。

        优先 preferred_token；账号都满时排队等待。返回空串表示没有可用账号或等待超时。
        拿到非空 token 后调用方必须用 release_text_slot 归还（推荐 text_slot() 上下文管理器）。
        """
        deadline = time.monotonic() + max(0.0, float(timeout_secs))
        excluded = set(excluded_tokens or set())
        while True:
            with self._text_slot_condition:
                max_concurrency = max(1, int(config.text_account_concurrency or 1))
                members = self._text_members_locked()
                preferred = self._resolve_access_token_locked(preferred_token) if preferred_token else ""
                candidates = [token for token in members if token not in excluded]
                if not candidates:
                    return ""
                free = [token for token in candidates if self._text_inflight.get(token, 0) < max_concurrency]
                if free:
                    if preferred in free:
                        access_token = preferred
                    else:
                        access_token = self._text_scheduler.pick(
                            [(token, 1.0, int(self._text_inflight.get(token, 0)))
                             for token in self._prefer_serviceable_locked(free)],
                            members=members,
                        ) or free[0]
                    self._text_inflight[access_token] = self._text_inflight.get(access_token, 0) + 1
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    self._text_slot_condition.wait(timeout=min(1.0, remaining))
                    continue
            try:
                resolved = self.refresh_access_token(access_token, event="get_text_access_token") or access_token
            except BaseException:
                self.release_text_slot(access_token)
                raise
            if resolved != access_token:
                with self._text_slot_condition:
                    self._move_text_slot_locked(access_token, resolved)
            return resolved

    def _move_text_slot_locked(self, old_token: str, new_token: str) -> None:
        current = self._text_inflight.get(old_token, 0)
        if current <= 1:
            self._text_inflight.pop(old_token, None)
        else:
            self._text_inflight[old_token] = current - 1
        self._text_inflight[new_token] = self._text_inflight.get(new_token, 0) + 1
        self._text_slot_condition.notify_all()

    def release_text_slot(self, access_token: str) -> None:
        """归还一个对话槽位；多归还不会变负数，并唤醒排队的请求。"""
        if not access_token:
            return
        with self._text_slot_condition:
            token = access_token if access_token in self._text_inflight else self._resolve_access_token_locked(access_token)
            current = self._text_inflight.get(token, 0)
            if current <= 1:
                self._text_inflight.pop(token, None)
            else:
                self._text_inflight[token] = current - 1
            self._text_slot_condition.notify_all()

    def rebind_text_slot(self, old_token: str, new_token: str) -> None:
        """调用方在持有槽位期间换了 token（刷新轮换）时，把占用移到新 token。"""
        if not old_token or not new_token or old_token == new_token:
            return
        with self._text_slot_condition:
            self._move_text_slot_locked(
                old_token if old_token in self._text_inflight else self._resolve_access_token_locked(old_token),
                new_token,
            )

    @contextmanager
    def text_slot(self, preferred_token: str = "", excluded_tokens: set[str] | None = None, timeout_secs: float = 120.0):
        """with account_service.text_slot() as token: ... 退出（含异常）时必定归还槽位；token 为空表示没有可用账号。"""
        token = self.acquire_text_access_token(preferred_token, excluded_tokens, timeout_secs)
        try:
            yield token
        finally:
            if token:
                self.release_text_slot(token)

    # ── 设备 ID ───────────────────────────────────────────────────
    @staticmethod
    def stored_device_id(account: dict | None) -> str:
        """账号已保存的设备 ID（顶层字段或 fp 指纹里的 oai-device-id）。"""
        account = account or {}
        raw_fp = account.get("fp")
        fp_device = ""
        if isinstance(raw_fp, dict):
            fp_device = next((str(v) for k, v in raw_fp.items() if str(k).lower() == "oai-device-id" and v), "")
        return str(account.get("oai-device-id") or fp_device or "").strip()

    def ensure_device_id(self, access_token: str, candidate: str) -> str:
        """给账号分配并保存一个固定设备 ID；已有则返回已有值。并发首次分配时只有一个候选生效。"""
        if not access_token or not candidate:
            return candidate
        with self._device_id_lock:
            existing = self.stored_device_id(self.get_account(access_token))
            if existing:
                return existing
            try:
                if self.update_account(access_token, {"oai-device-id": candidate}, quiet=True) is None:
                    return candidate
            except Exception as exc:  # noqa: BLE001 - 保存失败只影响“固定”，不影响本次请求
                log_service.add(LOG_TYPE_ACCOUNT, "设备 ID 保存失败", {"error": self._credential_error_text(exc, None)[:200]})
            return candidate

    # ── AT 失效待核验队列 ─────────────────────────────────────────
    # 状态转移：正常 --401--> pending（暂不接新请求）
    #   pending --核验通过--> ok（恢复调度）
    #   pending --AT 被拒且 RT 救不回--> handle_invalid_token（异常/重新登录/按配置移除）
    #   pending --网络/上游波动--> error（放回，由下次请求/定时刷新再判定）
    def schedule_auth_verification(
            self,
            access_token: str,
            event: str,
            *,
            remove_invalid: bool | None = None,
            scope: str = "account",
    ) -> bool:
        """把被上游 401 拒绝的账号标记为待核验并后台核验；成功入队返回 True。

        scope="image" 表示只是图片链路被拒，账号仍可接对话。
        """
        if not access_token:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(token)
            if current is None or current.get("status") in {"禁用", "异常"}:
                return False
            next_item = dict(current)
            existing_scope = str(next_item.get("pending_auth_scope") or "") \
                if next_item.get("last_remote_check_result") == "pending" else ""
            next_item["last_remote_check_result"] = "pending"
            next_item["last_remote_check_event"] = event
            next_item["last_remote_check_attempt_at"] = now
            next_item["pending_auth_at"] = now
            next_item["pending_auth_event"] = event
            next_item["pending_auth_scope"] = "account" if existing_scope == "account" or scope != "image" else "image"
            # 每次入队换一个核验 ID：迟到的旧一轮结果/降级写入据此识别并放弃，不盖掉新一轮。
            verification_id = uuid.uuid4().hex
            next_item["pending_auth_verification_id"] = verification_id
            requested_remove = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
            next_item["pending_auth_remove_invalid"] = bool(next_item.get("pending_auth_remove_invalid")) or requested_remove
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[token] = account
            self._save_accounts()
            self._image_slot_condition.notify_all()
        try:
            started = self._start_auth_verification(token, event)
        except Exception:  # noqa: BLE001 - 调度失败降级，不能让账号卡在待核验
            started = False
        if started is False:
            with self._auth_verify_lock:
                already_running = token in self._auth_verify_active
            if not already_running:
                # 后台核验没排上：把待核验降级成核验错误，账号放回调度，由下次请求/定时刷新再判定。
                self._record_remote_check_error(
                    token,
                    event,
                    "Account verification could not be scheduled.",
                    expected_verification_id=verification_id,
                )
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "账号核验调度失败",
                    {"source": event, "token": anonymize_token(token), "scope": scope},
                )
                return False
        return True

    def _start_auth_verification(self, access_token: str, event: str) -> bool:
        with self._auth_verify_lock:
            if access_token in self._auth_verify_active:
                return False
            self._auth_verify_active.add(access_token)

        def run() -> None:
            try:
                self.verify_pending_auth(access_token, event)
            finally:
                with self._auth_verify_lock:
                    self._auth_verify_active.discard(access_token)

        try:
            Thread(target=run, name="account-auth-verify", daemon=True).start()
        except Exception:
            with self._auth_verify_lock:
                self._auth_verify_active.discard(access_token)
            return False
        return True

    @staticmethod
    def _clear_pending_auth_fields(next_item: dict) -> None:
        for key in (
            "pending_auth_at",
            "pending_auth_event",
            "pending_auth_scope",
            "pending_auth_remove_invalid",
            "pending_auth_verification_id",
        ):
            next_item.pop(key, None)

    def verify_pending_auth(self, access_token: str, event: str = "auth_verify") -> str:
        """同步核验一个待核验账号，返回结果：ok / invalid / error / skipped。

        走 fetch_remote_info：先用 RT 刷新 AT，再请求用户信息。通过会写 ok 恢复调度；
        AT 仍被拒则由 handle_invalid_token 进入现有失效流程（异常/重新登录/移除，按 pending 时记录的
        remove_invalid 决定是否移除）；其他错误写 error，账号放回。
        """
        account = self.get_account(access_token)
        if not account or account.get("last_remote_check_result") != "pending":
            return "skipped"
        remove_invalid = account.get("pending_auth_remove_invalid")
        remove_invalid = None if remove_invalid is None else bool(remove_invalid)
        verification_id = str(account.get("pending_auth_verification_id") or "") or None
        token = str(account.get("access_token") or access_token)
        from services.openai_backend_api import InvalidAccessTokenError
        try:
            self.fetch_remote_info(token, f"{event}:auth_verify", remove_invalid=remove_invalid)
        except InvalidAccessTokenError:
            return "invalid"  # fetch_remote_info 内部已走 handle_invalid_token
        except Exception as exc:  # noqa: BLE001
            resolved = self.resolve_access_token(token)
            current = self.get_account(resolved) or {}
            if current.get("last_remote_check_result") == "pending":
                self._record_remote_check_error(resolved, event, str(exc), expected_verification_id=verification_id)
            return "error"
        resolved = self.resolve_access_token(token)
        current = self.get_account(resolved) or {}
        if current.get("last_remote_check_result") == "pending":
            # fetch_remote_info 成功路径正常会写 ok；兜底避免账号卡在待核验。
            self._record_refresh_success(resolved, event)
        return "ok"

    def list_pending_auth_verification(self) -> list[dict]:
        """待核验账号的摘要（给账号页和运维看）。"""
        with self._lock:
            return [
                {
                    "id": str(item.get("management_id") or ""),
                    "email": str(item.get("email") or ""),
                    "event": str(item.get("pending_auth_event") or item.get("last_remote_check_event") or ""),
                    "scope": str(item.get("pending_auth_scope") or "account"),
                    "since": str(item.get("pending_auth_at") or ""),
                    "stale": not self._auth_verification_pending(item),
                }
                for item in self._accounts.values()
                if item.get("last_remote_check_result") == "pending"
            ]

    def list_pending_auth_verification_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("last_remote_check_result") == "pending"
                and (token := str(item.get("access_token") or "").strip())
            ]

    def resume_pending_auth_verifications(self) -> int:
        """启动时（或周期性）把遗留的待核验账号重新排进核验；返回排入数量。"""
        count = 0
        for token in self.list_pending_auth_verification_tokens():
            account = self.get_account(token) or {}
            if self._start_auth_verification(token, str(account.get("pending_auth_event") or "resume")):
                count += 1
        return count

    # ── 调度视图（账号页用） ──────────────────────────────────────
    def scheduling_view(self, account: dict) -> dict[str, Any]:
        """此刻调度会怎么对待这个号；时间一律为 Unix 秒。"""
        now_wall = time.time()
        now_mono = time.monotonic()
        token = str(account.get("access_token") or "")

        def wall(mono: float | None) -> int | None:
            return int(now_wall + (mono - now_mono)) if mono and mono > now_mono else None

        def ts(value: object) -> int | None:
            moment = value if isinstance(value, datetime) else self._parse_time(value)
            return int(moment.timestamp()) if moment else None

        with self._lock:
            resolved = self._resolve_access_token_locked(token) if token else ""
            cooling = self._upstream_cooldown_until.get(resolved)
            image_inflight = int(self._image_inflight.get(resolved, 0))
            text_inflight = int(self._text_inflight.get(resolved, 0))
            streak = int(self._upstream_failure_streaks.get(resolved, 0)) \
                + int(self._image_failure_streaks.get(resolved, 0))
        stage = self.new_account_stage(account) or {}
        joined = ts(account.get("new_account_joined_at"))
        tomorrow = (
            (datetime.now(timezone.utc) + timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1) - timedelta(hours=8)
        )
        availability = project_upstream_credential_availability(
            token,
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=str(account.get("last_remote_check_result") or "").lower() == "invalid",
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )
        paused = self._auto_pause_active(account)
        pending = self._auth_verification_pending(account)
        return {
            "new_account_stage": str(stage.get("stage") or ""),
            "new_account_joined_at": joined,
            "cooldown_until": ts(stage.get("until")) if stage.get("stage") == "cooldown" else None,
            "ramp_until": ts(stage.get("until")) if stage.get("stage") == "ramp" else None,
            "ramp_used_today": int(stage.get("used_today") or 0),
            "ramp_daily_limit": int(stage.get("daily_limit") or 0),
            "ramp_exhausted": bool(stage.get("exhausted")),
            "ramp_resume_at": int(tomorrow.timestamp()) if stage.get("exhausted") else None,
            "failure_cooldown_until": wall(cooling),
            "failure_streak": streak,
            "auto_paused_until": ts(account.get("auto_paused_until")) if paused else None,
            "auto_pause_reason": str(account.get("auto_pause_reason") or "") if paused else "",
            "image_inflight": image_inflight,
            "image_concurrency": max(1, int(config.image_account_concurrency or 1)),
            "text_inflight": text_inflight,
            "text_concurrency": max(1, int(config.text_account_concurrency or 1)),
            "auth_pending": pending,
            "auth_pending_event": str(account.get("pending_auth_event") or "") if pending else "",
            "auth_pending_since": ts(account.get("pending_auth_at")) if pending else None,
            "credential_status": availability.status,
            "access_token_status": availability.access_status,
            "refresh_token_status": availability.refresh_status,
            "image_schedulable": bool(self._is_image_account_available(account)),
        }

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            self._upstream_failure_streaks.pop(access_token, None)
            self._upstream_cooldown_until.pop(access_token, None)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(
        self,
        access_token: str,
        event: str,
        quiet: bool = False,
        remove: bool | None = None,
        error: str | None = None,
    ) -> bool:
        return self.handle_invalid_token(
            access_token,
            event,
            error=error,
            quiet=quiet,
            remove=remove,
        )

    @staticmethod
    def _relogin_holds(account: dict | None) -> bool:
        if not account:
            return False
        try:
            from services.account_relogin_service import relogin_holds
            return bool(relogin_holds(account))
        except Exception:
            return False

    @staticmethod
    def _wake_relogin_service() -> None:
        try:
            from services.account_relogin_service import account_relogin_service
            account_relogin_service.wake()
        except Exception:
            pass

    def handle_invalid_token(
        self,
        access_token: str,
        event: str,
        error: str | None = None,
        quiet: bool = False,
        remove: bool | None = None,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> bool:
        """统一处理鉴权异常账号。

        口径固定为：先记录异常，再按“自动移除异常账号”配置删除或保留异常状态。
        expected_credentials 给出且账号凭据已换代时什么都不做（返回 False）。
        """
        if not self._record_invalid_token_seen(
            access_token,
            event,
            str(error or "invalid access token"),
            expected_credentials=expected_credentials,
        ):
            return False
        should_remove = config.auto_remove_invalid_accounts if remove is None else remove
        if not should_remove:
            return False
        if self._relogin_holds(self.get_account(access_token)):
            # 还能自动重新登录的账号先保留为异常，交给重登服务处理。
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
            self._wake_relogin_service()
            return False
        removed = bool(self.delete_accounts([access_token], return_items=False)["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token),
                             "error": self._credential_error_text(error, None)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0}, quiet=quiet)
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        self.sync_accounts_from_storage()
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result

    def get_account_by_id(self, account_id: str) -> dict | None:
        """按稳定管理 ID 取账号副本（附带 image_inflight）；不存在返回 None。"""
        management_id = str(account_id or "").strip().lower()
        if not management_id:
            return None
        self.sync_accounts_from_storage()
        with self._lock:
            for account in self._accounts.values():
                if str(account.get("management_id") or "").strip().lower() == management_id:
                    result = dict(account)
                    result["image_inflight"] = int(self._image_inflight.get(result.get("access_token") or "", 0))
                    return result
        return None

    def resolve_account_ids(self, account_ids: list[str]) -> tuple[list[str], list[str]]:
        """管理 ID -> 当前 access_token。返回 (tokens, 找不到的 id)，保持入参顺序并去重。"""
        requested = list(dict.fromkeys(
            str(item or "").strip().lower() for item in account_ids if str(item or "").strip()
        ))
        if not requested:
            return [], []
        self.sync_accounts_from_storage()
        with self._lock:
            token_by_id = {
                str(account.get("management_id") or "").strip().lower(): token
                for token, account in self._accounts.items()
                if str(account.get("management_id") or "").strip()
            }
        tokens = [token_by_id[item] for item in requested if item in token_by_id]
        missing = [item for item in requested if item not in token_by_id]
        return tokens, missing

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    # 不知道恢复时间的限流账号，隔这么久放回可用一次，由下一个请求的预检确认。
    LIMITED_RELEASE_UNKNOWN_RESET_SECS = 3 * 60 * 60
    # 放回后预检发现仍限流、且上游给的恢复时间没有更新时，至少隔这么久再放回，避免反复试。
    LIMITED_RELEASE_RETRY_SECS = 30 * 60

    @classmethod
    def _image_reset_at(cls, item: dict) -> datetime | None:
        """生图额度的重置时间：优先取 limits_progress 里 image_gen 的 reset_after，其次 restore_at。"""
        for entry in item.get("limits_progress") or []:
            if isinstance(entry, dict) and entry.get("feature_name") == "image_gen":
                parsed = cls._parse_time(entry.get("reset_after"))
                if parsed is not None:
                    return parsed
        return cls._parse_time(item.get("restore_at"))

    @classmethod
    def _limited_release_due(cls, item: dict, now: datetime) -> bool:
        """限流账号是否到了放回可用的时间（只看本地记录，不发请求）。

        Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
        """
        released = cls._parse_time(item.get("optimistic_release_at"))
        if released is not None and (now - released).total_seconds() < cls.LIMITED_RELEASE_RETRY_SECS:
            return False
        reset = cls._image_reset_at(item)
        if reset is not None:
            return reset <= now
        last = cls._parse_time(item.get("last_remote_check_attempt_at") or item.get("last_remote_checked_at"))
        return last is None or (now - last).total_seconds() >= cls.LIMITED_RELEASE_UNKNOWN_RESET_SECS

    def release_recovered_limited_accounts(self) -> list[str]:
        """额度恢复时间已到的限流账号直接放回可用（额度待确认），不发任何请求。

        被选中时选号流程本来就要预检，这次预检顺带确认额度：真恢复了就直接出图；还没恢复会被重新标成
        限流（带新的恢复时间），请求自动换下一个号，不会发起生图。返回放回的账号标签列表。
        """
        # 多个调用同时进来时只让一个做放回，其余直接跳过，避免同一账号被重复写入。
        if not self._limited_release_lock.acquire(blocking=False):
            return []
        try:
            now = datetime.now(timezone.utc)
            with self._lock:
                due = [
                    (token, str(item.get("email") or token[-8:]))
                    for item in self._accounts.values()
                    if item.get("status") == "限流"
                       and item.get("last_remote_check_result") != "pending"
                       and self._limited_release_due(item, now)
                       and (token := item.get("access_token") or "")
                ]
            released: list[str] = []
            for token, label in due:
                try:
                    updated = self.update_account(
                        token,
                        {
                            "status": "正常",
                            "image_quota_unknown": True,
                            "optimistic_release_at": now.isoformat(),
                            # 让预检新鲜度判定失效，放回后第一次被选中必须现场远程确认额度。
                            "last_quota_estimated_empty_at": now.isoformat(),
                        },
                        quiet=True,
                    )
                except Exception as exc:  # noqa: BLE001 - 单个账号写入失败不影响其它账号
                    log_service.add(LOG_TYPE_ACCOUNT, "限流账号放回失败", {
                        "token": anonymize_token(token),
                        "error": self._credential_error_text(exc, None),
                    })
                    continue
                if updated is not None:
                    released.append(label)
            if released:
                log_service.add(LOG_TYPE_ACCOUNT, "限流账号已乐观放回", {"count": len(released), "accounts": released})
            return released
        finally:
            self._limited_release_lock.release()

    def _auto_remove_tokens_locked(
        self,
        *,
        remove_invalid: bool,
        remove_rate_limited: bool,
    ) -> tuple[list[str], list[str]]:
        invalid_tokens = [
            token
            for item in self._accounts.values()
            if remove_invalid
               and item.get("status") == "异常"
               and (token := item.get("access_token") or "")
               and not self._relogin_holds(item)
        ]
        rate_limited_tokens = [
            token
            for item in self._accounts.values()
            if remove_rate_limited
               and item.get("status") == "限流"
               and (token := item.get("access_token") or "")
        ]
        return invalid_tokens, rate_limited_tokens

    def preview_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )
        invalid = len(invalid_tokens)
        rate_limited = len(rate_limited_tokens)
        return {
            "dry_run": True,
            "invalid": invalid,
            "rate_limited": rate_limited,
            "total_removed": invalid + rate_limited,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def cleanup_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )

        target_tokens = list(dict.fromkeys([*invalid_tokens, *rate_limited_tokens]))
        result = self.delete_accounts(target_tokens, return_items=False) if target_tokens else {"removed": 0}
        removed = int(result.get("removed") or 0)
        if removed:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "按账号自动移除策略清理账号",
                {
                    "removed": removed,
                    "invalid": len(invalid_tokens),
                    "rate_limited": len(rate_limited_tokens),
                },
            )
        return {
            "dry_run": False,
            "invalid": len(invalid_tokens),
            "rate_limited": len(rate_limited_tokens),
            "total_removed": removed,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def list_normal_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and (token := item.get("access_token") or "")
            ]

    @classmethod
    def _pool_health_freshness_seconds(cls, freshness_seconds: int | float | None = None) -> int:
        if freshness_seconds is not None:
            try:
                return max(60, int(float(freshness_seconds)))
            except (TypeError, ValueError):
                pass
        return max(60, int(config.refresh_account_interval_minute) * 60)

    @classmethod
    def _remote_check_is_fresh(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        checked_at = cls._parse_time(account.get("last_remote_checked_at"))
        return checked_at is not None and (now - checked_at).total_seconds() <= freshness_seconds

    @classmethod
    def _remote_check_attempt_is_recent(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        attempted_at = cls._parse_time(account.get("last_remote_check_attempt_at"))
        return attempted_at is not None and (now - attempted_at).total_seconds() <= freshness_seconds

    @classmethod
    def _pool_health_metrics_from_accounts(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any]:
        local_normal = [item for item in accounts if item.get("status") == "正常"]
        confirmed_normal = [
            item
            for item in local_normal
            if cls._remote_check_is_fresh(item, now, freshness_seconds)
        ]
        latest_checked_at = ""
        for item in accounts:
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            if checked_at is None:
                continue
            value = checked_at.isoformat()
            if not latest_checked_at or value > latest_checked_at:
                latest_checked_at = value
        return {
            "current_quota": sum(
                int(item.get("quota") or 0)
                for item in confirmed_normal
                if not item.get("image_quota_unknown")
            ),
            "current_available": len(confirmed_normal),
            "estimated_quota": sum(
                int(item.get("quota") or 0)
                for item in local_normal
                if not item.get("image_quota_unknown")
            ),
            "estimated_available": len(local_normal),
            "unconfirmed_available": max(0, len(local_normal) - len(confirmed_normal)),
            "unknown_quota_count": sum(1 for item in confirmed_normal if item.get("image_quota_unknown")),
            "pool_freshness_seconds": freshness_seconds,
            "pool_last_checked_at": latest_checked_at,
        }

    @classmethod
    def _pool_health_stale_tokens(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> list[str]:
        stale: list[tuple[float, str]] = []
        for item in accounts:
            token = str(item.get("access_token") or "").strip()
            if not token or item.get("status") != "正常":
                continue
            if cls._remote_check_is_fresh(item, now, freshness_seconds):
                continue
            if cls._remote_check_attempt_is_recent(item, now, freshness_seconds):
                continue
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            sort_key = checked_at.timestamp() if checked_at else 0.0
            stale.append((sort_key, token))
        stale.sort(key=lambda item: item[0])
        return [token for _, token in stale]

    @staticmethod
    def _pool_health_target_reached(
        metrics: dict[str, Any],
        *,
        target_quota: int | None = None,
        target_available: int | None = None,
    ) -> bool:
        if target_quota is not None and int(metrics.get("current_quota") or 0) >= max(1, int(target_quota)):
            return True
        if target_available is not None and int(metrics.get("current_available") or 0) >= max(1, int(target_available)):
            return True
        return False

    def evaluate_account_pool(
        self,
        *,
        refresh_stale: bool = False,
        target_quota: int | None = None,
        target_available: int | None = None,
        freshness_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        """Return registration-facing account pool metrics from remotely confirmed data.

        Local quota is useful for display, but registration stop decisions should not
        trust stale local quota.  This method refreshes only stale normal accounts,
        in small batches, until the requested target is confirmed or no eligible
        stale accounts remain.
        """
        freshness = self._pool_health_freshness_seconds(freshness_seconds)
        refreshed = 0
        refresh_errors: list[dict[str, Any]] = []

        while True:
            now = datetime.now(timezone.utc)
            accounts = self.list_accounts()
            metrics = self._pool_health_metrics_from_accounts(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not refresh_stale:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }
            if self._pool_health_target_reached(
                metrics,
                target_quota=target_quota,
                target_available=target_available,
            ):
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            stale_tokens = self._pool_health_stale_tokens(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not stale_tokens:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            batch = stale_tokens[: self._POOL_HEALTH_REFRESH_BATCH_SIZE]
            result = self.refresh_accounts(batch, remove_invalid=False)
            refreshed += int(result.get("refreshed") or 0)
            refresh_errors.extend(result.get("errors") or [])

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _prepare_account_payload(item: dict, *, restore: bool = False) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        if not restore:
            # 外部来源（CPA/Sub2API/其它实例导出）携带的管理 ID 不可信，由本实例重新生成。
            payload.pop("management_id", None)
        payload["access_token"] = access_token

        # Browser/session exports commonly use camelCase. Normalize the token
        # family before persisting so imported accounts retain refresh support.
        for camel_key, snake_key in (
            ("refreshToken", "refresh_token"),
            ("idToken", "id_token"),
            ("sessionToken", "session_token"),
            ("expiresAt", "expired"),
        ):
            if not payload.get(snake_key) and payload.get(camel_key):
                payload[snake_key] = payload[camel_key]
            payload.pop(camel_key, None)
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def add_account_items(self, items: list[dict], return_items: bool = True, *, restore: bool = False) -> dict:
        payloads = [
            payload
            for item in items
            if (payload := self._prepare_account_payload(item, restore=restore)) is not None
        ]
        return self._add_account_payloads(payloads, return_items=return_items)

    def add_accounts(self, tokens: list[str], source_type: str = "web", return_items: bool = True) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts() if return_items else []}
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ], return_items=return_items)

    def _add_account_payloads(self, payloads: list[dict], return_items: bool = True) -> dict:
        deduped: dict[str, dict] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            return {"added": 0, "skipped": 0, "items": self.list_accounts() if return_items else []}

        with self._lock:
            added = 0
            skipped = 0
            for access_token, payload in deduped.items():
                current = self._accounts.get(access_token)
                current_is_new = current is None
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    self._save_cumulative_total()
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                account = self._normalize_account(
                    {
                        **current,
                        **incoming,
                        "access_token": access_token,
                        "type": str(incoming.get("type") or current.get("type") or "free"),
                    }
                )
                if account is not None:
                    if current_is_new:
                        used_ids = {
                            str(other.get("management_id") or "")
                            for other_token, other in self._accounts.items()
                            if other_token != access_token
                        }
                        account["management_id"] = self._unique_management_id(
                            access_token, account.get("management_id"), used_ids
                        )
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()] if return_items else []
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def delete_accounts(self, tokens: list[str], return_items: bool = True) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts() if return_items else []}
        with self._lock:
            target_set = {self._resolve_access_token_locked(token) for token in target_set if token}
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
                self._image_released_at.pop(token, None)
                self._image_failure_streaks.pop(token, None)
                self._text_inflight.pop(token, None)
                self._upstream_failure_streaks.pop(token, None)
                self._upstream_cooldown_until.pop(token, None)
            self._token_aliases = {
                old: new
                for old, new in self._token_aliases.items()
                if old not in target_set and new not in target_set
            }
            if removed:
                self._image_scheduler.prune(self._accounts)
                self._text_scheduler.prune(self._accounts)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()] if return_items else []
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict, quiet: bool = False) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_updates = dict(updates)
            next_updates.pop("management_id", None)
            if self.ready_checkout_url(current) and any(
                str(key).startswith("checkout_") for key in next_updates
            ):
                # A later retry/failure may update the same account after the
                # instructions link was persisted. Only another complete success may
                # replace the locked checkout snapshot.
                if not self.ready_checkout_url(next_updates):
                    next_updates = {
                        key: value
                        for key, value in next_updates.items()
                        if not str(key).startswith("checkout_")
                    }
            account = self._normalize_account({**current, **next_updates, "access_token": access_token})
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除额度耗尽账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def _record_refresh_success(self, access_token: str, event: str = "fetch_remote_info") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            next_item["last_remote_checked_at"] = now
            next_item["last_remote_check_attempt_at"] = now
            next_item["last_remote_check_error"] = None
            next_item["last_remote_check_error_at"] = None
            next_item["last_remote_check_event"] = event
            next_item["last_remote_check_result"] = "ok"
            self._clear_pending_auth_fields(next_item)
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account

    def _record_remote_check_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_credentials: _CredentialGeneration | None = None,
        expected_verification_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            if expected_credentials is not None and self._credential_generation(access_token, current) != tuple(expected_credentials):
                # 检查期间凭据已换代：旧凭据上的失败不写到新凭据头上。
                return
            if expected_verification_id is not None and str(current.get("pending_auth_verification_id") or "") != expected_verification_id:
                # 这次核验已被更新的一轮取代（或已完成），不覆盖新的结果。
                return
            next_item = dict(current)
            next_item["last_remote_check_attempt_at"] = now
            next_item["last_remote_check_error"] = self._credential_error_text(error or "remote check failed", current)
            next_item["last_remote_check_error_at"] = now
            next_item["last_remote_check_event"] = event
            next_item["last_remote_check_result"] = "error"
            self._clear_pending_auth_fields(next_item)
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()

    def _record_invalid_token_seen(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> bool:
        """把账号标成异常；返回 False 表示凭据已换代，没有标记（旧凭据的 401 不拉黑刷新好的新凭据）。"""
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return True
            if expected_credentials is not None and self._credential_generation(access_token, current) != tuple(expected_credentials):
                return False
            error = self._credential_error_text(error or "invalid access token", current)
            next_item = dict(current)
            next_item["status"] = "异常"
            next_item["quota"] = 0
            next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
            next_item["last_invalid_at"] = now.isoformat()
            next_item["last_refresh_error"] = str(error or "invalid access token")
            next_item["last_refresh_error_at"] = now.isoformat()
            next_item["last_remote_checked_at"] = now.isoformat()
            next_item["last_remote_check_attempt_at"] = now.isoformat()
            next_item["last_remote_check_error"] = str(error or "invalid access token")
            next_item["last_remote_check_error_at"] = now.isoformat()
            next_item["last_remote_check_event"] = event
            next_item["last_remote_check_result"] = "invalid"
            self._clear_pending_auth_fields(next_item)
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "标记异常账号",
                {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
            )
        return True

    def _refresh_account_after_image_failure(self, access_token: str) -> None:
        try:
            self.fetch_remote_info(access_token, "image_failure")
        except Exception as exc:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "图片失败后刷新账号失败",
                {"token": anonymize_token(access_token), "error": self._credential_error_text(exc, None)},
            )

    def _schedule_account_refresh_after_image_failure(self, access_token: str) -> None:
        now = time.monotonic()
        with self._image_failure_refresh_lock:
            cutoff = now - self._IMAGE_FAILURE_REFRESH_COOLDOWN_SECONDS
            self._image_failure_refresh_started_at = {
                token: started_at
                for token, started_at in self._image_failure_refresh_started_at.items()
                if token in self._image_failure_refresh_active or started_at >= cutoff
            }
            last_started_at = self._image_failure_refresh_started_at.get(access_token, 0.0)
            if access_token in self._image_failure_refresh_active:
                return
            if now - last_started_at < self._IMAGE_FAILURE_REFRESH_COOLDOWN_SECONDS:
                return
            if len(self._image_failure_refresh_active) >= self._IMAGE_FAILURE_REFRESH_MAX_CONCURRENT:
                return
            self._image_failure_refresh_active.add(access_token)
            self._image_failure_refresh_started_at[access_token] = now

        def refresh() -> None:
            try:
                self._refresh_account_after_image_failure(access_token)
            finally:
                with self._image_failure_refresh_lock:
                    self._image_failure_refresh_active.discard(access_token)

        try:
            Thread(
                target=refresh,
                name="image-account-refresh",
                daemon=True,
            ).start()
        except Exception as exc:
            with self._image_failure_refresh_lock:
                self._image_failure_refresh_active.discard(access_token)
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "image failure refresh scheduling failed",
                {"token": anonymize_token(access_token), "error": self._credential_error_text(exc, None)},
            )

    # 这些失败是“请求文本被拒/上游文本回复”（HTTP 400 的文本结果），说明不了账号好坏。
    TEXT_RESULT_FAILURE_CODES = frozenset({
        "content_policy_violation",
        "invalid_image_input",
        "upstream_text_reply",
        "unsupported_model",
    })

    def mark_image_text_result(self, access_token: str, *, failure_code: str = "", status_code: int = 400) -> dict | None:
        """文本类结果（内容策略拒绝、上游文本回复等 400）：释放槽位，不计账号失败。"""
        return self.mark_image_result(
            access_token,
            False,
            failure_code=failure_code,
            status_code=status_code,
            account_fault=False,
        )

    def mark_image_result(
        self,
        access_token: str,
        success: bool,
        *,
        refresh_account: bool = False,
        failure_code: str = "",
        status_code: int = 0,
        account_fault: bool | None = None,
    ) -> dict | None:
        """记录一次图片请求的结果并释放槽位。

        account_fault 只对 success=False 有意义：True 计入账号失败（fail、冷却、连续失败自动暂停）；
        False 表示失败与账号无关（用户提示词被内容策略拒绝、上游给了文本回复），只更新使用时间与
        用量统计，不累计 fail、不冷却、不触发自动暂停、不重置连续失败计数。
        None（默认）按 failure_code 推断：属于 TEXT_RESULT_FAILURE_CODES 的按 False，其余按 True。

        槽位总是先释放；写入账号状态时保存失败会回滚内存、记日志并返回 None，不向调用方抛，
        因此调用方无需再为“结果更新失败导致槽位泄漏”做保护。
        """
        if not access_token:
            return None
        held_seconds = self.release_image_slot(access_token)
        if account_fault is None:
            account_fault = str(failure_code or "").strip().lower() not in self.TEXT_RESULT_FAILURE_CODES
        now = datetime.now(timezone.utc)
        should_refresh_after_failure = False
        auto_paused = False
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            self._image_usage.record_attempt(
                access_token,
                success=success,
                account_failure=(not success) and account_fault,
                failure_code=failure_code,
                status_code=status_code,
                held_seconds=held_seconds,
            )
            next_item = dict(current)
            next_item["last_used_at"] = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                self._image_failure_streaks.pop(access_token, None)
                self._upstream_failure_streaks.pop(access_token, None)
                self._upstream_cooldown_until.pop(access_token, None)
                self._count_new_account_image(next_item, now)
                # A paused account picked as a last resort and succeeding has recovered.
                self._clear_auto_pause_fields(next_item)
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not image_quota_unknown:
                    current_quota = max(0, int(next_item.get("quota") or 0))
                    next_item["quota"] = max(0, current_quota - 1)
                    if current_quota <= 1:
                        # 本地扣减到 0 只能说明“展示值需要远程刷新”，不能直接证明账号已限流。
                        # 下一次调度会进入远程预检，由 get_user_info 的结果决定是否写入“限流”。
                        next_item["image_quota_unknown"] = True
                        next_item["last_quota_estimated_empty_at"] = now.isoformat()
                if next_item.get("status") == "限流":
                    # 如果极端竞态下限流账号仍然成功出图，说明远程额度已恢复。
                    next_item["status"] = "正常"
                    next_item["image_quota_unknown"] = True
            elif account_fault:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
                # Drops the account out of the preflight-skip window until it is
                # remote-checked again.
                next_item["last_image_failure_at"] = now.isoformat()
                should_refresh_after_failure = refresh_account
                self._penalize_image_account_locked(access_token)
                auto_paused = self._apply_image_failure_streak_locked(access_token, next_item, now)
            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[access_token] = account
            try:
                self._save_accounts()
            except Exception as exc:  # noqa: BLE001 - 槽位已释放；回滚内存并记日志，不把存储错误抛给出图流程
                self._accounts[access_token] = current
                log_service.add(LOG_TYPE_ACCOUNT, "图片结果写入账号失败", {
                    "token": anonymize_token(access_token),
                    "error": self._credential_error_text(exc, current),
                })
                return None
            result = dict(account)
        if auto_paused:
            log_service.add(LOG_TYPE_ACCOUNT, "账号连续失败，已自动暂停", {
                "token": anonymize_token(access_token),
                "email": result.get("email") or "",
                "until": result.get("auto_paused_until"),
            })
        if should_refresh_after_failure:
            self._schedule_account_refresh_after_image_failure(access_token)
        return result

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        remove_invalid: bool | None = None,
    ) -> dict[str, Any] | None:
        """远程拉取账号信息并更新本地记录。

        凭据代保护：请求期间账号凭据被别处轮换（并发刷新/手动更新）时，旧凭据得到的 401 或失败
        不会拉黑/标记刚换好的新凭据，而是改用最新凭据重试；账号被删则抛 RefreshCredentialsChangedError。
        请求经过 account_processing_slot，与批量刷新共用全局并发上限。
        """
        if not access_token:
            raise ValueError("access_token is required")

        active_token = self.refresh_access_token(access_token, event=f"{event}:preflight") or access_token
        from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

        def request_user_info(token: str) -> dict[str, Any]:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    return backend.get_user_info()

        request_token, _request_refresh, request_account = self._credential_snapshot(active_token)
        if request_account is None:
            raise RefreshCredentialsChangedError()
        request_generation = self._credential_generation(request_token, request_account)

        rejected: Exception | None = None
        try:
            result = request_user_info(request_token)
        except InvalidAccessTokenError as exc:
            rejected = exc
        except Exception as exc:
            self._record_remote_check_error(request_token, event, str(exc), expected_credentials=request_generation)
            raise

        if rejected is not None:
            # 请求期间凭据可能已被轮换：旧凭据的 401 不算数，用最新凭据重试一次。
            current_token, _current_refresh, current = self._credential_snapshot(request_token)
            if current is None:
                raise RefreshCredentialsChangedError() from rejected
            current_generation = self._credential_generation(current_token, current)
            if current_generation != request_generation:
                request_token, request_generation = current_token, current_generation
                try:
                    result = request_user_info(current_token)
                    rejected = None
                except InvalidAccessTokenError as exc:
                    rejected = exc
                except Exception as exc:
                    self._record_remote_check_error(current_token, event, str(exc), expected_credentials=current_generation)
                    raise
            active_token = request_token

        if rejected is not None:
            # 凭据代没变而 AT 被拒：用 RT 强刷一次再验证；刷不动才算 AT 真失效。
            try:
                refreshed_token = self.force_refresh_access_token(
                    active_token,
                    event=f"{event}:invalid_access_token",
                    raise_on_error=True,
                    expected_credentials=request_generation,
                )
            except RefreshCredentialsChangedError:
                raise
            except Exception as refresh_exc:
                if self._refresh_exception_is_terminal(refresh_exc):
                    # RT 终态：AT 与 RT 都救不回，走异常/重登/移除流程。
                    self.handle_invalid_token(
                        active_token,
                        event,
                        error=str(rejected),
                        remove=remove_invalid,
                        expected_credentials=request_generation,
                    )
                    raise rejected
                # 刷新端点偶发故障：账号好坏未知，只记核验错误，不拉黑。
                self._record_remote_check_error(
                    active_token, event, str(refresh_exc), expected_credentials=request_generation,
                )
                raise
            if refreshed_token and refreshed_token != active_token:
                verify_token, _verify_refresh, verify_account = self._credential_snapshot(refreshed_token)
                if verify_account is None:
                    raise RefreshCredentialsChangedError()
                verify_generation = self._credential_generation(verify_token, verify_account)
                try:
                    result = request_user_info(verify_token)
                except InvalidAccessTokenError as retry_exc:
                    self.handle_invalid_token(
                        verify_token,
                        event,
                        error=str(retry_exc),
                        remove=remove_invalid,
                        expected_credentials=verify_generation,
                    )
                    raise
                except Exception as retry_exc:
                    self._record_remote_check_error(
                        verify_token, event, str(retry_exc), expected_credentials=verify_generation,
                    )
                    raise
                active_token = verify_token
            else:
                # 没有 RT 可用（或没能换出新 AT）：AT 被拒即失效。
                self.handle_invalid_token(
                    active_token,
                    event,
                    error=str(rejected),
                    remove=remove_invalid,
                    expected_credentials=request_generation,
                )
                raise rejected
        self._record_refresh_success(active_token, event)
        updated = self.update_account(active_token, result)
        if updated is not None:
            return updated
        # update_account 可能因为“自动移除额度耗尽账号”删除了远程确认限流的账号。
        # 调用方仍需要知道本次预检的真实结果，不能把它混成普通预检失败。
        return {**result, "access_token": active_token, "_removed_after_refresh": True}

    # ---- 刷新进度追踪 ----

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
                "events": [],
                "started_at": self._now(),
            }

    def ensure_refresh_progress(self, progress_id: str, total: int) -> None:
        """进度记录不存在（或已结束）才新建；已有进行中的记录原样保留（调用方可能已经写入了事件）。"""
        with self._refresh_progress_lock:
            existing = self._refresh_progress.get(progress_id)
            if existing is not None and not existing.get("done"):
                return
        self.init_refresh_progress(progress_id, total)

    _OPERATION_EVENT_LIMIT = 500

    def report_missing_accounts(self, progress_id: str, account_ids: list[str]) -> None:
        """选中的账号已不存在：计入进度总数并写一条失败事件（不含任何凭据）。"""
        ids = list(dict.fromkeys(str(item or "").strip() for item in account_ids if str(item or "").strip()))
        if not ids:
            return
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            events = progress.setdefault("events", [])
            for account_id in ids:
                sequence = int(progress.get("event_seq", 0)) + 1
                progress["event_seq"] = sequence
                progress["total"] = int(progress.get("total") or 0) + 1
                progress["processed"] = int(progress.get("processed") or 0) + 1
                events.append({
                    "sequence": sequence,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "account_id": account_id,
                    "account_label": account_id,
                    "action": "refresh",
                    "status": "failed",
                    "message": "账号不存在",
                })
            if len(events) > self._OPERATION_EVENT_LIMIT:
                del events[: len(events) - self._OPERATION_EVENT_LIMIT]

    def update_refresh_progress(self, progress_id: str, token: str, error: str = "") -> None:
        """处理完单个账号后，更新进度计数并记一条账号级事件（不含任何凭据）。"""
        account = self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "异常"
        quota = max(0, int(account.get("quota") or 0)) if account else 0
        label = (str(account.get("email") or "").strip() if account else "") or anonymize_token(token)
        account_id = str(account.get("management_id") or "") if account else ""
        error_text = str(error or "").strip()
        event_status = "failed" if error_text else ("skipped" if account is None else "success")
        message = error_text[:300] if error_text else (
            "账号已不存在" if account is None else f"状态 {status}，额度 {quota}"
        )

        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota
            events = progress.setdefault("events", [])
            sequence = int(progress.get("event_seq", 0)) + 1
            progress["event_seq"] = sequence
            events.append({
                "sequence": sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "account_id": account_id,
                "account_label": label,
                "action": "refresh",
                "status": event_status,
                "message": message,
            })
            if len(events) > self._OPERATION_EVENT_LIMIT:
                del events[: len(events) - self._OPERATION_EVENT_LIMIT]

    def finish_refresh_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记刷新完成。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            return {**progress, "events": list(progress.get("events") or [])} if progress else None

    def get_operation_progress(self, progress_id: str, after_sequence: int = 0) -> dict | None:
        """统一的账号操作进度：旧刷新进度字段 + status/percent/events（可按 sequence 增量取）。"""
        progress = self.get_refresh_progress(progress_id)
        if progress is None:
            return None
        total = max(0, int(progress.get("total") or 0))
        processed = max(0, int(progress.get("processed") or 0))
        if progress.get("error"):
            status = "failed"
        elif progress.get("done"):
            status = "success"
        else:
            status = "running"
        events = [e for e in progress.get("events", []) if int(e.get("sequence") or 0) > after_sequence]
        return {
            **progress,
            "progress_id": progress_id,
            "status": status,
            "percent": 100 if progress.get("done") and not total else (min(100, round(processed * 100 / total)) if total else 0),
            "events": events,
            "last_sequence": int(progress.get("event_seq") or 0),
        }

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)
    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        remove_invalid: bool | None = None,
    ) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            items = self.list_accounts()
            result = {"refreshed": 0, "errors": [], "items": items}
            if progress_id:
                self.finish_refresh_progress(progress_id, result)
            return result

        refreshed = 0
        errors = []
        max_workers = max(1, account_processing_worker_count(len(access_tokens)))

        if progress_id:
            self.ensure_refresh_progress(progress_id, len(access_tokens))

        def _fetch_with_slot(token: str):
            with account_processing_slot():
                return self.fetch_remote_info(token, "refresh_accounts", remove_invalid)

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(_fetch_with_slot, token): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                op_error = ""
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    error_str = self._credential_error_text(exc, None)
                    # TLS/代理连接错误是网络问题，不计入账号失败
                    from services.protocol.conversation import is_tls_connection_error
                    op_error = error_str
                    if not is_tls_connection_error(error_str):
                        errors.append({"token": anonymize_token(token), "error": error_str})
                else:
                    if account is not None:
                        refreshed += 1

                if progress_id:
                    self.update_refresh_progress(progress_id, token, error=op_error)
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        result = {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }

        if progress_id:
            self.finish_refresh_progress(progress_id, result)

        return result

    def build_export_item(self, account: dict) -> dict[str, str] | None:
        access_token = str(account.get("access_token") or "").strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        id_token = str(account.get("id_token") or "").strip()
        if not access_token or not refresh_token or not id_token:
            return None

        access_payload = self._decode_jwt_payload(access_token)
        id_payload = self._decode_jwt_payload(id_token)
        auth_claim = access_payload.get("https://api.openai.com/auth")
        auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
        profile_claim = access_payload.get("https://api.openai.com/profile")
        profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

        email = (
            str(account.get("email") or "").strip()
            or str(profile_claim.get("email") or "").strip()
            or str(id_payload.get("email") or "").strip()
        )
        account_id = (
            str(account.get("account_id") or "").strip()
            or str(auth_claim.get("chatgpt_account_id") or "").strip()
            or str(account.get("user_id") or "").strip()
        )
        item = {
            "type": str(account.get("export_type") or "codex"),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expired": self._timestamp_to_iso(access_payload.get("exp")),
            "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
        }
        password = str(account.get("password") or "").strip()
        if password:
            item["password"] = password
        return item

    def build_backup_records(self, access_tokens: list[str] | None = None) -> list[dict]:
        """完整备份：账号记录原样导出（含 token、管理 ID、分组、代理与统计），可用 restore 导入还原。"""
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            return [
                deepcopy(item)
                for token, item in self._accounts.items()
                if not target_tokens or token in target_tokens
            ]

    def build_export_items(self, access_tokens: list[str] | None = None) -> list[dict[str, str]]:
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        return [item for account in accounts if (item := self.build_export_item(account)) is not None]

    def get_stats(self) -> dict:
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        normal_items = [a for a in items if a.get("status") == "正常"]
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in normal_items)
        unlimited = sum(1 for a in normal_items if self._is_unlimited_image_quota_account(a))
        unknown_quota = sum(
            1
            for a in normal_items
            if (
                bool(a.get("image_quota_unknown"))
                or (not bool(a.get("image_quota_unknown")) and max(0, int(a.get("quota") or 0)) <= 0)
            )
            and not self._is_unlimited_image_quota_account(a)
        )
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "unlimited_quota_count": unlimited,
            "unknown_quota_count": unknown_quota,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def image_usage_snapshot(self) -> dict[str, Any]:
        """Runtime image usage for services.account_health: per-account and pool stats."""
        with self._lock:
            self._image_usage.prune(self._accounts)
            return {
                "accounts": {token: self._image_usage.account_stats(token) for token in self._accounts},
                "pool": self._image_usage.pool_stats(),
            }

    def is_unlimited_image_quota_account(self, account: dict) -> bool:
        return self._is_unlimited_image_quota_account(account)

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": stats["active"] > 0 or stats["unlimited_quota_count"] > 0,
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }


account_service = AccountService(config.get_storage_backend())

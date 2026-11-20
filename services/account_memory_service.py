"""自动关闭号池账号的官网记忆。

号池账号被很多下游用户共用。官网“启用记忆”（m3m）开着时，用户的提示词（文字和生图都会）
被写进账号记忆；请求里带 is_do_not_remember 实测挡不住，只有关掉 m3m 才行。
实测记录见 docs/dev/chatgpt-web-api.md。

后台线程逐个处理还没处理过的账号：关掉 m3m、清空已存记忆，结果写在账号上：
- memory_disabled_at：处理成功的时间（之后不再处理；官网开关是账号级的，换令牌、重新登录都不影响）；
- memory_cleared：当时清掉的记忆条数；
- memory_check_error / memory_check_error_at：最近一次失败原因，按 MEMORY_RETRY_SECS 退避后再试。
设置 account_memory_auto_disable 关掉后线程只空转。
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from services.config import config
from utils.log import logger

MEMORY_STARTUP_DELAY_SECS = 90.0
MEMORY_SCAN_INTERVAL_SECS = 600.0
MEMORY_GAP_SECS = 3.0
MEMORY_RETRY_SECS = 6 * 3600
MEMORY_BATCH_LIMIT = 30
# 被禁用的账号不处理；异常账号的令牌多半用不了，等它恢复后再处理。
_SKIP_STATUSES = {"禁用", "异常"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: object) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def memory_due(account: dict[str, Any] | None, now: float | None = None) -> bool:
    item = account or {}
    if not str(item.get("access_token") or "").strip():
        return False
    if item.get("memory_disabled_at"):
        return False
    if str(item.get("status") or "") in _SKIP_STATUSES:
        return False
    failed_at = _parse_ts(item.get("memory_check_error_at"))
    return not failed_at or (now or time.time()) - failed_at >= MEMORY_RETRY_SECS


def _default_disable(access_token: str) -> dict[str, Any]:
    from services.account_service import account_service
    from services.openai_backend_api import OpenAIBackendAPI

    # 临期的 AT 先用 RT 续一下，免得刚好过期白失败一次。
    access_token = account_service.ensure_access_token(access_token, event="memory_disable") or access_token
    with OpenAIBackendAPI(access_token) as backend:
        return backend.disable_memory()


class AccountMemoryService:
    def __init__(
        self,
        disable_fn: Callable[[str], dict[str, Any]] = _default_disable,
        accounts: Any = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._disable_fn = disable_fn
        self._accounts = accounts
        self._sleep_fn = sleep_fn
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def accounts(self) -> Any:
        if self._accounts is None:
            from services.account_service import account_service

            return account_service
        return self._accounts

    def start(self, stop_event: threading.Event | None = None) -> threading.Thread:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, args=(stop_event,), name="account-memory", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def wake(self) -> None:
        """有新账号入池时调用，立即扫一轮。"""
        self._wake.set()

    def _stopped(self, stop_event: threading.Event | None) -> bool:
        return self._stop.is_set() or bool(stop_event and stop_event.is_set())

    def _wait(self, seconds: float, stop_event: threading.Event | None) -> bool:
        """等 seconds 秒或被唤醒；返回 True 表示应退出。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stopped(stop_event):
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if self._wake.wait(timeout=min(1.0, left)):
                self._wake.clear()
                return self._stopped(stop_event)
        return True

    def _loop(self, stop_event: threading.Event | None) -> None:
        if self._wait(MEMORY_STARTUP_DELAY_SECS, stop_event):
            return
        while not self._stopped(stop_event):
            try:
                if config.account_memory_auto_disable:
                    self.run_due(stop_event)
            except Exception as exc:
                logger.warning({"event": "account_memory_loop_error", "error": str(exc)[:200]})
            if self._wait(MEMORY_SCAN_INTERVAL_SECS, stop_event):
                return

    def due_accounts(self) -> list[dict[str, Any]]:
        now = time.time()
        return [item for item in self.accounts.list_accounts() if memory_due(item, now)]

    def run_due(self, stop_event: threading.Event | None = None, limit: int = MEMORY_BATCH_LIMIT) -> int:
        """处理一批到期账号，返回成功个数。每批有上限，余下的下一轮（或被唤醒时）接着做。"""
        done = 0
        for position, account in enumerate(self.due_accounts()[:max(1, limit)]):
            if self._stopped(stop_event):
                break
            if position:
                seconds = MEMORY_GAP_SECS + random.uniform(0.2, 1.0)
                if self._sleep_fn is not None:
                    self._sleep_fn(seconds)
                elif self._wait(seconds, stop_event):
                    break
            done += int(self.process(account))
        return done

    def process(self, account: dict[str, Any]) -> bool:
        accounts = self.accounts
        access_token = str(account.get("access_token") or "").strip()
        try:
            result = self._disable_fn(access_token) or {}
        except Exception as exc:
            message = (str(exc) or exc.__class__.__name__)[:300]
            accounts.update_account(access_token, {
                "memory_check_error": message,
                "memory_check_error_at": _now_iso(),
            }, quiet=True)
            logger.warning({"event": "account_memory_disable_failed", "error": message})
            return False
        cleared = int(result.get("cleared") or 0)
        accounts.update_account(access_token, {
            "memory_disabled_at": _now_iso(),
            "memory_cleared": cleared,
            "memory_check_error": None,
            "memory_check_error_at": None,
        }, quiet=True)
        logger.info({
            "event": "account_memory_disabled",
            "was_enabled": bool(result.get("was_enabled")),
            "cleared": cleared,
        })
        return True


account_memory_service = AccountMemoryService()

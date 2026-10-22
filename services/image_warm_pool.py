"""生图预热池：给空闲账号提前做好首页加载和 sentinel 令牌。

一次生图在真正发起会话前要先请求首页、再取 chat-requirements（prepare + finalize），
约 1–2 秒。预热池在有生图流量时，替接下来可能被选中的空闲账号提前做完这两步，
请求拿到账号后直接接管这次"页面加载"的身份（会话 ID、cookie、PoW 脚本、令牌）。

- 只在最近有生图请求时预热（WARM_ACTIVE_WINDOW_SECS），服务空闲时不产生额外请求。
- 同时最多保持 WARM_MAX_ACCOUNTS 个账号的预热，优先最久未预热的账号，避免集中刷少数账号。
- 每份预热只给一个请求用，取走即删；超过 WARM_TTL_SECS 直接丢弃重取。
  实测令牌放 300 秒仍可用，这里取 120 秒留余量。
- 预热按 (账号 token, 出口, TLS 指纹, 设备 ID) 区分，出口或指纹不同不会误用。

移植自 chatgpt-tools 的 chatgpt2api（AGPL-3.0-only）。
"""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from services.config import config
from utils.log import logger

WARM_TTL_SECS = 120.0
WARM_REFRESH_BEFORE_SECS = 20.0
WARM_ACTIVE_WINDOW_SECS = 180.0
WARM_MAX_ACCOUNTS = 6
WARM_WORKERS = 3
WARM_LOOP_INTERVAL_SECS = 5.0
# conv_key_* / history_off_* 是每个会话专属的 cookie，不随预热转交。
PER_CONVERSATION_COOKIE_PREFIXES = ("conv_key_", "history_off_")

WarmKey = tuple[str, str, str, str]


@dataclass
class WarmState:
    key: WarmKey
    session_id: str
    cookies: list[Any]
    pow_script_sources: list[str]
    pow_data_build: str
    requirements: Any
    pow_fingerprint: Any = None
    created_at: float = field(default_factory=time.monotonic)

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.created_at


def backend_warm_key(backend: Any) -> WarmKey:
    profile = getattr(backend, "proxy_profile", None)
    return (
        str(getattr(backend, "access_token", "") or ""),
        str(getattr(profile, "egress_key", "") or "direct"),
        str((getattr(backend, "fp", None) or {}).get("impersonate") or ""),
        str(getattr(backend, "device_id", "") or ""),
    )


def page_cookies(session: Any) -> list[Any]:
    """要转交的 cookie（原始对象副本，保留"仅限主机"等属性）。"""
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar is None:
        return []
    return [
        copy.copy(cookie)
        for cookie in jar
        if not str(getattr(cookie, "name", "")).startswith(PER_CONVERSATION_COOKIE_PREFIXES)
    ]


def capture_warm_state(backend: Any, requirements: Any) -> WarmState:
    return WarmState(
        key=backend_warm_key(backend),
        session_id=str(backend.session_id),
        cookies=page_cookies(backend.session),
        pow_script_sources=list(backend.pow_script_sources),
        pow_data_build=str(backend.pow_data_build or ""),
        requirements=requirements,
        pow_fingerprint=getattr(backend, "pow_fingerprint", None),
    )


def _default_warm(token: str) -> WarmState:
    from services.openai_backend_api import OpenAIBackendAPI

    with OpenAIBackendAPI(access_token=token) as backend:
        backend._bootstrap()
        requirements = backend._get_chat_requirements()
        return capture_warm_state(backend, requirements)


def _default_idle_tokens() -> list[str]:
    """状态正常、当前没有生图在途的账号。"""
    from services.account_service import account_service

    return account_service.idle_image_tokens()


class ImageWarmPool:
    def __init__(
            self,
            warm_fn: Callable[[str], WarmState] = _default_warm,
            idle_tokens_fn: Callable[[], list[str]] = _default_idle_tokens,
            enabled_fn: Callable[[], bool] | None = None,
            clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._warm_fn = warm_fn
        self._idle_tokens_fn = idle_tokens_fn
        self._enabled_fn = enabled_fn or (lambda: bool(config.image_warm_enabled))
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, WarmState] = {}
        self._warming: set[str] = set()
        self._last_warmed_at: dict[str, float] = {}
        self._last_activity = float("-inf")
        self._executor: ThreadPoolExecutor | None = None
        self._loop_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopped = False
        self.hits = 0
        self.misses = 0

    # ── 请求侧 ──────────────────────────────────────────────
    def note_activity(self) -> None:
        """有生图请求时调用；开启预热循环并立即补一轮。"""
        if not self._enabled_fn():
            return
        with self._lock:
            if self._stopped:
                return
            self._last_activity = self._clock()
        self._ensure_loop()
        self._wake.set()

    def has_fresh(self, token: str) -> bool:
        with self._lock:
            state = self._states.get(token)
            return state is not None and state.age(self._clock()) < WARM_TTL_SECS

    def take(self, key: WarmKey) -> WarmState | None:
        """取走与 key 完全匹配且未过期的预热；取走即删，同一份不会给两个请求。"""
        token = key[0]
        with self._lock:
            state = self._states.get(token)
            if state is None:
                self.misses += 1
                return None
            fresh = state.age(self._clock()) < WARM_TTL_SECS
            self._states.pop(token, None)
            if not fresh or state.key != key:
                self.misses += 1
                return None
            self.hits += 1
        self._wake.set()
        return state

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "ready": len(self._states),
                "warming": len(self._warming),
                "hits": self.hits,
                "misses": self.misses,
            }

    # ── 预热侧 ──────────────────────────────────────────────
    def _is_active(self) -> bool:
        with self._lock:
            return self._clock() - self._last_activity < WARM_ACTIVE_WINDOW_SECS

    def _prune_locked(self, now: float) -> None:
        for token in [t for t, s in self._states.items() if s.age(now) >= WARM_TTL_SECS]:
            self._states.pop(token, None)

    def refill(self) -> list[str]:
        """补到最多 WARM_MAX_ACCOUNTS 个预热账号；返回本轮开始预热的 token。"""
        if self._stopped or not self._enabled_fn() or not self._is_active():
            with self._lock:
                self._prune_locked(self._clock())
            return []
        try:
            idle = list(self._idle_tokens_fn())
        except Exception as exc:
            logger.warning({"event": "image_warm_idle_tokens_failed", "error": str(exc)[:200]})
            return []
        idle_set = set(idle)
        started: list[str] = []
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            # 不再空闲的账号（被选中、被禁用、进入限流）快过期时不再保留预热位
            for token in [t for t in self._states if t not in idle_set]:
                if self._states[token].age(now) >= WARM_TTL_SECS - WARM_REFRESH_BEFORE_SECS:
                    self._states.pop(token, None)
            due = [
                t for t in idle
                if t not in self._warming
                and (t not in self._states or self._states[t].age(now) >= WARM_TTL_SECS - WARM_REFRESH_BEFORE_SECS)
            ]
            # 最久没预热过的账号优先，轮流分摊到不同账号
            due.sort(key=lambda t: self._last_warmed_at.get(t, float("-inf")))
            fresh_count = sum(
                1 for s in self._states.values()
                if s.age(now) < WARM_TTL_SECS - WARM_REFRESH_BEFORE_SECS
            )
            room = WARM_MAX_ACCOUNTS - fresh_count - len(self._warming)
            for token in due[:max(0, room)]:
                self._warming.add(token)
                self._last_warmed_at[token] = now
                started.append(token)
            # 已不存在的账号不再记录预热时间，长期运行不累积
            for token in [t for t in self._last_warmed_at if t not in idle_set and t not in self._states]:
                if now - self._last_warmed_at[token] >= WARM_ACTIVE_WINDOW_SECS:
                    self._last_warmed_at.pop(token, None)
        executor = self._get_executor()
        for token in started:
            try:
                executor.submit(self._warm_one, token)
            except RuntimeError:
                # 执行器已关闭（服务停止中）
                with self._lock:
                    self._warming.discard(token)
        return started

    def _warm_one(self, token: str) -> None:
        started = time.perf_counter()
        try:
            state = self._warm_fn(token)
        except Exception as exc:
            logger.warning({
                "event": "image_warm_failed",
                "token_suffix": token[-6:],
                "error": str(exc)[:200],
            })
            return
        finally:
            with self._lock:
                self._warming.discard(token)
        with self._lock:
            if self._stopped:
                return
            state.created_at = self._clock()
            self._states[state.key[0] or token] = state
        logger.debug({
            "event": "image_warm_ready",
            "token_suffix": token[-6:],
            "warm_ms": int((time.perf_counter() - started) * 1000),
        })

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=WARM_WORKERS, thread_name_prefix="image-warm")
            return self._executor

    def _ensure_loop(self) -> None:
        # 与 _loop 的退出判断在同一把锁里进行：退出前会重查活跃时间，所以这里看到
        # 线程还在就不必再起一个，看到 None 就一定要起新的。
        with self._lock:
            if self._loop_thread is not None or self._stopped:
                return
            self._loop_thread = threading.Thread(target=self._loop, name="image-warm-loop", daemon=True)
            self._loop_thread.start()

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=WARM_LOOP_INTERVAL_SECS)
            self._wake.clear()
            if self._stopped:
                with self._lock:
                    self._loop_thread = None
                return
            try:
                self.refill()
            except Exception as exc:
                logger.warning({"event": "image_warm_loop_error", "error": str(exc)[:200]})
            with self._lock:
                now = self._clock()
                if self._stopped or now - self._last_activity >= WARM_ACTIVE_WINDOW_SECS:
                    self._prune_locked(now)
                    if self._stopped or (not self._states and not self._warming):
                        self._loop_thread = None
                        return

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止预热循环和后台执行器，丢弃已有预热；之后的 note_activity 不再生效。"""
        with self._lock:
            self._stopped = True
            thread = self._loop_thread
            executor = self._executor
            self._executor = None
            self._states.clear()
        self._wake.set()
        if thread is not None:
            thread.join(timeout=timeout)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


image_warm_pool = ImageWarmPool()

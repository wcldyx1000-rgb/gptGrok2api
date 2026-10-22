"""管理员密钥登录失败节流（按来源 IP 的滑动窗口）。

Ported from yukkcat/chatgpt-register (AGPL-3.0-only) services/auth.py: too_many_attempts / record_failure /
clear_failures, 增加线程安全、Retry-After 计算与内存上限。

只用于登录 / 状态探测入口（/auth/login、/auth/status）；带 Bearer 的正常 API 调用不经过这里，
不会增加热路径开销。
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from fastapi import Request

DEFAULT_MAX_FAILURES = 10
DEFAULT_WINDOW_SECONDS = 300.0
MAX_TRACKED_CLIENTS = 4096


def _env_number(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value >= minimum else default


class LoginThrottle:
    def __init__(
        self,
        *,
        max_failures: int | None = None,
        window_seconds: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, list[float]] = OrderedDict()

    @property
    def max_failures(self) -> int:
        if self._max_failures is not None:
            return self._max_failures
        return int(_env_number("CHATGPT2API_LOGIN_MAX_FAILURES", DEFAULT_MAX_FAILURES, 1))

    @property
    def window_seconds(self) -> float:
        if self._window_seconds is not None:
            return self._window_seconds
        return _env_number("CHATGPT2API_LOGIN_FAILURE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS, 1)

    def _recent_locked(self, key: str, now: float) -> list[float]:
        window = self.window_seconds
        stamps = [stamp for stamp in self._failures.get(key, []) if now - stamp < window]
        if stamps:
            self._failures[key] = stamps
            self._failures.move_to_end(key)
        else:
            self._failures.pop(key, None)
        return stamps

    def retry_after(self, key: str) -> int:
        """已被节流时返回需要等待的秒数（向上取整，至少 1），否则返回 0。"""
        now = self._clock()
        with self._lock:
            stamps = self._recent_locked(key, now)
            if len(stamps) < self.max_failures:
                return 0
            # 最早的失败滑出窗口后，失败次数降到上限以下。
            release_at = stamps[len(stamps) - self.max_failures] + self.window_seconds
            return max(1, int(release_at - now) + 1)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            stamps = self._recent_locked(key, now)
            stamps.append(now)
            self._failures[key] = stamps
            self._failures.move_to_end(key)
            while len(self._failures) > MAX_TRACKED_CLIENTS:
                self._failures.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


login_throttle = LoginThrottle()


def client_key(request: Request) -> str:
    """来源 IP。默认只信任 TCP 对端；放在反向代理之后时设置
    CHATGPT2API_TRUST_PROXY_HEADERS=1 才读取 X-Forwarded-For 的第一个地址。"""
    if str(os.getenv("CHATGPT2API_TRUST_PROXY_HEADERS") or "").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"

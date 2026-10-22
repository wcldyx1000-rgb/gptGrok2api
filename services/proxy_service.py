"""Global outbound proxy and Cloudflare clearance helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Callable, Mapping
from urllib import request as urllib_request
from urllib.parse import urlparse

from curl_cffi.requests import Session

from services.config import config
from services.proxy_format import colon_proxy_to_url
from utils.locale_env import LocaleEnv, locale_from_fields


FlareSolverrRequestMethod = Callable[[str, bytes, dict[str, str], float], bytes]

# FlareSolverr 每个会话就是一个常驻 Chrome：设上限，空闲久了自动关，避免出口多时把内存吃满。
DEFAULT_FLARESOLVERR_MAX_SESSIONS = 4
DEFAULT_FLARESOLVERR_SESSION_IDLE_SEC = 600
# 凭证用到有效期的这个比例时后台提前换新，请求线程不用原地等一次清障。
DEFAULT_CLEARANCE_EARLY_REFRESH_RATIO = 0.8

DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT = 30
MAX_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT = 10000
# 出口连续失败多少次后临时停用、停用多久（秒）。
DEFAULT_PROXY_NODE_FAIL_THRESHOLD = 1
DEFAULT_PROXY_NODE_COOLDOWN_SEC = 600
_COMPOSE_PRIVOXY_URL = "http://privoxy:8118"
_DEFAULT_HOST_PRIVOXY_PORT = 40080
def _replace_proxy_scheme(candidate: str, scheme: str) -> str:
    return f"{scheme}://{candidate.split('://', 1)[-1]}"


class ProxyReferenceUnavailableError(RuntimeError):
    """配置里引用的代理组不可用（已删除/停用/无可用节点）：明确报错，不静默回落直连泄露真实 IP。"""


class ImageEgressDeadlineError(RuntimeError):
    """图片请求在截止时间（time.monotonic 口径）前没等到出口并发名额。"""


def normalize_proxy_url(url: str) -> str:
    """Normalize proxy URLs for curl_cffi.

    SOCKS proxies should use remote-DNS resolution by default, so generic
    ``socks://`` and ``socks5://`` inputs are upgraded to ``socks5h://``.
    HTTP/HTTPS/socks5h inputs are otherwise left untouched except trimming.
    """
    candidate = str(url or "").strip()
    if candidate and "://" not in candidate:
        candidate = colon_proxy_to_url(candidate)
    lowered = candidate.lower()
    if lowered.startswith("socks://"):
        return "socks5h://" + candidate[len("socks://") :]
    if lowered.startswith("socks5://"):
        return "socks5h://" + candidate[len("socks5://") :]
    return candidate


def normalize_proxy_url_list(value: str) -> str:
    """Normalize a proxy list without guessing a compact import's protocol.

    Compact formats remain exactly as entered.  The explicit sample-test flow
    performs authenticated requests and rewrites the list only after one
    protocol has actually succeeded.  Explicit URL schemes are still normalized.
    """
    lines = [str(line).strip() for line in str(value or "").splitlines() if str(line).strip()]
    return "\n".join(
        normalize_proxy_url(line) if "://" in line else line
        for line in lines
    )


def _is_container_environment() -> bool:
    override = str(os.getenv("CHATGPT2API_IN_CONTAINER") or "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _runtime_proxy_for_environment(url: str) -> str:
    candidate = normalize_proxy_url(url)
    if candidate.rstrip("/").lower() != _COMPOSE_PRIVOXY_URL or _is_container_environment():
        return candidate

    override = normalize_proxy_url(os.getenv("CHATGPT2API_HOST_PRIVOXY_URL") or "")
    if override:
        return override
    try:
        port = int(os.getenv("PRIVOXY_PORT") or _DEFAULT_HOST_PRIVOXY_PORT)
    except (TypeError, ValueError):
        port = _DEFAULT_HOST_PRIVOXY_PORT
    if not 1 <= port <= 65535:
        port = _DEFAULT_HOST_PRIVOXY_PORT
    return f"http://127.0.0.1:{port}"


@dataclass(frozen=True)
class ProxyRuntimeProfile:
    proxy_url: str = ""
    proxy_source: str = "direct"
    egress_key: str = "direct"
    egress_label: str = "direct"
    proxy_group_id: str = ""
    proxy_node_id: str = ""
    proxy_node_name: str = ""
    image_concurrency_limit: int = 0
    image_egress_reserved: bool = False
    image_egress_wait_ms: int = 0
    resource: bool = False
    runtime_enabled: bool = False
    egress_mode: str = "direct"
    skip_ssl_verify: bool = False
    reset_session_status_codes: tuple[int, ...] = field(default_factory=lambda: (403,))
    clearance: dict[str, object] = field(default_factory=dict, repr=False)
    # 出口地区环境（时区 + 语言）；None 表示未配置，调用方沿用原默认值。
    locale: LocaleEnv | None = None

    @property
    def clearance_enabled(self) -> bool:
        return (
            self.runtime_enabled
            and bool(self.clearance.get("enabled"))
            and self.clearance_mode in {"manual", "flaresolverr"}
        )

    @property
    def clearance_mode(self) -> str:
        return str(self.clearance.get("mode") or "none").strip().lower()

    @property
    def refresh_interval(self) -> int:
        try:
            return max(0, int(self.clearance.get("refresh_interval") or 0))
        except (OverflowError, TypeError, ValueError):
            return 0

    @property
    def timeout_sec(self) -> int:
        try:
            return max(1, int(self.clearance.get("timeout_sec") or 60))
        except (OverflowError, TypeError, ValueError):
            return 60

    @property
    def max_sessions(self) -> int:
        try:
            return min(16, max(1, int(self.clearance.get("max_sessions") or DEFAULT_FLARESOLVERR_MAX_SESSIONS)))
        except (OverflowError, TypeError, ValueError):
            return DEFAULT_FLARESOLVERR_MAX_SESSIONS

    @property
    def session_idle_sec(self) -> int:
        try:
            return max(30, int(self.clearance.get("session_idle_sec") or DEFAULT_FLARESOLVERR_SESSION_IDLE_SEC))
        except (OverflowError, TypeError, ValueError):
            return DEFAULT_FLARESOLVERR_SESSION_IDLE_SEC

    @property
    def early_refresh_ratio(self) -> float:
        try:
            value = float(self.clearance.get("early_refresh_ratio") or DEFAULT_CLEARANCE_EARLY_REFRESH_RATIO)
        except (OverflowError, TypeError, ValueError):
            return DEFAULT_CLEARANCE_EARLY_REFRESH_RATIO
        return min(0.95, max(0.1, value))


@dataclass
class _FlareSession:
    session_id: str
    last_used: float
    in_use: int = 0


@dataclass(frozen=True)
class ClearanceBundle:
    target_host: str
    proxy_url: str = ""
    cookies: dict[str, str] = field(default_factory=dict, repr=False)
    user_agent: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def is_valid_for(self, target_host: str, proxy_url: str, *, now: float | None = None) -> bool:
        host = _normalize_host(target_host)
        if self.target_host and host and _normalize_host(self.target_host) != host:
            return False
        if normalize_proxy_url(self.proxy_url) != normalize_proxy_url(proxy_url):
            return False
        if self.expires_at is not None and (time.time() if now is None else now) >= self.expires_at:
            return False
        return bool(self.cookies or self.user_agent)

    def cookie_header(self) -> str:
        return _cookies_to_header(self.cookies)


@dataclass(frozen=True)
class ProxyGroupSelection:
    proxy_url: str = ""
    group_id: str = ""
    node_id: str = ""
    node_name: str = ""
    image_concurrency_limit: int = 0
    image_egress_reserved: bool = False
    image_egress_wait_ms: int = 0
    locale: LocaleEnv | None = None

    @property
    def egress_key(self) -> str:
        if self.group_id and self.node_id:
            return f"group:{self.group_id}:{self.node_id}"
        return _egress_key_for_proxy(self.proxy_url)

    @property
    def egress_label(self) -> str:
        if self.group_id and self.node_id:
            return f"{self.group_id}/{self.node_name or self.node_id}"
        return _egress_key_for_proxy(self.proxy_url)


@dataclass(frozen=True)
class ResolvedProxyReference:
    proxy_url: str = ""
    source: str = "direct"
    terminal: bool = False
    egress_key: str = "direct"
    egress_label: str = "direct"
    proxy_group_id: str = ""
    proxy_node_id: str = ""
    proxy_node_name: str = ""
    image_concurrency_limit: int = 0
    image_egress_reserved: bool = False
    image_egress_wait_ms: int = 0
    locale: LocaleEnv | None = None


class FlareSolverrClearanceProvider:
    """FlareSolverr 清障客户端，按出口复用浏览器会话。

    同一出口复用一个 FlareSolverr session（常驻 Chrome），第二次过验证不用再冷启动浏览器；
    会话数有上限，满了先关最久没用的空闲会话，全部在忙时退回一次性无会话请求。
    """

    def __init__(
        self,
        flaresolverr_url: str,
        request_method: FlareSolverrRequestMethod | None = None,
        *,
        max_sessions: int = DEFAULT_FLARESOLVERR_MAX_SESSIONS,
        idle_sec: float = DEFAULT_FLARESOLVERR_SESSION_IDLE_SEC,
        clock: Callable[[], float] | None = None,
        start_sweeper: bool = True,
    ) -> None:
        self.flaresolverr_url = str(flaresolverr_url or "").strip().rstrip("/")
        self._request_method = request_method or self._urllib_post
        self._sessions: dict[str, _FlareSession] = {}
        self._session_lock = threading.Lock()
        self.max_sessions = max(1, int(max_sessions))
        self.idle_sec = max(1.0, float(idle_sec))
        self._clock = clock or time.time
        self._start_sweeper = start_sweeper
        self._sweeper: threading.Thread | None = None
        self._stop_event = threading.Event()

    def configure(self, *, max_sessions: int | None = None, idle_sec: float | None = None) -> None:
        """跟随设置页调整会话上限和空闲回收时间；收紧上限时多出来的空闲会话在下次取会话时淘汰。"""
        if max_sessions is not None:
            self.max_sessions = max(1, int(max_sessions))
        if idle_sec is not None:
            self.idle_sec = max(1.0, float(idle_sec))

    def get_clearance(self, target_url: str, proxy_url: str = "", timeout_sec: int = 60) -> ClearanceBundle | None:
        if not self.flaresolverr_url:
            return None

        timeout = _coerce_timeout(timeout_sec)
        proxy_url = normalize_proxy_url(proxy_url)
        session_id = self._ensure_session(proxy_url, timeout)
        try:
            data = self._request(self._request_payload(target_url, timeout, session_id, proxy_url), timeout)
            if data is None and session_id:
                # 会话可能已被 FlareSolverr 回收或浏览器崩了：丢掉它，换一个再试一次。
                self._drop_session(proxy_url, session_id)
                session_id = self._ensure_session(proxy_url, timeout)
                data = self._request(self._request_payload(target_url, timeout, session_id, proxy_url), timeout)
        finally:
            self._release_session(proxy_url, session_id)

        if not isinstance(data, dict) or str(data.get("status") or "").lower() != "ok":
            message = str(data.get("message") or "").lower() if isinstance(data, dict) else ""
            if session_id and "session" in message:
                self._drop_session(proxy_url, session_id)
            return None
        solution = data.get("solution")
        if not isinstance(solution, dict):
            return None

        target_host = _host_from_url(target_url)
        # 复用的浏览器会话里会攒下目标站的业务 Cookie（登录态、设备 ID 等），只带走 CF 相关的。
        cookies = {
            name: value
            for name, value in _filter_flaresolverr_cookies(solution.get("cookies"), target_host).items()
            if _is_clearance_cookie(name)
        }
        user_agent = str(solution.get("userAgent") or "").strip()
        if not cookies and not user_agent:
            return None
        return ClearanceBundle(
            target_host=target_host,
            proxy_url=proxy_url,
            cookies=cookies,
            user_agent=user_agent,
        )

    def close_idle_sessions(self) -> int:
        """关掉空闲超过 idle_sec 的会话，返回关掉的个数。"""
        now = self._clock()
        expired: list[str] = []
        with self._session_lock:
            for key, item in list(self._sessions.items()):
                if item.in_use <= 0 and now - item.last_used >= self.idle_sec:
                    expired.append(self._sessions.pop(key).session_id)
        for session_id in expired:
            self._destroy_remote(session_id)
        return len(expired)

    def session_count(self) -> int:
        with self._session_lock:
            return len(self._sessions)

    def shutdown(self) -> None:
        """停止空闲回收线程并关闭全部会话（服务退出时调用）。"""
        self._stop_event.set()
        with self._session_lock:
            session_ids = [item.session_id for item in self._sessions.values()]
            self._sessions.clear()
            sweeper = self._sweeper
        for session_id in session_ids:
            self._destroy_remote(session_id)
        if sweeper is not None and sweeper is not threading.current_thread():
            sweeper.join(timeout=2.0)

    @staticmethod
    def _request_payload(target_url: str, timeout: float, session_id: str, proxy_url: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "cmd": "request.get",
            "url": str(target_url or ""),
            "maxTimeout": int(timeout * 1000),
            # 只要 Cookie 和 UA：不回传整页 HTML、不加载媒体，过验证更快也更省内存（旧版 FlareSolverr 会忽略）。
            "returnOnlyCookies": True,
            "disableMedia": True,
        }
        if session_id:
            payload["session"] = session_id
        if proxy_url:
            # 会话在 sessions.create 时已绑定同一代理；部分版本会忽略带会话请求里的 proxy，照带无害。
            payload["proxy"] = {"url": proxy_url}
        return payload

    def _ensure_session(self, proxy_url: str, timeout: float) -> str:
        """取这个出口的会话并标记占用；满了先关最久没用的空闲会话，都在忙就不用会话。"""
        if self._stop_event.is_set():
            return ""
        key = proxy_url or "direct"
        self.close_idle_sessions()
        evict: list[str] = []
        with self._session_lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.in_use += 1
                existing.last_used = self._clock()
                return existing.session_id
            while len(self._sessions) >= self.max_sessions:
                idle = [(item.last_used, name) for name, item in self._sessions.items() if item.in_use <= 0]
                if not idle:
                    # 每个会话都在过验证：这次走一次性的无会话请求，不再多开浏览器。
                    return ""
                _last_used, victim = min(idle)
                evict.append(self._sessions.pop(victim).session_id)
        for session_id in evict:
            self._destroy_remote(session_id)

        create: dict[str, object] = {"cmd": "sessions.create"}
        if proxy_url:
            create["proxy"] = {"url": proxy_url}
        data = self._request(create, min(timeout, 20.0))
        session_id = ""
        if isinstance(data, dict) and str(data.get("status") or "").lower() == "ok":
            session_id = str(data.get("session") or "").strip()
        if not session_id:
            return ""
        orphan = ""
        with self._session_lock:
            current = self._sessions.get(key)
            if current is not None:
                # 并发时别的线程先建好了：用它的，把自己这个关掉。
                current.in_use += 1
                current.last_used = self._clock()
                orphan, session_id = session_id, current.session_id
            else:
                self._sessions[key] = _FlareSession(session_id=session_id, last_used=self._clock(), in_use=1)
        if orphan:
            self._destroy_remote(orphan)
        self._ensure_sweeper()
        return session_id

    def _release_session(self, proxy_url: str, session_id: str) -> None:
        if not session_id:
            return
        key = proxy_url or "direct"
        with self._session_lock:
            item = self._sessions.get(key)
            if item is not None and item.session_id == session_id:
                item.in_use = max(0, item.in_use - 1)
                item.last_used = self._clock()

    def _drop_session(self, proxy_url: str, session_id: str) -> None:
        key = proxy_url or "direct"
        with self._session_lock:
            item = self._sessions.get(key)
            if item is not None and item.session_id == session_id:
                self._sessions.pop(key, None)
        if session_id:
            self._destroy_remote(session_id)

    def _destroy_remote(self, session_id: str) -> None:
        if session_id:
            self._request({"cmd": "sessions.destroy", "session": session_id}, 8.0)

    def _ensure_sweeper(self) -> None:
        if not self._start_sweeper or self._stop_event.is_set():
            return
        with self._session_lock:
            if self._sweeper is not None and self._sweeper.is_alive():
                return
            interval = max(5.0, min(60.0, self.idle_sec / 4))

            def sweep() -> None:
                # 没有会话时退出，下次建会话再起；服务退出时由 stop_event 叫醒。
                while not self._stop_event.wait(interval):
                    try:
                        self.close_idle_sessions()
                    except Exception:
                        pass
                    if not self.session_count():
                        return

            self._sweeper = threading.Thread(target=sweep, daemon=True, name="flaresolverr-session-sweeper")
            self._sweeper.start()

    def _request(self, payload: dict[str, object], timeout: float) -> dict | None:
        if not self.flaresolverr_url:
            return None
        endpoint = f"{self.flaresolverr_url}/v1"
        try:
            body = json.dumps(payload).encode("utf-8")
            raw_response = self._request_method(
                endpoint,
                body,
                {"Content-Type": "application/json"},
                timeout,
            )
            data = json.loads(raw_response.decode("utf-8") if isinstance(raw_response, bytes) else raw_response)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _urllib_post(endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        req = urllib_request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib_request.urlopen(req, timeout=timeout) as response:
            return response.read()


class ProxySettingsStore:
    def __init__(
        self,
        config_store=None,
        clearance_provider_factory: Callable[[str], FlareSolverrClearanceProvider] | None = None,
    ) -> None:
        self._config = config_store or config
        self._clearance_provider_factory = clearance_provider_factory or FlareSolverrClearanceProvider
        self._clearance_cache: dict[tuple[str, str], ClearanceBundle] = {}
        self._provider_cache: dict[str, FlareSolverrClearanceProvider] = {}
        self._flight_locks: dict[tuple[str, str], threading.Lock] = {}
        self._egress_inflight: dict[str, int] = {}
        self._lock = threading.RLock()
        self._egress_condition = threading.Condition(self._lock)
        self._background_refreshing: set[tuple[str, str]] = set()
        self._challenge_cooldown_until: dict[str, float] = {}
        # 出口节点健康：连续失败次数、临时停用截止时间（time.time()）、最近错误；只在内存里，不改用户配置。
        self._node_failures: dict[str, int] = {}
        self._node_disabled_until: dict[str, float] = {}
        self._node_last_error: dict[str, str] = {}
        self._background_starter: Callable[[Callable[[], None]], None] = self._start_daemon
        self._closed = threading.Event()

    def get_profile(
        self,
        account: dict | None = None,
        proxy: str = "",
        resource: bool = False,
        upstream: bool = False,
        reserve_image_egress: bool = False,
        deadline_monotonic: float | None = None,
    ) -> ProxyRuntimeProfile:
        runtime = self._get_runtime_settings()
        clearance = dict(runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {})
        runtime_enabled = bool(runtime.get("enabled"))
        egress_mode = str(runtime.get("egress_mode") or "direct").strip().lower()

        runtime_proxy = ""
        runtime_proxy_source = "runtime"
        if upstream and runtime_enabled and egress_mode == "single_proxy":
            resource_proxy = _clean(runtime.get("resource_proxy_url")) if resource else ""
            runtime_proxy = _runtime_proxy_for_environment(resource_proxy or _clean(runtime.get("proxy_url")))
            runtime_proxy_source = "runtime_resource" if resource_proxy else "runtime"

        selected_proxy = ""
        source = "direct"
        terminal = False
        egress_key = "direct"
        egress_label = "direct"
        proxy_group_id = ""
        proxy_node_id = ""
        proxy_node_name = ""
        image_concurrency_limit = 0
        image_egress_reserved = False
        image_egress_wait_ms = 0
        locale: LocaleEnv | None = None

        account_proxy = _clean((account or {}).get("proxy") if isinstance(account, dict) else "")
        if account_proxy:
            resolved = self._resolve_proxy_reference(
                account_proxy,
                source="account",
                terminal_when_unresolved=True,
                reserve_image_egress=reserve_image_egress,
                deadline_monotonic=deadline_monotonic,
            )
            selected_proxy, source, terminal = resolved.proxy_url, resolved.source, resolved.terminal
            egress_key = resolved.egress_key
            egress_label = resolved.egress_label
            proxy_group_id = resolved.proxy_group_id
            proxy_node_id = resolved.proxy_node_id
            proxy_node_name = resolved.proxy_node_name
            image_concurrency_limit = resolved.image_concurrency_limit
            image_egress_reserved = resolved.image_egress_reserved
            image_egress_wait_ms = resolved.image_egress_wait_ms
            locale = resolved.locale

        if not selected_proxy and not terminal:
            account_group_proxy = self._account_group_proxy_reference(account)
            if account_group_proxy:
                resolved = self._resolve_proxy_reference(
                    account_group_proxy,
                    source="account_group",
                    terminal_when_unresolved=False,
                    reserve_image_egress=reserve_image_egress,
                    deadline_monotonic=deadline_monotonic,
                )
                selected_proxy, source, terminal = resolved.proxy_url, resolved.source, resolved.terminal
                egress_key = resolved.egress_key
                egress_label = resolved.egress_label
                proxy_group_id = resolved.proxy_group_id
                proxy_node_id = resolved.proxy_node_id
                proxy_node_name = resolved.proxy_node_name
                image_concurrency_limit = resolved.image_concurrency_limit
                image_egress_reserved = resolved.image_egress_reserved
                image_egress_wait_ms = resolved.image_egress_wait_ms
                locale = resolved.locale

        if not selected_proxy and not terminal:
            explicit_proxy = _clean(proxy)
            if explicit_proxy:
                resolved = self._resolve_proxy_reference(
                    explicit_proxy,
                    source="explicit",
                    terminal_when_unresolved=True,
                    reserve_image_egress=reserve_image_egress,
                    deadline_monotonic=deadline_monotonic,
                )
                selected_proxy, source, terminal = resolved.proxy_url, resolved.source, resolved.terminal
                egress_key = resolved.egress_key
                egress_label = resolved.egress_label
                proxy_group_id = resolved.proxy_group_id
                proxy_node_id = resolved.proxy_node_id
                proxy_node_name = resolved.proxy_node_name
                image_concurrency_limit = resolved.image_concurrency_limit
                image_egress_reserved = resolved.image_egress_reserved
                image_egress_wait_ms = resolved.image_egress_wait_ms
                locale = resolved.locale

        if not selected_proxy and not terminal:
            legacy_proxy = _clean(self._config.get_proxy_settings())
            if legacy_proxy:
                resolved = self._resolve_proxy_reference(
                    legacy_proxy,
                    source="default",
                    terminal_when_unresolved=False,
                    reserve_image_egress=reserve_image_egress,
                    deadline_monotonic=deadline_monotonic,
                )
                selected_proxy, source, terminal = resolved.proxy_url, resolved.source, resolved.terminal
                egress_key = resolved.egress_key
                egress_label = resolved.egress_label
                proxy_group_id = resolved.proxy_group_id
                proxy_node_id = resolved.proxy_node_id
                proxy_node_name = resolved.proxy_node_name
                image_concurrency_limit = resolved.image_concurrency_limit
                image_egress_reserved = resolved.image_egress_reserved
                image_egress_wait_ms = resolved.image_egress_wait_ms
                locale = resolved.locale

        if not selected_proxy and not terminal and runtime_proxy:
            selected_proxy = runtime_proxy
            source = runtime_proxy_source
            egress_key = _egress_key_for_proxy(runtime_proxy)
            egress_label = runtime_proxy_source

        if locale is None and selected_proxy:
            # 账号 / 默认代理直接指向某个出口槽位时，沿用该槽位设置的时区和语言。
            locale = _egress_slot_locale(selected_proxy)

        return ProxyRuntimeProfile(
            proxy_url=normalize_proxy_url(selected_proxy),
            proxy_source=source,
            egress_key=egress_key or _egress_key_for_proxy(selected_proxy),
            egress_label=egress_label or source,
            proxy_group_id=proxy_group_id,
            proxy_node_id=proxy_node_id,
            proxy_node_name=proxy_node_name,
            image_concurrency_limit=max(0, int(image_concurrency_limit or 0)),
            image_egress_reserved=bool(image_egress_reserved),
            image_egress_wait_ms=max(0, int(image_egress_wait_ms or 0)),
            resource=bool(resource),
            runtime_enabled=runtime_enabled,
            egress_mode=egress_mode,
            skip_ssl_verify=bool(runtime.get("skip_ssl_verify")),
            reset_session_status_codes=_status_codes_tuple(runtime.get("reset_session_status_codes")),
            clearance=clearance,
            locale=locale or self._default_locale(source, runtime),
        )

    def _default_locale(self, source: str, runtime: Mapping) -> LocaleEnv | None:
        """没有代理组节点 / 槽位地区环境时的兜底：
        运行时单代理沿用 proxy_runtime 的时区语言；直连与自定义代理 URL 优先使用“直连出口地区环境”，
        未配置则保持原行为（proxy_runtime 的时区语言）。
        """
        if not str(source or "").startswith("runtime"):
            getter = getattr(self._config, "get_direct_locale", None)
            direct = LocaleEnv.from_config(getter()) if callable(getter) else None
            if direct is not None:
                return direct
        return locale_from_fields(runtime)

    def get_fallback_proxy_reference(self) -> str:
        try:
            reference = _clean(self._config.get_proxy_fallback_settings())
        except AttributeError:
            data = getattr(self._config, "data", None)
            reference = _clean(data.get("fallback_proxy")) if isinstance(data, dict) else ""
        return "" if reference.lower() == "global" else reference

    def get_fallback_profile(
        self,
        *,
        resource: bool = False,
        upstream: bool = False,
        reserve_image_egress: bool = False,
    ) -> ProxyRuntimeProfile | None:
        reference = self.get_fallback_proxy_reference()
        if not reference:
            return None
        profile = self.get_profile(
            account=None,
            proxy=reference,
            resource=resource,
            upstream=upstream,
            reserve_image_egress=reserve_image_egress,
        )
        source = str(profile.proxy_source or "direct").strip() or "direct"
        if source.startswith("explicit"):
            source = "fallback" + source[len("explicit"):]
        elif not source.startswith("fallback"):
            source = f"fallback_{source}"
        label = str(profile.egress_label or "").strip()
        if not label or label.startswith("explicit"):
            label = "fallback" if profile.proxy_url else source
        return replace(profile, proxy_source=source, egress_label=label)

    def build_session_kwargs(
        self,
        account: dict | None = None,
        proxy: str = "",
        resource: bool = False,
        upstream: bool = False,
        **session_kwargs,
    ) -> dict[str, object]:
        profile = self.get_profile(account=account, proxy=proxy, resource=resource, upstream=upstream)
        if profile.proxy_url:
            session_kwargs["proxy"] = profile.proxy_url
        if profile.skip_ssl_verify:
            session_kwargs["verify"] = False
        return session_kwargs

    @staticmethod
    def build_session_kwargs_from_profile(
        profile: ProxyRuntimeProfile,
        **session_kwargs,
    ) -> dict[str, object]:
        if profile.proxy_url:
            session_kwargs["proxy"] = profile.proxy_url
        if profile.skip_ssl_verify:
            session_kwargs["verify"] = False
        return session_kwargs

    def build_headers(
        self,
        headers: Mapping[str, object] | None = None,
        target_url: str = "https://chatgpt.com",
        account: dict | None = None,
        proxy: str = "",
        resource: bool = False,
        upstream: bool = True,
    ) -> dict[str, object]:
        merged_headers: dict[str, object] = dict(headers or {})
        profile = self.get_profile(account=account, proxy=proxy, resource=resource, upstream=upstream)
        if not profile.clearance_enabled:
            return merged_headers

        target_host = _host_from_url(target_url)
        bundle = self._bundle_for_headers(profile, target_host)
        if bundle is None or not bundle.is_valid_for(target_host, profile.proxy_url):
            return merged_headers

        # 请求头路径也要触发提前刷新：注册/出图线程每个请求只走 build_headers，
        # 不然凭证只会在到期后遇挑战才强刷。只起后台刷新，当前请求仍用旧凭证。
        self._maybe_refresh_early(
            profile, target_url, self._cache_key(profile.proxy_url, target_host), bundle
        )

        if bundle.user_agent and _find_header_key(merged_headers, "user-agent") is None:
            merged_headers["User-Agent"] = bundle.user_agent

        if bundle.cookies:
            cookie_key = _find_header_key(merged_headers, "cookie") or "Cookie"
            existing_cookie = str(merged_headers.get(cookie_key) or "")
            cookie_header = _merge_cookie_header(existing_cookie, bundle.cookies)
            if cookie_header:
                merged_headers[cookie_key] = cookie_header
        return merged_headers

    def refresh_clearance(
        self,
        target_url: str = "https://chatgpt.com",
        account: dict | None = None,
        proxy: str = "",
        resource: bool = False,
        force: bool = False,
        upstream: bool = True,
    ) -> ClearanceBundle | None:
        profile = self.get_profile(account=account, proxy=proxy, resource=resource, upstream=upstream)
        if not profile.clearance_enabled:
            return None

        target_host = _host_from_url(target_url)
        key = self._cache_key(profile.proxy_url, target_host)
        if profile.clearance_mode == "manual":
            bundle = self._build_manual_bundle(profile, target_host)
            if bundle is not None:
                self._set_cached_bundle(key, bundle)
            return bundle
        if profile.clearance_mode != "flaresolverr":
            return None

        cached_before = self._get_cached_bundle(key)
        if cached_before is not None and not force and cached_before.is_valid_for(target_host, profile.proxy_url):
            self._maybe_refresh_early(profile, target_url, key, cached_before)
            return cached_before

        lock = self._get_flight_lock(key)
        if not lock.acquire(blocking=False):
            with lock:
                pass
            return self._get_cached_bundle(key) or cached_before

        try:
            cached_now = self._get_cached_bundle(key)
            if cached_now is not None and not force and cached_now.is_valid_for(target_host, profile.proxy_url):
                return cached_now

            flaresolverr_url = str(profile.clearance.get("flaresolverr_url") or "").strip()
            provider = self._get_provider(flaresolverr_url, profile)
            new_bundle = provider.get_clearance(target_url, proxy_url=profile.proxy_url, timeout_sec=profile.timeout_sec)
            if new_bundle is not None:
                # 新凭证只带回本次下发的 CF Cookie，旧凭证里其余仍有效的 Cookie 和 UA 一并保留。
                keep = cached_now or cached_before
                merged_cookies = dict(keep.cookies) if keep is not None else {}
                merged_cookies.update(new_bundle.cookies)
                now = time.time()
                new_bundle = replace(
                    new_bundle,
                    target_host=new_bundle.target_host or target_host,
                    proxy_url=profile.proxy_url,
                    cookies=merged_cookies,
                    user_agent=new_bundle.user_agent or (keep.user_agent if keep is not None else ""),
                    created_at=now,
                    expires_at=now + profile.refresh_interval if profile.refresh_interval else None,
                )
                self._set_cached_bundle(key, new_bundle)
                return new_bundle
            return cached_now or cached_before
        finally:
            lock.release()

    def invalidate_clearance(
        self,
        target_url: str = "https://chatgpt.com",
        account: dict | None = None,
        proxy: str = "",
        resource: bool = False,
        upstream: bool = True,
    ) -> None:
        profile = self.get_profile(account=account, proxy=proxy, resource=resource, upstream=upstream)
        target_host = _host_from_url(target_url)
        key = self._cache_key(profile.proxy_url, target_host)
        with self._lock:
            self._clearance_cache.pop(key, None)

    def warm_up(
        self,
        proxies: list[str],
        target_url: str = "https://auth.openai.com",
        *,
        on_done: Callable[[str, bool], None] | None = None,
    ) -> int:
        """按出口并行预热 FlareSolverr clearance，不阻塞调用方；返回起了几个预热。

        已有有效凭证的出口不会重复清障；请求线程碰上正在预热的出口会等这一次完成（single flight）。
        """
        started = 0
        seen: set[str] = set()
        for raw in proxies:
            reference = _clean(raw)
            try:
                profile = self.get_profile(proxy=reference, upstream=True)
            except ProxyReferenceUnavailableError:
                continue
            if not profile.clearance_enabled or profile.clearance_mode != "flaresolverr":
                continue
            if profile.proxy_url in seen:
                continue
            seen.add(profile.proxy_url)
            proxy_url = profile.proxy_url

            def run(proxy_url: str = proxy_url, reference: str = reference) -> None:
                ok = False
                try:
                    # 代理组引用每次解析可能落到不同节点，按已解析出的节点地址预热。
                    ok = self.refresh_clearance(
                        target_url=target_url,
                        proxy=proxy_url or reference,
                        upstream=True,
                    ) is not None
                except Exception:
                    ok = False
                if on_done is not None:
                    try:
                        on_done(proxy_url, ok)
                    except Exception:
                        pass

            if self._run_in_background(run):
                started += 1
        return started

    def warm_up_on_start(self, target_url: str = "https://chatgpt.com") -> int:
        """按设置页“启动后预热 clearance”在服务启动时预热默认出口；未开启时什么都不做。"""
        clearance = self._get_runtime_settings().get("clearance")
        if not isinstance(clearance, dict) or not clearance.get("warm_up_on_start"):
            return 0
        try:
            reference = _clean(self._config.get_proxy_settings())
        except AttributeError:
            reference = ""
        return self.warm_up(self.egress_candidates(reference) or [""], target_url)

    def proxy_group_references(self, group_id: object) -> list[str]:
        """列出仍引用该代理组（group:<id>）的地方；供删除前检查，空列表表示可以安全删除。

        检查：全局默认代理、备用代理、账号分组、各账号自己的 proxy、注册任务的 proxy。
        任一来源读取失败只跳过该来源，不让检查本身抛错。
        """
        target = _clean(group_id)
        if not target:
            return []

        def refers(value: object) -> bool:
            raw = _clean(value)
            return raw.lower().startswith("group:") and _clean(raw.split(":", 1)[1]) == target

        found: list[str] = []
        try:
            if refers(self._config.get_proxy_settings()):
                found.append("全局默认代理")
        except Exception:
            pass
        try:
            if refers(self.get_fallback_proxy_reference()):
                found.append("备用代理")
        except Exception:
            pass
        try:
            for group in self._config_dict_list("account_groups"):
                if refers(group.get("proxy")) or _clean(group.get("proxy_group_id")) == target:
                    found.append(f"账号分组「{_clean(group.get('name')) or _clean(group.get('id')) or '?'}」")
        except Exception:
            pass
        try:
            from services.account_service import account_service

            using = sum(1 for item in account_service.list_accounts() if refers(item.get("proxy")))
            if using:
                found.append(f"{using} 个账号")
        except Exception:
            pass
        try:
            from services.register_service import register_service

            if refers(register_service.get().get("proxy")):
                found.append("注册任务代理")
        except Exception:
            pass
        return found

    def egress_candidates(self, reference: str = "") -> list[str]:
        """展开一个代理引用能落到的全部出口：代理组展开为启用的节点，其余原样返回。"""
        raw = _clean(reference)
        if raw.lower().startswith("group:"):
            group_id = raw.split(":", 1)[1].strip()
            for group in self._config_dict_list("proxy_groups"):
                if _clean(group.get("id")) != group_id or group.get("enabled") is False:
                    continue
                return [
                    _clean(node.get("url"))
                    for node in group.get("nodes", [])
                    if isinstance(node, dict) and node.get("enabled", True) and _clean(node.get("url"))
                ]
            return []
        return [raw]

    def mark_challenge(self, proxy: str = "", *, cooldown_sec: float = 4.0) -> None:
        """记录某出口刚被 Cloudflare 拦截，短时间内让各线程错开重试。"""
        key = normalize_proxy_url(_clean(proxy)) or "direct"
        with self._lock:
            self._challenge_cooldown_until[key] = max(
                float(self._challenge_cooldown_until.get(key) or 0),
                time.time() + max(0.5, float(cooldown_sec)),
            )

    def wait_challenge_cooldown(
        self,
        proxy: str = "",
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> float:
        """若出口处于挑战冷却窗口则等待（加随机抖动，避免冷却结束瞬间齐射），返回实际等待秒数。"""
        key = normalize_proxy_url(_clean(proxy)) or "direct"
        with self._lock:
            until = float(self._challenge_cooldown_until.get(key) or 0)
        delay = until - time.time()
        if delay <= 0:
            return 0.0
        delay += random.uniform(0.2, 1.2)
        (sleep or time.sleep)(delay)
        return delay

    def report_proxy_outcome(self, proxy: str, *, ok: bool, error: str = "") -> bool:
        """上报一次出口结果：连续失败达到阈值后临时停用该出口，冷却后自动恢复。

        停用只影响代理组选节点（停用的节点不再被选中，整组都停用时照常选），
        不会删除或改动用户保存的代理配置。返回这次上报是否让出口进入停用。
        """
        key = normalize_proxy_url(_clean(proxy))
        if not key:
            return False
        with self._lock:
            if ok:
                self._node_failures.pop(key, None)
                self._node_disabled_until.pop(key, None)
                self._node_last_error.pop(key, None)
                return False
            threshold, cooldown = self._node_health_settings()
            failures = int(self._node_failures.get(key, 0)) + 1
            self._node_last_error[key] = _redact_url_credentials(_clean(error))[:200]
            if failures < threshold:
                self._node_failures[key] = failures
                return False
            now = time.time()
            already_disabled = float(self._node_disabled_until.get(key) or 0) > now
            self._node_disabled_until[key] = now + cooldown
            # 恢复后重新从 0 计数，再连续失败到阈值才会再次停用。
            self._node_failures[key] = 0
            return not already_disabled

    def is_proxy_disabled(self, proxy: str, *, now: float | None = None) -> bool:
        key = normalize_proxy_url(_clean(proxy))
        if not key:
            return False
        current = time.time() if now is None else now
        with self._lock:
            until = float(self._node_disabled_until.get(key) or 0)
            if until and until <= current:
                self._node_disabled_until.pop(key, None)
                return False
            return until > current

    def disabled_proxies(self) -> set[str]:
        now = time.time()
        with self._lock:
            for key, until in list(self._node_disabled_until.items()):
                if until <= now:
                    self._node_disabled_until.pop(key, None)
            return set(self._node_disabled_until)

    def node_health_snapshot(self) -> list[dict[str, object]]:
        """当前被临时停用的出口（地址已脱敏），供页面展示。"""
        now = time.time()
        with self._lock:
            items = [
                {
                    "proxy": _redact_url_credentials(key),
                    "disabled_until": until,
                    "remaining_sec": max(0, int(until - now)),
                    "last_error": self._node_last_error.get(key, ""),
                }
                for key, until in self._node_disabled_until.items()
                if until > now
            ]
        return sorted(items, key=lambda item: -int(item["remaining_sec"]))

    def _node_health_settings(self) -> tuple[int, float]:
        runtime = self._get_runtime_settings()
        try:
            threshold = int(runtime.get("node_fail_threshold") or DEFAULT_PROXY_NODE_FAIL_THRESHOLD)
        except (TypeError, ValueError):
            threshold = DEFAULT_PROXY_NODE_FAIL_THRESHOLD
        try:
            cooldown = float(runtime.get("node_cooldown_sec") or DEFAULT_PROXY_NODE_COOLDOWN_SEC)
        except (TypeError, ValueError):
            cooldown = float(DEFAULT_PROXY_NODE_COOLDOWN_SEC)
        return max(1, threshold), max(10.0, cooldown)

    def shutdown(self) -> None:
        """停止后台清障任务并关闭 FlareSolverr 会话（服务退出时调用）。"""
        self._closed.set()
        with self._lock:
            providers = list(self._provider_cache.values())
        for provider in providers:
            close = getattr(provider, "shutdown", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _maybe_refresh_early(
        self,
        profile: ProxyRuntimeProfile,
        target_url: str,
        key: tuple[str, str],
        bundle: ClearanceBundle,
        *,
        now: float | None = None,
    ) -> bool:
        """凭证有效期用掉 early_refresh_ratio（默认 80%）后在后台换新；返回是否起了后台刷新。"""
        if profile.clearance_mode != "flaresolverr" or bundle.expires_at is None:
            return False
        lifetime = bundle.expires_at - bundle.created_at
        if lifetime <= 0:
            return False
        current = time.time() if now is None else now
        if current < bundle.created_at + lifetime * profile.early_refresh_ratio:
            return False
        with self._lock:
            if key in self._background_refreshing:
                return False
            self._background_refreshing.add(key)
        proxy_url = profile.proxy_url

        def run() -> None:
            try:
                self.refresh_clearance(target_url=target_url, proxy=proxy_url, force=True, upstream=True)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._background_refreshing.discard(key)

        if self._run_in_background(run):
            return True
        with self._lock:
            self._background_refreshing.discard(key)
        return False

    def _run_in_background(self, target: Callable[[], None]) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._background_starter(target)
        except Exception:
            return False
        return True

    @staticmethod
    def _start_daemon(target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True, name="clearance-background-refresh").start()

    def get_runtime_status(self) -> dict[str, object]:
        try:
            profile = self.get_profile(upstream=True)
        except ProxyReferenceUnavailableError as error:
            # 状态接口不因默认代理引用失效而 500：把原因直接报出来。
            runtime = self._get_runtime_settings()
            return {
                "enabled": bool(runtime.get("enabled")),
                "egress_mode": str(runtime.get("egress_mode") or "direct"),
                "has_proxy": False,
                "reference_error": str(error),
                "disabled_proxy_nodes": self.node_health_snapshot(),
            }
        with self._lock:
            cached_hosts = [host for _proxy, host in self._clearance_cache]
            cached_count = len(self._clearance_cache)
            providers = list(self._provider_cache.values())
        session_count = 0
        for provider in providers:
            counter = getattr(provider, "session_count", None)
            if callable(counter):
                session_count += int(counter())
        return {
            "enabled": profile.runtime_enabled,
            "egress_mode": profile.egress_mode,
            "proxy_source": profile.proxy_source,
            "egress_key": _redact_url_credentials(profile.egress_key),
            "egress_label": _redact_url_credentials(profile.egress_label),
            "image_concurrency_limit": profile.image_concurrency_limit,
            "has_proxy": bool(profile.proxy_url),
            "skip_ssl_verify": profile.skip_ssl_verify,
            "clearance_enabled": profile.clearance_enabled,
            "clearance_mode": profile.clearance_mode,
            "has_clearance_bundle": cached_count > 0,
            "cached_clearance_hosts": sorted(set(cached_hosts)),
            "flaresolverr_sessions": session_count,
            "disabled_proxy_nodes": self.node_health_snapshot(),
        }

    def should_skip_ssl_verify(self) -> bool:
        return bool(self._get_runtime_settings().get("skip_ssl_verify"))

    def acquire_image_egress(
        self,
        profile: ProxyRuntimeProfile,
        *,
        deadline_monotonic: float | None = None,
    ) -> int:
        """占用出口并发名额；deadline_monotonic（time.monotonic 口径，<=0/None 表示不限）
        到期仍没名额则抛 ImageEgressDeadlineError。"""
        remaining = _deadline_remaining(deadline_monotonic)
        if remaining is not None and remaining <= 0:
            raise ImageEgressDeadlineError("image request deadline exceeded before egress acquisition")
        if bool(getattr(profile, "image_egress_reserved", False)):
            return max(0, int(getattr(profile, "image_egress_wait_ms", 0) or 0))
        limit = max(0, int(getattr(profile, "image_concurrency_limit", 0) or 0))
        if limit <= 0:
            return 0
        key = _clean(getattr(profile, "egress_key", "")) or _egress_key_for_proxy(profile.proxy_url)
        started = time.perf_counter()
        with self._egress_condition:
            while int(self._egress_inflight.get(key, 0)) >= limit:
                remaining = _deadline_remaining(deadline_monotonic)
                if remaining is not None and remaining <= 0:
                    raise ImageEgressDeadlineError(
                        "image request deadline exceeded while waiting for egress capacity"
                    )
                self._egress_condition.wait(
                    timeout=min(1.0, remaining) if remaining is not None else 1.0
                )
            self._egress_inflight[key] = int(self._egress_inflight.get(key, 0)) + 1
        return int((time.perf_counter() - started) * 1000)

    def release_image_egress(self, profile: ProxyRuntimeProfile) -> None:
        limit = max(0, int(getattr(profile, "image_concurrency_limit", 0) or 0))
        if limit <= 0:
            return
        key = _clean(getattr(profile, "egress_key", "")) or _egress_key_for_proxy(profile.proxy_url)
        with self._egress_condition:
            current = int(self._egress_inflight.get(key, 0))
            if current <= 1:
                self._egress_inflight.pop(key, None)
            else:
                self._egress_inflight[key] = current - 1
            self._egress_condition.notify_all()

    def _get_runtime_settings(self) -> dict[str, object]:
        try:
            runtime = self._config.get_proxy_runtime_settings()
        except AttributeError:
            runtime = {}
        return runtime if isinstance(runtime, dict) else {}

    def _config_dict_list(self, key: str) -> list[dict]:
        data = getattr(self._config, "data", None)
        if not isinstance(data, dict):
            try:
                data = self._config.get()
            except AttributeError:
                data = {}
        raw = data.get(key) if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    def _account_group_proxy_reference(self, account: dict | None) -> str:
        if not isinstance(account, dict):
            return ""
        group_id = _clean(account.get("group_id"))
        if not group_id:
            return ""
        for group in self._config_dict_list("account_groups"):
            if _clean(group.get("id")) != group_id or group.get("enabled") is False:
                continue
            proxy = _clean(group.get("proxy"))
            if proxy:
                return proxy
            proxy_group_id = _clean(group.get("proxy_group_id"))
            return f"group:{proxy_group_id}" if proxy_group_id else ""
        return ""

    def _resolve_proxy_reference(
        self,
        value: object,
        *,
        source: str,
        terminal_when_unresolved: bool,
        reserve_image_egress: bool = False,
        deadline_monotonic: float | None = None,
    ) -> ResolvedProxyReference:
        raw = _clean(value)
        lower = raw.lower()
        if not raw or lower == "global":
            return ResolvedProxyReference(source=source)
        if lower == "direct":
            return ResolvedProxyReference(source=f"{source}_direct", terminal=True)
        if lower.startswith("profile:"):
            profile_item = self._proxy_profile_item(raw.split(":", 1)[1])
            proxy = _clean(profile_item.get("proxy")) if profile_item else ""
            return ResolvedProxyReference(
                proxy_url=proxy,
                source=f"{source}_profile",
                terminal=bool(proxy) or terminal_when_unresolved,
                egress_key=_egress_key_for_proxy(proxy),
                egress_label=f"{source}_profile",
                locale=locale_from_fields(profile_item) if proxy else None,
            )
        if lower.startswith("group:"):
            group_ref = _clean(raw.split(":", 1)[1])
            selection = self._resolve_proxy_group(
                group_ref,
                reserve_image_egress=reserve_image_egress,
                deadline_monotonic=deadline_monotonic,
            )
            if not selection.proxy_url:
                raise ProxyReferenceUnavailableError(
                    f"proxy group is unavailable: {group_ref or '(missing id)'}"
                )
            return ResolvedProxyReference(
                proxy_url=selection.proxy_url,
                source=f"{source}_group",
                terminal=bool(selection.proxy_url) or terminal_when_unresolved,
                egress_key=selection.egress_key,
                egress_label=selection.egress_label,
                proxy_group_id=selection.group_id,
                proxy_node_id=selection.node_id,
                proxy_node_name=selection.node_name,
                image_concurrency_limit=selection.image_concurrency_limit,
                image_egress_reserved=selection.image_egress_reserved,
                image_egress_wait_ms=selection.image_egress_wait_ms,
                locale=selection.locale,
            )
        return ResolvedProxyReference(
            proxy_url=raw,
            source=source,
            terminal=True,
            egress_key=_egress_key_for_proxy(raw),
            egress_label=source,
        )

    def _resolve_proxy_profile(self, profile_id: object) -> str:
        profile = self._proxy_profile_item(profile_id)
        return _clean(profile.get("proxy")) if profile else ""

    def _proxy_profile_item(self, profile_id: object) -> dict | None:
        normalized = _clean(profile_id)
        if not normalized:
            return None
        for profile in self._config_dict_list("proxy_profiles"):
            if _clean(profile.get("id")) == normalized and profile.get("enabled", True):
                return profile
        return None

    def _resolve_proxy_group(
        self,
        group_id: object,
        *,
        reserve_image_egress: bool = False,
        deadline_monotonic: float | None = None,
    ) -> ProxyGroupSelection:
        normalized = _clean(group_id)
        if not normalized:
            return ProxyGroupSelection()
        for group in self._config_dict_list("proxy_groups"):
            if _clean(group.get("id")) != normalized or group.get("enabled") is False:
                continue
            nodes = [
                node for node in group.get("nodes", [])
                if isinstance(node, dict)
                and node.get("enabled", True)
                and _clean(node.get("url"))
            ]
            if not nodes:
                return ProxyGroupSelection()
            # 临时停用的节点不参与选择；整组都停用时照常选，宁可试一次也不让请求无出口可用。
            healthy = [node for node in nodes if not self.is_proxy_disabled(_clean(node.get("url")))]
            nodes = healthy or nodes
            started = time.perf_counter()
            indexed_nodes = list(enumerate(nodes))
            with self._egress_condition:
                while True:
                    remaining = _deadline_remaining(deadline_monotonic)
                    if remaining is not None and remaining <= 0:
                        raise ImageEgressDeadlineError(
                            "image request deadline exceeded while selecting proxy group capacity"
                        )
                    available_nodes = [
                        (node_index, node)
                        for node_index, node in indexed_nodes
                        if self._proxy_node_has_image_capacity(normalized, node, node_index)
                    ]
                    if available_nodes:
                        selected_index, selected = random.choice(available_nodes)
                        selection = _proxy_group_selection(normalized, selected, selected_index)
                        if reserve_image_egress and selection.image_concurrency_limit > 0:
                            self._egress_inflight[selection.egress_key] = int(
                                self._egress_inflight.get(selection.egress_key, 0)
                            ) + 1
                            selection = replace(
                                selection,
                                image_egress_reserved=True,
                                image_egress_wait_ms=int((time.perf_counter() - started) * 1000),
                            )
                        return selection
                    if not reserve_image_egress:
                        selected_index, selected = min(
                            indexed_nodes,
                            key=lambda item: self._proxy_node_load_score(normalized, item[1], item[0]),
                        )
                        return _proxy_group_selection(normalized, selected, selected_index)
                    self._egress_condition.wait(
                        timeout=min(1.0, remaining) if remaining is not None else 1.0
                    )
        return ProxyGroupSelection()

    def _proxy_node_has_image_capacity(self, group_id: str, node: Mapping[str, object], index: int) -> bool:
        limit = _proxy_node_image_concurrency_limit(node)
        if limit <= 0:
            return True
        key = _proxy_group_node_key(group_id, node, index)
        return int(self._egress_inflight.get(key, 0)) < limit

    def _proxy_node_load_score(self, group_id: str, node: Mapping[str, object], index: int) -> tuple[float, int]:
        key = _proxy_group_node_key(group_id, node, index)
        current = int(self._egress_inflight.get(key, 0))
        limit = _proxy_node_image_concurrency_limit(node)
        if limit <= 0:
            return 0.0, current
        return current / max(1, limit), current

    def _bundle_for_headers(self, profile: ProxyRuntimeProfile, target_host: str) -> ClearanceBundle | None:
        key = self._cache_key(profile.proxy_url, target_host)
        if profile.clearance_mode == "manual":
            bundle = self._build_manual_bundle(profile, target_host)
            if bundle is not None:
                self._set_cached_bundle(key, bundle)
            return bundle
        if profile.clearance_mode == "flaresolverr":
            return self._get_cached_bundle(key)
        return None

    def _build_manual_bundle(self, profile: ProxyRuntimeProfile, target_host: str) -> ClearanceBundle | None:
        cookies = _parse_cookie_header(str(profile.clearance.get("cf_cookies") or ""))
        cf_clearance = str(profile.clearance.get("cf_clearance") or "").strip()
        if cf_clearance and "cf_clearance" not in cookies:
            cookies["cf_clearance"] = cf_clearance
        user_agent = str(profile.clearance.get("user_agent") or "").strip()
        if not cookies and not user_agent:
            return None

        now = time.time()
        expires_at = now + profile.refresh_interval if profile.refresh_interval else None
        return ClearanceBundle(
            target_host=target_host,
            proxy_url=profile.proxy_url,
            cookies=cookies,
            user_agent=user_agent,
            created_at=now,
            expires_at=expires_at,
        )

    def _get_provider(
        self,
        flaresolverr_url: str,
        profile: ProxyRuntimeProfile | None = None,
    ) -> FlareSolverrClearanceProvider:
        url = str(flaresolverr_url or "").strip().rstrip("/")
        with self._lock:
            provider = self._provider_cache.get(url)
            if provider is None:
                provider = self._clearance_provider_factory(url)
                self._provider_cache[url] = provider
        configure = getattr(provider, "configure", None)
        if profile is not None and callable(configure):
            configure(max_sessions=profile.max_sessions, idle_sec=profile.session_idle_sec)
        return provider

    def _get_flight_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            lock = self._flight_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._flight_locks[key] = lock
            return lock

    def _get_cached_bundle(self, key: tuple[str, str]) -> ClearanceBundle | None:
        with self._lock:
            return self._clearance_cache.get(key)

    def _set_cached_bundle(self, key: tuple[str, str], bundle: ClearanceBundle) -> None:
        with self._lock:
            self._clearance_cache[key] = bundle

    @staticmethod
    def _cache_key(proxy_url: str, target_host: str) -> tuple[str, str]:
        return (normalize_proxy_url(proxy_url), _normalize_host(target_host))


def _clean(value: object) -> str:
    return str(value or "").strip()


def _colon_proxy_to_url(url: str) -> str:
    """Backward-compatible wrapper for existing internal callers."""
    return colon_proxy_to_url(url)


def _normalize_host(host: str) -> str:
    return str(host or "").strip().strip(".").lower()


def _host_from_url(url: str) -> str:
    candidate = str(url or "").strip()
    parsed = urlparse(candidate)
    if not parsed.hostname and candidate and "://" not in candidate:
        parsed = urlparse(f"https://{candidate}")
    return _normalize_host(parsed.hostname or "")


def _status_codes_tuple(value: object) -> tuple[int, ...]:
    source = value if isinstance(value, list) else [403]
    codes: list[int] = []
    for item in source:
        if isinstance(item, bool):
            continue
        try:
            code = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= code <= 599 and code not in codes:
            codes.append(code)
    return tuple(codes or [403])


def _egress_key_for_proxy(proxy_url: object) -> str:
    normalized = normalize_proxy_url(_clean(proxy_url))
    return f"proxy:{normalized}" if normalized else "direct"


def _proxy_node_id(node: Mapping[str, object], index: int) -> str:
    return _clean(node.get("id")) or _clean(node.get("name")) or f"node-{index + 1}"


def _proxy_group_node_key(group_id: str, node: Mapping[str, object], index: int) -> str:
    return f"group:{group_id}:{_proxy_node_id(node, index)}"


PROXY_NODE_IMAGE_CONCURRENCY_FIELDS = ("image_concurrency_limit", "image_concurrency", "max_image_concurrency")


def _deadline_remaining(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None or deadline_monotonic <= 0:
        return None
    return deadline_monotonic - time.monotonic()


def proxy_node_image_concurrency_limit(
    node: Mapping[str, object],
    *,
    fallback: Mapping[str, object] | None = None,
) -> int:
    """节点出图并发上限：0 表示不限；上界夹取到 MAX_...；非法值回退默认值。"""
    for source in (node, fallback):
        if source is None:
            continue
        for key in PROXY_NODE_IMAGE_CONCURRENCY_FIELDS:
            value = source.get(key)
            if value is None or value == "":
                continue
            try:
                return max(0, min(int(float(value)), MAX_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT))
            except (OverflowError, TypeError, ValueError):
                return DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT
    return DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT


_proxy_node_image_concurrency_limit = proxy_node_image_concurrency_limit


def _proxy_group_selection(group_id: str, node: Mapping[str, object], index: int) -> ProxyGroupSelection:
    node_id = _proxy_node_id(node, index)
    return ProxyGroupSelection(
        proxy_url=_clean(node.get("url")),
        group_id=group_id,
        node_id=node_id,
        node_name=_clean(node.get("name")) or node_id,
        image_concurrency_limit=_proxy_node_image_concurrency_limit(node),
        locale=locale_from_fields(dict(node)),
    )


def _coerce_timeout(value: object) -> float:
    try:
        timeout = float(value)
    except (OverflowError, TypeError, ValueError):
        timeout = 60.0
    return max(1.0, timeout)


def _egress_slot_locale(proxy_url: str) -> LocaleEnv | None:
    try:
        from services import egress_slots

        return egress_slots.slot_locale_for_proxy(proxy_url)
    except Exception:  # noqa: BLE001 - 槽位不可用时回退到全局地区环境
        return None


def _is_valid_proxy_url(url: str) -> bool:
    parsed = urlparse(normalize_proxy_url(url))
    return parsed.scheme in {"http", "https", "socks5", "socks5h"} and bool(parsed.netloc)


def _domain_matches(host: str, domain: str) -> bool:
    normalized_host = _normalize_host(host)
    normalized_domain = _normalize_host(domain.lstrip("."))
    if not normalized_domain:
        return True
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def _is_clearance_cookie(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return False
    if lowered in {"cf_clearance", "_cfuvid", "__cf_bm", "__cfruid", "__cfwaitingroom"}:
        return True
    return lowered.startswith(("cf_", "__cf", "cf_chl"))


def _filter_flaresolverr_cookies(raw_cookies: object, target_host: str) -> dict[str, str]:
    if not isinstance(raw_cookies, list):
        return {}

    filtered_cookies: dict[str, str] = {}
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip()
        if not domain or _domain_matches(target_host, domain):
            filtered_cookies[name] = value
    return filtered_cookies


def _parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name.strip()] = value.strip()
    return cookies


def _cookies_to_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name)


def _merge_cookie_header(existing_header: str, cookies: Mapping[str, str]) -> str:
    existing = str(existing_header or "").strip()
    existing_names = set(_parse_cookie_header(existing).keys())
    additions = [f"{name}={value}" for name, value in cookies.items() if name and name not in existing_names]
    if existing and additions:
        return existing.rstrip("; ") + "; " + "; ".join(additions)
    if existing:
        return existing
    return "; ".join(additions)


def _find_header_key(headers: Mapping[str, object], name: str) -> str | None:
    target = name.lower()
    for key in headers:
        if str(key).lower() == target:
            return str(key)
    return None


def _redact_url_credentials(text: str) -> str:
    return re.sub(
        r"((?:https?|socks5h?|socks)://)([^\s/@:]+):([^\s/@]+)@",
        r"\1[REDACTED]@",
        str(text or ""),
        flags=re.IGNORECASE,
    )


_PROXY_NODE_IMPORT_MAX_LINES = 10_000
_PROXY_NODE_IMPORT_ERRORS = {
    "proxy node url is required": "地址为空",
    "proxy node url cannot contain whitespace": "地址不能包含空白字符",
    "proxy node url must start with http://, https://, socks5://, or socks5h://": (
        "请填写以 http://、https://、socks5:// 或 socks5h:// 开头的地址，或 host:port:user:pass 简写"
    ),
    "proxy node url has an invalid host or port": "代理主机或端口格式无效",
    "proxy node url uses an unsupported scheme": "仅支持 HTTP、HTTPS 和 SOCKS5 代理",
    "proxy node url requires a host": "缺少代理主机",
    "proxy node url has an invalid port": "代理端口格式无效",
    "proxy node url cannot include a path": "代理地址不能包含路径",
    "proxy node url cannot include parameters, a query, or a fragment": "代理地址不能包含参数、查询或片段",
    "proxy node url has an invalid internationalized host": "国际化域名格式无效",
}


def _normalize_import_node_url(value: object) -> tuple[str, str]:
    """Return ``(canonical_url, error)`` for one imported proxy node address.

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only). Compact ``host:port:user:pass``
    style inputs are accepted through the shared colon-format helper.
    """
    raw = _clean(value)
    if not raw:
        return "", "proxy node url is required"
    if any(char.isspace() for char in raw):
        return "", "proxy node url cannot contain whitespace"
    if "://" not in raw:
        converted = colon_proxy_to_url(raw)
        if "://" not in converted:
            return "", "proxy node url must start with http://, https://, socks5://, or socks5h://"
        raw = converted
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "", "proxy node url has an invalid host or port"
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h", "socks"}:
        return "", "proxy node url uses an unsupported scheme"
    if not host:
        return "", "proxy node url requires a host"
    if parsed.netloc.endswith(":") or port == 0:
        return "", "proxy node url has an invalid port"
    if parsed.path not in {"", "/"}:
        return "", "proxy node url cannot include a path"
    if parsed.params or parsed.query or parsed.fragment:
        return "", "proxy node url cannot include parameters, a query, or a fragment"

    normalized = urlparse(normalize_proxy_url(raw))
    scheme = normalized.scheme.lower()
    hostname = (normalized.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    else:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return "", "proxy node url has an invalid internationalized host"
    userinfo = f"{normalized.netloc.rsplit('@', 1)[0]}@" if "@" in normalized.netloc else ""
    normalized_port = normalized.port
    if (scheme == "http" and normalized_port == 80) or (scheme == "https" and normalized_port == 443):
        normalized_port = None
    port_text = f":{normalized_port}" if normalized_port is not None else ""
    return f"{scheme}://{userinfo}{hostname}{port_text}", ""


def import_proxy_nodes(text: object, existing_urls: object = ()) -> dict[str, object]:
    """Parse pasted proxy nodes (one per line, optional trailing image concurrency).

    Duplicates (against ``existing_urls`` and inside the pasted text) are counted
    and dropped; invalid lines are reported with their 1-based line number.
    """
    existing: set[str] = set()
    for value in existing_urls if isinstance(existing_urls, (list, tuple, set)) else ():
        normalized, error = _normalize_import_node_url(value)
        if normalized and not error:
            existing.add(normalized)
    lines = [
        (index, value.strip())
        for index, value in enumerate(str(text or "").splitlines(), start=1)
        if value.strip()
    ]
    if len(lines) > _PROXY_NODE_IMPORT_MAX_LINES:
        raise ValueError(f"一次最多导入 {_PROXY_NODE_IMPORT_MAX_LINES} 个代理节点")
    seen: set[str] = set()
    nodes: list[dict[str, object]] = []
    invalid: list[dict[str, object]] = []
    duplicates = 0
    concurrency_error = "图片并发必须是 0 到 10000 的整数"
    for index, raw in lines:
        parts = raw.split()
        if len(parts) > 2:
            invalid.append({"line": index, "raw": raw, "reason": "格式应为：代理地址 [图片并发]"})
            continue
        limit = DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY_LIMIT
        if len(parts) == 2:
            if not parts[1].isdecimal() or int(parts[1]) > 10_000:
                invalid.append({"line": index, "raw": raw, "reason": concurrency_error})
                continue
            limit = int(parts[1])
        normalized, error = _normalize_import_node_url(parts[0])
        if error:
            invalid.append({"line": index, "raw": raw, "reason": _PROXY_NODE_IMPORT_ERRORS.get(error, error)})
            continue
        if normalized in existing or normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        nodes.append({"url": normalized, "image_concurrency_limit": limit})
    return {
        "nodes": nodes,
        "added_count": len(nodes),
        "duplicate_count": duplicates,
        "invalid_count": len(invalid),
        "invalid_items": invalid,
    }


def test_proxy(url: str = "", *, timeout: float = 15.0) -> dict:
    candidate = normalize_proxy_url(_clean(url))
    proxy_source = "input"
    if not candidate:
        try:
            profile = proxy_settings.get_profile(upstream=True)
        except ProxyReferenceUnavailableError as error:
            return {
                "ok": False,
                "status": 0,
                "latency_ms": 0,
                "error": str(error),
                "proxy_source": proxy_source,
                "has_proxy": False,
            }
        candidate = profile.proxy_url
        proxy_source = profile.proxy_source
    result_base = {"proxy_source": proxy_source, "has_proxy": bool(candidate)}
    if not candidate:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": 0,
            "error": "no active proxy configured",
            **result_base,
        }
    if not _is_valid_proxy_url(candidate):
        return {
            "ok": False,
            "status": 0,
            "latency_ms": 0,
            "error": "invalid proxy url",
            **result_base,
        }
    session = Session(impersonate="edge101", verify=not proxy_settings.should_skip_ssl_verify(), proxy=candidate)
    started = time.perf_counter()
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"user-agent": "Mozilla/5.0 (chatgpt2api proxy test)"},
            timeout=timeout,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": response.status_code < 500,
            "status": int(response.status_code),
            "latency_ms": latency_ms,
            "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
            **result_base,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "status": 0,
            "latency_ms": latency_ms,
            "error": _redact_url_credentials(str(exc) or exc.__class__.__name__),
            **result_base,
        }
    finally:
        session.close()


def _proxy_sample_candidates(raw_proxy: str) -> list[tuple[str, str]]:
    """Return authenticated transport candidates for one imported proxy."""
    raw = _clean(raw_proxy)
    candidate = normalize_proxy_url(raw)
    if not candidate:
        return []
    if "://" in raw:
        try:
            scheme = urlparse(candidate).scheme.lower()
        except ValueError:
            return []
        return [(scheme, candidate)] if scheme else []
    return [
        (scheme, _replace_proxy_scheme(candidate, scheme))
        for scheme in ("http", "socks5h", "https")
    ]


def _proxy_test_error_label(value: object) -> str:
    error = _clean(value)
    lowered = error.lower()
    if "407" in lowered or "authentication" in lowered or "user was rejected" in lowered:
        return "代理认证失败"
    if "wrong_version_number" in lowered or "wrong version number" in lowered:
        return "TLS 协议不匹配"
    if "timeout" in lowered or "timed out" in lowered:
        return "代理连接超时"
    if "could not connect" in lowered or "connection refused" in lowered or "proxy connect aborted" in lowered:
        return "代理连接失败"
    return error or "测试失败"


def _proxy_attempt_summary(attempts: list[dict]) -> str:
    parts: list[str] = []
    for attempt in attempts:
        scheme = _clean(attempt.get("scheme")).upper() or "UNKNOWN"
        error = _proxy_test_error_label(attempt.get("error")) if attempt.get("error") else (
            f"HTTP {attempt.get('status')}" if attempt.get("status") else "测试失败"
        )
        parts.append(f"{scheme}: {error}")
    return "；".join(parts) or "代理测试失败"


def test_proxy_list_sample(value: str, *, timeout: float = 10.0) -> dict:
    """Test one random list entry and normalize compact imports on success.

    Compact proxy formats do not carry a protocol.  Test HTTP, SOCKS5H and
    HTTPS with real authenticated requests instead of inferring the protocol
    from a socket greeting alone.  Explicit URL schemes remain authoritative.
    """
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if not lines:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": 0,
            "error": "proxy list is empty",
            "scheme": "",
            "sample_index": 0,
            "sample_count": 0,
            "attempts": [],
        }

    sample = random.choice(lines)
    sample_index = lines.index(sample) + 1
    candidates = _proxy_sample_candidates(sample)
    if not candidates:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": 0,
            "error": "invalid proxy url",
            "scheme": "",
            "sample_index": sample_index,
            "sample_count": len(lines),
            "attempts": [],
        }

    def run(candidate: tuple[str, str]) -> dict:
        scheme, proxy_url = candidate
        result = test_proxy(proxy_url, timeout=timeout)
        return {
            "scheme": scheme,
            "ok": bool(result.get("ok")),
            "status": int(result.get("status") or 0),
            "latency_ms": int(result.get("latency_ms") or 0),
            "error": _clean(result.get("error")) or None,
        }

    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        attempts = list(executor.map(run, candidates))

    successful = next((attempt for attempt in attempts if attempt["ok"]), None)
    if successful is None:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": max((attempt["latency_ms"] for attempt in attempts), default=0),
            "error": _proxy_attempt_summary(attempts),
            "scheme": "",
            "sample_index": sample_index,
            "sample_count": len(lines),
            "attempts": attempts,
        }

    detected_scheme = str(successful["scheme"])
    normalized_lines = [
        normalize_proxy_url(line)
        if "://" in line
        else _replace_proxy_scheme(normalize_proxy_url(line), detected_scheme)
        for line in lines
    ]
    normalized_urls = "\n".join(normalized_lines)
    return {
        **successful,
        "error": None,
        "sample_index": sample_index,
        "sample_count": len(lines),
        "attempts": attempts,
        "normalized_urls": normalized_urls,
        "normalized_changed": normalized_urls != "\n".join(lines),
    }


def test_clearance(target_url: str = "https://chatgpt.com") -> dict:
    target_url = str(target_url or "https://chatgpt.com").strip() or "https://chatgpt.com"
    started = time.perf_counter()
    status = proxy_settings.get_runtime_status()
    if not status.get("clearance_enabled"):
        return {
            "ok": False,
            "status": "disabled",
            "latency_ms": 0,
            "has_cookies": False,
            "user_agent": "",
            "error": "clearance is disabled",
            "runtime": status,
        }
    try:
        bundle = proxy_settings.refresh_clearance(target_url=target_url, force=True, upstream=True)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "status": "error",
            "latency_ms": latency_ms,
            "has_cookies": False,
            "user_agent": "",
            "error": _redact_url_credentials(str(exc) or exc.__class__.__name__),
            "runtime": proxy_settings.get_runtime_status(),
        }

    latency_ms = int((time.perf_counter() - started) * 1000)
    runtime = proxy_settings.get_runtime_status()
    if bundle is None:
        return {
            "ok": False,
            "status": "failed",
            "latency_ms": latency_ms,
            "has_cookies": False,
            "user_agent": "",
            "error": "clearance refresh returned no bundle",
            "runtime": runtime,
        }
    return {
        "ok": True,
        "status": "ok",
        "latency_ms": latency_ms,
        "has_cookies": bool(bundle.cookies),
        "user_agent": bundle.user_agent or "",
        "error": None,
        "runtime": runtime,
    }


proxy_settings = ProxySettingsStore()

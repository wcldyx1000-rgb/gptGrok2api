"""出口槽位：在控制台管理 mihomo 出口网关（订阅、住宅代理、槽位分配），默认关闭。

- 来源：订阅链接（Clash/Base64 格式，由 mihomo 自己解析）和手动粘贴的代理（住宅 IP）。
- 节点检测：mihomo 上有 4 个检测端口（probe-1..4），切到某个节点后按 OpenAI 实际看到的出口测一遍。
- 槽位：slot-N 对应 mihomo 的 7000+N 端口，每个槽位固定一个节点；只有 OpenAI 相关域名走槽位节点。
- 下发：整份配置通过 mihomo 控制接口热更新；配置里带一个 cfg-<hash> 标记组，
  后台定期核对，mihomo 重启后自动重新下发。
- 槽位组默认选 REJECT，再按名字切到分配的节点；节点不存在（订阅更新后改名或下线）时保持 REJECT，
  OpenAI 请求宁可失败也不会从别的出口出去。
- 账号绑定：账号的 proxy 设为某个槽位地址即绑定该槽位（对话链路沿用 proxy_service 的按账号选代理）。
  换出口时先把账号停用一段时间（默认 24 小时），到点再切到新出口并恢复，避免同一账号在短时间内换 IP。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from services import egress_check
from services.config import DATA_DIR, config, mask_proxy_credentials, restore_masked_proxy_credentials
from services.json_file import read_json_object, write_json_file
from services.proxy_service import normalize_proxy_url
from utils.locale_env import LocaleEnv, normalize_language, normalize_timezone, suggest_locale

logger = logging.getLogger("egress")
STORE_FILE = DATA_DIR / "egress.json"
SLOT_BASE_PORT = 7000
MAX_SLOTS = 32
DEFAULT_SLOTS = 4
PROBE_PORTS = (7091, 7092, 7093, 7094)
REGISTER_GROUP_ID = "egress-slots"
RECONCILE_INTERVAL_SECS = 20.0
DEFAULT_SWITCH_HOLD_HOURS = 24
DEFAULT_SLOT_CAPACITY = 1000
MAX_SLOT_CAPACITY = 100_000
SWITCH_DISABLED_STATUS = "禁用"
OPENAI_DOMAINS = (
    "+.openai.com",
    "+.chatgpt.com",
    "+.chat.com",
    "+.oaistatic.com",
    "+.oaiusercontent.com",
    "+.sora.com",
    "+.auth0.com",
    "challenges.cloudflare.com",
)
_BUILTIN_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay", "Direct", "Reject", "RejectDrop", "Pass", "Compatible", "Dns"}
# 控制接口一律直连，不走进程的 HTTP(S)_PROXY。
_DIRECT_OPENER = build_opener(ProxyHandler({}))

_lock = threading.RLock()
_probe_locks = [threading.Lock() for _ in PROBE_PORTS]
_status: dict[str, Any] = {"ok": False, "error": "尚未连接", "version": "", "applied_at": "", "checked_at": ""}
_job: dict[str, Any] = {"running": False, "total": 0, "done": 0, "started_at": "", "finished_at": ""}
_slot_state: dict[int, dict[str, Any]] = {}
_store_path: Path = STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 存储 ──────────────────────────────────────────────────────
def _load() -> dict[str, Any]:
    data = read_json_object(_store_path, name=_store_path.name) if _store_path.exists() else {}
    data.setdefault("sources", [])
    data.setdefault("slots", {})
    data.setdefault("checks", {})
    data.setdefault("settings", {})
    return data


def _save(data: dict[str, Any]) -> None:
    write_json_file(_store_path, data)


def settings(data: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = (data or _load()).get("settings") or {}
    try:
        hold_hours = max(0, min(168, int(stored.get("switch_hold_hours", DEFAULT_SWITCH_HOLD_HOURS))))
    except (TypeError, ValueError):
        hold_hours = DEFAULT_SWITCH_HOLD_HOURS
    try:
        capacity = max(1, min(MAX_SLOT_CAPACITY, int(stored.get("slot_capacity", DEFAULT_SLOT_CAPACITY))))
    except (TypeError, ValueError):
        capacity = DEFAULT_SLOT_CAPACITY
    return {
        "enabled": bool(stored.get("enabled", False)),
        "controller_url": str(stored.get("controller_url") or os.environ.get("MIHOMO_CONTROLLER_URL") or "http://mihomo:9090").rstrip("/"),
        "secret": str(stored.get("secret") or os.environ.get("MIHOMO_SECRET") or ""),
        # 注册任务和对话链路用槽位时的地址；本机开发时 mihomo 端口映射成 1700x，就填 http://127.0.0.1:1{port}
        "proxy_template": str(stored.get("proxy_template") or os.environ.get("EGRESS_PROXY_TEMPLATE") or "http://mihomo:{port}"),
        "switch_hold_hours": hold_hours,
        # 单个槽位最多绑定多少个账号（Ported from yukkcat/chatgpt2api 的 egress_slot_capacity）。
        "slot_capacity": capacity,
    }


def is_enabled() -> bool:
    return bool(settings()["enabled"])


def slot_ports(data: dict[str, Any] | None = None) -> dict[int, int]:
    """槽位编号 → mihomo 端口（7001 起）；数量 1～32 个。"""
    stored = (data or _load()).get("settings") or {}
    try:
        count = int(stored.get("slot_count") or DEFAULT_SLOTS)
    except (TypeError, ValueError):
        count = DEFAULT_SLOTS
    count = max(1, min(MAX_SLOTS, count))
    return {slot: SLOT_BASE_PORT + slot for slot in range(1, count + 1)}


def slot_proxy_url(slot: int, data: dict[str, Any] | None = None) -> str:
    return normalize_proxy_url(settings(data)["proxy_template"].format(port=SLOT_BASE_PORT + slot))


def _probe_url(index: int, data: dict[str, Any] | None = None) -> str:
    return settings(data)["proxy_template"].format(port=PROBE_PORTS[index])


def slot_of_proxy(proxy: object, data: dict[str, Any] | None = None) -> int | None:
    """账号 proxy 等于某个槽位地址时返回槽位号。"""
    value = normalize_proxy_url(str(proxy or "").strip())
    if not value:
        return None
    for slot in slot_ports(data):
        if slot_proxy_url(slot, data) == value:
            return slot
    return None


# ── mihomo 控制接口 ───────────────────────────────────────────
def slot_locales(data: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    raw = (data or _load()).get("slot_locales")
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def set_slot_locale(slot: int, timezone_name: object, language: object) -> dict[str, Any]:
    """保存槽位的时区和语言；两项都为空表示清除（回到全局地区环境）。"""
    with _lock:
        data = _load()
        if int(slot) not in slot_ports(data):
            raise ValueError(f"槽位只有 1 到 {len(slot_ports(data))}")
        locales = slot_locales(data)
        if not str(timezone_name or "").strip() and not str(language or "").strip():
            locales.pop(str(int(slot)), None)
        else:
            tz = normalize_timezone(timezone_name)
            lang = normalize_language(language)
            if not tz or not lang:
                raise ValueError("时区和语言都要填")
            locales[str(int(slot))] = {"timezone": tz, "language": lang}
        data["slot_locales"] = locales
        _save(data)
    return snapshot()


_locale_cache: tuple[str, float, dict[str, dict[str, str]], dict[str, str]] | None = None


def slot_locale_for_proxy(proxy: object) -> LocaleEnv | None:
    """请求路径用：代理是某个槽位地址时返回该槽位的地区环境，否则 None；不发网络请求。

    按存储文件的修改时间缓存，避免每个请求都读盘。
    """
    global _locale_cache
    value = normalize_proxy_url(str(proxy or "").strip())
    if not value:
        return None
    try:
        mtime = _store_path.stat().st_mtime
    except OSError:
        return None
    key = str(_store_path)
    cached = _locale_cache
    if cached is None or cached[0] != key or cached[1] != mtime:
        try:
            data = _load()
            urls = {slot_proxy_url(slot, data): str(slot) for slot in slot_ports(data)}
            locales = slot_locales(data)
        except Exception:  # noqa: BLE001 - 读不到就当没配置
            return None
        cached = (key, mtime, locales, urls)
        _locale_cache = cached
    slot = cached[3].get(value)
    return LocaleEnv.from_config(cached[2].get(slot)) if slot else None


class ControllerError(RuntimeError):
    pass


def _call(method: str, path: str, body: Any = None, *, data: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
    conf = settings(data)
    request = Request(
        conf["controller_url"] + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {conf['secret']}"} if conf["secret"] else {})},
    )
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200] if error.fp else ""
        if error.code == 401:
            raise ControllerError("mihomo 控制接口密钥不对（MIHOMO_SECRET）") from error
        raise ControllerError(f"mihomo HTTP {error.code} {detail}".strip()) from error
    except (URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ControllerError(f"连不上 mihomo 控制接口 {conf['controller_url']}（{reason}）") from error


def _select(group: str, node: str, data: dict[str, Any] | None = None) -> bool:
    try:
        _call("PUT", f"/proxies/{quote(group, safe='')}", {"name": node}, data=data)
        return True
    except ControllerError:
        return False


# ── 来源解析 ──────────────────────────────────────────────────
def _manual_proxy(url: str, name: str) -> dict[str, Any] | None:
    candidate = normalize_proxy_url(url)
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port:
        return None
    proxy: dict[str, Any] = {
        "name": name,
        "type": "socks5" if scheme.startswith("socks5") else "http",
        "server": parsed.hostname,
        "port": int(parsed.port),
        "udp": False,
    }
    if scheme == "https":
        proxy["tls"] = True
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
        proxy["password"] = unquote(parsed.password or "")
    return proxy


def _manual_proxies(source: dict[str, Any]) -> list[dict[str, Any]]:
    proxies: list[dict[str, Any]] = []
    for line in source.get("lines") or []:
        parsed = urlparse(normalize_proxy_url(line))
        label = f"[{source['name']}] {parsed.hostname}:{parsed.port}"
        name, n = label, 2
        while any(item["name"] == name for item in proxies):
            name, n = f"{label} #{n}", n + 1
        proxy = _manual_proxy(line, name)
        if proxy:
            proxies.append(proxy)
    return proxies


def parse_manual_lines(text: str) -> tuple[list[str], list[str]]:
    """粘贴的住宅代理，每行一个；返回（能用的、认不出的）。"""
    good: list[str] = []
    bad: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = normalize_proxy_url(line)
        if _manual_proxy(normalized, "x") and normalized not in good:
            good.append(normalized)
        else:
            bad.append(line)
    return good, bad


def _regex_exact(name: str) -> str:
    # mihomo 用 Go 的正则：只转义元字符，emoji 和中文原样保留
    return "^" + re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", name) + "$"


def _source_of(node: str, data: dict[str, Any]) -> dict[str, Any] | None:
    for source in data["sources"]:
        if node.startswith(f"[{source['name']}] "):
            return source
    return None


# ── 生成并下发配置 ────────────────────────────────────────────
def build_config(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    manual = [proxy for source in data["sources"] if source.get("kind") == "manual" for proxy in _manual_proxies(source)]
    manual_names = {proxy["name"] for proxy in manual}
    providers = {
        source["id"]: {
            "type": "http",
            "url": source["url"],
            "interval": 3600,
            "path": f"./providers/{source['id']}.yaml",
            "health-check": {"enable": True, "url": "https://www.gstatic.com/generate_204", "interval": 900, "lazy": True},
            "override": {"additional-prefix": f"[{source['name']}] "},
        }
        for source in data["sources"]
        if source.get("kind") == "subscription"
    }
    groups: list[dict[str, Any]] = []
    ports = slot_ports(data)
    for slot in ports:
        node = str((data["slots"].get(str(slot)) or {}).get("node") or "")
        # REJECT 放第一位：没分配或节点找不到时，槽位保持拒绝，不会回落到其它出口。
        group: dict[str, Any] = {"name": f"slot-{slot}", "type": "select", "proxies": ["REJECT"]}
        if node in manual_names:
            group["proxies"].append(node)
        elif node:
            source = _source_of(node, data)
            if source and source.get("kind") == "subscription":
                group["use"] = [source["id"]]
                group["filter"] = _regex_exact(node)
        groups.append(group)
    for index in range(len(PROBE_PORTS)):
        groups.append({"name": f"probe-{index + 1}", "type": "select", "proxies": ["REJECT"], "include-all": True})
    payload: dict[str, Any] = {
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "allow-lan": True,
        "bind-address": "*",
        "dns": {"enable": False},
        "profile": {"store-selected": False},
        "proxies": manual,
        "proxy-providers": providers,
        "proxy-groups": groups,
        "rule-providers": {"openai": {"type": "inline", "behavior": "domain", "payload": list(OPENAI_DOMAINS)}},
        "sub-rules": {
            **{f"slot-{slot}": [f"RULE-SET,openai,slot-{slot}", "MATCH,DIRECT"] for slot in ports},
            **{f"probe-{index + 1}": [f"MATCH,probe-{index + 1}"] for index in range(len(PROBE_PORTS))},
        },
        "listeners": [
            *({"name": f"slot-{slot}", "type": "mixed", "port": port, "listen": "0.0.0.0", "udp": False, "rule": f"slot-{slot}"} for slot, port in ports.items()),
            *({"name": f"probe-{index + 1}", "type": "mixed", "port": port, "listen": "0.0.0.0", "udp": False, "rule": f"probe-{index + 1}"} for index, port in enumerate(PROBE_PORTS)),
        ],
        "rules": ["MATCH,DIRECT"],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    payload["proxy-groups"].append({"name": f"cfg-{digest}", "type": "select", "proxies": ["DIRECT"]})
    return payload, digest


def apply(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """把完整配置下发给 mihomo，再把每个槽位切到分配的节点。"""
    with _lock:
        data = data or _load()
        payload, digest = build_config(data)
        try:
            _call("PUT", "/configs?force=true", {"path": "", "payload": json.dumps(payload, ensure_ascii=False)}, data=data, timeout=60.0)
            _status.update({"ok": True, "error": "", "applied_at": _now(), "hash": digest})
        except ControllerError as error:
            _status.update({"ok": False, "error": str(error)})
            raise
        _sync_slots(data)
        return dict(_status)


def _sync_slots(data: dict[str, Any]) -> None:
    _slot_state.clear()
    for slot in slot_ports(data):
        node = str((data["slots"].get(str(slot)) or {}).get("node") or "")
        if not node:
            _slot_state[slot] = {"active": "REJECT", "ok": True, "error": ""}
            continue
        ok = _select(f"slot-{slot}", node, data)
        _slot_state[slot] = {
            "active": node if ok else "REJECT",
            "ok": ok,
            "error": "" if ok else "节点不在了（订阅更新后改名或下线），槽位保持拒绝，请重新选择",
        }


def slot_down(slot: int | None) -> bool:
    """槽位当前是否处于拒绝状态（没分配节点或节点不在了）。"""
    if slot is None:
        return False
    state = _slot_state.get(int(slot))
    return bool(state) and state.get("active") == "REJECT"


def reconcile() -> None:
    """mihomo 重启后会回到底线配置；发现标记组不在就重新下发，槽位没指对就重新切。"""
    with _lock:
        data = _load()
        if not settings(data)["enabled"]:
            return
        _, digest = build_config(data)
        try:
            version = _call("GET", "/version", data=data)
            _status.update({"ok": True, "error": "", "version": str(version.get("version") or ""), "checked_at": _now()})
            try:
                _call("GET", f"/proxies/cfg-{digest}", data=data)
            except ControllerError:
                apply(data)
                logger.info({"event": "egress_config_reapplied", "hash": digest})
                return
            for slot in slot_ports(data):
                node = str((data["slots"].get(str(slot)) or {}).get("node") or "")
                if not node:
                    continue
                now = str((_call("GET", f"/proxies/slot-{slot}", data=data) or {}).get("now") or "")
                if now != node:
                    _sync_slots(data)
                    break
        except ControllerError as error:
            _status.update({"ok": False, "error": str(error), "checked_at": _now()})


class _Reconciler:
    """后台核对 mihomo 配置并执行到期的账号换出口；功能关闭时只空转等待。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self, stop_event: threading.Event | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = stop_event or threading.Event()
        self._thread = threading.Thread(target=self._loop, name="egress-reconciler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if is_enabled():
                try:
                    reconcile()
                except Exception as exc:  # 不能让后台线程挂掉
                    logger.warning({"event": "egress_reconcile_error", "error": str(exc)[:200]})
                try:
                    apply_due_switches()
                except Exception as exc:
                    logger.warning({"event": "egress_switch_apply_error", "error": str(exc)[:200]})
            self._wake.wait(RECONCILE_INTERVAL_SECS)
            self._wake.clear()


reconciler = _Reconciler()


# ── 节点列表与检测 ────────────────────────────────────────────
def _node_rows(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    provider_info: dict[str, dict[str, Any]] = {}
    for source in data["sources"]:
        if source.get("kind") == "manual":
            for proxy in _manual_proxies(source):
                rows.append({"name": proxy["name"], "type": proxy["type"], "source_id": source["id"]})
    if any(source.get("kind") == "subscription" for source in data["sources"]):
        try:
            providers = (_call("GET", "/providers/proxies", data=data) or {}).get("providers") or {}
        except ControllerError:
            providers = {}
        for source in data["sources"]:
            if source.get("kind") != "subscription":
                continue
            provider = providers.get(source["id"]) or {}
            sub = provider.get("subscriptionInfo") or {}
            provider_info[source["id"]] = {
                "updated_at": str(provider.get("updatedAt") or ""),
                "count": len(provider.get("proxies") or []),
                "used": int(sub.get("Upload") or 0) + int(sub.get("Download") or 0),
                "total": int(sub.get("Total") or 0),
                "expire": int(sub.get("Expire") or 0),
            }
            for proxy in provider.get("proxies") or []:
                if proxy.get("type") in _BUILTIN_TYPES:
                    continue
                rows.append({"name": str(proxy.get("name") or ""), "type": str(proxy.get("type") or "").lower(), "source_id": source["id"]})
    return rows, provider_info


def _recommendation(check: dict[str, Any] | None) -> str:
    if not check:
        return "unchecked"
    verdict = check.get("verdict")
    if verdict in {"blocked", "error"}:
        return "unusable"
    if verdict == "ok" and not check.get("hosting") and not check.get("flagged_proxy"):
        return "register"  # 支持地区的非机房 IP：适合注册新号
    return "daily"  # 能用但是机房/代理 IP：只建议给老号日常用


def _node_error_text(raw: str) -> str:
    """节点检测失败的原因：给人看的一句话，不要 curl 的原始报错。"""
    text = raw.lower()
    if "找不到" in raw:
        return raw
    if "超时" in raw or "timed out" in text or "timeout" in text:
        return "节点连不上（超时），可能已下线"
    if "ssl" in text or "tls" in text or "closed" in text or "断开" in raw:
        return "节点握手失败，可能已失效或被封"
    if "refused" in text or "拒绝" in raw:
        return "节点拒绝连接"
    return "节点连不上 OpenAI"


def _run_checks(names: list[str]) -> None:
    data = _load()
    pending = list(names)
    pending_lock = threading.Lock()

    def worker(index: int) -> None:
        while True:
            with pending_lock:
                if not pending:
                    return
                name = pending.pop(0)
            with _probe_locks[index]:
                result: dict[str, Any]
                if _select(f"probe-{index + 1}", name, data):
                    result = egress_check.check_egress(_probe_url(index, data), use_cache=False)
                else:
                    result = {"verdict": "error", "message": "mihomo 里找不到这个节点"}
                _select(f"probe-{index + 1}", "REJECT", data)
            result.pop("proxy", None)
            if result.get("verdict") == "error":
                result["message"] = _node_error_text(str(result.get("message") or ""))
            result["checked_at"] = _now()
            with _lock:
                fresh = _load()
                fresh["checks"][name] = result
                _save(fresh)
                _job["done"] += 1

    try:
        with ThreadPoolExecutor(max_workers=len(PROBE_PORTS)) as pool:
            for index in range(len(PROBE_PORTS)):
                pool.submit(worker, index)
    finally:
        with _lock:
            _job.update({"running": False, "finished_at": _now()})


def start_checks(names: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        if _job["running"]:
            return dict(_job)
        data = _load()
        rows, _ = _node_rows(data)
        available = [row["name"] for row in rows]
        targets = [name for name in (names or available) if name in available]
        if not targets:
            raise ValueError("没有可检测的节点")
        _job.update({"running": True, "total": len(targets), "done": 0, "started_at": _now(), "finished_at": ""})
    threading.Thread(target=_run_checks, args=(targets,), name="egress-checks", daemon=True).start()
    return dict(_job)


# ── 对外操作 ──────────────────────────────────────────────────
def _mask_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}/…" if parsed.hostname else "…"


def snapshot() -> dict[str, Any]:
    with _lock:
        data = _load()
        conf = settings(data)
        rows, provider_info = _node_rows(data) if conf["enabled"] else ([], {})
        checks = data["checks"]
        nodes = []
        for row in rows:
            check = checks.get(row["name"])
            nodes.append({**row, "check": check, "recommendation": _recommendation(check)})
        order = {"register": 0, "daily": 1, "unchecked": 2, "unusable": 3}
        nodes.sort(key=lambda item: (order[item["recommendation"]], int((item.get("check") or {}).get("latency_ms") or 99999), item["name"]))
        node_map = {node["name"]: node for node in nodes}
        locales = slot_locales(data)
        slots = []
        for slot, port in slot_ports(data).items():
            node = str((data["slots"].get(str(slot)) or {}).get("node") or "")
            state = _slot_state.get(slot) or {}
            slots.append({
                "slot": slot,
                "port": port,
                "proxy_url": mask_proxy_credentials(slot_proxy_url(slot, data)),
                "node": node,
                "node_info": node_map.get(node),
                "missing": bool(node) and bool(rows) and node not in node_map,
                "active": state.get("active") or ("" if node else "REJECT"),
                "error": state.get("error") or "",
                "locale": locales.get(str(slot)),
                # 按节点检测到的出口国家推荐时区和语言；美国等多时区国家只给语言和候选时区
                "suggested_locale": suggest_locale(((node_map.get(node) or {}).get("check") or {}).get("country_code")),
            })
        sources = []
        for source in data["sources"]:
            count = sum(1 for row in rows if row["source_id"] == source["id"])
            sources.append({
                "id": source["id"],
                "kind": source["kind"],
                "name": source["name"],
                "url": _mask_url(source.get("url") or "") if source["kind"] == "subscription" else "",
                "line_count": len(source.get("lines") or []),
                "node_count": count,
                "info": provider_info.get(source["id"]),
                "created_at": source.get("created_at") or "",
            })
        return {
            "status": dict(_status),
            "settings": {
                "enabled": conf["enabled"],
                "controller_url": mask_proxy_credentials(str(conf["controller_url"] or "")),
                "proxy_template": mask_proxy_credentials(str(conf["proxy_template"] or "")),
                "secret_set": bool(conf["secret"]),
                "slot_count": len(slot_ports(data)),
                "max_slots": MAX_SLOTS,
                "switch_hold_hours": conf["switch_hold_hours"],
                "slot_capacity": conf["slot_capacity"],
            },
            "sources": sources,
            "nodes": nodes,
            "slots": slots,
            "job": dict(_job),
            "register_group_id": REGISTER_GROUP_ID,
        }


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        stored = data["settings"]
        for key in ("controller_url", "proxy_template", "secret"):
            if key in values and values[key] is not None:
                value = str(values[key]).strip()
                if key != "secret":
                    # 前端回显的是打码值（user:***@host），还原为已存凭据
                    value = str(restore_masked_proxy_credentials(value, stored.get(key) or ""))
                if key == "secret" and not value:
                    continue  # 留空表示不改密钥
                stored[key] = value
        if "enabled" in values and values["enabled"] is not None:
            stored["enabled"] = bool(values["enabled"])
        if "switch_hold_hours" in values and values["switch_hold_hours"] is not None:
            stored["switch_hold_hours"] = max(0, min(168, int(values["switch_hold_hours"])))
        if "slot_capacity" in values and values["slot_capacity"] is not None:
            stored["slot_capacity"] = max(1, min(MAX_SLOT_CAPACITY, int(values["slot_capacity"])))
        if stored.get("proxy_template") and "{port}" not in stored["proxy_template"]:
            raise ValueError("槽位地址模板里要有 {port}，例如 http://mihomo:{port}")
        _save(data)
    if settings()["enabled"]:
        reconciler.wake()
    return snapshot()


def _require_enabled() -> None:
    if not is_enabled():
        raise ValueError("出口槽位未启用，请先在设置里开启")


def set_slot_count(count: int) -> dict[str, Any]:
    """调整槽位数量；减少时，多出来的槽位上的分配一并清掉。"""
    _require_enabled()
    count = max(1, min(MAX_SLOTS, int(count)))
    with _lock:
        data = _load()
        data["settings"]["slot_count"] = count
        for key in list(data["slots"]):
            if int(key) > count:
                data["slots"].pop(key, None)
        data["slot_locales"] = {key: value for key, value in slot_locales(data).items() if int(key) <= count}
        _save(data)
        apply(data)
    return snapshot()


def add_source(kind: str, name: str, url: str = "", text: str = "") -> dict[str, Any]:
    _require_enabled()
    name = str(name or "").strip()[:24]
    if not name or any(ch in name for ch in "[]"):
        raise ValueError("请填一个名字（不能有方括号）")
    with _lock:
        data = _load()
        if any(source["name"] == name for source in data["sources"]):
            raise ValueError(f"已经有叫「{name}」的来源了")
        source: dict[str, Any] = {"id": f"src-{uuid.uuid4().hex[:8]}", "kind": kind, "name": name, "created_at": _now()}
        if kind == "subscription":
            link = str(url or "").strip()
            if not link.lower().startswith(("http://", "https://")):
                raise ValueError("订阅链接要以 http:// 或 https:// 开头")
            source["url"] = link
        elif kind == "manual":
            good, bad = parse_manual_lines(text)
            if not good:
                raise ValueError("没有认出可用的代理地址" + (f"：{bad[0][:40]}" if bad else ""))
            source["lines"] = good
            source["skipped"] = len(bad)
        else:
            raise ValueError("未知来源类型")
        data["sources"].append(source)
        _save(data)
        apply(data)
    if kind == "subscription":
        time.sleep(2)  # 给 mihomo 一点时间把订阅拉下来
    return snapshot()


def remove_source(source_id: str) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        data = _load()
        source = next((item for item in data["sources"] if item["id"] == source_id), None)
        if source is None:
            raise ValueError("来源不存在")
        prefix = f"[{source['name']}] "
        data["sources"] = [item for item in data["sources"] if item["id"] != source_id]
        for key, value in list(data["slots"].items()):
            if str((value or {}).get("node") or "").startswith(prefix):
                data["slots"].pop(key, None)
        data["checks"] = {name: value for name, value in data["checks"].items() if not name.startswith(prefix)}
        _save(data)
        apply(data)
    return snapshot()


def refresh_source(source_id: str) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        data = _load()
        if not any(item["id"] == source_id and item["kind"] == "subscription" for item in data["sources"]):
            raise ValueError("只有订阅可以刷新")
        _call("PUT", f"/providers/proxies/{quote(source_id, safe='')}", data=data, timeout=60.0)
        _sync_slots(data)
    return snapshot()


def set_slot(slot: int, node: str | None) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        data = _load()
        if slot not in slot_ports(data):
            raise ValueError(f"槽位只有 1 到 {len(slot_ports(data))}")
        if node:
            rows, _ = _node_rows(data)
            if node not in {row["name"] for row in rows}:
                raise ValueError("节点不存在，刷新一下列表")
            data["slots"][str(slot)] = {"node": node, "assigned_at": _now()}
        else:
            data["slots"].pop(str(slot), None)
        _save(data)
        apply(data)
    return snapshot()


def _upsert_managed_proxy_group(item: dict[str, Any]) -> None:
    groups = [dict(group) for group in (config.data.get("proxy_groups") or []) if isinstance(group, dict)]
    next_groups = [group for group in groups if group.get("id") != item["id"]]
    next_groups.append(item)
    config.update({"proxy_groups": next_groups})


def use_for_register() -> dict[str, Any]:
    """把已分配的槽位做成注册用的代理组，并让注册任务用它。"""
    _require_enabled()
    from services.register_service import register_service

    with _lock:
        data = _load()
        locales = slot_locales(data)
        nodes = [
            {
                "id": f"slot-{slot}",
                "name": f"槽位 {slot} · {(data['slots'].get(str(slot)) or {}).get('node')}",
                "url": slot_proxy_url(slot, data),
                "enabled": True,
                # 注册走代理组时，请求按槽位的时区/语言生成
                "timezone": (locales.get(str(slot)) or {}).get("timezone", ""),
                "language": (locales.get(str(slot)) or {}).get("language", ""),
            }
            for slot in slot_ports(data)
            if (data["slots"].get(str(slot)) or {}).get("node")
        ]
    if not nodes:
        raise ValueError("还没有分配任何槽位")
    _upsert_managed_proxy_group({
        "id": REGISTER_GROUP_ID,
        "name": "出口槽位",
        "strategy": "request_random",
        "enabled": True,
        "notes": "由「出口槽位」自动维护",
        "nodes": nodes,
    })
    register_service.update({"proxy": f"group:{REGISTER_GROUP_ID}"})
    return {**snapshot(), "slot_count": len(nodes)}


# ── 账号绑定槽位 ──────────────────────────────────────────────
def parse_target(value: object) -> str:
    """换出口目标：slot:N（槽位）、direct（直连）、default（清空，沿用全局代理设置）。"""
    raw = str(value or "").strip().lower()
    if raw in {"direct", "default"}:
        return raw
    match = re.fullmatch(r"slot[:\-](\d+)", raw)
    if match and int(match.group(1)) in slot_ports():
        return f"slot:{int(match.group(1))}"
    raise ValueError("出口目标只能是 slot:N、direct 或 default")


def target_proxy(target: str) -> str:
    if target == "direct":
        return "direct"
    if target == "default":
        return ""
    return slot_proxy_url(int(target.split(":", 1)[1]))


def account_current_target(account: dict[str, Any]) -> str:
    proxy = str(account.get("proxy") or "").strip()
    slot = slot_of_proxy(proxy)
    if slot is not None:
        return f"slot:{slot}"
    return "direct" if proxy.lower() == "direct" else "default"


def _parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _account_service():
    from services.account_service import account_service

    return account_service


def _apply_switch(service: Any, token: str, account: dict[str, Any]) -> None:
    target = str(account.get("egress_switch_target") or "")
    previous_status = str(account.get("egress_switch_prev_status") or "正常")
    service.update_account(
        token,
        {
            "proxy": target_proxy(target),
            "status": previous_status if previous_status != SWITCH_DISABLED_STATUS else "正常",
            **_cleared_switch_fields(),
            "egress_switched_at": _now(),
        },
        quiet=True,
    )


def _cleared_switch_fields() -> dict[str, Any]:
    return {
        "egress_switch_target": None,
        "egress_switch_at": None,
        "egress_switch_prev_status": None,
        "egress_switch_requested_at": None,
    }


def request_switch(access_tokens: list[str], target: object, *, service: Any = None) -> dict[str, Any]:
    """申请把账号换到新出口：先停用到期（默认 24 小时），到点后台切换并恢复；停用时长为 0 时立即切换。"""
    normalized = parse_target(target)
    service = service or _account_service()
    hold_hours = int(settings()["switch_hold_hours"])
    due_at = datetime.now(timezone.utc) + timedelta(hours=hold_hours)
    scheduled, applied, skipped = [], [], []
    capacity = int(settings()["slot_capacity"])
    # 目标槽位已占用数（当前绑定 + 已排队待切入），同一批申请里随分配递增。
    occupied = 0
    if normalized.startswith("slot:"):
        for existing in service.list_accounts():
            if account_current_target(existing) == normalized or existing.get("egress_switch_target") == normalized:
                occupied += 1
    for token in [str(item or "").strip() for item in access_tokens if str(item or "").strip()]:
        account = service.get_account(token)
        if not account:
            skipped.append({"token": token[:12], "reason": "账号不存在"})
            continue
        if account_current_target(account) == normalized and not account.get("egress_switch_target"):
            skipped.append({"token": token[:12], "reason": "已经是这个出口"})
            continue
        if normalized.startswith("slot:") and not account.get("egress_switch_target") == normalized and occupied >= capacity:
            skipped.append({"token": token[:12], "reason": f"{normalized} 已满（{capacity} 个）"})
            continue
        if normalized.startswith("slot:") and account.get("egress_switch_target") != normalized:
            occupied += 1
        previous_status = account.get("egress_switch_prev_status") or account.get("status") or "正常"
        pending = {
            "egress_switch_target": normalized,
            "egress_switch_at": due_at.isoformat(),
            "egress_switch_prev_status": previous_status,
            "egress_switch_requested_at": _now(),
        }
        if hold_hours <= 0:
            _apply_switch(service, token, {**account, **pending})
            applied.append(token[:12])
            continue
        service.update_account(token, {**pending, "status": SWITCH_DISABLED_STATUS}, quiet=True)
        scheduled.append(token[:12])
    return {
        "target": normalized,
        "due_at": due_at.isoformat() if hold_hours > 0 else "",
        "scheduled": scheduled,
        "applied": applied,
        "skipped": skipped,
    }


def cancel_switch(access_tokens: list[str], *, service: Any = None) -> dict[str, Any]:
    service = service or _account_service()
    cancelled = []
    for token in [str(item or "").strip() for item in access_tokens if str(item or "").strip()]:
        account = service.get_account(token)
        if not account or not account.get("egress_switch_target"):
            continue
        service.update_account(
            token,
            {"status": account.get("egress_switch_prev_status") or "正常", **_cleared_switch_fields()},
            quiet=True,
        )
        cancelled.append(token[:12])
    return {"cancelled": cancelled}


def apply_due_switches(now: datetime | None = None, *, service: Any = None) -> list[str]:
    """把到期的换出口申请落地：切 proxy、恢复原状态。"""
    service = service or _account_service()
    current = now or datetime.now(timezone.utc)
    applied = []
    for account in service.list_accounts():
        target = account.get("egress_switch_target")
        due = _parse_utc(account.get("egress_switch_at"))
        token = str(account.get("access_token") or "")
        if not target or not token or due is None or due > current:
            continue
        try:
            _apply_switch(service, token, account)
        except ValueError:
            # 槽位数量变少导致目标不存在：取消申请，恢复原状态。
            cancel_switch([token], service=service)
            continue
        applied.append(token[:12])
    return applied


def overview(*, service: Any = None) -> dict[str, Any]:
    """各出口上的账号数与待换出口的账号，供页面展示。"""
    service = service or _account_service()
    counts: dict[str, int] = {}
    pending = []
    for account in service.list_accounts():
        target = account_current_target(account)
        counts[target] = counts.get(target, 0) + 1
        if account.get("egress_switch_target"):
            pending.append({
                "email": account.get("email") or "",
                "token": str(account.get("access_token") or "")[:12],
                "from": target,
                "to": account.get("egress_switch_target"),
                "due_at": account.get("egress_switch_at"),
            })
    capacity = int(settings()["slot_capacity"])
    slots = [
        {"slot": slot, "accounts": counts.get(f"slot:{slot}", 0), "down": slot_down(slot), "capacity": capacity}
        for slot in slot_ports()
    ]
    return {
        "capacity": capacity,
        "slots": slots,
        "direct_accounts": counts.get("direct", 0),
        "default_accounts": counts.get("default", 0),
        "pending_switches": pending,
    }


def set_store_path(path: Path) -> Callable[[], None]:
    """测试用：切换存储文件，返回恢复函数。"""
    global _store_path
    previous = _store_path
    _store_path = path

    def restore() -> None:
        global _store_path
        _store_path = previous

    return restore

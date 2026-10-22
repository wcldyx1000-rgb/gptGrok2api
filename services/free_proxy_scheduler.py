"""公开免费代理定时拉取，测通后放进“免费公开代理”代理组，只供注册使用。默认关闭。

风险：免费代理由陌生人运营，可能记录、篡改经过的流量（包括邮箱验证码、账号凭据），
也常常很快失效。只有在明确接受这些风险时才开启，并且不要把这个代理组用于对话/生图出口。

每个抓到的代理都要先过出口检测（egress_check：OpenAI 看到的地区受支持、能连通）才会入库；
入库的是独立代理组，需要在注册设置里手动选它才会被使用，不会自动改动注册出口。
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from services import egress_check
from services.config import DATA_DIR, config
from services.json_file import read_json_object, write_json_file
from services.proxy_service import normalize_proxy_url

SETTINGS_FILE = DATA_DIR / "free_proxy.json"
FREE_PROXY_GROUP_ID = "free-public"
FREE_PROXY_GROUP_NAME = "免费公开代理"
SUPPORTED_COUNTRIES = ("US", "GB", "CA", "JP", "SG", "DE", "FR", "NL", "AU", "IE", "KR", "TW")
_USER_AGENT = "gptgrok2api-free-proxy/1.0"
_PROXY_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5h?)://)?"
    r"(?P<host>\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9.-]+):"
    r"(?P<port>\d{2,5})$"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_bool(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _safe_int(value: object, fallback: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = fallback
    return max(minimum, min(maximum, number))


def normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    protocol = str(data.get("protocol") or "http").strip().lower()
    if protocol not in {"http", "socks5"}:
        protocol = "http"
    return {
        "enabled": _safe_bool(data.get("enabled"), False),
        "protocol": protocol,
        "limit": _safe_int(data.get("limit"), 120, 10, 500),
        "test_limit": _safe_int(data.get("test_limit"), 40, 5, 120),
        "max_nodes": _safe_int(data.get("max_nodes"), 20, 1, 200),
        "interval_minutes": _safe_int(data.get("interval_minutes"), 60, 10, 1440),
        "allow_datacenter": _safe_bool(data.get("allow_datacenter"), True),
        "last_run_at": str(data.get("last_run_at") or ""),
        "last_error": str(data.get("last_error") or ""),
        "last_fetched": int(data.get("last_fetched") or 0),
        "last_ok": int(data.get("last_ok") or 0),
        "last_message": str(data.get("last_message") or ""),
    }


def parse_proxy_line(raw: str, *, protocol: str, country: str = "") -> dict[str, str] | None:
    line = str(raw or "").strip()
    if not line or line.startswith("#") or line.startswith("<"):
        return None
    line = line.split()[0].strip(",")
    match = _PROXY_LINE_RE.match(line)
    if not match:
        return None
    host = match.group("host")
    port = int(match.group("port"))
    if not 1 <= port <= 65535 or not _is_public_host(host):
        return None
    scheme = (match.group("scheme") or protocol).lower()
    scheme = "socks5" if scheme.startswith("socks5") else "http"
    return {
        "url": normalize_proxy_url(f"{scheme}://{host}:{port}"),
        "country": str(country or "").upper(),
    }


def _is_public_host(host: str) -> bool:
    value = str(host or "").strip().lower()
    if not value or value == "localhost":
        return False
    try:
        return bool(ipaddress.ip_address(value).is_global)
    except ValueError:
        return True


def _http_get(url: str, timeout: float = 15.0) -> str:
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _source_specs(protocol: str) -> list[dict[str, str]]:
    specs = [
        {
            "id": f"proxyscrape:{protocol}:{country}",
            "country": country,
            "url": (
                "https://api.proxyscrape.com/v2/?request=displayproxies"
                f"&protocol={protocol}&timeout=8000&country={country}&ssl=all&anonymity=all"
            ),
        }
        for country in SUPPORTED_COUNTRIES
    ]
    specs.append({
        "id": f"geonode:{protocol}",
        "country": "",
        "kind": "geonode",
        "url": (
            "https://proxylist.geonode.com/api/proxy-list?limit=200&page=1"
            f"&sort_by=lastChecked&sort_type=desc&protocols={protocol}"
        ),
    })
    return specs


def _parse_source(spec: dict[str, str], body: str, protocol: str) -> list[dict[str, str]]:
    if spec.get("kind") == "geonode":
        try:
            payload = json.loads(body)
        except ValueError:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else payload
        result = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            row = parse_proxy_line(
                f"{item.get('ip') or ''}:{item.get('port') or ''}",
                protocol=protocol,
                country=str(item.get("country") or item.get("countryCode") or ""),
            )
            if row:
                result.append(row)
        return result
    return [row for line in body.splitlines() if (row := parse_proxy_line(line, protocol=protocol, country=spec.get("country", "")))]


def fetch_candidates(
    settings: dict[str, Any],
    *,
    http_get: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    getter = http_get or _http_get
    protocol = settings["protocol"]
    merged: dict[str, dict[str, str]] = {}

    def fetch(spec: dict[str, str]) -> list[dict[str, str]]:
        try:
            return _parse_source(spec, getter(spec["url"]), protocol)
        except Exception:
            return []

    specs = _source_specs(protocol)
    with ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
        for future in as_completed([pool.submit(fetch, spec) for spec in specs]):
            for row in future.result():
                country = row.get("country") or ""
                if country and country not in SUPPORTED_COUNTRIES:
                    continue
                merged.setdefault(row["url"], row)
    return list(merged.values())[: settings["limit"]]


def usable_by_check(result: dict[str, Any], *, allow_datacenter: bool) -> bool:
    if result.get("verdict") not in {"ok", "warn"}:
        return False
    if not allow_datacenter and result.get("hosting"):
        return False
    return True


def refresh_pool(
    settings: dict[str, Any],
    *,
    http_get: Callable[[str], str] | None = None,
    checker: Callable[..., dict[str, Any]] | None = None,
    save_group: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """抓取 → 出口检测 → 把通过的节点写入“免费公开代理”代理组（替换旧节点）。"""
    candidates = fetch_candidates(settings, http_get=http_get)
    to_test = candidates[: settings["test_limit"]]
    check = checker or egress_check.check_egress
    passed: list[dict[str, Any]] = []
    if to_test:
        with ThreadPoolExecutor(max_workers=min(16, len(to_test))) as pool:
            futures = {pool.submit(check, item["url"], use_cache=False): item for item in to_test}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except Exception:
                    continue
                if usable_by_check(result, allow_datacenter=settings["allow_datacenter"]):
                    passed.append({**item, "latency_ms": int(result.get("latency_ms") or 0), "country": result.get("country_code") or item.get("country")})
    passed.sort(key=lambda item: item["latency_ms"])
    passed = passed[: settings["max_nodes"]]
    if passed:
        (save_group or _save_group)(passed)
        message = f"获取 {len(candidates)}，检测 {len(to_test)}，通过 {len(passed)}，已更新代理组"
    else:
        message = f"获取 {len(candidates)}，检测 {len(to_test)}，通过 0，保留现有代理组"
    return {"fetched": len(candidates), "tested": len(to_test), "ok": len(passed), "message": message}


def _save_group(nodes: list[dict[str, Any]]) -> None:
    group_nodes = []
    for item in nodes:
        parsed = urlparse(item["url"])
        group_nodes.append({
            "id": f"{parsed.hostname}-{parsed.port}".replace(".", "-"),
            "name": f"{item.get('country') or '??'} {parsed.hostname}:{parsed.port}",
            "url": item["url"],
            "enabled": True,
        })
    groups = [dict(group) for group in (config.data.get("proxy_groups") or []) if isinstance(group, dict)]
    existing = next((group for group in groups if group.get("id") == FREE_PROXY_GROUP_ID), {})
    item = {
        "id": FREE_PROXY_GROUP_ID,
        "name": FREE_PROXY_GROUP_NAME,
        "strategy": "request_random",
        "enabled": bool(existing.get("enabled", True)),
        "notes": "公开免费代理，自动抓取并通过出口检测；只建议用于注册，流量可能被第三方查看",
        "nodes": group_nodes,
    }
    config.update({"proxy_groups": [group for group in groups if group.get("id") != FREE_PROXY_GROUP_ID] + [item]})


class FreeProxyScheduler:
    def __init__(self, settings_file: Path | None = None) -> None:
        self._settings_file = settings_file or SETTINGS_FILE
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._run_now = False

    def _load(self) -> dict[str, Any]:
        raw = read_json_object(self._settings_file, name=self._settings_file.name) if self._settings_file.exists() else {}
        return normalize_settings(raw)

    def _store(self, values: dict[str, Any]) -> dict[str, Any]:
        settings = normalize_settings(values)
        write_json_file(self._settings_file, settings, backup=False)
        return settings

    def start(self, stop_event: threading.Event | None = None) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = stop_event or threading.Event()
            self._thread = threading.Thread(target=self._loop, name="free-proxy-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def status(self) -> dict[str, Any]:
        settings = self._load()
        next_run_at = ""
        if settings["enabled"] and settings["last_run_at"]:
            try:
                last = datetime.fromisoformat(settings["last_run_at"])
                next_run_at = (last + timedelta(minutes=settings["interval_minutes"])).isoformat()
            except ValueError:
                next_run_at = ""
        with self._lock:
            running = self._running
        return {**settings, "running": running, "next_run_at": next_run_at, "group_id": FREE_PROXY_GROUP_ID}

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self._load()
        allowed = {key: value for key, value in (updates or {}).items() if not str(key).startswith("last_")}
        saved = self._store({**current, **allowed})
        if saved["enabled"] and not current["enabled"]:
            self._run_now = True
        self._wake.set()
        return self.status()

    def run_now(self, **kwargs: Any) -> dict[str, Any]:
        return {"free_proxy": self.status(), "result": self._execute(**kwargs)}

    def _due(self, settings: dict[str, Any]) -> bool:
        if not settings["enabled"]:
            return False
        if self._run_now or not settings["last_run_at"]:
            return True
        try:
            last = datetime.fromisoformat(settings["last_run_at"])
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= last + timedelta(minutes=settings["interval_minutes"])

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._load()
            if self._due(settings):
                self._run_now = False
                try:
                    self._execute()
                except Exception:
                    pass
            self._wake.wait(60.0)
            self._wake.clear()

    def _execute(self, **kwargs: Any) -> dict[str, Any]:
        if not self._job_lock.acquire(blocking=False):
            raise RuntimeError("正在获取公开代理")
        settings = self._load()
        with self._lock:
            self._running = True
        try:
            result = refresh_pool(settings, **kwargs)
            self._store({
                **settings,
                "last_run_at": _now(),
                "last_error": "",
                "last_fetched": result["fetched"],
                "last_ok": result["ok"],
                "last_message": result["message"],
            })
            return result
        except Exception as exc:
            self._store({
                **settings,
                "last_run_at": _now(),
                "last_error": str(exc)[:240],
                "last_message": f"自动获取失败: {exc}"[:240],
            })
            raise
        finally:
            with self._lock:
                self._running = False
            self._job_lock.release()


free_proxy_scheduler = FreeProxyScheduler()

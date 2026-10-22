"""注册前出口检测：看 OpenAI 实际看到的出口 IP 在哪、是什么类型，不合格就不让注册开跑。

- 出口 IP 和地区取自 auth.openai.com/cdn-cgi/trace（Cloudflare 回显），走的就是注册用的代理，
  所以经过 mihomo 这类按域名分流的网关时，测到的也是 OpenAI 流量真正用的那个出口。
- IP 类型（机房/住宅）用 ip-api.com 按 IP 查询，直连查，不消耗代理流量。
- 不支持地区按配置拦截；机房 IP、被标记为代理的 IP 只提示不拦。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from curl_cffi.requests import Session

from services.proxy_service import _redact_url_credentials, normalize_proxy_url, proxy_settings

TRACE_URL = "https://auth.openai.com/cdn-cgi/trace"
IP_INFO_URL = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,isp,org,as,hosting,proxy,mobile"
# OpenAI 不提供服务的常见地区。
UNSUPPORTED_COUNTRIES = {"CN", "HK", "MO", "RU", "BY", "IR", "KP", "SY", "CU", "VE"}
CACHE_TTL_SECS = 600
IP_INFO_TTL_SECS = 6 * 3600
CHECK_TIMEOUT_SECS = 15.0
TRACE_IMPERSONATE = "chrome142"

# ip-api 的 hosting 标记会漏掉不少机房，运营商/组织名里带这些字样的一律按机房算。
DATACENTER_HINTS = (
    "datacamp", "cdn77", "m247", "digitalocean", "ovh", "hetzner", "amazon", "aws", "google",
    "microsoft", "azure", "linode", "akamai", "vultr", "choopa", "leaseweb", "ipxo", "oracle",
    "alibaba", "tencent", "huawei", "contabo", "hostinger", "cloudflare", "zenlayer", "psychz",
    "quadranet", "colocrossing", "gcore", "g-core", "ucloud", "kamatera", "scaleway", "servers.com",
    "hosting", "datacenter", "data center", "server", "vps", "cloud", "colo", "dedicated",
)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_ip_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def parse_trace(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in str(text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def _fetch_trace(proxy_url: str) -> dict[str, str]:
    session = Session(impersonate=TRACE_IMPERSONATE, proxy=proxy_url or None, verify=False)
    try:
        response = session.get(TRACE_URL, timeout=CHECK_TIMEOUT_SECS)
        if response.status_code != 200:
            raise RuntimeError(f"cdn-cgi/trace HTTP {response.status_code}")
        trace = parse_trace(response.text)
        if not trace.get("ip"):
            raise RuntimeError("cdn-cgi/trace 没有返回出口 IP")
        return trace
    finally:
        session.close()


def _fetch_ip_info(ip: str) -> dict[str, Any]:
    # ip-api 免费版每分钟 45 次；同一个 IP 的类型不会常变，缓存起来（机场节点常常共用 IP）。
    with _cache_lock:
        cached = _ip_info_cache.get(ip)
        if cached and time.monotonic() - cached[0] < IP_INFO_TTL_SECS:
            return dict(cached[1])
    session = Session(impersonate=TRACE_IMPERSONATE, verify=False)
    try:
        response = session.get(IP_INFO_URL.format(ip=ip), timeout=CHECK_TIMEOUT_SECS)
        data = response.json() if response.status_code == 200 else {}
        info = data if isinstance(data, dict) and data.get("status") == "success" else {}
    except Exception:
        info = {}
    finally:
        session.close()
    if info:
        with _cache_lock:
            _ip_info_cache[ip] = (time.monotonic(), dict(info))
    return info


def looks_datacenter(info: dict[str, Any]) -> bool:
    text = " ".join(str(info.get(key) or "") for key in ("isp", "org", "as")).lower()
    return bool(info.get("hosting")) or any(hint in text for hint in DATACENTER_HINTS)


def verdict(country_code: str, info: dict[str, Any]) -> tuple[str, str]:
    if country_code.upper() in UNSUPPORTED_COUNTRIES:
        return "blocked", f"出口在 {country_code.upper()}，OpenAI 不支持该地区，注册的号容易被停用"
    if looks_datacenter(info):
        return "warn", "机房 IP（不是住宅），风控分偏低，建议换静态住宅 IP"
    if info.get("proxy"):
        return "warn", "IP 被标记为代理/VPN，风控分偏低"
    if not info:
        return "ok", "地区支持（IP 类型没查到）"
    return "ok", "地区支持，非机房 IP"


def _proxy_label(proxy: str) -> str:
    return _redact_url_credentials(proxy) if proxy else "直连"


def check_egress(
    proxy_url: str = "",
    *,
    use_cache: bool = True,
    fetch_trace: Callable[[str], dict[str, str]] | None = None,
    fetch_ip_info: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """检测一条出口；proxy_url 为空表示直连。"""
    proxy = normalize_proxy_url(str(proxy_url or "").strip()) if proxy_url else ""
    if use_cache:
        with _cache_lock:
            cached = _cache.get(proxy)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_SECS:
                return dict(cached[1])
    result: dict[str, Any] = {"proxy": _proxy_label(proxy)}
    started = time.perf_counter()
    try:
        trace = (fetch_trace or _fetch_trace)(proxy)
    except Exception as error:
        raw = _redact_url_credentials(str(error))
        if "Connection closed abruptly" in raw or "SSL_ERROR_SYSCALL" in raw:
            raw = "连接被代理断开"
        result.update({
            "verdict": "error",
            "message": f"连不上 OpenAI（{raw[:160]}）",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        })
        return result
    ip = trace.get("ip", "")
    country_code = trace.get("loc", "").upper()
    info = (fetch_ip_info or _fetch_ip_info)(ip)
    level, message = verdict(country_code, info)
    result.update({
        "verdict": level,
        "message": message,
        "ip": ip,
        "country_code": country_code,
        "country": info.get("country") or country_code,
        "city": info.get("city") or "",
        "isp": info.get("isp") or "",
        "org": info.get("org") or "",
        "hosting": looks_datacenter(info),
        "flagged_proxy": bool(info.get("proxy")),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    })
    with _cache_lock:
        _cache[proxy] = (time.monotonic(), dict(result))
    return result


def register_egress_candidates(reference: str = "") -> list[str]:
    """注册任务可能用到的所有出口：代理组展开为启用且未停用的节点，其余按实际解析结果（可能是全局代理）。"""
    raw = str(reference or "").strip()
    if raw.lower().startswith("group:"):
        nodes = [normalize_proxy_url(item) for item in proxy_settings.egress_candidates(raw) if item]
        healthy = [node for node in nodes if not proxy_settings.is_proxy_disabled(node)]
        return healthy or nodes
    return [proxy_settings.get_profile(proxy=raw, upstream=True).proxy_url]


def preflight(
    reference: str = "",
    *,
    block_unsupported: bool = True,
    use_cache: bool = True,
    checker: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """注册前检查。

    - 有出口落在不支持地区且开启了拦截：不放行。
    - 连不上 OpenAI 的出口：只有全部出口都连不上才不放行（个别坏节点交给运行中的自动停用处理）。
    - 机房/代理 IP：只提示。
    """
    candidates = register_egress_candidates(reference)
    if not candidates:
        return {"ok": False, "results": [], "message": "代理组没有可用节点"}
    check = checker or check_egress
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        results = list(pool.map(lambda url: check(url, use_cache=use_cache), candidates))
    blocked = [item for item in results if item.get("verdict") == "blocked"]
    errors = [item for item in results if item.get("verdict") == "error"]
    if blocked and block_unsupported:
        names = "；".join(f"{item['proxy']}：{item['message']}" for item in blocked[:4])
        return {"ok": False, "results": results, "message": f"出口检测没通过，已阻止启动。{names}"}
    if errors and len(errors) == len(results):
        names = "；".join(f"{item['proxy']}：{item['message']}" for item in errors[:4])
        return {"ok": False, "results": results, "message": f"所有出口都连不上 OpenAI，已阻止启动。{names}"}
    notes = []
    warned = [item for item in results if item.get("verdict") == "warn"]
    if warned:
        notes.append(f"{len(warned)} 个出口有风险提示")
    if errors:
        notes.append(f"{len(errors)} 个出口连不上")
    if blocked:
        notes.append(f"{len(blocked)} 个出口在不支持地区（未开启拦截）")
    message = "出口检测通过" + (f"（{'，'.join(notes)}）" if notes else "")
    return {"ok": True, "results": results, "message": message}


def describe(result: dict[str, Any]) -> str:
    """一行日志：出口 → 国家/城市 · 运营商 · 类型 · 结论。"""
    if result.get("verdict") == "error":
        return f"{result.get('proxy')} → {result.get('message')}"
    kind = "机房" if result.get("hosting") else "非机房"
    place = " ".join(part for part in (result.get("country"), result.get("city")) if part)
    return f"{result.get('proxy')} → {place} · {result.get('isp') or result.get('org') or '-'} · {kind} · {result.get('message')}"


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _ip_info_cache.clear()

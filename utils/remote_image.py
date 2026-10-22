"""公网图片下载：所有用户提供的图片 URL 统一走这里，防止 SSRF。

移植自 chatgpt2api v3.2.3（AGPL-3.0）api/image_inputs.py 的 SSRF 防护：
- 每一跳都解析 DNS，只放行 is_global 的公网地址；
- 关闭自动重定向，逐跳校验并限制次数；
- 直连时用 CURLOPT_RESOLVE 把连接钉在已校验的地址上，避免 DNS rebinding；
- 流式读取并在超出上限时立即中止。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from urllib.parse import ParseResult, urljoin, urlparse

from curl_cffi import CurlOpt
from curl_cffi.requests import Session
from fastapi import HTTPException

MAX_IMAGE_REDIRECTS = 3
IMAGE_FETCH_CONCURRENCY = 8
_READ_CHUNK_BYTES = 64 * 1024

_IMAGE_FETCH_SLOTS = threading.BoundedSemaphore(IMAGE_FETCH_CONCURRENCY)

# Clash/mihomo 的 fake-ip 模式会把所有域名解析到 198.18.0.0/15，真实目标由代理再解析；
# 域名解析到该网段时放行，字面量 IP 仍然拒绝。
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class RemoteImage:
    data: bytes
    content_type: str
    final_url: str
    path: str


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": message})


def _limit_label(max_bytes: int) -> str:
    return f"{max_bytes // (1024 * 1024)}MB"


def _is_allowed_address(address: str, *, resolved: bool) -> bool:
    # IPv6 地址可能带 zone id（fe80::1%eth0），先去掉再判断
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.is_global:
        return True
    return resolved and ip.version == 4 and ip in _FAKE_IP_NETWORK


def validate_public_image_url(source: str, label: str = "image_url") -> tuple[ParseResult, tuple[str, ...]]:
    """校验远程图片 URL：只允许解析到公网地址的 http/https 链接。"""
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _bad_request(f"{label} must be an http or https URL")
    if parsed.username or parsed.password:
        raise _bad_request(f"{label} must not include credentials")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _bad_request(f"invalid {label} host") from exc
    if not hostname:
        raise _bad_request(f"invalid {label} host")
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith((".local", ".localhost")):
        raise _bad_request(f"{label} host is not publicly reachable")
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(normalized_host, port, type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise _bad_request(f"{label} host could not be resolved") from exc
    if not addresses or any(not _is_allowed_address(address, resolved=literal is None) for address in addresses):
        raise _bad_request(f"{label} host is not publicly reachable")
    return parsed, tuple(dict.fromkeys(addresses))


def _pinned_curl_options(parsed: ParseResult, addresses: tuple[str, ...]) -> dict[CurlOpt, object]:
    """把本跳连接钉在已校验的地址上；字面量 IP 无需钉。"""
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname.rstrip("."))
        return {}
    except ValueError:
        pass
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolved = [f"{hostname}:{port}:{f'[{address}]' if ':' in address else address}" for address in addresses]
    return {CurlOpt.RESOLVE: resolved}


def _read_limited(response, max_bytes: int, label: str = "image_url") -> bytes:
    data = bytearray()
    for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise _bad_request(f"{label} exceeds {_limit_label(max_bytes)} limit")
    return bytes(data)


def _fetch_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    accept: str,
    label: str,
    session_kwargs: dict[str, object] | None = None,
    max_redirects: int = MAX_IMAGE_REDIRECTS,
) -> RemoteImage:
    """下载公网图片：每跳校验地址，返回原始字节与响应 content-type。

    session_kwargs 用于透传全局代理配置；走代理时由代理解析域名，
    此时仍会在本地做公网校验，但无法钉住最终连接地址。
    """
    current = str(url or "").strip()
    extra_kwargs = dict(session_kwargs or {})
    for redirect_count in range(max_redirects + 1):
        parsed, addresses = validate_public_image_url(current, label)
        if not _IMAGE_FETCH_SLOTS.acquire(timeout=60):
            raise _bad_request(f"{label} fetch failed: image fetch concurrency limit reached")
        try:
            try:
                session = Session(curl_options=_pinned_curl_options(parsed, addresses), **extra_kwargs)
            except Exception as exc:
                raise _bad_request(f"{label} fetch failed: {exc}") from exc
            try:
                try:
                    response = session.get(
                        current,
                        headers={"Accept": accept, "User-Agent": user_agent},
                        timeout=timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                except Exception as exc:
                    raise _bad_request(f"{label} fetch failed: {exc}") from exc
                try:
                    if 300 <= response.status_code < 400:
                        if redirect_count >= max_redirects:
                            raise _bad_request(f"{label} has too many redirects")
                        location = str(response.headers.get("location") or "").strip()
                        if not location:
                            raise _bad_request(f"{label} redirect is missing a location")
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise _bad_request(f"{label} fetch failed: HTTP {response.status_code}")
                    content_length = str(response.headers.get("content-length") or "").strip()
                    if content_length.isdigit() and int(content_length) > max_bytes:
                        raise _bad_request(f"{label} exceeds {_limit_label(max_bytes)} limit")
                    try:
                        data = _read_limited(response, max_bytes, label)
                    except HTTPException:
                        raise
                    except Exception as exc:
                        raise _bad_request(f"{label} fetch failed: {exc}") from exc
                    if not data:
                        raise _bad_request(f"{label} returned empty content")
                    content_type = str(response.headers.get("content-type") or "")
                    return RemoteImage(data=data, content_type=content_type, final_url=current, path=parsed.path)
                finally:
                    response.close()
            finally:
                session.close()
        finally:
            _IMAGE_FETCH_SLOTS.release()
    raise _bad_request(f"{label} has too many redirects")


def fetch_public_image(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    session_kwargs: dict[str, object] | None = None,
) -> RemoteImage:
    """下载公网图片：每跳校验地址，返回原始字节与响应 content-type。"""
    return _fetch_public(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        user_agent=user_agent,
        accept="image/*,*/*;q=0.8",
        label="image_url",
        session_kwargs=session_kwargs,
    )


class RemoteFetchError(ValueError):
    """fetch_public_text 的失败原因（SSRF 拦截、超限、HTTP 错误等）。"""


def fetch_public_text(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    accept: str = "application/json,text/markdown,text/html,text/plain,*/*",
    max_redirects: int = MAX_IMAGE_REDIRECTS,
    session_kwargs: dict[str, object] | None = None,
) -> RemoteImage:
    """抓取公网文本资源（提示词来源等）：与图片下载相同的逐跳公网校验、
    连接固定、流式限长；仅 http/https，失败统一抛 RemoteFetchError。"""
    try:
        return _fetch_public(
            url,
            max_bytes=max_bytes,
            timeout=timeout,
            user_agent=user_agent,
            accept=accept,
            label="source_url",
            session_kwargs=session_kwargs,
            max_redirects=max_redirects,
        )
    except HTTPException as exc:
        detail = exc.detail
        message = detail.get("error") if isinstance(detail, dict) else detail
        raise RemoteFetchError(str(message or "fetch failed")) from exc

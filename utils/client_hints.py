"""按 User-Agent 推导 Client Hints（Sec-Ch-Ua-*），保证请求头与 UA 自洽。

- 品牌列表按 Chromium 的 GREASE 算法（以主版本号为种子）生成，品牌名、顺序和占位品牌都与真实浏览器一致。
- 只有 UA 真的携带完整版本号（或账号指纹显式提供）时才发送完整版本类高熵头；
  Chrome 精简 UA（如 145.0.0.0）不知道真实构建号，宁可不发，也不编造。
- Firefox / Safari 等非 Chromium UA 不发送任何 Sec-Ch-Ua 头。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

# Chromium components/embedder_support/user_agent_utils.cc 的 GREASE 参数。
_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_GREASE_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))

# Windows NT 10.0 同时代表 Win10/Win11，UA 无法区分；未显式提供时按 Windows 11 报告。
DEFAULT_WINDOWS_PLATFORM_VERSION = "19.0.0"

CLIENT_HINT_HEADERS = (
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-model",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-platform-version",
)


@dataclass(frozen=True)
class UserAgentInfo:
    brand: str  # "Microsoft Edge" / "Google Chrome"；非 Chromium 为空
    major: int
    brand_full: str  # 品牌完整版本；精简 UA 时为空
    chromium_full: str  # Chromium 完整版本；精简 UA 时为空
    platform: str  # Windows / macOS / Linux / Android / Chrome OS
    mobile: bool

    @property
    def is_chromium(self) -> bool:
        return bool(self.brand) and self.major > 0


def _full_or_empty(version: str) -> str:
    """精简版本（x.0.0.0）不是真实构建号，返回空串。"""
    parts = version.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return ""
    return "" if parts[1:] == ["0", "0", "0"] else version


def parse_user_agent(user_agent: str) -> UserAgentInfo:
    ua = str(user_agent or "")
    chrome = re.search(r"Chrome/(\d+)(?:\.(\d+\.\d+\.\d+))?", ua)
    edge = re.search(r"Edg/(\d+)(?:\.(\d+\.\d+\.\d+))?", ua)
    if "Android" in ua:
        platform = "Android"
    elif "CrOS" in ua:
        platform = "Chrome OS"
    elif "Windows" in ua:
        platform = "Windows"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        platform = "macOS"
    elif "Linux" in ua:
        platform = "Linux"
    else:
        platform = ""
    mobile = "Mobile" in ua
    if not chrome or "Firefox/" in ua or "OPR/" in ua:
        return UserAgentInfo("", 0, "", "", platform, mobile)
    chromium_full = _full_or_empty(f"{chrome.group(1)}.{chrome.group(2)}" if chrome.group(2) else "")
    if edge:
        major = int(edge.group(1))
        brand_full = _full_or_empty(f"{edge.group(1)}.{edge.group(2)}" if edge.group(2) else "")
        return UserAgentInfo("Microsoft Edge", major, brand_full, chromium_full, platform, mobile)
    return UserAgentInfo("Google Chrome", int(chrome.group(1)), chromium_full, chromium_full, platform, mobile)


def _grease_brand(major: int) -> tuple[str, str]:
    name = f"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand"
    return name, _GREASE_VERSIONS[major % 3]


def _brand_list(info: UserAgentInfo, versions: tuple[str, str, str]) -> str:
    """versions 依次为 (GREASE, Chromium, 品牌) 的版本号。"""
    grease_name, _ = _grease_brand(info.major)
    names = (grease_name, "Chromium", info.brand)
    order = _GREASE_ORDERS[info.major % 6]
    slots: list[str] = [""] * 3
    for index, position in enumerate(order):
        slots[position] = f'"{names[index]}";v="{versions[index]}"'
    return ", ".join(slots)


def build_client_hints(
    user_agent: str,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """返回小写头名到值的映射；overrides 为账号指纹里显式保存的同名头，优先使用。"""
    info = parse_user_agent(user_agent)
    if not info.is_chromium:
        return {}
    explicit = {
        str(key).lower(): str(value)
        for key, value in (overrides or {}).items()
        if str(key).lower() in CLIENT_HINT_HEADERS and str(value or "").strip()
    }
    grease_version = _grease_brand(info.major)[1]
    major = str(info.major)
    desktop = info.platform in {"Windows", "macOS", "Linux", "Chrome OS"} and not info.mobile
    hints = {
        "sec-ch-ua": _brand_list(info, (grease_version, major, major)),
        "sec-ch-ua-mobile": "?1" if info.mobile else "?0",
        "sec-ch-ua-platform": f'"{info.platform}"',
    }
    if desktop:
        hints["sec-ch-ua-arch"] = '"x86"'
        hints["sec-ch-ua-bitness"] = '"64"'
        hints["sec-ch-ua-model"] = '""'
    if info.brand_full and info.chromium_full:
        hints["sec-ch-ua-full-version"] = f'"{info.brand_full}"'
        hints["sec-ch-ua-full-version-list"] = _brand_list(
            info,
            (f"{grease_version}.0.0.0", info.chromium_full, info.brand_full),
        )
    if info.platform == "Windows":
        hints["sec-ch-ua-platform-version"] = f'"{DEFAULT_WINDOWS_PLATFORM_VERSION}"'
    hints.update(explicit)
    return hints


def consistent_overrides(user_agent: str, overrides: Mapping[str, object] | None) -> dict[str, str]:
    """过滤账号指纹里保存的 Client Hints，只保留与 user_agent 一致的项。

    历史账号可能保存了与 UA 不符的写死值（例如 UA 为 Chrome 136 却带 Chrome 142 的 GREASE
    品牌），这类值发出去反而自相矛盾，直接丢弃，改用按 UA 推导的值。
    """
    info = parse_user_agent(user_agent)
    if not info.is_chromium or not overrides:
        return {}
    grease_name = _grease_brand(info.major)[0]
    major_marker = f'v="{info.major}'
    platform = f'"{info.platform}"'
    mobile = "?1" if info.mobile else "?0"
    result: dict[str, str] = {}
    for key, value in overrides.items():
        name = str(key).lower()
        text = str(value or "").strip()
        if name not in CLIENT_HINT_HEADERS or not text:
            continue
        if name in {"sec-ch-ua", "sec-ch-ua-full-version-list"}:
            if f'"{grease_name}"' not in text or major_marker not in text or f'"{info.brand}"' not in text:
                continue
        elif name == "sec-ch-ua-platform":
            if text != platform:
                continue
        elif name == "sec-ch-ua-mobile":
            if text != mobile:
                continue
        elif name == "sec-ch-ua-full-version":
            if not text.strip('"').startswith(f"{info.major}."):
                continue
        result[name] = text
    return result


def rewrite_client_hints(headers: Mapping[str, str], user_agent: str) -> dict[str, str]:
    """把已有请求头里的 Sec-Ch-Ua-* 替换为与 user_agent 一致的值；无法推导的项直接去掉。"""
    hints = build_client_hints(user_agent)
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower = str(key).lower()
        if lower in CLIENT_HINT_HEADERS:
            if lower in hints:
                result[key] = hints[lower]
            continue
        result[key] = value
    return result


def browser_session_kwargs(user_agent: str, impersonate: str) -> dict[str, object]:
    """curl_cffi Session 参数：指定 TLS 指纹，并把 UA 与低熵 Client Hints 设为会话默认头。

    impersonate 会为未显式设置的请求自动补上目标浏览器自己的 sec-ch-ua / 平台（实测
    chrome110 会补 Chrome 110 的品牌，chrome142 会补 "macOS"），这里用与 UA 一致的值覆盖。

    TLS 指纹不按 UA 自动换新：实测较新的目标（chrome131～146）访问 chatgpt.com 首页约一半
    会被 Cloudflare 挑战，chrome110 稳定放行，因此由调用方沿用经过验证的目标。
    """
    hints = build_client_hints(user_agent)
    headers = {"User-Agent": user_agent}
    for name in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
        if name in hints:
            headers[name] = hints[name]
    return {"impersonate": impersonate, "headers": headers}

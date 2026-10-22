"""浏览器指纹池：每个账号固定一套 UA + TLS 目标，UA 版本与 TLS 目标一致。

注册（random_fingerprint）和账号池（pick_fingerprint）共用同一份池子，注册时的 UA / TLS 目标
随账号写入 fp，入池后沿用同一套身份。

只收录实测稳定的组合（2026-10-16，直连出口）：chatgpt.com 首页冷启动 20/20 放行、登录对话正常。
未收录：chrome131 / safari260（首页全部被 Cloudflare 挑战），chrome120 / 136 / 145 / 146
（约 1/5 被挑战），chrome110 / edge101（能用但版本过旧，少见反而显眼），Safari / Firefox
（非 Chromium，现有请求头链路按 Chromium 构造）。

Client Hints（Sec-Ch-Ua-*）不写进指纹，运行时按 UA 推导（utils.client_hints），保证三者一致。
已有指纹的账号不重新分配，避免老账号指纹突然改变。
"""
from __future__ import annotations

import hashlib
import re

_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
_MACOS = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"


def _chrome(platform: str, major: int) -> str:
    return f"{platform} Chrome/{major}.0.0.0 Safari/537.36"


# (权重, TLS 目标, User-Agent)；新版本权重更高，接近真实用户分布。
FINGERPRINT_POOL: tuple[tuple[int, str, str], ...] = (
    (35, "chrome142", _chrome(_WINDOWS, 142)),
    (20, "chrome142", _chrome(_MACOS, 142)),
    (15, "chrome124", _chrome(_WINDOWS, 124)),
    (10, "chrome124", _chrome(_MACOS, 124)),
    (12, "chrome116", _chrome(_WINDOWS, 116)),
    (8, "chrome116", _chrome(_MACOS, 116)),
)


def pick_fingerprint(seed: str) -> dict[str, str]:
    """按 seed 稳定地加权选一套指纹；同一 seed 永远得到同一套。"""
    total = sum(weight for weight, _, _ in FINGERPRINT_POOL)
    point = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") % total
    for weight, impersonate, user_agent in FINGERPRINT_POOL:
        if point < weight:
            return {"user-agent": user_agent, "impersonate": impersonate}
        point -= weight
    _, impersonate, user_agent = FINGERPRINT_POOL[0]
    return {"user-agent": user_agent, "impersonate": impersonate}


def random_fingerprint() -> tuple[str, str]:
    """注册时按权重随机选一套，返回 (TLS 目标, User-Agent)。"""
    import secrets

    total = sum(weight for weight, _, _ in FINGERPRINT_POOL)
    point = secrets.randbelow(total)
    for weight, impersonate, user_agent in FINGERPRINT_POOL:
        if point < weight:
            return impersonate, user_agent
        point -= weight
    _, impersonate, user_agent = FINGERPRINT_POOL[0]
    return impersonate, user_agent


# curl_cffi 支持的 Chrome TLS 目标，按主版本降序；UA 版本落在两档之间时取不超过它的最近一档。
_CHROME_IMPERSONATE_BY_MAJOR: tuple[tuple[int, str], ...] = (
    (146, "chrome146"),
    (145, "chrome145"),
    (142, "chrome142"),
    (136, "chrome136"),
    (133, "chrome133a"),
    (131, "chrome131"),
    (124, "chrome124"),
    (123, "chrome123"),
    (120, "chrome120"),
    (119, "chrome119"),
    (116, "chrome116"),
    (110, "chrome110"),
)


def impersonate_for_user_agent(user_agent: str, default: str = "") -> str:
    """按 UA 里的 Chrome 主版本选 TLS 目标，让 TLS 指纹跟着 UA 走。

    UA 不是 Chromium（或解析不出版本）时返回 default，由调用方保留原 TLS 目标。
    """
    match = re.search(r"(?:Chrome|Chromium|CriOS)/(\d+)", str(user_agent or ""))
    if not match:
        return default
    major = int(match.group(1))
    for version, name in _CHROME_IMPERSONATE_BY_MAJOR:
        if major >= version:
            return name
    return "chrome110"


def has_browser_fingerprint(account: dict) -> bool:
    """账号自带 UA 或 TLS 目标（顶层字段或 fp）时视为已有指纹，不覆盖。"""
    raw_fp = account.get("fp")
    fp = {str(k).lower(): v for k, v in raw_fp.items()} if isinstance(raw_fp, dict) else {}
    return any(str(source.get(key) or "").strip() for source in (account, fp) for key in ("user-agent", "impersonate"))

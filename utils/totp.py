"""TOTP 两步验证码计算（RFC 6238），与验证器 App 算法一致。

账号导入里 `邮箱|密码|2FA` 的 2FA 是一串 base32 密钥；在本地算出当前 6 位码，
不需要收短信或第三方接码网站。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time


def normalize_secret(secret: str) -> str:
    """去掉空格和 otpauth:// 包装，统一成大写 base32 密钥。"""
    raw = str(secret or "").strip()
    if not raw:
        return ""
    match = re.search(r"[?&]secret=([^&]+)", raw, re.IGNORECASE)
    if match:
        raw = match.group(1)
    return re.sub(r"\s+", "", raw).upper()


def _decode_secret(secret: str) -> bytes:
    cleaned = normalize_secret(secret)
    if not cleaned:
        raise ValueError("2FA 密钥为空")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except Exception as error:
        raise ValueError("2FA 密钥不是有效的 base32") from error


def is_valid_secret(secret: str) -> bool:
    try:
        _decode_secret(secret)
        return True
    except ValueError:
        return False


def generate(secret: str, *, at: float | None = None, digits: int = 6, period: int = 30, algorithm: str = "SHA1") -> str:
    """指定时刻的 TOTP 码（默认当前时间、6 位、30 秒周期、SHA1）。"""
    key = _decode_secret(secret)
    counter = int((at if at is not None else time.time()) // period)
    digestmod = getattr(hashlib, algorithm.lower(), hashlib.sha1)
    digest = hmac.new(key, struct.pack(">Q", counter), digestmod).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def generate_window(secret: str, *, period: int = 30, spread: int = 1, at: float | None = None, **kwargs) -> list[str]:
    """当前码在前，其后是相邻周期的码；提交失败时依次再试，容忍时钟误差。"""
    now = at if at is not None else time.time()
    current = generate(secret, at=now, period=period, **kwargs)
    codes = [current]
    for step in range(-spread, spread + 1):
        code = generate(secret, at=now + step * period, period=period, **kwargs)
        if code not in codes:
            codes.append(code)
    return codes

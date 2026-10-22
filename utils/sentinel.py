"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from utils.locale_env import LocaleEnv
from utils.pow import (
    WINDOW_PRESENCE_FLAGS,
    PowFingerprint,
    ScriptSrcParser,
    navigator_probe,
    window_own_key,
)
from utils.turnstile import solve_turnstile_token

if TYPE_CHECKING:
    from curl_cffi.requests import Session


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。

    字段对齐 Sentinel SDK 的 getConfig（运行在只含 SDK 的 frame.html 中）；
    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        device_id: str,
        ua: str,
        locale: LocaleEnv | None = None,
        sdk_src: str = "",
    ):
        self.device_id = device_id
        self.user_agent = ua
        self.locale = locale
        # frame.html 里只有 SDK 一个 script，第 5 位即它的地址。
        self.sdk_src = sdk_src or DEFAULT_SENTINEL_SDK_SRC
        # 屏幕、核数、堆上限与 pow 共用同一个 device id 派生的设备参数，保证两处一致。
        fingerprint = PowFingerprint.create(device_id)
        self.sid = fingerprint.sid
        self.cores = fingerprint.cores
        self.screen_sum = fingerprint.screen_sum
        self.heap_size_limit = fingerprint.heap_size_limit
        # 同一生成器（一次 SDK 页面会话）内 timeOrigin 固定。
        self.time_origin_ms = time.time() * 1000 - random.uniform(1000, 50000)

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = time.time() * 1000 - self.time_origin_ms
        language = self.locale.language if self.locale else "en-US"
        languages = ",".join(self.locale.languages) if self.locale else "en-US,en"
        return [
            self.screen_sum,
            (
                self.locale.date_string()
                if self.locale
                else time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
            ),
            self.heap_size_limit,
            random.random(),
            self.user_agent,
            self.sdk_src,
            None,  # frame.html 没有 c/…/_ 脚本，也没有 data-build
            language,
            languages,
            random.random(),
            navigator_probe(self.cores, language),
            "location",  # frame.html 上 Object.keys(document) 只有 location
            window_own_key(),
            perf_now,
            self.sid,
            "",
            self.cores,
            self.time_origin_ms,
            *WINDOW_PRESENCE_FLAGS,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        started = time.perf_counter()
        data = self._get_config()
        # 与 SDK 的 _generateRequirementsTokenAnswerBlocking 一致：第 9 位为生成配置的耗时（毫秒，不取整）。
        data[3] = 1
        data[9] = (time.perf_counter() - started) * 1000
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


# ── 默认 User-Agent 和 sec-ch-ua ──────────────────────────────
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"'
SENTINEL_ORIGIN = "https://chatgpt.com"
SENTINEL_VERSION = "20260423af3c"
SENTINEL_REQ_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/req"
SENTINEL_FRAME_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html"
# 仅在拉取 frame.html 失败时使用。
DEFAULT_SENTINEL_SDK_SRC = f"{SENTINEL_ORIGIN}/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_SDK_CACHE_TTL_SECS = 1800.0
SENTINEL_SDK_FAILURE_TTL_SECS = 60.0
_frame_sdk_cache: tuple[str, float] | None = None
_frame_sdk_lock = threading.Lock()


def sentinel_frame_sdk_src(session: "Session", user_agent: str) -> str:
    """读取 sentinel frame.html，取其中 SDK 的实际地址（版本随上游变化）；失败返回空串。"""
    global _frame_sdk_cache
    now = time.monotonic()
    with _frame_sdk_lock:
        if _frame_sdk_cache is not None and _frame_sdk_cache[1] > now:
            return _frame_sdk_cache[0]
    sdk = ""
    try:
        resp = session.get(
            SENTINEL_FRAME_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{SENTINEL_ORIGIN}/",
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": user_agent,
            },
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200 and isinstance(resp.text, str):
            # 直接用解析器，避免 parse_pow_resources 在无 script 时回落到 loader。
            parser = ScriptSrcParser()
            parser.feed(resp.text)
            sdk = next(
                (urljoin(SENTINEL_FRAME_URL, src) for src in parser.script_sources if src.endswith("/sdk.js")),
                "",
            )
    except Exception:
        sdk = ""
    ttl = SENTINEL_SDK_CACHE_TTL_SECS if sdk else SENTINEL_SDK_FAILURE_TTL_SECS
    with _frame_sdk_lock:
        _frame_sdk_cache = (sdk, now + ttl)
    return sdk


def _get_vm_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str,
    sec_ch_ua: str,
) -> tuple[str, str] | None:
    if os.getenv("OPENAI_SENTINEL_DISABLE_VM"):
        return None
    try:
        from utils.sentinel_vm import get_sentinel_token_via_vm

        result = get_sentinel_token_via_vm(
            session,
            device_id,
            flow,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
        )
        if isinstance(result, tuple) and len(result) == 2:
            return str(result[0]), str(result[1])
        return None
    except Exception:
        return None


def _response_cookie_value(response: object, session: object, name: str) -> str:
    raw_values: list[str] = []
    headers = getattr(response, "headers", None)
    if headers is not None:
        for method_name in ("get_list", "getlist"):
            method = getattr(headers, method_name, None)
            if callable(method):
                try:
                    raw_values.extend(str(value) for value in method("set-cookie") or [])
                except Exception:
                    pass
        try:
            raw = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
            if raw:
                raw_values.append(str(raw))
        except Exception:
            pass
    pattern = re.compile(rf"(?:^|[\r\n])\s*{re.escape(name)}=([^;\r\n]+)", re.IGNORECASE)
    for raw in raw_values:
        match = pattern.search(raw)
        if match:
            return match.group(1).strip()
    try:
        return str(getattr(session, "cookies").get(name) or "")
    except Exception:
        return ""


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    locale: LocaleEnv | None = None,
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    vm_bundle = _get_vm_sentinel_token(session, device_id, flow, user_agent=ua, sec_ch_ua=ch_ua)
    if vm_bundle:
        return vm_bundle
    generator = SentinelTokenGenerator(device_id, ua, locale, sentinel_frame_sdk_src(session, ua))
    requirements_token = generator.generate_requirements_token()
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "Origin": SENTINEL_ORIGIN,
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    turnstile_data = data.get("turnstile") or {}
    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        # 对齐 sentinel sdk.js 的 token(flow) 行为：
        # t 字段由 req 阶段返回的 turnstile.dx 与本次 requirements_token 计算得到。
        turnstile_token = solve_turnstile_token(str(turnstile_data.get("dx") or ""), requirements_token) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")
    sentinel_value = json.dumps(
        {"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = _response_cookie_value(resp, session, "oai-sc")
    if not oai_sc_value:
        raise RuntimeError("sentinel_req_missing_oai_sc")
    return sentinel_value, oai_sc_value


def build_sentinel_with_so_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    locale: LocaleEnv | None = None,
) -> tuple[str, str, str]:
    """请求 Sentinel token，并返回主 token、Session Observer token 和 oai-sc。

    ``turnstile`` 和 Session Observer token 是两个独立字段：前者写入
    主 token 的 ``t`` 字段，后者才可作为 ``OpenAI-Sentinel-SO-Token``
    请求头。不能把 Turnstile 解题结果复用为 SO token。当前无浏览器 VM
    仅生成官方 SDK 的主 token；SO token 仍保守留空。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "oauth_create_account" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖

    Returns:
        (openai-sentinel-token, openai-sentinel-so-token, oai-sc cookie) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    vm_bundle = _get_vm_sentinel_token(session, device_id, flow, user_agent=ua, sec_ch_ua=ch_ua)
    if vm_bundle:
        return vm_bundle[0], "", vm_bundle[1]
    generator = SentinelTokenGenerator(device_id, ua, locale, sentinel_frame_sdk_src(session, ua))
    requirements_token = generator.generate_requirements_token()
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "Origin": SENTINEL_ORIGIN,
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, "", ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )

    turnstile_data = data.get("turnstile") or {}
    turnstile_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        turnstile_token = solve_turnstile_token(str(turnstile_data.get("dx") or ""), requirements_token) or ""
        if not turnstile_token:
            raise RuntimeError("sentinel_turnstile_token_failed")

    # Sentinel token 包含 PoW / Turnstile 字段。/req 的 so.snapshot_dx
    # 不是可直接发送的 SO token，未执行官方 Session Observer VM 时必须留空。
    sentinel_value = json.dumps(
        {"p": p_value, "t": turnstile_token, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = _response_cookie_value(resp, session, "oai-sc")
    if not oai_sc_value:
        raise RuntimeError("sentinel_req_missing_oai_sc")

    return sentinel_value, "", oai_sc_value

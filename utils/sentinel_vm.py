"""Run the official Sentinel SDK's PoW/Turnstile path in a Node VM.

The VM has no network access. Python keeps ownership of the curl_cffi session,
its TLS fingerprint, proxy, and cookies; it only gives the SDK challenge data
and receives the generated ``p``/``t`` values through stdin/stdout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from utils.turnstile import solve_turnstile_token

if TYPE_CHECKING:
    from utils.locale_env import LocaleEnv
    from utils.pow import PowFingerprint


SENTINEL_VERSION = "20260423af3c"
SENTINEL_ORIGIN = "https://chatgpt.com"
SENTINEL_BOOTSTRAP_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/sdk.js"
SENTINEL_SDK_URL = f"{SENTINEL_ORIGIN}/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = f"{SENTINEL_ORIGIN}/backend-api/sentinel/req"
RUNNER_PATH = Path(__file__).with_name("openai_sentinel_vm.js")
MAX_SDK_BYTES = 4 * 1024 * 1024
MIN_TURNSTILE_TOKEN_LENGTH = 16
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'


_NODE_WRAPPER = r"""
const fs = require('fs');
const sdkFile = process.env.OPENAI_SENTINEL_SDK_FILE;
const runnerFile = process.env.OPENAI_SENTINEL_VM_RUNNER;
const timeoutMs = Number(process.env.OPENAI_SENTINEL_VM_TIMEOUT_MS || '30000');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    globalThis.__payload_json = input;
    globalThis.__sdk_source = fs.readFileSync(sdkFile, 'utf8');
    globalThis.__vm_done = false;
    globalThis.__vm_output_json = '';
    globalThis.__vm_error = '';
    eval(fs.readFileSync(runnerFile, 'utf8'));
    const started = Date.now();
    while (!globalThis.__vm_done) {
      if (Date.now() - started > timeoutMs) throw new Error('Sentinel VM timeout');
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    if (String(globalThis.__vm_error || '').trim()) throw new Error(String(globalThis.__vm_error));
    process.stdout.write(String(globalThis.__vm_output_json || ''));
  } catch (error) {
    process.stderr.write(String((error && error.stack) || error));
    process.exit(1);
  }
});
""".strip()


def _node_binary() -> str:
    configured = str(os.getenv("OPENAI_SENTINEL_NODE_PATH") or "").strip()
    candidates = [configured, shutil.which("node") or "", "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Node.js is unavailable; set OPENAI_SENTINEL_NODE_PATH")


def _cache_file(version: str) -> Path:
    folder = Path(tempfile.gettempdir()) / "chatgpt2api-sentinel" / version
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "sdk.js"


def _ensure_sdk(session: Any, *, timeout_seconds: int) -> Path:
    version = SENTINEL_VERSION
    sdk_url = SENTINEL_SDK_URL
    try:
        bootstrap = session.get(
            SENTINEL_BOOTSTRAP_URL,
            headers={
                "accept": "*/*",
                "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
                "referer": f"{SENTINEL_ORIGIN}/",
                "sec-fetch-dest": "script",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-origin",
            },
            timeout=timeout_seconds,
            verify=False,
        )
        if getattr(bootstrap, "status_code", 0) == 200:
            source = str(getattr(bootstrap, "text", "") or "")
            match = re.search(
                r"https://chatgpt\.com/sentinel/([A-Za-z0-9_-]+)/sdk\.js",
                source,
            )
            if match:
                version = match.group(1)
                sdk_url = match.group(0)
    except Exception:
        pass

    cache_file = _cache_file(version)
    if cache_file.is_file() and 0 < cache_file.stat().st_size <= MAX_SDK_BYTES:
        return cache_file
    response = session.get(
        sdk_url,
        headers={
            "accept": "*/*",
            "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
            "referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={version}",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=timeout_seconds,
        verify=False,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"sentinel_sdk_http_{getattr(response, 'status_code', 'unknown')}")
    source = bytes(getattr(response, "content", b"") or b"")
    if not source:
        source = str(getattr(response, "text", "") or "").encode("utf-8")
    if not source or len(source) > MAX_SDK_BYTES:
        raise RuntimeError("invalid_sentinel_sdk_size")
    cache_file.write_bytes(source)
    return cache_file


def _run_action(*, sdk_file: Path, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    if not RUNNER_PATH.is_file():
        raise RuntimeError("sentinel_vm_runner_missing")
    process = subprocess.run(
        [_node_binary(), "-e", _NODE_WRAPPER],
        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        text=True,
        capture_output=True,
        timeout=max(10, timeout_seconds + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            "OPENAI_SENTINEL_VM_RUNNER": str(RUNNER_PATH),
            "OPENAI_SENTINEL_VM_TIMEOUT_MS": str(timeout_seconds * 1000),
            "TZ": os.environ.get("OPENAI_SENTINEL_TIMEZONE", "America/Sao_Paulo"),
        },
    )
    if process.returncode != 0:
        raise RuntimeError(f"sentinel_vm_failed: {(process.stderr or process.stdout or 'unknown')[:240]}")
    try:
        result = json.loads(str(process.stdout or ""))
    except json.JSONDecodeError as exc:
        raise RuntimeError("sentinel_vm_invalid_json") from exc
    if not isinstance(result, dict):
        raise RuntimeError("sentinel_vm_invalid_result")
    return result


def _fetch_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    user_agent: str,
    sec_ch_ua: str,
    sdk_version: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], str]:
    before_cookie = ""
    try:
        before_cookie = str(session.cookies.get("oai-sc") or "")
    except Exception:
        pass
    sec_ch_ua_platform = '"macOS"' if "Macintosh" in user_agent else '"Windows"'
    response = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps({"p": request_p, "id": device_id, "flow": flow}, separators=(",", ":")),
        headers={
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
            "content-type": "text/plain;charset=UTF-8",
            "origin": SENTINEL_ORIGIN,
            "referer": f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={sdk_version}",
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": sec_ch_ua_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        },
        timeout=timeout_seconds,
        verify=False,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"sentinel_req_http_{getattr(response, 'status_code', 'unknown')}")
    try:
        challenge = response.json()
    except Exception as exc:
        raise RuntimeError("sentinel_req_invalid_json") from exc
    if not isinstance(challenge, dict) or not str(challenge.get("token") or "").strip():
        raise RuntimeError("sentinel_req_missing_token")
    oai_sc = _response_cookie_value(response, "oai-sc")
    if not oai_sc:
        try:
            candidate = str(session.cookies.get("oai-sc") or "")
        except Exception:
            candidate = ""
        if candidate and candidate != before_cookie:
            oai_sc = candidate
    if not oai_sc:
        raise RuntimeError("sentinel_req_missing_oai_sc")
    return challenge, oai_sc


def _response_cookie_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    raw_values: list[str] = []
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
    return ""


def get_sentinel_token_via_vm(
    session: Any,
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    timeout_seconds: int = 30,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str] | None:
    """Return a real-SDK primary Sentinel token, or ``None`` on a safe fallback."""
    log = log or (lambda _message: None)
    did = str(device_id or uuid.uuid4()).strip()
    ua = str(user_agent or DEFAULT_USER_AGENT).strip()
    ch_ua = str(sec_ch_ua or DEFAULT_SEC_CH_UA).strip()
    timeout_seconds = max(10, min(45, int(timeout_seconds)))
    try:
        sdk_file = _ensure_sdk(session, timeout_seconds=timeout_seconds)
        sdk_version = sdk_file.parent.name
        sdk_url = f"{SENTINEL_ORIGIN}/sentinel/{sdk_version}/sdk.js"
        frame_url = f"{SENTINEL_ORIGIN}/backend-api/sentinel/frame.html?sv={sdk_version}"
        requirements = _run_action(
            sdk_file=sdk_file,
            payload={
                "action": "requirements",
                "device_id": did,
                "user_agent": ua,
                "sdk_url": sdk_url,
                "frame_url": frame_url,
            },
            timeout_seconds=timeout_seconds,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            raise RuntimeError("sentinel_vm_missing_requirements")
        challenge, oai_sc = _fetch_challenge(
            session,
            device_id=did,
            flow=flow,
            request_p=request_p,
            user_agent=ua,
            sec_ch_ua=ch_ua,
            sdk_version=sdk_version,
            timeout_seconds=timeout_seconds,
        )
        solved = _run_action(
            sdk_file=sdk_file,
            payload={
                "action": "solve",
                "device_id": did,
                "user_agent": ua,
                "request_p": request_p,
                "challenge": challenge,
                "sdk_url": sdk_url,
                "frame_url": frame_url,
            },
            timeout_seconds=timeout_seconds,
        )
        final_p = str(solved.get("final_p") or "").strip()
        raw_turnstile = solved.get("t")
        turnstile = "" if raw_turnstile is None else str(raw_turnstile).strip()
        if len(turnstile) < MIN_TURNSTILE_TOKEN_LENGTH:
            turnstile_data = challenge.get("turnstile") or {}
            dx = str(turnstile_data.get("dx") or "") if isinstance(turnstile_data, dict) else ""
            protocol_turnstile = solve_turnstile_token(dx, request_p) if dx else None
            if protocol_turnstile:
                turnstile = str(protocol_turnstile).strip()
                log(f"Sentinel VM replaced malformed Turnstile value (t_len={len(turnstile)})")
        challenge_token = str(challenge.get("token") or "").strip()
        if not final_p or not turnstile or not challenge_token:
            raise RuntimeError("sentinel_vm_missing_solution")
        result = json.dumps(
            {"p": final_p, "t": turnstile, "c": challenge_token, "id": did, "flow": flow},
            separators=(",", ":"),
        )
        log(
            "Sentinel VM success "
            f"(sdk={sdk_version}, p_len={len(final_p)}, t_len={len(turnstile)}, "
            f"c_len={len(challenge_token)}, oai_sc_len={len(oai_sc)})"
        )
        return result, oai_sc
    except Exception as exc:
        log(f"Sentinel VM fallback: {type(exc).__name__}: {str(exc)[:160]}")
        return None


# ── Chat page (chat-requirements) ─────────────────────────────────────────────
# The registration/checkout helpers above emulate the bare Sentinel frame. The
# chat-requirements tokens are produced by the SDK the chatgpt.com page loads,
# so the runtime below mirrors that page: its scripts and data-build, React
# document keys, Next.js window globals, the exit's locale and a device-stable
# screen/cores/heap shared with the pure-Python PoW fallback.

CHAT_PAGE_URL = f"{SENTINEL_ORIGIN}/"
SDK_DISCOVERY_TTL_SECS = 1800.0
SDK_DISCOVERY_FAILURE_TTL_SECS = 60.0
_sdk_discovery_lock = threading.Lock()
_sdk_discovery: tuple[str, str, float] | None = None
_node_probe: tuple[bool, float] | None = None
NODE_PROBE_TTL_SECS = 300.0


def node_available() -> bool:
    """Whether a Node.js binary is usable; cached briefly to keep the hot path cheap."""
    global _node_probe
    now = time.monotonic()
    cached = _node_probe
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        _node_binary()
        available = RUNNER_PATH.is_file()
    except RuntimeError:
        available = False
    _node_probe = (available, now + NODE_PROBE_TTL_SECS)
    return available


def discover_sdk(session: Any, *, accept_language: str = "", timeout_seconds: float = 10.0) -> tuple[str, str]:
    """Return ``(version, sdk_url)`` the sentinel loader injects; cached process-wide.

    A failed lookup falls back to the bundled version and is retried after a minute.
    """
    global _sdk_discovery
    now = time.monotonic()
    with _sdk_discovery_lock:
        if _sdk_discovery is not None and _sdk_discovery[2] > now:
            return _sdk_discovery[0], _sdk_discovery[1]
    version, sdk_url, found = SENTINEL_VERSION, SENTINEL_SDK_URL, False
    try:
        headers = {
            "accept": "*/*",
            "referer": CHAT_PAGE_URL,
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-origin",
        }
        if accept_language:
            headers["accept-language"] = accept_language
        response = session.get(SENTINEL_BOOTSTRAP_URL, headers=headers, timeout=timeout_seconds)
        if getattr(response, "status_code", 0) == 200:
            match = re.search(
                r"https://chatgpt\.com/sentinel/([A-Za-z0-9_-]+)/sdk\.js",
                str(getattr(response, "text", "") or ""),
            )
            if match:
                version, sdk_url, found = match.group(1), match.group(0), True
    except Exception:
        found = False
    ttl = SDK_DISCOVERY_TTL_SECS if found else SDK_DISCOVERY_FAILURE_TTL_SECS
    with _sdk_discovery_lock:
        _sdk_discovery = (version, sdk_url, now + ttl)
    return version, sdk_url


def _chat_sdk_file(session: Any, *, accept_language: str, timeout_seconds: int) -> tuple[Path, str]:
    version, sdk_url = discover_sdk(session, accept_language=accept_language, timeout_seconds=timeout_seconds)
    cache_file = _cache_file(version)
    if cache_file.is_file() and 0 < cache_file.stat().st_size <= MAX_SDK_BYTES:
        return cache_file, sdk_url
    headers = {
        "accept": "*/*",
        "referer": CHAT_PAGE_URL,
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-origin",
    }
    if accept_language:
        headers["accept-language"] = accept_language
    response = session.get(sdk_url, headers=headers, timeout=timeout_seconds)
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"sentinel_sdk_http_{getattr(response, 'status_code', 'unknown')}")
    source = bytes(getattr(response, "content", b"") or b"")
    if not source or len(source) > MAX_SDK_BYTES:
        raise RuntimeError("invalid_sentinel_sdk_size")
    # Write atomically: concurrent chat requests may race on the first download.
    tmp = cache_file.with_name(f"sdk.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(source)
    os.replace(tmp, cache_file)
    return cache_file, sdk_url


def _timezone_fields(locale: "LocaleEnv | None") -> tuple[int, str]:
    """Offset (JS getTimezoneOffset sign) and Chrome's long zone name for Date#toString."""
    if locale is None:
        # Same fixed zone as utils.pow's legacy date string, so VM and fallback agree.
        return 300, "Eastern Standard Time"
    text = locale.date_string()
    match = re.search(r"\(([^()]*)\)\s*$", text)
    return locale.offset_min(), match.group(1) if match else ""


@dataclass(frozen=True)
class ChatPageEnv:
    """Browser environment of one chatgpt.com tab, fed into the Sentinel VM."""

    device_id: str
    user_agent: str
    sid: str
    time_origin_ms: float
    screen_width: int
    screen_height: int
    cores: int
    heap_size_limit: int
    language: str
    languages: tuple[str, ...]
    timezone_offset_min: int
    timezone_long_name: str
    page_scripts: tuple[str, ...] = ()
    data_build: str = ""
    document_keys: tuple[str, ...] = ()
    window_keys: tuple[str, ...] = ()
    navigator_probes: tuple[tuple[str, str], ...] = field(default=())

    def payload(self, *, sdk_url: str) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "user_agent": self.user_agent,
            "sid": self.sid,
            "sdk_url": sdk_url,
            "page_url": CHAT_PAGE_URL,
            "referrer": "",
            "time_origin": self.time_origin_ms,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "hardware_concurrency": self.cores,
            "js_heap_size_limit": self.heap_size_limit,
            "language": self.language,
            "languages": list(self.languages),
            "timezone_offset_min": self.timezone_offset_min,
            "timezone_long_name": self.timezone_long_name,
            "page_scripts": [src for src in self.page_scripts if src != sdk_url],
            "data_build": self.data_build,
            "document_keys": list(self.document_keys),
            "window_keys": list(self.window_keys),
            "navigator_probes": [list(item) for item in self.navigator_probes],
        }


def chat_page_env(
    fingerprint: "PowFingerprint",
    *,
    device_id: str,
    user_agent: str,
    locale: "LocaleEnv | None",
    script_sources: list[str] | tuple[str, ...],
    data_build: str,
) -> ChatPageEnv:
    """Build the VM environment from the same per-tab fingerprint the Python PoW uses."""
    from utils.pow import CHATGPT_PAGE_WINDOW_KEYS, NAVIGATOR_PROBES, WINDOW_OWN_KEYS

    language = locale.language if locale else "en-US"
    languages = tuple(locale.languages) if locale else ("en-US", "es-US", "en", "es")
    probes = [
        tuple(item.split("−", 1)) if "−" in item else (item, "null")
        for item in (*NAVIGATOR_PROBES, f"language−{language}", f"hardwareConcurrency−{fingerprint.cores}")
    ]
    offset_min, long_name = _timezone_fields(locale)
    width = int(fingerprint.screen_width or 0)
    height = int(fingerprint.screen_height or 0)
    if not width or not height:
        width, height = fingerprint.screen_sum - 1080, 1080
    return ChatPageEnv(
        device_id=device_id,
        user_agent=user_agent,
        sid=fingerprint.sid,
        time_origin_ms=fingerprint.time_origin_ms,
        screen_width=width,
        screen_height=height,
        cores=fingerprint.cores,
        heap_size_limit=fingerprint.heap_size_limit,
        language=language,
        languages=languages,
        timezone_offset_min=offset_min,
        timezone_long_name=long_name,
        page_scripts=tuple(script_sources or ()),
        data_build=data_build or "",
        document_keys=tuple(fingerprint.document_keys),
        window_keys=(*WINDOW_OWN_KEYS, *CHATGPT_PAGE_WINDOW_KEYS),
        navigator_probes=tuple((str(name), str(value)) for name, value in probes),
    )


def chat_requirements_token_via_vm(
    session: Any,
    env: ChatPageEnv,
    *,
    accept_language: str = "",
    timeout_seconds: int = 20,
) -> str:
    """``p`` for chat-requirements/prepare from the official SDK; raises on any failure."""
    timeout_seconds = max(5, min(45, int(timeout_seconds)))
    sdk_file, sdk_url = _chat_sdk_file(session, accept_language=accept_language, timeout_seconds=timeout_seconds)
    result = _run_action(
        sdk_file=sdk_file,
        payload={**env.payload(sdk_url=sdk_url), "action": "requirements"},
        timeout_seconds=timeout_seconds,
    )
    token = str(result.get("request_p") or "").strip()
    if not token.startswith("gAAAAAC"):
        raise RuntimeError("sentinel_vm_invalid_requirements")
    return token


def chat_proof_token_via_vm(
    session: Any,
    env: ChatPageEnv,
    *,
    request_p: str,
    challenge: dict[str, Any],
    accept_language: str = "",
    timeout_seconds: int = 30,
) -> tuple[str, str]:
    """Return ``(proof_token, turnstile_token)`` for chat-requirements/finalize.

    The turnstile value is empty when the SDK build does not expose its solver;
    callers then use the protocol solver. Raises on any other failure.
    """
    timeout_seconds = max(5, min(45, int(timeout_seconds)))
    sdk_file, sdk_url = _chat_sdk_file(session, accept_language=accept_language, timeout_seconds=timeout_seconds)
    result = _run_action(
        sdk_file=sdk_file,
        payload={
            **env.payload(sdk_url=sdk_url),
            "action": "solve",
            "request_p": request_p,
            "challenge": challenge,
        },
        timeout_seconds=timeout_seconds,
    )
    proof = str(result.get("final_p") or "").strip()
    if not proof.startswith("gAAAAAB"):
        raise RuntimeError("sentinel_vm_invalid_proof")
    raw_turnstile = result.get("t")
    turnstile = "" if raw_turnstile is None else str(raw_turnstile).strip()
    if len(turnstile) < MIN_TURNSTILE_TOKEN_LENGTH:
        turnstile = ""
    return proof, turnstile
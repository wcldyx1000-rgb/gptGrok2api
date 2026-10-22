"""Credential availability projection for Upstream Accounts.

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Answers one question for the schedulers: can this account execute a request, or
at least recover (refresh its access token) before executing one? An account
whose access token is expired or confirmed rejected, and which has no usable
refresh token, can do neither and must not take part in scheduling.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Literal

ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 24 * 60 * 60
AccessTokenStatus = Literal["valid", "expiring", "invalid"]
RefreshTokenStatus = Literal["valid", "missing", "invalid"]
CredentialAvailabilityStatus = Literal["usable", "recoverable", "unavailable"]


@dataclass(frozen=True)
class UpstreamCredentialAvailability:
    status: CredentialAvailabilityStatus
    access_status: AccessTokenStatus
    refresh_status: RefreshTokenStatus


def _jwt_time_claim(access_token: str, name: str) -> int | None:
    try:
        payload = str(access_token or "").split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        value = int(claims.get(name) or 0) if isinstance(claims, dict) else 0
    except Exception:
        return None
    return value if value > 0 else None


def access_token_expires_at(access_token: str) -> int | None:
    """JWT exp claim in Unix seconds; None for opaque (non-JWT) tokens."""
    return _jwt_time_claim(access_token, "exp")


def access_token_issued_at(access_token: str) -> int | None:
    """JWT iat claim in Unix seconds; None for opaque (non-JWT) tokens."""
    return _jwt_time_claim(access_token, "iat")


def project_upstream_credential_availability(
    access_token: str,
    refresh_token: str = "",
    *,
    access_confirmed_invalid: bool = False,
    refresh_confirmed_invalid: bool = False,
    now_seconds: int | None = None,
) -> UpstreamCredentialAvailability:
    token = str(access_token or "").strip()
    now = int(time.time()) if now_seconds is None else int(now_seconds)
    expires_at = access_token_expires_at(token) if token else None
    if not token or access_confirmed_invalid or (expires_at is not None and expires_at <= now):
        access: AccessTokenStatus = "invalid"
    elif expires_at is not None and expires_at - now <= ACCESS_TOKEN_REFRESH_SKEW_SECONDS:
        access = "expiring"
    else:
        access = "valid"
    refresh_text = str(refresh_token or "").strip()
    refresh: RefreshTokenStatus = (
        "missing" if not refresh_text else "invalid" if refresh_confirmed_invalid else "valid"
    )
    if access != "invalid":
        status: CredentialAvailabilityStatus = "usable"
    elif refresh == "valid":
        status = "recoverable"
    else:
        status = "unavailable"
    return UpstreamCredentialAvailability(status=status, access_status=access, refresh_status=refresh)


def oauth_session_kwargs(user_agent: str, default_impersonate: str = "chrome110") -> dict[str, object]:
    """OAuth 端点（auth.openai.com）的 curl_cffi Session 参数。

    只写 User-Agent 时 curl_cffi 会按 impersonate 自动补出该目标浏览器自己的 sec-ch-ua，
    与 UA 的 Chrome 版本对不上。这里由 UA 推导 sec-ch-ua*（utils.client_hints），
    TLS 目标保持 default_impersonate 不变：参考项目实测较新的 TLS 目标访问 OpenAI 域名
    更容易被 Cloudflare 挑战，所以只修 Client Hints 与 UA 不一致的问题。

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only) browser_session_kwargs idea.
    """
    from utils.client_hints import browser_session_kwargs

    return browser_session_kwargs(user_agent, default_impersonate)

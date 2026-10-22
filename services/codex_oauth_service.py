"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Codex 登录：给已有账号额外绑定一套 Codex 客户端 token，只给 codex 生图模型用。

/backend-api/codex/responses 只认 Codex 客户端签发的 token，网页 token 和平台 OAuth token
都会被 401 拒绝。这里走 Codex CLI 同款 OAuth + PKCE 授权码流程：

  1. start() 生成 authorize URL；用户在浏览器登录后会被重定向到
     http://localhost:1455/auth/callback?code=...（本机没有服务监听，页面打不开是正常的），
     把地址栏里的完整地址复制回来。
  2. finish() 用 code + code_verifier 换 token，按 id_token 里的邮箱找到已有账号，
     把 token 存进 codex_* 字段。不新建账号，也不改账号原有的 access_token / refresh_token。
  3. ensure_fresh() 在调用 Codex 接口前检查过期时间，临期用 refresh_token 续期。
     refresh_token 是轮换的（用一次就作废），同一账号的续期加锁串行。
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests

from services.account_credentials import oauth_session_kwargs
from services.account_service import account_service
from services.oauth_login_service import OAuthLoginError, OAuthLoginService
from services.register.openai_register import auth_base, user_agent
from services.proxy_service import proxy_settings
from utils.diagnostics import sanitize_diagnostic_text

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_OAUTH_SCOPE = "openid profile email offline_access"
CODEX_TOKEN_REFRESH_BEFORE_SECS = 10 * 60
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def _claims(token: str) -> dict[str, Any]:
    payload = account_service._decode_jwt_payload(token) if token else {}
    return payload if isinstance(payload, dict) else {}


def _token_email(id_token: str, access_token: str) -> str:
    for token in (id_token, access_token):
        claims = _claims(token)
        profile = claims.get(PROFILE_CLAIM) if isinstance(claims.get(PROFILE_CLAIM), dict) else {}
        email = str(claims.get("email") or profile.get("email") or "").strip().lower()
        if email:
            return email
    return ""


def _auth_claims(*tokens: str) -> dict[str, Any]:
    for token in tokens:
        auth = _claims(token).get(AUTH_CLAIM)
        if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
            return auth
    return {}


def codex_fields_from_tokens(tokens: dict[str, str]) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    auth = _auth_claims(id_token, access_token)
    return {
        "codex_access_token": access_token,
        "codex_refresh_token": str(tokens.get("refresh_token") or "").strip(),
        "codex_account_id": str(auth.get("chatgpt_account_id") or "").strip(),
        "codex_plan_type": str(auth.get("chatgpt_plan_type") or "").strip(),
        "codex_token_expires_at": int(_claims(access_token).get("exp") or 0),
    }


def _token_session() -> requests.Session:
    # UA、sec-ch-ua* 与 TLS 目标同源（见 oauth_session_kwargs）；换码和续期共用这个会话。
    kwargs = proxy_settings.build_session_kwargs(**oauth_session_kwargs(user_agent, "chrome"), verify=True)
    return requests.Session(**kwargs)


def _post_token(form: dict[str, str]) -> dict[str, str]:
    session = _token_session()
    try:
        response = session.post(
            f"{auth_base}/oauth/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=60,
        )
    except Exception as exc:
        raise OAuthLoginError(f"Codex 换 token 网络异常: {sanitize_diagnostic_text(exc, limit=300)}") from exc
    finally:
        session.close()
    try:
        data = response.json() if response.text else {}
    except Exception:
        data = {}
    if response.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error_description") or data.get("error") or "")
        raise OAuthLoginError(f"OpenAI 拒绝 Codex 换 token (HTTP {response.status_code}){': ' + detail if detail else ''}")
    return {key: str(data.get(key) or "").strip() for key in ("access_token", "refresh_token", "id_token")}


class CodexOAuthService(OAuthLoginService):
    def __init__(self) -> None:
        super().__init__()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._refresh_locks_guard = threading.Lock()

    def start(self, email_hint: str = "") -> dict[str, str]:
        verifier, challenge = self._generate_pkce()
        session_id = uuid.uuid4().hex
        state = f"{session_id}.{secrets.token_urlsafe(16)}"
        params = {
            "response_type": "code",
            "client_id": CODEX_OAUTH_CLIENT_ID,
            "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
            "scope": CODEX_OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "codex_cli_rs",
        }
        if str(email_hint or "").strip():
            params["login_hint"] = str(email_hint).strip()
        with self._lock:
            self._purge_expired_locked()
            self._sessions[session_id] = {
                "code_verifier": verifier,
                "state": state,
                "created_at": time.time(),
                "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
            }
        return {
            "session_id": session_id,
            "authorize_url": f"{auth_base}/oauth/authorize?{urlencode(params)}",
            "expires_in": str(self._SESSION_TTL_SECONDS),
            "redirect_uri_prefix": CODEX_OAUTH_REDIRECT_URI,
        }

    @staticmethod
    def _exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict[str, str]:
        tokens = _post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri or CODEX_OAUTH_REDIRECT_URI,
            "client_id": CODEX_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        })
        if not tokens.get("refresh_token"):
            raise OAuthLoginError("OpenAI 没有返回 refresh_token（code 可能已使用过）")
        return tokens

    def bind(self, session_id: str, callback: str, account_id: str = "") -> dict[str, Any]:
        """换 token 并绑定到已有账号；返回不含 token 的绑定结果。

        指定 account_id（稳定管理 ID）时直接绑定到该账号，账号有邮箱则要求与 token 邮箱一致；
        否则按 token 里的邮箱匹配账号。不新建账号，不改账号原有的 access_token / refresh_token。
        """
        tokens = self.finish(session_id, callback)
        email = _token_email(tokens.get("id_token", ""), tokens.get("access_token", ""))
        wanted_id = str(account_id or "").strip()
        if wanted_id:
            account = account_service.get_account_by_id(wanted_id)
            if account is None:
                raise OAuthLoginError("账号不存在")
            account_email = str(account.get("email") or "").strip().lower()
            if account_email and email and account_email != email:
                raise OAuthLoginError(f"登录的账号 {email} 与目标账号 {account_email} 不一致")
            email = email or account_email
        else:
            if not email:
                raise OAuthLoginError("Codex token 里没有邮箱，无法确定绑定到哪个账号")
            account = next(
                (item for item in account_service.list_accounts()
                 if str(item.get("email") or "").strip().lower() == email and item.get("access_token")),
                None,
            )
            if account is None:
                raise OAuthLoginError(f"账号列表里没有 {email}，请先导入这个账号再做 Codex 登录")
        fields = codex_fields_from_tokens(tokens)
        if not fields["codex_account_id"]:
            raise OAuthLoginError("Codex token 里没有 ChatGPT 账号 ID")
        fields["codex_bound_at"] = int(time.time())
        account_service.update_account(account["access_token"], fields)
        return {
            "id": str(account.get("management_id") or ""),
            "email": email,
            "codex_plan_type": fields["codex_plan_type"],
            "codex_token_expires_at": fields["codex_token_expires_at"],
        }

    def _refresh_lock(self, key: str) -> threading.Lock:
        with self._refresh_locks_guard:
            return self._refresh_locks.setdefault(key, threading.Lock())

    def ensure_fresh(self, access_token: str, *, force: bool = False) -> tuple[str, str] | None:
        """返回 (codex_access_token, chatgpt_account_id)；账号没绑定 Codex 时返回 None。"""
        account = account_service.get_account(access_token) or {}
        if not account.get("codex_access_token"):
            return None
        key = str(account.get("email") or access_token)
        with self._refresh_lock(key):
            account = account_service.get_account(access_token) or account
            expires_at = int(account.get("codex_token_expires_at") or 0)
            if force or (expires_at and expires_at - time.time() < CODEX_TOKEN_REFRESH_BEFORE_SECS):
                refresh_token = str(account.get("codex_refresh_token") or "")
                if not refresh_token:
                    raise OAuthLoginError("Codex token 已过期且没有 refresh_token，请重新做 Codex 登录")
                tokens = _post_token({
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "scope": "openid profile email",
                })
                fields = codex_fields_from_tokens({**tokens, "refresh_token": tokens.get("refresh_token") or refresh_token})
                if not fields["codex_account_id"]:
                    fields["codex_account_id"] = str(account.get("codex_account_id") or "")
                account_service.update_account(access_token, fields)
                account = {**account, **fields}
            return str(account["codex_access_token"]), str(account.get("codex_account_id") or "")


codex_oauth_service = CodexOAuthService()

from __future__ import annotations

import copy
import random
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from services.register import mail_provider
from services.register.grok_protocol import GrokProtocolClient, GrokProtocolError
from services.proxy_service import proxy_settings
from utils.timezone import TIME_FORMAT, beijing_now_str


config: dict[str, Any] = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 180,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 1,
    "threads": 1,
    "target": "grok",
    "grok": {
        "signup_flow": "xconsole",
        "max_mail_retries": 3,
        "provider": "yescaptcha",
        "api_key": "",
        "api_base": "",
        "action": "",
        "sitekey": "",
        "action_id": "",
        "base_url": "https://accounts.x.ai",
        "request_timeout": 30,
        "captcha_timeout": 180,
        "captcha_poll_interval": 3,
        "oauth_fresh_account_retry_delay": 15,
        "local_concurrency": 2,
        "local_attempt_timeout": 45,
        "local_queue_timeout": 60,
        "local_max_attempts": 3,
        "castle_timeout": 20,
        "castle_pk": "",
        "castle_sdk_url": "",
        "next_router_state_tree": "",
        "create_path": "/createTask",
        "result_path": "/getTaskResult",
        "custom_headers": {},
    },
}

register_log_sink = None
account_result_sink: Callable[[dict[str, Any]], None] | None = None
_print_lock = threading.Lock()

# A Turnstile token is accepted for about five minutes; anything older than
# this is re-solved inline rather than risking a rejected account creation.
TURNSTILE_PREFETCH_TTL_SECONDS = 240.0


def _write_console(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    with _print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{beijing_now_str(TIME_FORMAT)} {text}{suffix}")


def log(text: str, color: str = "") -> None:
    if register_log_sink:
        try:
            register_log_sink(str(text), str(color or ""))
        except Exception:
            pass
    _write_console(text, color)


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def debug_step(index: int, text: str, color: str = "") -> None:
    """Keep protocol diagnostics in the server console, outside the user progress feed."""
    _write_console(f"[任务{index}] {text}", color)


def _short_error(error: Exception, limit: int = 180) -> str:
    text = " ".join(str(error or type(error).__name__).split()) or type(error).__name__
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _persist_account_snapshot(payload: dict[str, Any]) -> bool:
    if not account_result_sink:
        return False
    try:
        account_result_sink(copy.deepcopy(payload))
        return True
    except Exception as exc:
        log(f"Grok 账号凭据暂存失败: {type(exc).__name__}: {exc}", "red")
        return False


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    lowered = str(value or "").strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _mail_config(register_proxy: str) -> dict[str, Any]:
    mail = copy.deepcopy(config.get("mail") if isinstance(config.get("mail"), dict) else {})
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    mail["api_use_register_proxy"] = use_register_proxy
    mail["proxy"] = str(register_proxy or "").strip() if use_register_proxy else ""
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("type") or "") in {"icloud_api", "icloud_local"}:
            provider["project"] = "grok"
        provider["keyword"] = "xAI"
    return mail


def _resolve_register_proxy(raw_proxy: str) -> tuple[str, str]:
    profile = proxy_settings.get_profile(proxy=str(raw_proxy or "").strip(), upstream=True)
    proxy_url = profile.proxy_url or "direct"
    task_proxy = _task_scoped_proxy(proxy_url)
    source = profile.proxy_source
    if task_proxy != proxy_url:
        source = f"{source}_task_sid"
    return task_proxy, source


def _task_scoped_proxy(proxy_url: str, *, session_id: str = "") -> str:
    candidate = str(proxy_url or "").strip()
    scheme, separator, authority = candidate.partition("://")
    if not separator or "@" not in authority:
        return candidate
    userinfo, at, destination = authority.rpartition("@")
    username, password_separator, password = userinfo.partition(":")
    sid = str(session_id or "").strip() or secrets.token_hex(4)
    rotated_username, replacements = re.subn(
        r"(?i)(-sid-)[^-:]+(?=-t-\d+(?:-|$))",
        rf"\g<1>{sid}",
        username,
        count=1,
    )
    if replacements != 1:
        return candidate
    rotated_userinfo = rotated_username
    if password_separator:
        rotated_userinfo = f"{rotated_username}:{password}"
    return f"{scheme}://{rotated_userinfo}{at}{destination}"


def _random_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
        *[secrets.choice(alphabet) for _ in range(max(0, length - 4))],
    ]
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def _random_name() -> tuple[str, str]:
    first_names = ("James", "Robert", "John", "Michael", "David", "Emma", "Olivia", "Sophia")
    last_names = ("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis")
    return secrets.choice(first_names), secrets.choice(last_names)


def _max_mail_retries(grok_config: dict[str, Any]) -> int:
    value = grok_config.get("max_mail_retries", grok_config.get("max_mail_retry", 3))
    try:
        return max(1, min(20, int(value or 3)))
    except (TypeError, ValueError):
        return 3


def _mail_retryable(error: Exception) -> bool:
    if isinstance(error, GrokProtocolError):
        return error.mail_retryable
    lowered = str(error or "").lower()
    return any(
        marker in lowered
        for marker in (
            "等待 grok 验证码超时",
            "邮箱域名",
            "邮箱已存在",
            "email domain",
            "disposable email",
            "email_in_use",
        )
    )


class _TurnstilePrefetch:
    """Solve Turnstile on a helper thread while the mailbox wait runs.

    The solver never touches the protocol session.  Its browser state is
    adopted on the registration thread once the email and password steps are
    done, so the protocol requests keep their original order.
    """

    def __init__(self, index: int, client: GrokProtocolClient, timeout: float) -> None:
        self._index = index
        self._client = client
        self._timeout = timeout
        self._cookies_before = client.session_cookie_jar()
        self._token = ""
        self._solution: dict[str, Any] = {}
        self._error: Exception | None = None
        self._solved_at = 0.0
        self._done = threading.Event()
        threading.Thread(
            target=self._run,
            daemon=True,
            name=f"grok-turnstile-prefetch-{index}",
        ).start()

    def _run(self) -> None:
        try:
            token, solution = self._client.solve_turnstile_detached(list(self._cookies_before))
            self._token = str(token or "").strip()
            self._solution = solution if isinstance(solution, dict) else {}
            self._solved_at = time.monotonic()
        except Exception as error:
            self._error = error
        finally:
            self._done.set()

    def take(self) -> str:
        """Return a fresh token already adopted into the session, or "" to solve inline."""
        if not self._done.wait(self._timeout):
            debug_step(self._index, "Turnstile 预取超时，改为当场解题", "yellow")
            return ""
        if self._error is not None or not self._token:
            reason = _short_error(self._error) if self._error is not None else "未返回 token"
            debug_step(self._index, f"Turnstile 预取失败，改为当场解题：{reason}", "yellow")
            return ""
        age = time.monotonic() - self._solved_at
        if age > TURNSTILE_PREFETCH_TTL_SECONDS:
            debug_step(self._index, f"Turnstile 预取 token 已过期（{age:.0f} 秒），重新解题", "yellow")
            return ""
        self._client.adopt_turnstile_solution(self._solution, cookies_before=self._cookies_before)
        debug_step(self._index, f"使用预取的 Turnstile token（{age:.0f} 秒前生成）")
        return self._token


def _start_turnstile_prefetch(index: int, client: GrokProtocolClient) -> _TurnstilePrefetch | None:
    grok_config = client.config if isinstance(getattr(client, "config", None), dict) else {}
    if not _truthy(grok_config.get("turnstile_prefetch"), True):
        return None
    if not callable(getattr(client, "solve_turnstile_detached", None)):
        return None
    # The same-session browser solves its own visible widget; nothing to prefetch.
    if getattr(client, "browser_bridge", None) is not None:
        return None
    try:
        timeout = max(10.0, float(grok_config.get("captcha_timeout") or 180))
    except (TypeError, ValueError):
        timeout = 180.0
    try:
        return _TurnstilePrefetch(index, client, timeout)
    except Exception as error:
        debug_step(index, f"Turnstile 预取未启动：{_short_error(error)}", "yellow")
        return None


def _authorize_live_oauth(
    index: int,
    client: GrokProtocolClient,
    account: dict[str, Any],
) -> dict[str, Any]:
    grok_config = client.config
    email = str(account.get("email") or "").strip()
    if not _truthy(grok_config.get("xai_cli_oauth_enabled"), True):
        return {}
    flow = str(grok_config.get("xai_cli_oauth_flow") or "device").strip().lower()
    browser_session_id = getattr(client, "browser_session_id", "")
    if flow == "device" and isinstance(browser_session_id, str) and browser_session_id.strip():
        from services.xai_device_oauth_protocol import XaiDeviceOAuthProtocol, XaiDeviceOAuthProtocolError

        bridge = getattr(client, "browser_bridge", None)
        step(index, f"账号创建成功，正在完成 OAuth 授权：{email}")
        protocol = XaiDeviceOAuthProtocol(
            grok_config,
            proxy=client.proxy,
            progress=lambda _stage, message: debug_step(index, f"{message}：{email}"),
        )
        authorize_kwargs = {
            "email": email,
            "sso": str(account.get("sso") or "").strip(),
            "browser_bridge": bridge,
        }
        try:
            credential = protocol.authorize_browser_session(**authorize_kwargs)
        except XaiDeviceOAuthProtocolError as error:
            if not bool(getattr(error, "retryable", False)) or str(getattr(error, "stage", "")) != "token":
                raise
            retry_delay = max(
                5,
                min(60, int(grok_config.get("oauth_fresh_account_retry_delay") or 15)),
            )
            step(index, f"新账号 OAuth 权限可能尚未生效，{retry_delay} 秒后换新 Device Code 重试：{email}", "yellow")
            time.sleep(retry_delay)
            credential = protocol.authorize_browser_session(**authorize_kwargs)
        step(index, f"OAuth 授权完成，正在验证额度：{email}", "green")
        return credential

    if flow != "pkce_reference":
        return {}

    from services.xai_reference_pkce_protocol import XaiReferencePkceProtocol

    reference_dir = str(grok_config.get("xai_cli_pkce_reference_dir") or "").strip()
    timeout = max(60, int(grok_config.get("captcha_timeout") or 180) + 60)
    protocol = XaiReferencePkceProtocol(
        reference_dir,
        proxy=client.proxy,
        timeout=timeout,
        progress=lambda _stage, message: debug_step(index, f"{message}：{email}"),
        turnstile_config=grok_config,
    )
    step(index, f"账号创建成功，正在完成 OAuth 授权：{email}")
    credential = protocol.authorize_live_session(
        email=email,
        password=str(account.get("password") or "").strip(),
        sso=str(account.get("sso") or "").strip(),
        session=client.session,
        session_cookies=(
            dict(account.get("oauth_session_cookies"))
            if isinstance(account.get("oauth_session_cookies"), dict)
            else {}
        ),
        session_cookie_jar=(
            list(account.get("oauth_cookie_jar"))
            if isinstance(account.get("oauth_cookie_jar"), list)
            else []
        ),
    )
    step(index, f"OAuth 授权完成，正在验证额度：{email}", "green")
    return credential


def _register_once(
    index: int,
    client: GrokProtocolClient,
    mail_config: dict[str, Any],
    attempt: int,
    max_attempts: int,
) -> dict[str, Any]:
    suffix = f"（第 {attempt}/{max_attempts} 次）" if max_attempts > 1 else ""
    step(index, f"获取注册邮箱{suffix}")
    mailbox = mail_provider.create_mailbox(mail_config)
    email = str(mailbox.get("address") or "").strip()
    if not email:
        mail_provider.release_mailbox(mailbox)
        raise GrokProtocolError("邮箱服务未返回 address", stage="mail", mail_retryable=True)
    step(index, f"注册邮箱已获取：{email}")
    mailbox_finalized = False
    draft: dict[str, Any] | None = None
    try:
        mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        client.bootstrap()
        client.send_email_validation_code(email)
        step(index, f"验证码已发送，等待邮件：{email}")
        turnstile_prefetch = _start_turnstile_prefetch(index, client)
        code = mail_provider.wait_for_code(mail_config, mailbox)
        if not code:
            raise GrokProtocolError("等待 Grok 验证码超时", stage="mail", mail_retryable=True)
        code_text = str(code)
        normalized_code = code_text.strip()
        client.verify_email_validation_code(email, normalized_code)
        step(index, f"邮箱验证完成，正在进行安全校验：{email}")

        given_name, family_name = _random_name()
        password = _random_password()
        client.validate_password(email, password)
        turnstile_token = turnstile_prefetch.take() if turnstile_prefetch is not None else ""
        if not turnstile_token:
            turnstile_token = client.solve_turnstile()
        draft = {
            "email": email,
            "password": password,
            "sso": "",
            "profile": {
                "given_name": given_name,
                "family_name": family_name,
                "session_state": "submitting",
            },
            "source_type": "protocol",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "submitting",
        }
        _persist_account_snapshot(draft)
        step(index, f"安全校验完成，正在创建账号：{email}")
        try:
            account = client.create_user_and_session(
                email=email,
                code=normalized_code,
                given_name=given_name,
                family_name=family_name,
                password=password,
                turnstile_token=turnstile_token,
            )
        except Exception as error:
            partial = getattr(error, "partial_result", None)
            if isinstance(partial, dict) and partial:
                persisted = _persist_account_snapshot(partial)
                try:
                    error.partial_persisted = persisted
                except Exception:
                    pass
            elif draft is not None:
                uncertain = copy.deepcopy(draft)
                reason = str(
                    getattr(error, "reason_code", "") or getattr(error, "stage", "") or type(error).__name__
                )
                pre_submit_failure = reason.startswith("prewarm_")
                uncertain["status"] = "submission_failed" if pre_submit_failure else "submission_unknown"
                uncertain["profile"]["session_state"] = "not_submitted" if pre_submit_failure else "unknown"
                uncertain["profile"]["session_reason"] = reason
                _persist_account_snapshot(uncertain)
            if bool(getattr(error, "account_created", False)):
                mail_provider.mark_mailbox_result(mailbox, success=True)
                mailbox_finalized = True
            raise
        sso = str(account.get("sso") or "").strip()
        if not sso:
            raise GrokProtocolError("Grok 注册结果缺少 sso", stage="create_account")
        mail_provider.mark_mailbox_result(mailbox, success=True)
        mailbox_finalized = True
        session_cookie_reader = getattr(client, "session_cookies", None)
        session_cookies = session_cookie_reader() if callable(session_cookie_reader) else {}
        if not isinstance(session_cookies, dict):
            session_cookies = {}
        session_cookie_jar_reader = getattr(client, "session_cookie_jar", None)
        session_cookie_jar = session_cookie_jar_reader() if callable(session_cookie_jar_reader) else []
        if not isinstance(session_cookie_jar, list):
            session_cookie_jar = []
        result = {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_session_cookies": session_cookies,
            "oauth_cookie_jar": session_cookie_jar,
            "profile": {
                "given_name": given_name,
                "family_name": family_name,
                "redirect_url": str(account.get("redirect_url") or ""),
            },
            "source_type": "protocol",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
        }
        try:
            oauth_credential = _authorize_live_oauth(index, client, result)
        except Exception as error:
            result["oauth_live_error"] = _short_error(error, limit=500)
            oauth_error = GrokProtocolError(
                f"账号已创建，但 OAuth 授权失败: {result['oauth_live_error']}",
                stage="oauth",
                account_created=True,
                partial_result=result,
            )
            raise oauth_error from error
        if oauth_credential:
            result["oauth_credential"] = oauth_credential
        return result
    except Exception as error:
        try:
            error.registration_email = email
        except Exception:
            pass
        if not mailbox_finalized:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
        raise


def worker(index: int) -> dict[str, Any]:
    started = time.monotonic()
    client: GrokProtocolClient | None = None
    last_error: Exception | None = None
    try:
        step(index, "准备注册环境")
        proxy, proxy_source = _resolve_register_proxy(str(config.get("proxy") or "").strip())
        debug_step(index, f"注册网络出口: {proxy_source}")
        grok_config = copy.deepcopy(config.get("grok") if isinstance(config.get("grok"), dict) else {})
        mail_config = _mail_config(proxy)
        max_attempts = _max_mail_retries(grok_config)
        for attempt in range(1, max_attempts + 1):
            client = GrokProtocolClient(grok_config, proxy=proxy, log=lambda message: debug_step(index, message))
            try:
                result = _register_once(index, client, mail_config, attempt, max_attempts)
                elapsed = time.monotonic() - started
                step(index, f"注册成功（{elapsed:.1f} 秒）：{str(result.get('email') or '').strip()}", "green")
                return {"ok": True, "index": index, "result": result}
            except Exception as error:
                last_error = error
                if attempt >= max_attempts or not _mail_retryable(error):
                    raise
                client.close()
                client = None
                failed_email = str(getattr(error, "registration_email", "") or "").strip()
                email_text = f"：{failed_email}" if failed_email else ""
                step(index, f"邮箱不可用{email_text}，正在更换（下一次 {attempt + 1}/{max_attempts}）", "yellow")
        raise last_error or RuntimeError("Grok 注册失败")
    except Exception as error:
        elapsed = time.monotonic() - started
        failed_email = str(getattr(error, "registration_email", "") or "").strip()
        email_text = f"：{failed_email}，原因：" if failed_email else "："
        step(index, f"注册失败（{elapsed:.1f} 秒）{email_text}{_short_error(error)}", "red")
        result = {"ok": False, "index": index, "error": str(error)}
        partial = getattr(error, "partial_result", None)
        if isinstance(partial, dict) and partial:
            result["account"] = partial
            result["account_persisted"] = bool(getattr(error, "partial_persisted", False))
        return result
    finally:
        if client is not None:
            client.close()


__all__ = ["account_result_sink", "config", "log", "register_log_sink", "step", "worker"]

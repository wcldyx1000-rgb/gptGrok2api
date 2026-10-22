"""RT 失效自动重新登录：优先用账号里存的密码（+ 2FA 密钥算 TOTP）登录，
没有密码或密码登录失败时，用账号的 Outlook 凭据收码，走 passwordless 登录换一组新 token。

移植自 chatgpt2api 衍生版的 account_relogin_service，登录流程复用注册模块里
PlatformRegistrar 的登录步骤，收码复用 OutlookTokenProvider。

- 只处理“refresh_token 已作废（或缺失且账号已异常）、且有密码或找得到 Outlook 凭据”的账号；
  RT 还能用的账号继续走正常续期。
- 密码/2FA 本身不对（密码错、密钥错、缺 2FA）且没有 Outlook 兜底时直接放弃，不按退避反复试。
- Outlook 凭据优先取账号自身字段（outlook_client_id / outlook_refresh_token），
  没有时按邮箱到邮箱配置里的 OutlookToken 邮箱池中查找。
- 失败按 RELOGIN_BACKOFF_SECS 退避，连续失败 RELOGIN_MAX_FAILURES 次后放弃；
  失败次数和最近错误写在账号上，重启不丢，前端可直接展示。
- 默认开启：可在设置里关掉 account_relogin_enabled。
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from services.config import config
from utils.log import logger

RELOGIN_MAX_FAILURES = 3
RELOGIN_BACKOFF_SECS = (30 * 60, 2 * 3600)
RELOGIN_GAP_SECS = 8.0
RELOGIN_STARTUP_DELAY_SECS = 60.0
RELOGIN_CODE_WAIT_SECS = 120.0
RELOGIN_TASK_INDEX = 0

OUTLOOK_ACCOUNT_FIELDS = ("outlook_login_email", "outlook_client_id", "outlook_refresh_token", "outlook_password")

# 密码登录里“再试也没用”的错误：密码错、2FA 密钥错、缺 2FA、账号只有验证码登录、资料未补全。
# 没有 Outlook 兜底时直接放弃，避免按退避反复提交错误密码把账号锁了。
_PASSWORD_PERMANENT_MARKERS = (
    "login_wrong_password",
    "login_bad_totp_secret",
    "login_mfa_required",
    "login_passwordless_only",
    "login_incomplete_profile",
)

# refresh_token 被吊销、过期或复用时 OAuth 刷新返回的错误标记；
# 网络错误、429、5xx、不带错误码的偶发 401 不算作废，留给正常续期重试。
# 终态以账号服务结构化判定写入的 refresh_token_invalid_at / relogin_needed_at 为准，这里只兜旧数据。
_DEAD_REFRESH_TOKEN_MARKERS = (
    "invalid_grant",
    "invalid_refresh_token",
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
    "token_revoked",
    "token has been revoked",
)


class ReloginDeferred(RuntimeError):
    """这次没法登录但不怪账号（例如注册任务被停止占住了发码通道），不计失败。"""


def _clean(value: object) -> str:
    return str(value or "").replace("﻿", "").replace(" ", " ").strip()


def _now() -> float:
    return time.time()


def _parse_ts(value: object) -> float:
    raw = _clean(value)
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mailbox_match_keys(email: str) -> list[str]:
    """精确地址 + 去掉 plus 别名后的登录邮箱。"""
    address = _clean(email).lower()
    if "@" not in address:
        return [address] if address else []
    local, _, domain = address.partition("@")
    keys = [address]
    base = f"{local.split('+', 1)[0]}@{domain}"
    if base not in keys:
        keys.append(base)
    return keys


# ── Outlook 凭据 ──────────────────────────────────────────────


def has_account_outlook_credentials(account: dict[str, Any] | None) -> bool:
    item = account or {}
    return bool(
        "@" in _clean(item.get("email"))
        and _clean(item.get("outlook_client_id"))
        and _clean(item.get("outlook_refresh_token"))
    )


def has_password_credentials(account: dict[str, Any] | None) -> bool:
    """账号上存了密码（可选带 2FA 密钥），可以不靠邮箱收码直接用密码重新登录。"""
    item = account or {}
    return bool("@" in _clean(item.get("email")) and _clean(item.get("password")))


def account_totp_secret(account: dict[str, Any] | None) -> str:
    item = account or {}
    return _clean(item.get("totp_secret") or item.get("totp") or item.get("2fa"))


def _mail_settings() -> dict[str, Any]:
    """注册中心保存的邮箱配置（含 OutlookToken 邮箱池和收信代理）。"""
    try:
        from services.register_service import register_service

        mail = register_service._mail_config_with_proxy()
    except Exception as exc:
        logger.warning({"event": "account_relogin_mail_config_failed", "error": str(exc)[:200]})
        return {}
    return mail if isinstance(mail, dict) else {}


def _outlook_entries(mail: dict[str, Any]) -> list[dict[str, Any]]:
    from services.register import mail_provider

    if not isinstance(mail.get("providers"), list):
        return []
    try:
        entries = mail_provider._entries(mail)
    except Exception:
        return []
    return [entry for entry in entries if str(entry.get("type") or "").strip().lower() == "outlook_token"]


def _pool_mailbox(email: str, mail: dict[str, Any]) -> dict[str, Any] | None:
    """按邮箱到 OutlookToken 邮箱池里找收信凭据（含加号别名）。"""
    from services.register import mail_provider

    keys = set(_mailbox_match_keys(email))
    if not keys:
        return None
    for entry in _outlook_entries(mail):
        try:
            pool = mail_provider._normalize_outlook_pool(entry.get("mailboxes") or entry.get("pool"), entry)
        except Exception:
            continue
        for credential in pool:
            candidates = {
                *_mailbox_match_keys(str(credential.get("email") or "")),
                _clean(credential.get("login_email")).lower(),
            }
            if keys & {item for item in candidates if item}:
                return {
                    "provider": "outlook_token",
                    "provider_ref": str(entry.get("provider_ref") or ""),
                    "address": _clean(email),
                    "login_email": _clean(credential.get("login_email") or credential.get("email")),
                    "password": _clean(credential.get("password")),
                    "client_id": _clean(credential.get("client_id")),
                    "refresh_token": _clean(credential.get("refresh_token")),
                }
    return None


def resolve_outlook_mailbox(account: dict[str, Any] | None, mail: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """组一个 OutlookToken 收信邮箱；找不到凭据时返回 None。"""
    item = account or {}
    email = _clean(item.get("email"))
    if "@" not in email:
        return None
    settings = _mail_settings() if mail is None else mail
    if has_account_outlook_credentials(item):
        entries = _outlook_entries(settings)
        return {
            "provider": "outlook_token",
            "provider_ref": str(entries[0].get("provider_ref") or "") if entries else "",
            "address": email,
            "login_email": _clean(item.get("outlook_login_email")) or email,
            "password": _clean(item.get("outlook_password")),
            "client_id": _clean(item.get("outlook_client_id")),
            "refresh_token": _clean(item.get("outlook_refresh_token")),
        }
    return _pool_mailbox(email, settings)


# ── 判定 ──────────────────────────────────────────────────────


def refresh_token_dead(account: dict[str, Any] | None) -> bool:
    """正常续期救不回来：账号服务已标记需重登，或 RT 已被 OAuth 明确拒绝、
    或 RT 缺失且账号已异常（后几条兜住标记机制上线前就失效的老账号）。"""
    item = account or {}
    if item.get("status") == "禁用":
        return False
    if item.get("relogin_needed_at") or item.get("refresh_token_invalid_at"):
        return True
    if _clean(item.get("source_type")).lower() == "chatgpt_web":
        # 网页会话 token 不走 OAuth 续期，失效后只能重新登录。
        return item.get("status") == "异常"
    refresh_token = _clean(item.get("refresh_token"))
    if not refresh_token:
        return item.get("status") == "异常"
    error = _clean(item.get("last_token_refresh_error")).lower()
    return bool(error) and any(marker in error for marker in _DEAD_REFRESH_TOKEN_MARKERS)


def relogin_exhausted(account: dict[str, Any] | None) -> bool:
    return int((account or {}).get("relogin_attempts") or 0) >= RELOGIN_MAX_FAILURES


def needs_relogin(account: dict[str, Any] | None, mail: dict[str, Any] | None = None) -> bool:
    if not refresh_token_dead(account):
        return False
    return has_password_credentials(account) or resolve_outlook_mailbox(account, mail) is not None


def relogin_holds(account: dict[str, Any] | None, mail: dict[str, Any] | None = None) -> bool:
    """还有机会自动重新登录的账号；“自动移除异常账号”应先放过它们。"""
    if not config.account_relogin_enabled:
        return False
    return needs_relogin(account, mail) and not relogin_exhausted(account)


def relogin_due(account: dict[str, Any] | None, now: float | None = None, mail: dict[str, Any] | None = None) -> bool:
    item = account or {}
    if relogin_exhausted(item) or not needs_relogin(item, mail):
        return False
    failures = int(item.get("relogin_attempts") or 0)
    if failures <= 0:
        return True
    backoff = RELOGIN_BACKOFF_SECS[min(failures, len(RELOGIN_BACKOFF_SECS)) - 1]
    current = now if now is not None else _now()
    return current - _parse_ts(item.get("last_relogin_at")) >= backoff


# ── 登录 ──────────────────────────────────────────────────────


def _wait_outlook_code(mailbox: dict[str, Any]) -> str | None:
    from services.register import mail_provider

    mail = _mail_settings()
    entries = _outlook_entries(mail)
    ref = str(mailbox.get("provider_ref") or "")
    entry = next((item for item in entries if item.get("provider_ref") == ref), None) or (entries[0] if entries else None)
    entry = dict(entry or {"type": "outlook_token", "provider_ref": "outlook_token:relogin", "label": "relogin"})
    conf = mail_provider._config(mail)
    conf["wait_timeout"] = max(float(conf.get("wait_timeout") or 0), RELOGIN_CODE_WAIT_SECS)
    provider = mail_provider.OutlookTokenProvider(entry, conf)
    try:
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def _apply_account_identity(registrar: Any, account: dict[str, Any], proxy: str) -> None:
    """沿用账号自己的浏览器身份（UA + TLS 目标 + 设备 ID），避免重登换出一个新设备。"""
    from services.register import openai_register

    fp = account.get("fp") if isinstance(account.get("fp"), dict) else {}
    device_id = _clean(fp.get("oai-device-id") or account.get("oai-device-id"))
    if device_id:
        registrar.device_id = device_id
    user_agent = _clean(fp.get("user-agent") or fp.get("user_agent"))
    impersonate = _clean(fp.get("impersonate"))
    if not user_agent or not impersonate:
        return
    try:
        fingerprint = openai_register._complete_browser_fingerprint(
            openai_register._browser_profile(impersonate, user_agent)
        )
    except Exception:
        return
    registrar.session.close()
    registrar.fingerprint = fingerprint
    registrar.session = openai_register.create_session(proxy, fingerprint)


def _default_login(account: dict[str, Any], mailbox: dict[str, Any]) -> dict[str, str]:
    from services.register import openai_register as reg

    email = _clean(account.get("email"))
    proxy = _clean(account.get("proxy"))
    index = RELOGIN_TASK_INDEX
    registrar = reg.PlatformRegistrar(proxy)
    try:
        _apply_account_identity(registrar, account, proxy)
        reg.step(index, f"自动重新登录 {email}")
        registrar._platform_authorize(email, index, screen_hint="login_or_signup")
        resp = None
        for attempt in range(2):
            if attempt:
                registrar._reset_auth_cookies()
                registrar._platform_authorize(email, index, screen_hint="login_or_signup")
            registrar._authorize_continue_login(email, mailbox, index)
            boundary = datetime.now(timezone.utc) - timedelta(seconds=5)
            mailbox["_received_after"] = boundary.isoformat()
            mailbox["_code_not_before"] = boundary
            registrar._send_passwordless_otp(index)
            code = _wait_outlook_code(mailbox)
            if not code:
                raise RuntimeError("等待 OpenAI 登录验证码超时")
            resp, error = reg.validate_otp(registrar.session, registrar.device_id, code, registrar.fingerprint)
            if resp is not None and resp.status_code == 200:
                break
            if attempt == 0 and registrar._is_passwordless_invalid_state(resp):
                continue
            raise RuntimeError(error or f"passwordless_validate_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        data = reg._response_json(resp)
        continue_url = _clean(data.get("continue_url")) or f"{reg.auth_base}/sign-in-with-chatgpt/platform/consent"
        if reg._url_path(continue_url) == "/about-you":
            raise RuntimeError("账号资料未完善（about-you），不自动补建资料")
        errors: list[str] = []
        tokens = reg.exchange_tokens_from_continue_url(
            registrar.session,
            registrar.device_id,
            registrar.code_verifier,
            continue_url,
            registrar.proxy,
            registrar.clearance_user_agent,
            errors,
            registrar.fingerprint,
        )
        if not tokens or not _clean(tokens.get("access_token")):
            raise RuntimeError(f"重新登录换 token 失败: {'；'.join(errors[-4:]) or '未返回 token'}")
        return {key: _clean(tokens.get(key)) for key in ("access_token", "refresh_token", "id_token") if _clean(tokens.get(key))}
    except reg.RegisterStopped as error:
        raise ReloginDeferred("注册任务已停止，发码通道暂不可用") from error
    finally:
        registrar.close()


def _default_password_login(account: dict[str, Any]) -> dict[str, str]:
    """用账号里存的密码（+ 2FA 密钥算出的 TOTP）重新登录，沿用账号自己的浏览器身份。"""
    from services.register import openai_register as reg

    email = _clean(account.get("email"))
    proxy = _clean(account.get("proxy"))
    if not proxy:
        # 账号没绑代理：和登录导入一样走注册中心的出口，而不是服务器直连。
        try:
            from services import login_import_service

            login_import_service._default_prepare()
            proxy = _clean(reg.login_egress_for_attempt())
        except Exception as exc:  # noqa: BLE001 - 取不到出口就退回直连，不挡住重登
            logger.warning({"event": "account_relogin_egress_failed", "error": str(exc)[:200]})
    registrar = reg.PlatformRegistrar(proxy)
    try:
        _apply_account_identity(registrar, account, proxy)
        reg.step(RELOGIN_TASK_INDEX, f"自动重新登录（密码{'+2FA' if account_totp_secret(account) else ''}）{email}")
        tokens = registrar.login_with_password(
            email, _clean(account.get("password")), account_totp_secret(account), RELOGIN_TASK_INDEX
        )
        if not _clean((tokens or {}).get("access_token")):
            raise RuntimeError("密码登录未返回 access_token")
        return {key: _clean(tokens.get(key)) for key in ("access_token", "refresh_token", "id_token") if _clean(tokens.get(key))}
    except reg.RegisterStopped as error:
        raise ReloginDeferred("注册任务已停止，登录通道暂不可用") from error
    finally:
        registrar.close()


def login_account(
    account: dict[str, Any],
    mailbox: dict[str, Any] | None,
    *,
    login_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] | None = None,
    password_login_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None,
) -> dict[str, str]:
    """换一组新 token：有密码先用密码（+2FA），失败且有 Outlook 凭据再退回 Outlook 收码。"""
    password_error: Exception | None = None
    if has_password_credentials(account):
        try:
            return (password_login_fn or _default_password_login)(account)
        except ReloginDeferred:
            raise
        except Exception as exc:  # noqa: BLE001
            if mailbox is None:
                raise
            password_error = exc
            logger.info({"event": "account_relogin_password_failed_fallback_outlook", "error": str(exc)[:200]})
    if mailbox is None:
        raise RuntimeError("没有可用的登录凭据（密码或 Outlook 邮箱）")
    try:
        return (login_fn or _default_login)(account, mailbox)
    except ReloginDeferred:
        raise
    except Exception as exc:  # noqa: BLE001
        if password_error is not None:
            raise RuntimeError(f"密码登录失败：{password_error}；Outlook 重登失败：{exc}") from exc
        raise


def password_failure_is_permanent(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _PASSWORD_PERMANENT_MARKERS)


# ── 回写账号 ──────────────────────────────────────────────────


def _apply_tokens(account_service: Any, access_token: str, account: dict[str, Any], tokens: dict[str, str]) -> str:
    """换上新 token；账号服务负责清重登标记、退出暂停、异常转正常。返回新的 access_token。"""
    new_token = account_service.apply_relogin_tokens(access_token, tokens, event="account_relogin")
    if _clean(account.get("source_type")).lower() == "chatgpt_web":
        # 新 token 来自 Platform OAuth，之后能按号池方式续期。
        account_service.update_account(new_token, {"source_type": "web"}, quiet=True)
    return new_token


def _record_failure(account_service: Any, access_token: str, error: str, *, exhausted: bool) -> None:
    account_service.record_relogin_failure(access_token, error)
    if exhausted:
        account_service.update_account(access_token, {"relogin_attempts": RELOGIN_MAX_FAILURES}, quiet=True)


class AccountReloginService:
    def __init__(
        self,
        login_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] = _default_login,
        sleep_fn: Callable[[float], None] | None = None,
        accounts: Any = None,
        password_login_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    ) -> None:
        self._login_fn = login_fn
        self._password_login_fn = password_login_fn
        self._sleep_fn = sleep_fn
        self._accounts = accounts
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: set[str] = set()

    @property
    def accounts(self) -> Any:
        if self._accounts is None:
            from services.account_service import account_service

            return account_service
        return self._accounts

    # ── 调度 ──────────────────────────────────────────────
    def start(self, stop_event: threading.Event | None = None) -> threading.Thread:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                args=(stop_event,),
                name="account-relogin",
                daemon=True,
            )
            self._thread.start()
            return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def wake(self) -> None:
        """有账号可能需要重新登录时调用，立即扫一轮。"""
        self._wake.set()

    def _stopped(self, stop_event: threading.Event | None) -> bool:
        return self._stop.is_set() or bool(stop_event and stop_event.is_set())

    def _wait(self, seconds: float, stop_event: threading.Event | None) -> bool:
        """等 seconds 秒或被唤醒；返回 True 表示应退出。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stopped(stop_event):
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if self._wake.wait(timeout=min(1.0, left)):
                self._wake.clear()
                return self._stopped(stop_event)
        return True

    def _loop(self, stop_event: threading.Event | None) -> None:
        if self._wait(RELOGIN_STARTUP_DELAY_SECS, stop_event):
            return
        while not self._stopped(stop_event):
            try:
                if config.account_relogin_enabled:
                    self.run_due(stop_event)
            except Exception as exc:
                logger.warning({"event": "account_relogin_loop_error", "error": str(exc)[:200]})
            if self._wait(float(config.account_relogin_interval_secs), stop_event):
                return

    def due_accounts(self) -> list[dict[str, Any]]:
        accounts = self.accounts
        now = _now()
        mail = _mail_settings()
        candidates = {_clean(item.get("access_token")): item for item in accounts.list_relogin_needed_accounts()}
        # 标记机制上线前就失效的老账号：补打标记，之后统一走 list_relogin_needed_accounts。
        for item in accounts.list_accounts():
            token = _clean(item.get("access_token"))
            if token and token not in candidates and refresh_token_dead(item):
                marked = accounts.mark_account_relogin_needed(token, _clean(item.get("last_token_refresh_error")) or "refresh token unavailable")
                candidates[token] = marked or item
        return [item for item in candidates.values() if relogin_due(item, now, mail)]

    def run_due(self, stop_event: threading.Event | None = None) -> int:
        """把到期的账号重新登录一遍，返回成功个数。"""
        if not config.account_relogin_enabled:
            return 0
        due = self.due_accounts()
        if not due:
            return 0
        workers = max(1, min(int(config.account_relogin_concurrency), len(due)))
        if workers == 1:
            done = 0
            for position, account in enumerate(due):
                if self._stopped(stop_event):
                    break
                if position:
                    self._gap(stop_event)
                done += int(self.relogin(account))
            return done
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="account-relogin") as pool:
            return sum(int(ok) for ok in pool.map(self.relogin, due))

    def _gap(self, stop_event: threading.Event | None) -> None:
        seconds = RELOGIN_GAP_SECS + random.uniform(0.4, 1.6)
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
        else:
            self._wait(seconds, stop_event)

    # ── 单个账号 ──────────────────────────────────────────
    def relogin(self, account: dict[str, Any]) -> bool:
        accounts = self.accounts
        access_token = _clean(account.get("access_token"))
        current = accounts.get_account(access_token) if access_token else None
        mail = _mail_settings()
        if not current or not relogin_due(current, mail=mail):
            return False
        mailbox = resolve_outlook_mailbox(current, mail)
        if mailbox is None and not has_password_credentials(current):
            return False
        with self._lock:
            if access_token in self._running:
                return False
            self._running.add(access_token)
        account_id = _clean(current.get("management_id") or current.get("email"))
        started = time.perf_counter()
        try:
            tokens = login_account(
                current, mailbox, login_fn=self._login_fn, password_login_fn=self._password_login_fn
            )
            if not _clean((tokens or {}).get("access_token")):
                raise RuntimeError("重新登录未返回 access_token")
            _apply_tokens(accounts, access_token, current, tokens)
        except ReloginDeferred as exc:
            logger.info({"event": "account_relogin_deferred", "account_id": account_id, "reason": str(exc)[:200]})
            return False
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            failures = int(current.get("relogin_attempts") or 0) + 1
            # 号已被停用，或只有密码这一条路且密码/2FA 本身不对：不再退避重试。
            deactivated = (
                "account_deactivated" in message
                or "deleted or deactivated" in message.lower()
                or (mailbox is None and password_failure_is_permanent(message))
            )
            if deactivated:
                failures = RELOGIN_MAX_FAILURES
            try:
                _record_failure(accounts, access_token, message, exhausted=deactivated)
            except Exception as record_error:
                logger.warning({"event": "account_relogin_record_failed", "error": str(record_error)[:200]})
            logger.warning({
                "event": "account_relogin_failed",
                "account_id": account_id,
                "failures": failures,
                "error": message[:300],
            })
            return False
        finally:
            with self._lock:
                self._running.discard(access_token)
        logger.info({
            "event": "account_relogin_ok",
            "account_id": account_id,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })
        return True


account_relogin_service = AccountReloginService()

"""已有账号登录导入与入池验证。

- 登录导入：粘贴 `邮箱|密码|2FA`，逐个用密码（+ 本地 TOTP）登录拿 token，连同浏览器身份写入号池。
  出口沿用注册中心的代理设置；任务进度和日志在内存里，前端轮询查看。
- 重新激活：号池里已有账号用 Outlook 邮箱无密码登录换新 token（复用自动重登的登录实现）。
- 入池验证（verify_on_import）：入池前用 refresh_token 换一次 token，入池后同步额度；不可用的不留在号池。
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from utils.log import logger

GAP_SECONDS = 4.0
LOGIN_MAX_ATTEMPTS = 2
JOB_LOG_LIMIT = 300
JOB_KEEP = 10
USABLE_STATUSES = {"正常", "限流"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _split(line: str) -> list[str]:
    for sep in ("----", "|", "\t", ","):
        if sep in line:
            return line.split(sep)
    return line.split()


def parse_login_lines(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """解析 `邮箱|密码|2FA`（也接受 ----、制表符、逗号、空白分隔）；返回（可用行、无法识别的行）。"""
    rows: list[dict[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in _split(line)]
        if len(parts) < 2 or "@" not in parts[0] or not parts[1]:
            # 原始行可能含密码，只回显开头便于定位。
            bad.append(line[:24] + ("…" if len(line) > 24 else ""))
            continue
        email = parts[0].lower()
        if email in seen:
            continue
        seen.add(email)
        rows.append({"email": email, "password": parts[1], "totp": parts[2] if len(parts) > 2 else ""})
    return rows, bad


def _accounts() -> Any:
    from services.account_service import account_service

    return account_service


def verify_account_payload(
    payload: dict[str, Any],
    *,
    accounts: Any = None,
) -> dict[str, Any]:
    """入池前验证一个账号：有 refresh_token 时先换一次 token，入池后同步额度，不可用就移出号池。"""
    accounts = accounts or _accounts()
    item = dict(payload)
    email = _clean(item.get("email"))
    result: dict[str, Any] = {"email": email, "usable": False, "reason": "", "status": ""}
    refresh_token = _clean(item.get("refresh_token"))
    if refresh_token:
        try:
            tokens = accounts._request_access_token_refresh(refresh_token, item)
        except Exception as exc:
            result["reason"] = f"refresh_failed: {str(exc)[:200]}"
            return result
        for key in ("access_token", "refresh_token", "id_token"):
            value = _clean((tokens or {}).get(key))
            if value:
                item[key] = value
    access_token = _clean(item.get("access_token"))
    if not access_token:
        result["reason"] = "missing_access_token"
        return result
    accounts.add_account_items([item], return_items=False)
    try:
        accounts.fetch_remote_info(access_token, event="import_verify", remove_invalid=False)
    except Exception as exc:
        result["reason"] = f"remote_check_failed: {str(exc)[:200]}"
    latest = accounts.get_account(access_token) or {}
    status = _clean(latest.get("status"))
    result["status"] = status
    result["usable"] = status in USABLE_STATUSES and not result["reason"]
    if not result["usable"]:
        result["reason"] = result["reason"] or f"status_{status or 'unknown'}"
        accounts.delete_accounts([_clean(latest.get("access_token")) or access_token], return_items=False)
    else:
        result["access_token"] = _clean(latest.get("access_token")) or access_token
    return result


def verify_and_import(payloads: list[dict[str, Any]], *, accounts: Any = None) -> dict[str, Any]:
    accounts = accounts or _accounts()
    results = [verify_account_payload(payload, accounts=accounts) for payload in payloads if isinstance(payload, dict)]
    usable = [item for item in results if item["usable"]]
    rejected = [
        {"email": item["email"], "reason": item["reason"], "status": item["status"]}
        for item in results
        if not item["usable"]
    ]
    return {
        "verified": len(results),
        "added": len(usable),
        "tokens": [item["access_token"] for item in usable],
        "rejected": rejected,
    }


def _default_login(row: dict[str, str], proxy: str, index: int) -> dict[str, Any]:
    from services.register import openai_register

    return openai_register.login_existing_account(row["email"], row["password"], row["totp"], proxy=proxy, index=index)


def _default_pick_proxy(failed: set[str]) -> str:
    from services.register import openai_register

    return openai_register.login_egress_for_attempt(failed)


def _default_prepare() -> None:
    """让注册模块带上当前注册中心的代理与邮箱设置（登录导入与注册共用出口）。"""
    from services.register_service import register_service

    register_service._sync_backend_config("openai")


def _default_log_scope(sink: Callable[[str, str], None]):
    from services.register import openai_register

    return openai_register.thread_log_sink(sink)


def _is_proxy_error(error: Exception) -> bool:
    from services.register import openai_register

    return openai_register._is_proxy_transport_error(error)


class LoginImportService:
    def __init__(
        self,
        *,
        login_fn: Callable[[dict[str, str], str, int], dict[str, Any]] | None = None,
        pick_proxy_fn: Callable[[set[str]], str] | None = None,
        prepare_fn: Callable[[], None] | None = None,
        log_scope_fn: Callable[[Callable[[str, str], None]], Any] | None = None,
        accounts: Any = None,
        gap_seconds: float = GAP_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._active = ""
        self._login_fn = login_fn or _default_login
        self._pick_proxy_fn = pick_proxy_fn or _default_pick_proxy
        self._prepare_fn = prepare_fn or _default_prepare
        self._log_scope_fn = log_scope_fn or _default_log_scope
        self._accounts = accounts
        self._gap_seconds = gap_seconds
        self._stop_events: dict[str, threading.Event] = {}

    @property
    def accounts(self) -> Any:
        return self._accounts or _accounts()

    # ── 任务状态 ────────────────────────────────────────────
    def get(self, job_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id or self._active)
            return copy.deepcopy(job) if job else None

    def stop(self, job_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            target = job_id or self._active
            event = self._stop_events.get(target)
        if event is not None:
            event.set()
        return self.get(target)

    def _update(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(updates)
                job["updated_at"] = _now()

    def _log(self, job_id: str, text: str, level: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["logs"].append({"time": _now(), "text": str(text)[:500], "level": level or "info"})
            del job["logs"][:-JOB_LOG_LIMIT]

    def _add_result(self, job_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["results"].append(entry)

    def start(self, text: str, *, verify: bool = True, background: bool = True) -> dict[str, Any]:
        rows, bad = parse_login_lines(text)
        if not rows:
            raise ValueError("没有可用的行，格式：邮箱|密码|2FA（2FA 可留空）")
        with self._lock:
            active = self._jobs.get(self._active)
            if active and not active.get("terminal"):
                raise ValueError("已有登录导入任务在运行")
            job_id = uuid.uuid4().hex
            self._active = job_id
            self._order.append(job_id)
            for old in self._order[:-JOB_KEEP]:
                self._jobs.pop(old, None)
                self._stop_events.pop(old, None)
            del self._order[:-JOB_KEEP]
            self._stop_events[job_id] = threading.Event()
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "terminal": False,
                "verify": bool(verify),
                "total": len(rows),
                "done": 0,
                "ok": 0,
                "pool_ok": 0,
                "fail": 0,
                "current": "",
                "skipped_lines": bad,
                "results": [],
                "logs": [],
                "created_at": _now(),
                "updated_at": _now(),
            }
        if background:
            threading.Thread(target=self._run, args=(job_id, rows), name="login-import", daemon=True).start()
        else:
            self._run(job_id, rows)
        return self.get(job_id) or {}

    # ── 执行 ────────────────────────────────────────────────
    def _login_with_retry(self, job_id: str, row: dict[str, str], index: int) -> dict[str, Any]:
        failed: set[str] = set()
        last_error: Exception | None = None
        for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
            proxy = self._pick_proxy_fn(failed)
            try:
                return self._login_fn(row, proxy, index)
            except Exception as error:
                last_error = error
                if attempt >= LOGIN_MAX_ATTEMPTS or not proxy or not _is_proxy_error(error):
                    raise
                failed.add(proxy)
                self._log(job_id, f"{row['email']} 代理连接失败，换节点重试", "yellow")
        raise last_error or RuntimeError("login failed")

    def _run(self, job_id: str, rows: list[dict[str, str]]) -> None:
        stop_event = self._stop_events.get(job_id) or threading.Event()
        verify = bool((self.get(job_id) or {}).get("verify"))
        ok = pool_ok = fail = 0
        try:
            try:
                self._prepare_fn()
            except Exception as exc:
                logger.warning({"event": "login_import_prepare_failed", "error": str(exc)[:200]})
            with self._log_scope_fn(lambda text, color="": self._log(job_id, text, color)):
                for index, row in enumerate(rows, start=1):
                    if stop_event.is_set():
                        self._log(job_id, "已停止", "yellow")
                        break
                    email = row["email"]
                    self._update(job_id, current=email)
                    entry: dict[str, Any] = {"email": email, "ok": False, "usable": None, "error": ""}
                    try:
                        result = self._login_with_retry(job_id, row, index)
                        ok += 1
                        entry["ok"] = True
                        if verify:
                            checked = verify_account_payload(result, accounts=self.accounts)
                            entry["usable"] = checked["usable"]
                            if checked["usable"]:
                                pool_ok += 1
                                self._log(job_id, f"{email} 登录成功，入池验证通过", "green")
                            else:
                                entry["error"] = checked["reason"]
                                self._log(job_id, f"{email} 登录成功，但入池验证未通过：{checked['reason']}", "yellow")
                        else:
                            self.accounts.add_account_items([result], return_items=False)
                            pool_ok += 1
                            entry["usable"] = True
                            self._log(job_id, f"{email} 登录成功，已写入号池", "green")
                    except Exception as error:
                        fail += 1
                        entry["error"] = str(error)[:300]
                        self._log(job_id, f"{email} 登录失败：{str(error)[:200]}", "red")
                    self._add_result(job_id, entry)
                    self._update(job_id, done=index, ok=ok, pool_ok=pool_ok, fail=fail)
                    if index < len(rows) and stop_event.wait(self._gap_seconds):
                        self._log(job_id, "已停止", "yellow")
                        break
        except Exception as exc:
            logger.exception({"event": "login_import_crashed", "error": str(exc)[:200]})
            self._log(job_id, f"任务异常结束：{str(exc)[:200]}", "red")
        finally:
            stopped = stop_event.is_set()
            self._update(
                job_id,
                status="stopped" if stopped else "completed",
                terminal=True,
                current="",
                result_message=f"完成：登录成功 {ok}，入池可用 {pool_ok}，失败 {fail}",
            )

    # ── 重新激活：号池已有账号用 Outlook 无密码登录换新 token ─────
    def reactivate(self, emails: list[str]) -> dict[str, Any]:
        from services import account_relogin_service as relogin

        accounts = self.accounts
        wanted: list[str] = []
        for raw in emails:
            email = _clean(raw).lower()
            if email and email not in wanted:
                wanted.append(email)
        by_email = {
            _clean(item.get("email")).lower(): item
            for item in accounts.list_accounts()
            if _clean(item.get("email"))
        }
        results: list[dict[str, Any]] = []
        for index, email in enumerate(wanted):
            account = by_email.get(email)
            entry: dict[str, Any] = {"email": email, "ok": False, "error": ""}
            try:
                if account is None:
                    raise RuntimeError("号池里没有这个邮箱的账号")
                mailbox = relogin.resolve_outlook_mailbox(account)
                if mailbox is None:
                    raise RuntimeError("没有找到这个账号的 Outlook 邮箱凭据")
                tokens = relogin._default_login(account, mailbox)
                if not _clean((tokens or {}).get("access_token")):
                    raise RuntimeError("重新登录未返回 access_token")
                relogin._apply_tokens(accounts, _clean(account.get("access_token")), account, tokens)
                entry["ok"] = True
            except Exception as error:
                entry["error"] = str(error)[:300]
            results.append(entry)
            if index < len(wanted) - 1:
                time.sleep(self._gap_seconds)
        return {
            "ok": sum(1 for item in results if item["ok"]),
            "fail": sum(1 for item in results if not item["ok"]),
            "results": results,
        }


login_import_service = LoginImportService()

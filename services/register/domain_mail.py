"""自建域名邮箱：Cloudflare Email Routing catch-all 转发 + IMAP 收码。

- 一键配置（cf_setup）：对每个域名开启 Email Routing（自动加 MX/SPF），把收信邮箱加为目标地址，
  再把 catch-all 规则设为转发到它。目标邮箱首次需要点一下 Cloudflare 发来的验证邮件。
  令牌权限：Zone:Read、DNS:Edit、Email Routing Rules:Edit（区域）+ Email Routing Addresses:Edit（账户）。
- 注册时：在域名之间轮换，每个域名每天有上限；地址按真人习惯生成，不重复使用。
- 收码：所有地址的信都转发到同一个收信邮箱，用 IMAP 读取，按原收件人区分（转发保留原 To）。
"""
from __future__ import annotations

import imaplib
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR
from services.json_file import read_json_object, write_json_file

CF_API = "https://api.cloudflare.com/client/v4"
USAGE_FILE = DATA_DIR / "domain_mail_usage.json"
ISSUED_KEEP = 20000
DAYS_KEEP = 7
_lock = threading.Lock()

_FIRST = (
    "james", "john", "robert", "michael", "david", "william", "richard", "joseph", "thomas", "daniel",
    "matthew", "anthony", "mark", "steven", "paul", "andrew", "joshua", "kevin", "brian", "ryan",
    "jacob", "nicholas", "eric", "jonathan", "justin", "brandon", "samuel", "benjamin", "nathan", "adam",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "susan", "jessica", "sarah", "karen", "emily",
    "michelle", "amanda", "melissa", "stephanie", "rebecca", "laura", "hannah", "olivia", "emma", "grace",
    "chloe", "megan", "rachel", "lauren", "sophia", "natalie", "victoria", "julia", "kelly", "amy",
)
_LAST = (
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "wilson", "anderson",
    "taylor", "thomas", "moore", "jackson", "martin", "lee", "thompson", "white", "harris", "clark",
    "lewis", "robinson", "walker", "young", "allen", "king", "wright", "scott", "hill", "green",
    "adams", "baker", "nelson", "carter", "mitchell", "roberts", "turner", "phillips", "campbell", "parker",
    "evans", "edwards", "collins", "stewart", "morris", "murphy", "cook", "rogers", "morgan", "cooper",
)
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def parse_domains(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").replace(",", "\n").splitlines()
    seen: list[str] = []
    for item in items:
        domain = str(item or "").strip().lower().lstrip("@").strip(".")
        if domain and _DOMAIN_RE.match(domain) and domain not in seen:
            seen.append(domain)
    return seen


def human_local_part(rng: random.Random | None = None) -> str:
    """像真人起的邮箱名：名.姓、名姓+数字、名_姓+年份、名首字母+姓 等。"""
    rng = rng or random.Random()
    first, last = rng.choice(_FIRST), rng.choice(_LAST)
    year = rng.randint(1975, 2004)
    styles = (
        f"{first}.{last}",
        f"{first}{last}{rng.randint(1, 99)}",
        f"{first}_{last}{year % 100:02d}",
        f"{first[0]}{last}{rng.randint(10, 999)}",
        f"{first}.{last}{year}",
        f"{last}.{first}{rng.randint(1, 9)}",
    )
    return rng.choice(styles)


# ── 用量：每个域名每天上限，地址不重复 ───────────────────────────
class DomainMailExhausted(RuntimeError):
    """今天所有域名都已到上限。"""


def _today(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return (current + timedelta(hours=8)).strftime("%Y-%m-%d")


def _load(path: Path) -> dict[str, Any]:
    data = read_json_object(path, name=path.name) if path.exists() else {}
    if not isinstance(data.get("days"), dict):
        data["days"] = {}
    if not isinstance(data.get("issued"), list):
        data["issued"] = []
    return data


def issue_address(
    domains: list[str],
    daily_limit: int,
    *,
    usage_file: Path | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """挑今天用得最少、未到上限的域名，生成一个从没发过的地址并记下来。"""
    if not domains:
        raise RuntimeError("自建域名邮箱没有填域名")
    path = usage_file or USAGE_FILE
    rng = rng or random.Random()
    current = now or datetime.now(timezone.utc)
    with _lock:
        data = _load(path)
        today = _today(current)
        cutoff = _today(current - timedelta(days=DAYS_KEEP))
        for key in [key for key in data["days"] if key < cutoff]:
            data["days"].pop(key, None)
        day = data["days"].setdefault(today, {})
        candidates = [domain for domain in domains if daily_limit <= 0 or int(day.get(domain) or 0) < daily_limit]
        if not candidates:
            raise DomainMailExhausted(f"自建域名邮箱今天每个域名都已用满 {daily_limit} 个（暂无可用邮箱），明天再试或增加域名")
        domain = min(candidates, key=lambda item: (int(day.get(item) or 0), rng.random()))
        issued = set(data["issued"])
        address = ""
        for _ in range(50):
            candidate = f"{human_local_part(rng)}@{domain}"
            if candidate not in issued:
                address = candidate
                break
        if not address:
            address = f"{human_local_part(rng)}{int(time.time()) % 10000}@{domain}"
        day[domain] = int(day.get(domain) or 0) + 1
        data["issued"] = (data["issued"] + [address])[-ISSUED_KEEP:]
        write_json_file(path, data, backup=False)
        return address


def usage_today(*, usage_file: Path | None = None) -> dict[str, int]:
    with _lock:
        return {key: int(value or 0) for key, value in _load(usage_file or USAGE_FILE)["days"].get(_today(), {}).items()}


# ── IMAP 收码 ────────────────────────────────────────────────
def _decode(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def parse_imap_message(raw: bytes) -> dict[str, Any]:
    message = message_from_bytes(raw, policy=policy.default)
    try:
        received = parsedate_to_datetime(str(message.get("Date") or ""))
        if received is not None and received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
    except Exception:
        received = None
    plain: list[str] = []
    html: list[str] = []
    for part in (message.walk() if message.is_multipart() else [message]):
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not payload:
            continue
        (html if part.get_content_type() == "text/html" else plain).append(str(payload))
    return {
        "message_id": _decode(message.get("Message-ID")),
        "subject": _decode(message.get("Subject")),
        "sender": _decode(message.get("From")),
        "to": _decode(message.get("To")),
        "delivered_to": _decode(message.get("Delivered-To")),
        "x_forwarded_to": _decode(message.get("X-Forwarded-To")),
        "x_original_to": _decode(message.get("X-Original-To")),
        "text_content": "\n".join(plain).strip(),
        "html_content": "\n".join(html).strip(),
        "received_at": received,
        "raw": None,
    }


def fetch_imap_messages(
    host: str,
    user: str,
    password: str,
    *,
    port: int = 993,
    limit: int = 20,
    timeout: float = 20.0,
    imap_factory: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """读取收信邮箱 INBOX 最近 `limit` 封邮件，最新在前。"""
    if not host or not user or not password:
        raise RuntimeError("自建域名邮箱缺少 IMAP 主机、账号或密码")
    factory = imap_factory or imaplib.IMAP4_SSL
    imap = factory(host, port, timeout=timeout)
    try:
        imap.login(user, password)
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("自建域名邮箱 IMAP select INBOX 失败")
        status, data = imap.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("自建域名邮箱 IMAP search 失败")
        uids = (data[0] or b"").split() if data else []
        messages: list[dict[str, Any]] = []
        for uid in reversed(uids[-max(1, limit):]):
            status, fetched = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not fetched:
                continue
            for item in fetched:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                    parsed = parse_imap_message(bytes(item[1]))
                    parsed["uid"] = uid.decode() if isinstance(uid, bytes) else str(uid)
                    messages.append(parsed)
                    break
        return messages
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def message_addressed_to(message: dict[str, Any], address: str) -> bool:
    """收信邮箱里混着所有地址的邮件，必须严格按原收件人匹配。"""
    target = str(address or "").strip().lower()
    if not target:
        return False
    for key in ("to", "x_original_to", "x_forwarded_to", "delivered_to"):
        value = str(message.get(key) or "").lower()
        if re.search(rf"(?<![a-z0-9._%+-]){re.escape(target)}(?![a-z0-9.-])", value):
            return True
    return False


# ── Cloudflare 一键配置 ──────────────────────────────────────
class CloudflareError(RuntimeError):
    pass


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return f"{text[:4]}***{text[-2:]}" if len(text) > 8 else "***"


def _cf(session: Any, token: str, method: str, path: str, body: Any = None) -> Any:
    response = session.request(
        method,
        f"{CF_API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    try:
        data = response.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data.get("success"):
        errors = data.get("errors") if isinstance(data, dict) else None
        message = "; ".join(str(item.get("message") if isinstance(item, dict) else item) for item in (errors or []) if item)
        message = message or f"HTTP {getattr(response, 'status_code', 'unknown')}"
        if getattr(response, "status_code", 0) in (401, 403):
            message += "（令牌无效或权限不够：需要 Zone:Read、DNS:Edit、Email Routing Rules:Edit、Email Routing Addresses:Edit）"
        # 错误信息里不能带出令牌。
        raise CloudflareError(message.replace(token, mask_secret(token))[:300])
    return data.get("result")


def cf_setup(token: str, domains: list[str], forward_to: str, *, session_factory: Callable[[], Any] | None = None) -> list[dict[str, Any]]:
    """为每个域名开启 Email Routing，并把所有地址转发到 forward_to；返回每个域名的结果。"""
    token = str(token or "").strip()
    forward_to = str(forward_to or "").strip().lower()
    if not token:
        raise CloudflareError("没有填 Cloudflare API 令牌")
    if "@" not in forward_to:
        raise CloudflareError("没有填「转发到」的收信邮箱")
    if not domains:
        raise CloudflareError("没有填域名")
    if session_factory is None:
        from curl_cffi import requests

        session = requests.Session(impersonate="chrome142")
    else:
        session = session_factory()
    results: list[dict[str, Any]] = []
    try:
        account_states: dict[str, str] = {}
        for domain in domains:
            item: dict[str, Any] = {"domain": domain, "ok": False, "verified": False, "steps": [], "error": ""}
            try:
                zones = _cf(session, token, "GET", f"/zones?name={domain}") or []
                if not zones:
                    raise CloudflareError("这个域名不在当前 Cloudflare 账户里（需要先把域名接入 Cloudflare）")
                zone_id = zones[0]["id"]
                account_id = str((zones[0].get("account") or {}).get("id") or "")
                try:
                    _cf(session, token, "POST", f"/zones/{zone_id}/email/routing/dns")
                except CloudflareError as error:
                    if "already" not in str(error).lower():
                        raise
                item["steps"].append("已开启 Email Routing")
                if account_id and account_id not in account_states:
                    addresses = _cf(session, token, "GET", f"/accounts/{account_id}/email/routing/addresses") or []
                    existing = next((row for row in addresses if str(row.get("email") or "").lower() == forward_to), None)
                    if existing is None:
                        existing = _cf(session, token, "POST", f"/accounts/{account_id}/email/routing/addresses", {"email": forward_to}) or {}
                    account_states[account_id] = "verified" if existing.get("verified") else "pending"
                item["verified"] = account_states.get(account_id) == "verified"
                item["steps"].append("目标邮箱已验证" if item["verified"] else f"请到 {forward_to} 点击 Cloudflare 的验证邮件")
                _cf(session, token, "PUT", f"/zones/{zone_id}/email/routing/rules/catch_all", {
                    "enabled": True,
                    "matchers": [{"type": "all"}],
                    "actions": [{"type": "forward", "value": [forward_to]}],
                })
                item["steps"].append("所有地址都转发到收信邮箱")
                item["ok"] = True
            except CloudflareError as error:
                item["error"] = str(error)
            except Exception as error:
                item["error"] = str(error).replace(token, mask_secret(token))[:300]
            results.append(item)
    finally:
        try:
            session.close()
        except Exception:
            pass
    return results

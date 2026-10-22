"""搜索执行：占用对话账号并发槽位，上游鉴权被拒时换号重试，整体受同一个截止时间约束。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from services.account_service import account_service
from services.image_failure import classify_image_exception
from services.openai_backend_api import OpenAIBackendAPI, SEARCH_TIMEOUT_SECS

MAX_AUTH_SWITCHES = 1


def _is_account_auth_failure(exc: BaseException) -> bool:
    return classify_image_exception(exc).capability == "auth"


def execute_search(
    query: str,
    *,
    backend_factory: Callable[[str], Any] = OpenAIBackendAPI,
    timeout_secs: float = SEARCH_TIMEOUT_SECS,
    max_auth_switches: int = MAX_AUTH_SWITCHES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """执行一次搜索，返回 (搜索结果, 实际使用的账号快照)。

    账号被上游鉴权拒绝（401/登录态失效）时，把它排除后换另一个对话账号再试，最多换
    ``max_auth_switches`` 次；账号的待核验标记由后端的搜索鉴权失败处理负责。其他错误原样抛出。
    没有任何账号时保持匿名链路。
    """
    deadline = time.monotonic() + max(0.001, float(timeout_secs))
    attempted: set[str] = set()
    switches = 0
    last_auth_error: BaseException | None = None

    while True:
        with account_service.text_slot(excluded_tokens=set(attempted)) as token:
            if not token and account_service.list_tokens():
                if last_auth_error is not None:
                    raise last_auth_error
                raise RuntimeError("no available text account")  # 有账号但都在忙/不可用
            if token:
                attempted.add(token)
            account = (account_service.get_account(token) or {}) if token else {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("web search timed out")
            try:
                with backend_factory(token) as backend:
                    result = backend.search(query, timeout_secs=remaining)
            except Exception as exc:
                if not token or not _is_account_auth_failure(exc):
                    raise
                last_auth_error = exc
                switches += 1
                if switches > max_auth_switches:
                    raise
                continue
            account_service.mark_text_used(token)
            return dict(result), dict(account) if isinstance(account, dict) else {}

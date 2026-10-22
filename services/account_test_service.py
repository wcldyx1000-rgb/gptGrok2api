"""Ported from yukkcat/chatgpt2api (AGPL-3.0-only).

Real capability test for one pool account: send a chat prompt or generate an
image through the account's own token, report the content and image quota
before/after. The lightweight remote-info probe stays in api/accounts.py.
"""
from __future__ import annotations

import time
from typing import Literal, cast

from services.account_processing import account_processing_slot
from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import (
    ConversationRequest,
    collect_image_outputs,
    conversation_events,
    stream_codex_image_outputs,
    stream_image_outputs,
)
from utils.helper import build_chat_image_markdown_content, is_codex_image_model


def _account_label(account: dict[str, object], account_id: str) -> str:
    return str(account.get("email") or account.get("name") or account_id).strip()


def _quota_label(account: dict[str, object]) -> str:
    if account_service.is_unlimited_image_quota_account(account):
        return "无限"
    if bool(account.get("image_quota_unknown")):
        return "未知"
    return str(max(0, int(account.get("quota") or 0)))


def _safe_message(exc: Exception, account: dict[str, object]) -> str:
    message = str(exc or "").strip() or exc.__class__.__name__
    for key in ("access_token", "refresh_token", "id_token", "codex_access_token", "codex_refresh_token"):
        secret = str(account.get(key) or "").strip()
        if secret:
            message = message.replace(secret, "***")
    return message[:500]


class AccountTestService:
    """Execute one upstream capability test against one selected account."""

    def execute(self, payload: dict[str, object]) -> dict[str, object]:
        account_id = str(payload.get("account_id") or "").strip()
        mode = cast(Literal["chat", "image"], "image" if str(payload.get("mode")) == "image" else "chat")
        model = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        started = time.perf_counter()
        account = account_service.get_account_by_id(account_id)
        if account is None:
            raise LookupError("account not found")

        label = _account_label(account, account_id)
        quota_before = _quota_label(account)
        base = {
            "account_id": account_id,
            "account_label": label,
            "mode": mode,
            "mode_label": "对话" if mode == "chat" else "画图",
            "model": model,
        }
        try:
            if mode == "image":
                images, updated = self._execute_image(account, model=model, prompt=prompt)
                content = build_chat_image_markdown_content({"data": [{"url": image} for image in images]})
                quota_after = _quota_label(updated)
                deducted = (
                    quota_before.isdigit() and quota_after.isdigit() and int(quota_after) < int(quota_before)
                )
            else:
                content = self._execute_chat(account, model=model, prompt=prompt)
                quota_after, deducted = quota_before, False
        except Exception as exc:
            return {
                **base,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "failed",
                "status_label": "测试失败",
                "tone": "danger",
                "content": "",
                "quota_before_label": quota_before,
                "quota_after_label": quota_before,
                "quota_deducted": False,
                "error_code": type(exc).__name__,
                "error_message": _safe_message(exc, account) or "账号测试失败",
            }
        return {
            **base,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "status": "success",
            "status_label": "测试通过",
            "tone": "success",
            "content": content,
            "quota_before_label": quota_before,
            "quota_after_label": quota_after,
            "quota_deducted": deducted,
            "error_code": "",
            "error_message": "",
        }

    @staticmethod
    def _active_token(account: dict[str, object], event: str) -> str:
        access_token = str(account.get("access_token") or "").strip()
        return account_service.refresh_access_token(access_token, event=event) or access_token

    @classmethod
    def _execute_chat(cls, account: dict[str, object], *, model: str, prompt: str) -> str:
        active_token = cls._active_token(account, "account_test_chat")
        backend: OpenAIBackendAPI | None = None
        with account_processing_slot():
            try:
                backend = OpenAIBackendAPI(active_token)
                text = "".join(
                    str(event.get("delta") or "")
                    for event in conversation_events(backend, model=model, prompt=prompt)
                    if event.get("type") == "conversation.delta"
                ).strip()
            finally:
                if backend is not None:
                    backend.close()
        account_service.mark_text_used(active_token)
        return text

    @classmethod
    def _execute_image(
        cls, account: dict[str, object], *, model: str, prompt: str,
    ) -> tuple[list[str], dict[str, object]]:
        active_token = cls._active_token(account, "account_test_image")
        backend: OpenAIBackendAPI | None = None
        with account_processing_slot():
            try:
                backend = OpenAIBackendAPI(active_token)
                request = ConversationRequest(model=model, prompt=prompt, response_format="b64_json")
                stream = stream_codex_image_outputs if is_codex_image_model(model) else stream_image_outputs
                result = collect_image_outputs(stream(backend, request))
            finally:
                if backend is not None:
                    backend.close()

        images: list[str] = []
        for item in result.get("data", []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url:
                images.append(url)
                continue
            encoded = str(item.get("b64_json") or "").strip()
            if encoded:
                images.append(f"data:image/png;base64,{encoded}")
        if not images:
            raise RuntimeError(str(result.get("message") or "上游没有返回图片"))

        updated = account_service.mark_image_result(active_token, True)
        if updated is None:
            raise RuntimeError("图片已生成，但账号额度更新失败")
        return list(dict.fromkeys(images)), updated


account_test_service = AccountTestService()

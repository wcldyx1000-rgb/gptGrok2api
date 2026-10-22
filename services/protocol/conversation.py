from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed, wait
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator

import tiktoken
from curl_cffi.requests import exceptions as curl_exceptions

from services.account_service import ImageAccountSelectionError, account_service
from services.config import config, mask_proxy_credentials
from services.image_failure import (
    ImageFailure,
    classify_image_error_text,
    classify_image_exception,
    classify_conversation_failure,
    classify_image_message_text,
    classify_message_facts,
    extract_message_facts,
    friendly_message_key,
    image_failure,
    image_failure_priority,
    is_terminal_message_status,
    is_connection_timeout_error,
    is_content_policy_image_message as _is_content_policy_image_message,
    is_model_text_reply_instead_of_image,
    is_stream_transport_error,
    is_tls_connection_error,
    is_token_invalid_error,
    is_upstream_error_reply_text,
    merge_failures,
    merge_message_failure,
    should_refresh_image_account,
    TEXT_REVIEW_FAILURE_CODES,
    terminal_assistant_text,
    text_says_image_quota_exhausted,
    upstream_error_reply_failure,
)
from services.image_storage_service import image_storage_service
from services.openai_backend_api import (
    ImageContentPolicyError,
    ImageDownloadError,
    ImagePollTimeoutError,
    ImageTextReplyError,
    OpenAIBackendAPI,
)
from services.proxy_service import ImageEgressDeadlineError, ProxyReferenceUnavailableError, proxy_settings
from services.realtime_monitor_service import realtime_monitor_service
from services.request_cancel_service import RequestCancelledError, request_cancel_service
from utils.helper import (
    IMAGE_MODELS,
    UpstreamHTTPError,
    extract_image_from_message_content,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from services.image_upscale_service import upscale_image_if_needed
from utils.image_tokens import count_image_content_tokens, image_size_from_bytes
from utils.log import logger
from utils.diagnostics import diagnostic_excerpt


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        raw_error: str = "",
        upstream_error: str = "",
        raw_upstream_message: str = "",
        failure: ImageFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.raw_error = raw_error
        self.upstream_error = upstream_error
        self.raw_upstream_message = raw_upstream_message
        self.image_attempts: list[dict[str, Any]] = []
        self.failure: ImageFailure | None = None
        if failure is not None:
            self.reclassify(failure)

    def reclassify(self, failure: ImageFailure) -> None:
        self.failure = failure
        self.status_code = failure.status_code
        self.error_type = failure.error_type
        self.code = failure.code

    def to_openai_error(self) -> dict[str, Any]:
        # 不回传 account_email：上游账号邮箱只留在日志里，不能暴露给 API 调用方（HTTP 与 SSE 都一样）。
        return {
            "error": {
                "message": public_image_error_message(str(self), code=self.code),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


def public_image_error_message(message: str, code: str | None = None) -> str:
    text = str(message or "").strip()
    if not config.image_error_friendly_enabled:
        return _legacy_public_image_error_message(text)
    key = friendly_message_key(classify_image_error_text(text, code=code))
    if key == "text_reply":
        return _public_text_reply_message(text)
    if key:
        return _friendly_image_error_message(key)
    if not text:
        return _friendly_image_error_message("fallback")
    if any(item in text.lower() for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return _friendly_image_error_message("fallback")
    return text


def _legacy_public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    fallback = "The image generation request failed. Please try again later."
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return fallback
    return text or fallback


def _friendly_image_error_message(key: str, text: str = "") -> str:
    messages = config.get_image_error_messages()
    template = str(messages.get(key) or messages.get("fallback") or "图片生成请求失败，请稍后重试。").strip()
    if text:
        if "{text}" in template:
            return template.replace("{text}", text)
        return f"{template}\n文本如下：{text}"
    return template.replace("{text}", "").strip()


def _public_text_excerpt(message: str, limit: int = 260) -> str:
    text = str(message or "").strip()
    text = re.sub(r"(?i)^status(?:_code)?\s*=\s*\d+\s*[,，:：]?\s*", "", text)
    text = re.sub(r"(?i)^error\s*[:：]\s*", "", text)
    text = " ".join(text.split())
    lower = text.lower()
    if any(item in lower for item in ("backend-api/", "authorization", "access_token", "refresh_token", "cookie", "bearer ")):
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _public_text_reply_message(message: str) -> str:
    excerpt = _public_text_excerpt(message)
    return _friendly_image_error_message("text_reply", excerpt) if excerpt else _friendly_image_error_message("text_reply")


def _monitor_image_stage(request: "ConversationRequest", event: str, **data: Any) -> None:
    if request.trace_image_perf and request.call_id:
        realtime_monitor_service.stage(request.call_id, event, model=request.model, **data)


def _raise_if_request_cancelled(request: "ConversationRequest") -> None:
    if request.call_id:
        request_cancel_service.raise_if_cancelled(request.call_id)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _proxy_hash(proxy_url: object) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return "direct"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _backend_egress_data(backend: OpenAIBackendAPI) -> dict[str, Any]:
    profile = getattr(backend, "proxy_profile", None)
    proxy_url = getattr(profile, "proxy_url", "") if profile else ""
    return {
        "proxy_source": str(getattr(profile, "proxy_source", "") or "direct"),
        "proxy_hash": _proxy_hash(proxy_url),
        # 进入日志 / 监控的出口标识不含代理密码（内部限流仍用 profile.egress_key 原值）
        "egress_key": mask_proxy_credentials(str(getattr(profile, "egress_key", "") or "direct")),
        "egress_label": mask_proxy_credentials(str(getattr(profile, "egress_label", "") or "")),
        "proxy_group_id": str(getattr(profile, "proxy_group_id", "") or ""),
        "proxy_node_id": str(getattr(profile, "proxy_node_id", "") or ""),
        "proxy_node_name": str(getattr(profile, "proxy_node_name", "") or ""),
        "image_egress_limit": int(getattr(profile, "image_concurrency_limit", 0) or 0),
        "has_proxy": bool(proxy_url),
        "egress_mode": str(getattr(profile, "egress_mode", "") or "direct"),
    }


def _backend_http_timing_data(backend: OpenAIBackendAPI | None, name: str = "image_generation_stream") -> dict[str, Any]:
    if backend is None or not hasattr(backend, "pop_http_timing"):
        return {}
    try:
        return backend.pop_http_timing(name)
    except Exception:
        return {}


_IMAGE_PROGRESS_STAGE_EVENTS = {
    "uploading": "image_uploading",
    "bootstrapping": "image_bootstrapping",
    "getting_token": "image_getting_token",
    "preparing_conversation": "image_preparing_conversation",
    "starting_generation": "image_starting_generation",
    "generating": "image_generating",
}

_IMAGE_PROGRESS_DURATION_KEYS = {
    "uploading": "upload_ms",
    "bootstrapping": "bootstrap_ms",
    "getting_token": "requirements_ms",
    "preparing_conversation": "prepare_conversation_ms",
    "starting_generation": "generation_start_ms",
}


def _image_progress_callback_with_monitor(
        request: "ConversationRequest",
        index: int,
        total: int,
        account_email_getter: Callable[[], str],
) -> Callable[[str], None]:
    original_callback = request.progress_callback
    last_step = ""
    last_step_started = time.perf_counter()

    def report(step: str) -> None:
        nonlocal last_step, last_step_started
        now = time.perf_counter()
        step_name = str(step or "").strip()
        data: dict[str, Any] = {
            "index": index,
            "total": total,
        }
        account_email = account_email_getter()
        if account_email:
            data["account_email"] = account_email
        duration_key = _IMAGE_PROGRESS_DURATION_KEYS.get(last_step)
        if duration_key:
            data[duration_key] = int((now - last_step_started) * 1000)
        stage_event = _IMAGE_PROGRESS_STAGE_EVENTS.get(step_name)
        if stage_event:
            _monitor_image_stage(request, stage_event, **data)
        last_step = step_name
        last_step_started = now
        if original_callback:
            original_callback(step_name)

    return report


def _resolve_image_urls_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        file_ids: list[str],
        sediment_ids: list[str],
        index: int,
        total: int,
        path: str = "",
        **kwargs: Any,
) -> list[str]:
    resolve_started = time.perf_counter()
    try:
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id,
            file_ids,
            sediment_ids,
            **kwargs,
        )
    except Exception as exc:
        text_reply = isinstance(exc, ImageTextReplyError)
        if request.trace_image_perf:
            resolve_ms = _elapsed_ms(resolve_started)
            _monitor_image_stage(
                request,
                "image_text_reply" if text_reply else "image_resolve_failed",
                conversation_id=conversation_id,
                resolve_ms=resolve_ms,
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
            )
            log_payload: dict[str, Any] = {
                "event": "image_text_reply" if text_reply else "image_resolve_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "resolve_ms": resolve_ms,
                "error": repr(exc)[:300],
            }
            if path:
                log_payload["path"] = path
            if text_reply:
                logger.info(log_payload)
            else:
                logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        resolve_ms = _elapsed_ms(resolve_started)
        _monitor_image_stage(
            request,
            "image_resolve_done",
            conversation_id=conversation_id,
            resolve_ms=resolve_ms,
            url_count=len(image_urls),
            index=index,
            total=total,
        )
        log_payload = {
            "event": "image_resolve_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "resolve_ms": resolve_ms,
            "url_count": len(image_urls),
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return image_urls


def _download_image_bytes_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        path: str = "",
) -> list[bytes]:
    download_started = time.perf_counter()
    try:
        downloaded_images = backend.download_image_bytes(image_urls)
    except Exception as exc:
        if request.trace_image_perf:
            download_ms = _elapsed_ms(download_started)
            _monitor_image_stage(
                request,
                "image_download_failed",
                conversation_id=conversation_id,
                download_ms=download_ms,
                url_count=len(image_urls),
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
            )
            log_payload: dict[str, Any] = {
                "event": "image_download_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "download_ms": download_ms,
                "url_count": len(image_urls),
                "error": repr(exc)[:300],
            }
            if path:
                log_payload["path"] = path
            logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        download_ms = _elapsed_ms(download_started)
        download_bytes = sum(len(image) for image in downloaded_images)
        download_kbps = int((download_bytes / 1024) / (download_ms / 1000)) if download_ms > 0 else 0
        _monitor_image_stage(
            request,
            "image_download_done",
            conversation_id=conversation_id,
            download_ms=download_ms,
            download_bytes=download_bytes,
            download_kbps=download_kbps,
            url_count=len(image_urls),
            image_count=len(downloaded_images),
            index=index,
            total=total,
        )
        log_payload = {
            "event": "image_download_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "download_ms": download_ms,
            "download_bytes": download_bytes,
            "download_kbps": download_kbps,
            "url_count": len(image_urls),
            "image_count": len(downloaded_images),
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return downloaded_images


def _retry_sleep_with_monitor(
        request: "ConversationRequest",
        seconds: float,
        *,
        conversation_id: str = "",
        account_email: str = "",
        index: int = 1,
        total: int = 1,
) -> None:
    wait_secs = max(0.0, float(seconds or 0))
    _monitor_image_stage(
        request,
        "image_retry_wait",
        conversation_id=conversation_id,
        account_email=account_email,
        retry_wait_ms=int(wait_secs * 1000),
        index=index,
        total=total,
        status="waiting",
    )
    if wait_secs > 0:
        time.sleep(wait_secs)


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if not config.image_error_friendly_enabled:
        return _legacy_image_stream_error_message(text)
    if is_token_invalid_error(text):
        return _friendly_image_error_message("token_invalid")
    if is_stream_transport_error(text):
        return _friendly_image_error_message("stream_interrupted")
    if is_tls_connection_error(text):
        return _friendly_image_error_message("connection_failed")
    if is_connection_timeout_error(text):
        return _friendly_image_error_message("connection_timeout")
    return text or _friendly_image_error_message("fallback")


def _legacy_image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "image generation failed"
    if is_stream_transport_error(text):
        return "upstream image stream interrupted, please retry later"
    if is_tls_connection_error(text):
        return "upstream image connection failed, please retry later"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    return text or "image generation failed"


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(
        image_data: bytes,
        base_url: str | None = None,
        *,
        deadline_monotonic: float | None = None,
) -> str:
    """落盘结果图：失败重试 1 次（请求已过截止时间则不再重试），仍失败抛带 failure 的 ImageDownloadError。

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return image_storage_service.save(image_data, base_url).url
        except Exception as exc:
            last_error = exc
        logger.warning({
            "event": "image_result_storage_retry" if attempt == 0 else "image_result_storage_failed",
            "attempt": attempt + 1,
            "error": diagnostic_excerpt(repr(last_error), 500),
        })
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
    raise ImageDownloadError(
        f"image result storage failed: {diagnostic_excerpt(last_error, 500)}"
    ) from last_error


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


STREAM_URL_RESPONSE_FORMAT = "b64_json+url"


def stream_response_format(response_format: str) -> str:
    """流式图片事件必须携带图片数据；请求 url 时额外带上 url。"""
    return STREAM_URL_RESPONSE_FORMAT if response_format == "url" else response_format


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
    requested_size: str | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        image_bytes = base64.b64decode(b64_json)
        upscaled = upscale_image_if_needed(image_bytes, requested_size)
        if upscaled is not image_bytes:
            image_bytes = upscaled
            b64_json = base64.b64encode(image_bytes).decode("ascii")
        if deadline_monotonic is None:
            stored_url = save_image_bytes(image_bytes, base_url)
        else:
            stored_url = save_image_bytes(image_bytes, base_url, deadline_monotonic=deadline_monotonic)
        asset: dict[str, Any] = {"revised_prompt": revised_prompt}
        # STREAM_URL_RESPONSE_FORMAT：流式 + url 时两者都带，客户端读哪个字段都能拿到图。
        if response_format in {"b64_json", STREAM_URL_RESPONSE_FORMAT}:
            asset["b64_json"] = b64_json
        if response_format != "b64_json":
            asset["url"] = stored_url
        dimensions = image_size_from_bytes(image_bytes)
        if dimensions:
            asset["width"], asset["height"] = dimensions
        data.append(asset)
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    call_id: str = ""
    trace_image_perf: bool = False
    # 请求级总截止时间（time.monotonic 口径，<=0 表示不限）：排队等号、等出口并发、断流恢复轮询都受它约束。
    deadline_monotonic: float = 0.0


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""
    async_task_type: str = ""
    message_type: str = ""
    # 当前助手/工具消息的结构化事实（role/content_type/status/end_turn/is_error/codes），
    # 以及据此得到的失败分类；没有图片产物时才有意义。
    message_facts: dict[str, Any] = field(default_factory=dict)
    failure: ImageFailure | None = None
    # 终态 code 消息里只回显了生图指令（没有图）：按 image_tool_error 处理，而不是文本回复。
    terminal_tool_arguments: dict[str, Any] | None = None


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""
    # Failed attempts on other accounts before this output (diagnostics only).
    image_attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.image_attempts:
            chunk["_image_attempts"] = list(self.image_attempts)
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


_IMAGE_TOOL_ARGUMENT_KEYS = frozenset({
    "prompt",
    "instructions",
    "size",
    "n",
    "transparent_background",
    "is_style_transfer",
    "referenced_image_ids",
})


def is_image_generation_arguments(text: str, *, role: str = "", content_type: str = "") -> bool:
    """助手的 code 消息里是否只是生图工具的参数回显（Ported from yukkcat/chatgpt2api, AGPL-3.0-only）。"""
    if str(role or "").strip().lower() != "assistant" or str(content_type or "").strip().lower() != "code":
        return False
    try:
        arguments = json.loads(str(text or "").strip())
    except (TypeError, ValueError):
        return False
    if not isinstance(arguments, dict) or not arguments:
        return False
    has_prompt = any(
        isinstance(arguments.get(key), str) and bool(arguments[key].strip())
        for key in ("prompt", "instructions")
    )
    return has_prompt and set(arguments) <= _IMAGE_TOOL_ARGUMENT_KEYS


def _merge_message_facts(state: ConversationState, facts: dict[str, Any]) -> None:
    if facts.get("blocked") is True:
        state.blocked = True
    if facts.get("turn_use_case"):
        state.turn_use_case = str(facts["turn_use_case"])
    if facts.get("async_task_type"):
        state.async_task_type = str(facts["async_task_type"])
    if facts.get("message_type"):
        state.message_type = str(facts["message_type"])
    role = str(facts.get("role") or "").strip().lower()
    current_role = str(state.message_facts.get("role") or "").strip().lower()
    message_id = str(facts.get("message_id") or "").strip()
    current_message_id = str(state.message_facts.get("message_id") or "").strip()
    should_update = False
    if role in {"assistant", "tool"}:
        if (current_role and current_role != role) or (
            message_id and current_message_id and message_id != current_message_id
        ):
            state.message_facts = {}
        should_update = True
    elif not role and current_role in {"assistant", "tool"}:
        should_update = True
    if should_update:
        accumulated_codes = set(state.message_facts.get("codes") or ())
        state.message_facts.update(facts)
        incoming_codes = set(facts.get("codes") or ())
        if accumulated_codes or incoming_codes:
            state.message_facts["codes"] = accumulated_codes.union(incoming_codes)


def refresh_state_failure(state: ConversationState, message_text: str = "") -> None:
    """按当前消息事实更新 state.failure / terminal_tool_arguments（只在还没有图片产物时有意义）。"""
    if state.file_ids or state.sediment_ids:
        state.failure = None
        state.terminal_tool_arguments = None
        return
    facts = state.message_facts
    role = str(facts.get("role") or "")
    content_type = str(facts.get("content_type") or "")
    status = str(facts.get("status") or "")
    end_turn = facts.get("end_turn")
    is_terminal = end_turn is True or (end_turn is None and is_terminal_message_status(status))
    text = str(message_text or state.raw_text or "")
    if is_terminal and is_image_generation_arguments(text, role=role, content_type=content_type):
        if state.tool_invoked is not True:
            state.terminal_tool_arguments = {
                "text": text.strip(),
                "content_type": content_type.strip().lower(),
                "status": status.strip().lower(),
                "end_turn": end_turn is True,
            }
    candidate = classify_message_facts(
        role=role,
        content_type=content_type,
        status=status,
        end_turn=bool(end_turn),
        is_error=bool(facts.get("is_error")),
        blocked=state.blocked,
        has_image_output=False,
        has_text=bool(facts.get("has_text")),
        turn_use_case=str(facts.get("turn_use_case") or state.turn_use_case),
        async_task_type=str(facts.get("async_task_type") or ""),
        message_type=str(facts.get("message_type") or ""),
        codes=facts.get("codes") or (),
        raw_detail=text,
    )
    if state.terminal_tool_arguments is not None and candidate is not None and candidate.code == "upstream_text_reply":
        # 指令回显不是给用户看的文字回复。
        candidate = None
    if candidate is not None:
        # 显式错误码优先于终态文本；终态文本只替换同一轮里的笼统工具错误。
        state.failure = merge_message_failure(state.failure, candidate)


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
                if state.tool_invoked is True:
                    state.terminal_tool_arguments = None
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)
    _merge_message_facts(state, extract_message_facts(event))
    if image_context:
        state.terminal_tool_arguments = None


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        "async_task_type": state.async_task_type,
        "message_type": state.message_type,
        "message_role": str(state.message_facts.get("role") or ""),
        "content_type": str(state.message_facts.get("content_type") or ""),
        "_image_failure": state.failure,
        "_terminal_tool_arguments": state.terminal_tool_arguments,
        **extra,
    }


def iter_conversation_payloads(payloads: Iterator[str], history_text: str = "",
                               history_messages: list[str] | None = None) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        refresh_state_failure(state, next_text)
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages)


def _text_account_email(access_token: str) -> str:
    account = account_service.get_account(access_token)
    if not account:
        return ""
    return str(account.get("email") or "").strip()


def _remember_text_account(backend: OpenAIBackendAPI, access_token: str) -> str:
    email = _text_account_email(access_token)
    setattr(backend, "account_email", email)
    return email


def text_backend() -> OpenAIBackendAPI:
    access_token = account_service.get_text_access_token()
    backend = OpenAIBackendAPI(access_token=access_token)
    _remember_text_account(backend, access_token)
    return backend


TEXT_MAX_ACCOUNT_ATTEMPTS = 3
# 所有账号的对话并发槽位都占满时，换号重试最多排队等待的秒数。
TEXT_SLOT_WAIT_SECS = 120.0


def _is_text_switchable_failure(exc: Exception) -> bool:
    """上游或账号侧的失败（连接、超时、5xx、风控）才计入账号的连续失败并换号；请求本身的问题不计。"""
    try:
        failure = classify_image_exception(exc)
    except Exception:
        return False
    if failure.code == "upstream_error" and not isinstance(exc, UpstreamHTTPError):
        return False  # 本地/未知异常不是账号或上游的问题
    return failure.scope in {"transient", "account"} and failure.code != "upstream_error_reply"


def _is_text_auth_failure(exc: Exception) -> bool:
    if is_token_invalid_error(str(exc)):
        return True
    if isinstance(exc, UpstreamHTTPError) and getattr(exc, "credential_scope", "account") == "account":
        return int(exc.status_code) == 401
    return False


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    """流式文本；整段（含换号重试）占用一个账号并发槽位，结束/异常/流被中断（GeneratorExit）都会归还。

    在还没向调用方输出任何内容前，遇到可换号的失败（鉴权、连接、超时、5xx、风控）会换下一个账号，
    最多 TEXT_MAX_ACCOUNT_ATTEMPTS 个。Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    attempted_tokens: set[str] = set()
    hint_token = getattr(backend, "access_token", "")
    token = account_service.acquire_text_access_token(preferred_token=hint_token) if hint_token else ""
    if hint_token and not token:
        raise RuntimeError("no available text account")
    emitted = False
    try:
        while True:
            if token and token in attempted_tokens:
                raise RuntimeError("no available text account")
            if token:
                attempted_tokens.add(token)
            active_backend: OpenAIBackendAPI | None = None
            try:
                _remember_text_account(backend, token)
                active_backend = OpenAIBackendAPI(access_token=token)
                _remember_text_account(active_backend, token)
                for event in conversation_events(
                    active_backend,
                    messages=request.messages,
                    model=request.model,
                    prompt=request.prompt,
                    thinking_effort=request.thinking_effort,
                ):
                    if event.get("type") != "conversation.delta":
                        continue
                    delta = str(event.get("delta") or "")
                    if delta:
                        emitted = True
                        yield delta
                account_service.mark_text_used(token)
                return
            except Exception as exc:
                failed_token = token
                if token and not emitted and _is_text_auth_failure(exc):
                    refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                    if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                        account_service.rebind_text_slot(token, refreshed_token)
                        token = refreshed_token
                    else:
                        # 401 先入待核验队列（核验通过恢复，失败再走失效/重新登录流程），本次请求换号继续。
                        try:
                            account_service.schedule_auth_verification(token, "text_stream")
                        except Exception as schedule_error:  # noqa: BLE001 - 调度失败不能盖掉原始错误
                            logger.warning({
                                "event": "auth_verification_schedule_failed",
                                "source": "text_stream",
                                "error": diagnostic_excerpt(schedule_error, 300),
                            })
                        released, token = token, ""
                        account_service.release_text_slot(released)
                        token = account_service.acquire_text_access_token(
                            excluded_tokens=attempted_tokens,
                            timeout_secs=TEXT_SLOT_WAIT_SECS,
                        )
                    if token:
                        continue
                elif token and not emitted and _is_text_switchable_failure(exc):
                    account_service.record_upstream_failure(token)
                    if len(attempted_tokens) < TEXT_MAX_ACCOUNT_ATTEMPTS:
                        released, token = token, ""
                        account_service.release_text_slot(released)
                        token = account_service.acquire_text_access_token(
                            excluded_tokens=attempted_tokens,
                            timeout_secs=TEXT_SLOT_WAIT_SECS,
                        )
                        if token:
                            logger.warning({
                                "event": "text_cross_account_retry",
                                "account_email": _text_account_email(failed_token),
                                "next_account_email": _text_account_email(token),
                                "attempted_account_count": len(attempted_tokens),
                                "error": diagnostic_excerpt(exc, 300),
                            })
                            continue
                if failed_token and not getattr(exc, "account_email", ""):
                    setattr(exc, "account_email", _text_account_email(failed_token))
                raise
            finally:
                if active_backend is not None:
                    active_backend.close()
    finally:
        if token:
            account_service.release_text_slot(token)


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": diagnostic_excerpt(exc, 300),
        })
        return ""


def _recover_image_conversation_id(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        *,
        reason: str,
        message: str = "",
        started_at: float | None = None,
) -> str:
    """从最近对话中补救 conversation_id；只做一次，失败不影响主流程。"""
    if not request.prompt:
        return ""
    try:
        recovered_id = backend.find_conversation_by_prompt(
            request.prompt,
            started_at or time.time(),
            timeout_secs=5.0,
        )
        if recovered_id:
            logger.info({
                "event": "image_conversation_id_recovered",
                "reason": reason,
                "conversation_id": recovered_id,
                "message_preview": message[:200],
            })
            return recovered_id
    except Exception as exc:
        logger.warning({
            "event": "image_conversation_id_recovery_failed",
            "reason": reason,
            "error": repr(exc)[:300],
        })
    return ""


def _image_stream_timeout_task_diagnostics(
        backend: OpenAIBackendAPI,
        conversation_id: str,
) -> tuple[str, list[dict[str, Any]], str]:
    """Collect a small task summary after an upstream SSE timeout."""
    try:
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=5.0)
    except Exception as exc:
        return "", [], diagnostic_excerpt(repr(exc), 1000)

    summaries: list[dict[str, Any]] = []
    first_error = ""
    for task in tasks[:5]:
        if not isinstance(task, dict):
            continue
        is_error, error_msg, metadata = backend.check_task_error(task)
        metadata = metadata if isinstance(metadata, dict) else {}
        if error_msg and not first_error:
            first_error = error_msg
        summary: dict[str, Any] = {
            "id": str(task.get("id") or task.get("task_id") or "")[:80],
            "status": str(task.get("status") or metadata.get("status") or "")[:80],
            "type": str(task.get("type") or metadata.get("async_task_type") or "")[:80],
            "is_error": bool(is_error or metadata.get("is_error")),
        }
        for key in ("conversation_id", "original_conversation_id"):
            value = task.get(key)
            if value not in (None, ""):
                summary[key] = str(value)[:80]
        if error_msg:
            summary["error_preview"] = diagnostic_excerpt(error_msg, 500)
        summaries.append({key: value for key, value in summary.items() if value not in (None, "")})
    return first_error, summaries, ""


def _image_stream_timeout_error(
        raw_error: str,
        conversation_id: str,
        upstream_error: str,
        raw_upstream_message: str,
        followup: dict[str, Any],
        conversation_snapshot: dict[str, Any] | None = None,
        failure_code: str = "image_stream_timeout",
) -> ImageGenerationError:
    exc = ImageGenerationError(
        image_stream_error_message(raw_error),
        status_code=502,
        error_type="server_error",
        code=failure_code,
        conversation_id=conversation_id,
        raw_error=raw_error,
        upstream_error=diagnostic_excerpt(upstream_error or raw_error, 4000),
        raw_upstream_message=diagnostic_excerpt(raw_upstream_message, 4000),
    )
    if failure_code == "image_stream_timeout":
        setattr(exc, "stream_timeout_secs", config.image_stream_timeout_secs)
    setattr(exc, "stream_timeout_followup", followup)
    if followup.get("task_error"):
        setattr(exc, "last_task_error", followup["task_error"])
    if conversation_snapshot:
        setattr(exc, "last_conversation_snapshot", conversation_snapshot)
    if raw_upstream_message:
        setattr(exc, "upstream_message_preview", diagnostic_excerpt(raw_upstream_message, 1000))
    return exc


def _recover_after_image_stream_timeout(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        last: dict[str, Any],
        timeout_error: Exception,
        index: int,
        total: int,
        stream_started_at: float,
        *,
        failure_code: str = "image_stream_timeout",
) -> ImageOutput:
    """SSE 超时/中途断开后凭 conversation_id 继续读会话；上游多半还在画并已扣额度，不能直接报错。"""
    is_timeout = failure_code == "image_stream_timeout"
    recovery_reason = "stream_timeout" if is_timeout else "stream_interrupted"
    recovery_path = f"{recovery_reason}_followup"
    raw_error = str(timeout_error) or (
        f"SSE stream exceeded {config.image_stream_timeout_secs}s" if is_timeout else "SSE stream interrupted"
    )
    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()

    if not conversation_id:
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason=recovery_reason,
            message=message or raw_error,
            started_at=stream_started_at,
        )

    followup: dict[str, Any] = {
        "reason": "sse_timeout" if is_timeout else "sse_interrupted",
        "timeout_secs": config.image_stream_timeout_secs,
        "conversation_id": conversation_id,
        "stream_error": raw_error,
        "last_stream_state": {
            "conversation_id": str(last.get("conversation_id") or ""),
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "blocked": bool(last.get("blocked")),
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": str(last.get("turn_use_case") or ""),
            "text_preview": diagnostic_excerpt(message, 1000),
        },
    }
    task_error = ""
    task_summaries: list[dict[str, Any]] = []
    task_probe_error = ""
    conversation_snapshot: dict[str, Any] = {}
    latest_assistant_text = ""
    policy_message = ""
    conversation_probe_error = ""
    stream_failure = last.get("_image_failure")
    stream_failure = stream_failure if isinstance(stream_failure, ImageFailure) else None
    conversation_failure: ImageFailure | None = None

    if conversation_id:
        task_error, task_summaries, task_probe_error = _image_stream_timeout_task_diagnostics(backend, conversation_id)
        try:
            conversation = backend._get_conversation(conversation_id, timeout_secs=10)
            conversation_snapshot, latest_assistant_text = backend._conversation_poll_snapshot(conversation)
            for record in backend._extract_image_tool_records(conversation):
                add_unique(file_ids, [str(item) for item in record.get("file_ids") or []])
                add_unique(sediment_ids, [str(item) for item in record.get("sediment_ids") or []])
            policy_message = backend._find_content_policy_error_in_conversation(conversation)
            if not (file_ids or sediment_ids):
                conversation_failure = classify_conversation_failure(conversation)
                snapshot_terminal_text = terminal_assistant_text(conversation)
                if snapshot_terminal_text:
                    latest_assistant_text = snapshot_terminal_text
        except Exception as exc:
            conversation_probe_error = diagnostic_excerpt(repr(exc), 1000)

    followup.update({
        "task_error": diagnostic_excerpt(task_error, 2000),
        "task_count": len(task_summaries),
        "tasks": task_summaries,
        "task_probe_error": task_probe_error,
        "conversation_probe_error": conversation_probe_error,
        "conversation_message_count": len(conversation_snapshot.get("messages") or []) if conversation_snapshot else 0,
        "latest_assistant_text": diagnostic_excerpt(latest_assistant_text, 2000),
        "policy_message": diagnostic_excerpt(policy_message, 2000),
        "recovered_file_ids": file_ids,
        "recovered_sediment_ids": sediment_ids,
    })

    policy_error = policy_message or (task_error if task_error and _is_content_policy_image_message(task_error) else "")
    if policy_error:
        policy_exc = ImageGenerationError(
            policy_error,
            status_code=400,
            error_type="invalid_request_error",
            code="content_policy_violation",
            conversation_id=conversation_id,
            raw_error=raw_error,
            upstream_error=policy_error,
            raw_upstream_message=policy_error,
        )
        setattr(policy_exc, "stream_timeout_secs", config.image_stream_timeout_secs)
        setattr(policy_exc, "stream_timeout_followup", followup)
        if conversation_snapshot:
            setattr(policy_exc, "last_conversation_snapshot", conversation_snapshot)
        raise policy_exc

    if file_ids or sediment_ids:
        try:
            image_urls = _resolve_image_urls_with_monitor(
                backend,
                request,
                conversation_id,
                file_ids,
                sediment_ids,
                index=index,
                total=total,
                path=recovery_path,
                poll=False,
            )
            result_output = _image_result_output_from_urls(
                backend,
                request,
                conversation_id,
                image_urls,
                index,
                total,
                path=recovery_path,
            )
            if result_output:
                logger.info({
                    "event": f"image_{recovery_reason}_recovered_result",
                    "call_id": request.call_id,
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "url_count": len(image_urls),
                })
                return result_output
        except ImageDownloadError:
            raise
        except Exception as exc:
            followup["result_recovery_error"] = diagnostic_excerpt(repr(exc), 1000)

    terminal_failure = merge_message_failure(stream_failure, conversation_failure)
    if terminal_failure is not None:
        followup["failure_code"] = terminal_failure.code

    # 断流或流超时时上游多半还在画：只查一次会话往往还没出图。实测连接中途断开后上游照样画完
    # 并扣了额度，直接报错等于白扣一次。没有失败信号（或只有“最新是生图指令”的笼统工具错误）时，
    # 按正常结果路径继续轮询这条会话；轮询到期仍没有图，再按原来的失败报出。
    may_still_be_drawing = terminal_failure is None or terminal_failure.code == "image_tool_error"
    if conversation_id and may_still_be_drawing and not (file_ids or sediment_ids):
        poll_budget = _recovery_poll_budget_secs(request)
        if poll_budget > 0:
            logger.info({
                "event": f"image_{recovery_reason}_poll_recovery_start",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "poll_budget_secs": round(poll_budget, 1),
            })
            try:
                image_urls = _resolve_image_urls_with_monitor(
                    backend,
                    request,
                    conversation_id,
                    [],
                    [],
                    index=index,
                    total=total,
                    path=f"{recovery_path}_poll",
                    poll_timeout_secs=poll_budget,
                )
                result_output = _image_result_output_from_urls(
                    backend,
                    request,
                    conversation_id,
                    image_urls,
                    index,
                    total,
                    path=f"{recovery_path}_poll",
                )
                if result_output:
                    logger.info({
                        "event": f"image_{recovery_reason}_poll_recovered_result",
                        "call_id": request.call_id,
                        "conversation_id": conversation_id,
                        "url_count": len(image_urls),
                    })
                    return result_output
            except ImageDownloadError:
                raise
            except Exception as exc:
                followup["poll_recovery_error"] = diagnostic_excerpt(repr(exc), 1000)
                # 轮询查到了明确的失败（如内容违规、鉴权失败）就按它报，比笼统的超时更准确。
                if classify_image_exception(exc).code != "image_poll_timeout":
                    raise

    if terminal_failure is not None and terminal_failure.code != "image_tool_error":
        failure_detail = ""
        raw_detail = terminal_failure.raw_detail
        if isinstance(raw_detail, str):
            failure_detail = raw_detail.strip()
        elif raw_detail not in (None, ""):
            failure_detail = json.dumps(raw_detail, ensure_ascii=False, default=str)
        failure_detail = failure_detail or message or raw_error
        terminal_exc = ImageGenerationError(
            failure_detail,
            failure=terminal_failure.with_raw_detail(failure_detail),
            conversation_id=conversation_id,
            raw_error=raw_error,
            upstream_error="" if terminal_failure.code == "upstream_text_reply" else failure_detail,
            raw_upstream_message=latest_assistant_text or message,
        )
        if is_timeout:
            setattr(terminal_exc, "stream_timeout_secs", config.image_stream_timeout_secs)
        setattr(terminal_exc, "stream_timeout_followup", followup)
        if conversation_snapshot:
            setattr(terminal_exc, "last_conversation_snapshot", conversation_snapshot)
        raise terminal_exc

    upstream_error = task_error or policy_message
    if not upstream_error:
        upstream_error = json.dumps(
            {
                key: value
                for key, value in followup.items()
                if key not in {"conversation_snapshot"}
            },
            ensure_ascii=False,
            default=str,
        )
    logger.warning({
        "event": f"image_{recovery_reason}_followup",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "task_error": diagnostic_excerpt(task_error, 500),
        "task_count": len(task_summaries),
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "conversation_probe_error": conversation_probe_error,
        "task_probe_error": task_probe_error,
    })
    raise _image_stream_timeout_error(
        raw_error,
        conversation_id,
        upstream_error,
        latest_assistant_text or message,
        followup,
        conversation_snapshot,
        failure_code=failure_code,
    )


def _cleanup_image_conversations_after_success(backend: OpenAIBackendAPI, outputs: Iterable[ImageOutput]) -> None:
    if not config.image_remove_conversation_after_result:
        return
    conversation_ids: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        conversation_id = str(getattr(output, "conversation_id", "") or "").strip()
        if output.kind != "result" or not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        conversation_ids.append(conversation_id)
    for conversation_id in conversation_ids:
        try:
            backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": diagnostic_excerpt(exc, 500),
            })


def _image_result_output_from_urls(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        *,
        path: str = "",
) -> ImageOutput | None:
    if not image_urls:
        return None
    if request.progress_callback:
        request.progress_callback("receiving_image")
    downloaded_images = _download_image_bytes_with_monitor(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
        path=path,
    )
    image_items = [
        {"b64_json": base64.b64encode(image_data).decode("ascii")}
        for image_data in downloaded_images
    ]
    data = format_image_result(
        image_items,
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
        requested_size=request.size,
        **({"deadline_monotonic": request.deadline_monotonic} if request.deadline_monotonic > 0 else {}),
    )["data"]
    if not data:
        return None
    return ImageOutput(
        kind="result",
        model=request.model,
        index=index,
        total=total,
        data=data,
        conversation_id=conversation_id,
    )


def _is_stream_interruption(exc: BaseException) -> bool:
    if isinstance(exc, (UpstreamHTTPError, RequestCancelledError)):
        return False
    if isinstance(exc, curl_exceptions.RequestException):
        return True
    return is_stream_transport_error(str(exc))


def _tool_arguments_failure_code(message: str, argument_text: str) -> str:
    """只有生图指令、没有图时的失败码。指令后面跟着“已达生图上限”之类的说明按额度耗尽处理；
    只看指令以外上游补充的话，指令里带着用户提示词，提示词本身可能就含这类字样。"""
    trailing_text = message.replace(argument_text, "") if argument_text else message
    return "image_quota_exhausted" if text_says_image_quota_exhausted(trailing_text) else "image_tool_error"


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    """执行一张 ChatGPT 图片任务。

    统一原则：上游 SSE 只负责启动/生成阶段；SSE 结束后只进入一次结果解析/轮询。
    不再在文本回复、空结果、轮询超时后叠加多轮长重试，避免一个配置的 300 秒被隐式放大到十几分钟。
    """
    last: dict[str, Any] = {}
    conversation_stream_started = time.perf_counter()
    conversation_wall_started = time.time()
    try:
        for event in conversation_events(
                backend,
                prompt=request.prompt,
                model=request.model,
                images=request.images or [],
                size=request.size,
                quality=request.quality,
        ):
            last = event
            if event.get("type") == "conversation.delta":
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=str(event.get("delta") or ""),
                    upstream_event_type="conversation.delta",
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                )
    except TimeoutError as exc:
        yield _recover_after_image_stream_timeout(
            backend,
            request,
            last,
            exc,
            index,
            total,
            conversation_wall_started,
        )
        return
    except Exception as exc:
        # SSE 中途断开（连接重置、HTTP/2 流错误、超时类请求异常）：已经拿到会话/图片 id 才有得救，
        # 上游多半还在画并已扣额度，继续轮询会话而不是直接报 image_stream_interrupted。
        if not _is_stream_interruption(exc) or not any((
            last.get("conversation_id"),
            last.get("file_ids"),
            last.get("sediment_ids"),
        )):
            raise
        yield _recover_after_image_stream_timeout(
            backend,
            request,
            last,
            exc,
            index,
            total,
            conversation_wall_started,
            failure_code="image_stream_timeout" if isinstance(exc, curl_exceptions.Timeout) else "image_stream_interrupted",
        )
        return

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    should_poll_for_image = bool(request.images) or last.get("turn_use_case") == "image gen"
    # Image-generation SSE can finish with a human-readable queue/status
    # placeholder while the actual file IDs are still committed via the
    # conversation document.  When the turn structurally says an image should be
    # polled, do not treat stream text as terminal; let the poll path decide
    # from conversation/tool structure.
    is_text_reply = bool(
        message
        and not should_poll_for_image
        and backend._is_human_facing_image_text_reply_payload(message, last)
    )
    conversation_stream_ms = int((time.perf_counter() - conversation_stream_started) * 1000)
    http_timing = _backend_http_timing_data(backend)
    _monitor_image_stage(
        request,
        "image_stream_resolve_start",
        conversation_id=conversation_id,
        conversation_stream_ms=conversation_stream_ms,
        index=index,
        total=total,
        **http_timing,
    )
    logger.info({
        "event": "image_stream_resolve_start",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
        "is_text_reply": is_text_reply,
        "should_poll_for_image": should_poll_for_image,
        "conversation_stream_ms": conversation_stream_ms,
        **http_timing,
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")

    if is_text_reply:
        logger.info({
            "event": "image_stream_text_reply_detected",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    if not conversation_id and (should_poll_for_image or is_text_reply):
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason="stream_result",
            message=message,
        )

    if message and not file_ids and not sediment_ids and last.get("blocked"):
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=error_text, conversation_id=conversation_id)
        return

    if is_text_reply and message and not file_ids and not sediment_ids:
        text_exc = ImageTextReplyError(message)
        setattr(text_exc, "conversation_id", conversation_id or "")
        setattr(text_exc, "upstream_error", message)
        setattr(text_exc, "raw_upstream_message", message)
        setattr(text_exc, "last_assistant_text", message)
        raise text_exc

    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id, timeout_secs=5.0, wait_secs=1.0)
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and _is_content_policy_image_message(detailed_error):
            logger.info({
                "event": "image_task_policy_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error:
            logger.info({
                "event": "image_task_error_observed_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    if message and not file_ids and not sediment_ids and not should_poll_for_image and not is_text_reply:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    image_urls = _resolve_image_urls_with_monitor(
        backend,
        request,
        conversation_id,
        file_ids,
        sediment_ids,
        poll_timeout_secs=config.image_poll_timeout_secs,
        index=index,
        total=total,
    )
    result_output = _image_result_output_from_urls(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
    )
    if result_output:
        yield result_output
        return

    terminal_args = last.get("_terminal_tool_arguments")
    if isinstance(terminal_args, dict) and terminal_args.get("text"):
        # 终态消息只是生图指令的回显，没有图：工具没真正出图（或额度已用完），不是给用户的文字回复。
        code = _tool_arguments_failure_code(message, str(terminal_args.get("text") or ""))
        raise ImageGenerationError(
            message or str(terminal_args.get("text") or ""),
            conversation_id=conversation_id,
            raw_upstream_message=message,
            failure=image_failure(code, raw_detail=message),
        )
    stream_failure = last.get("_image_failure")
    if isinstance(stream_failure, ImageFailure) and stream_failure.code == "image_quota_exhausted":
        raise ImageGenerationError(
            message or "image quota exhausted",
            conversation_id=conversation_id,
            raw_upstream_message=message,
            failure=stream_failure,
        )

    if message and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    if detailed_error:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
        return

    if conversation_id:
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text="Image generation completed upstream but the result could not be retrieved. Please try again in a moment.",
            conversation_id=conversation_id,
        )
        return

    yield ImageOutput(
        kind="message",
        model=request.model,
        index=index,
        total=total,
        text="Image generation started upstream but the response was incomplete. Please try again.",
        conversation_id=conversation_id,
    )

def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    codex_started = time.perf_counter()
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if request.trace_image_perf:
        _monitor_image_stage(
            request,
            "image_codex_response_done",
            response_ms=int((time.perf_counter() - codex_started) * 1000),
            image_count=len(images),
            index=index,
            total=total,
        )
        logger.info({
            "event": "image_codex_response_done",
            "call_id": request.call_id,
            "model": request.model,
            "index": index,
            "total": total,
            "response_ms": int((time.perf_counter() - codex_started) * 1000),
            "image_count": len(images),
        })
    if not images:
        raise ImageGenerationError("No image result found in response")
    data = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
        requested_size=request.size,
    )["data"]
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


RECOVERY_POLL_DEADLINE_MARGIN_SECS = 10.0


def _image_start_deadline(request: ConversationRequest) -> float | None:
    """最晚可以开始一次生成的时间点：请求截止时间减去一次生成流的超时。

    排队等账号等到这个时间还没拿到号，剩下的时间已经不够跑完生成，再开始只会白白消耗
    上游额度后被截止时间中断，因此直接按账号繁忙排队超时失败。
    """
    if request.deadline_monotonic <= 0:
        return None
    return request.deadline_monotonic - float(config.image_stream_timeout_secs)


def _recovery_poll_budget_secs(request: ConversationRequest) -> float:
    """断流后继续轮询的时长：不超过结果等待上限，也不越过请求的总截止时间（留出下载余量）。"""
    budget = float(config.image_poll_timeout_secs)
    if request.deadline_monotonic > 0:
        budget = min(budget, request.deadline_monotonic - time.monotonic() - RECOVERY_POLL_DEADLINE_MARGIN_SECS)
    return max(0.0, budget)


def _acquire_image_token(excluded_tokens: set[str], **kwargs: Any) -> str:
    """Lease an image account, avoiding accounts that already failed this image.

    Raises ImageAccountSelectionError when only excluded accounts are available.
    """
    if excluded_tokens:
        kwargs["excluded_tokens"] = set(excluded_tokens)
    return account_service.get_available_access_token(**kwargs)


_SYNTHETIC_INCOMPLETE_MESSAGES = frozenset({
    "Image generation completed upstream but the result could not be retrieved. Please try again in a moment.",
    "Image generation started upstream but the response was incomplete. Please try again.",
})

# 上游文本回复/内容策略拒绝/送达失败等是请求或送达层面的问题，不是账号的错：只放回槽位，不降权。
_REQUEST_SCOPED_NO_PENALTY_CODES = frozenset(
    {"image_download_failed", "account_busy_timeout", "account_queue_full"}
) | TEXT_REVIEW_FAILURE_CODES


def _release_image_account_without_failure(token: str, failure: ImageFailure) -> None:
    """Free the slot for a failure that is not the account's fault; at most deprioritize it."""
    account_service.release_image_slot(token)
    if failure.code in _REQUEST_SCOPED_NO_PENALTY_CODES:
        return
    try:
        account_service.penalize_image_account(token, reason=failure.code)
    except Exception as exc:
        logger.warning({"event": "image_account_penalize_failed", "code": failure.code, "error": str(exc)[:200]})


def _output_size_text(outputs: list[ImageOutput]) -> str:
    """Actual result sizes, e.g. "1024x1024" or "1024x1024,1536x1024"; empty when unknown."""
    sizes: list[str] = []
    for output in outputs:
        if output.kind != "result":
            continue
        for item in output.data:
            if isinstance(item, dict) and item.get("width") and item.get("height"):
                sizes.append(f"{item['width']}x{item['height']}")
    return ",".join(sizes)


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    可换号的失败（上游错误回复、额度耗尽、429 等）最多换
    config.image_account_retry_count 次账号重试（= image_max_account_attempts - 1，
    image_account_retry_enabled 关闭时为 0）。每次尝试（成功/失败/出口回退）记录到
    image_attempts，随输出或异常带给日志层。
    """
    account_email = ""
    retry_token = ""
    fallback_retry_pending = False
    fallback_retry_used = False
    fallback_from_egress: dict[str, Any] = {}
    single_started = time.perf_counter()
    excluded_tokens: set[str] = set()
    account_retries_left = config.image_account_retry_count
    image_attempts: list[dict[str, Any]] = []
    attempt_started = time.perf_counter()
    current_egress: dict[str, Any] = {}
    current_fallback = False
    best_error: ImageGenerationError | None = None
    best_failure: ImageFailure | None = None

    def final_error(error: ImageGenerationError, failure: ImageFailure) -> ImageGenerationError:
        """Return the most informative error across attempts.

        The winning error keeps its own public status/code; the classified
        failure is attached for diagnostics only, so API responses stay as before.
        """
        nonlocal best_error, best_failure
        if error.failure is None:
            error.failure = failure
        merged = merge_failures(best_failure, failure)
        if merged is not None and merged.code == failure.code:
            best_error = error
        best_failure = merged
        resolved = best_error or error
        resolved.image_attempts = list(image_attempts)
        return resolved

    def attempt_base(status: str) -> dict[str, Any]:
        attempt: dict[str, Any] = {
            "slot": index,
            "attempt": len(image_attempts) + 1,
            "account_email": account_email,
            "status": status,
            "switched_account": False,
            "duration_ms": int((time.perf_counter() - attempt_started) * 1000),
        }
        for key in ("egress_key", "egress_label", "proxy_source", "proxy_group_id", "proxy_node_name"):
            value = current_egress.get(key)
            if value:
                attempt[key] = value
        if current_fallback:
            attempt["egress_fallback"] = True
        return attempt

    def record_attempt(failure: ImageFailure, retry_kind: str = "") -> dict[str, Any]:
        attempt = {**attempt_base("failed"), **failure.diagnostic_fields()}
        if retry_kind:
            attempt["retry_kind"] = retry_kind
        image_attempts.append(attempt)
        return attempt

    def record_success(status: str = "success") -> None:
        image_attempts.append(attempt_base(status))

    def switch_account(error: ImageGenerationError, failure: ImageFailure) -> bool:
        """Move to another account when the failure allows it; False means raise."""
        nonlocal account_retries_left
        attempt = record_attempt(failure)
        final_error(error, failure)
        if not failure.switch_account or account_retries_left <= 0:
            return False
        account_retries_left -= 1
        excluded_tokens.add(token)
        attempt["switched_account"] = True
        logger.warning({
            "event": "image_account_switch_retry",
            "call_id": request.call_id,
            "account_email": account_email,
            "failure_code": failure.code,
            "retries_left": account_retries_left,
            "index": index,
        })
        _monitor_image_stage(
            request,
            "image_account_switch_retry",
            account_email=account_email,
            failure_code=failure.code,
            status="retrying",
            index=index,
            total=total,
        )
        return True

    while True:
        account_wait_started = time.perf_counter()
        stream_started = 0.0
        try:
            _raise_if_request_cancelled(request)
            if retry_token:
                token = retry_token
                retry_token = ""
            else:
                if request.progress_callback:
                    request.progress_callback("getting_account")
                _monitor_image_stage(request, "image_getting_account", index=index, total=total)
                plan_type, _ = split_image_model(request.model)
                codex_model = is_codex_image_model(request.model)
                acquire_kwargs: dict[str, Any] = {}
                start_deadline = _image_start_deadline(request)
                if start_deadline is not None:
                    acquire_kwargs["deadline_monotonic"] = start_deadline
                token = _acquire_image_token(
                    excluded_tokens,
                    plan_type=plan_type,
                    source_type=account_service.CODEX_IMAGE_SOURCE if codex_model else None,
                    plan_types=None,
                    **acquire_kwargs,
                )
                if not token and best_error is not None:
                    raise best_error
        except ImageAccountSelectionError as exc:
            if best_error is not None:
                # No other account left: report the failure that made us switch.
                raise best_error from exc
            _monitor_image_stage(
                request,
                "image_local_rejected",
                local_reason="account_pool",
                status="failed",
                index=index,
                total=total,
            )
            raise ImageGenerationError(
                str(exc) or "image generation failed",
                status_code=exc.status_code,
                error_type=exc.error_type,
                code=exc.code,
                account_email=account_email,
            ) from exc
        except RuntimeError as exc:
            if best_error is not None:
                raise best_error from exc
            _monitor_image_stage(
                request,
                "image_local_rejected",
                local_reason="account_pool",
                status="failed",
                index=index,
                total=total,
            )
            raise ImageGenerationError(str(exc) or "image generation failed", account_email=account_email) from exc

        emitted_for_token = False
        returned_message = False
        returned_result = False
        image_slot_finalized = False

        def finalize_image_slot(
                success: bool,
                *,
                refresh_account: bool = False,
                failure: ImageFailure | None = None,
                upstream_status: int = 0,
                account_fault: bool = True,
        ) -> None:
            nonlocal image_slot_finalized
            if image_slot_finalized:
                return
            image_slot_finalized = True
            if not success and (not account_fault or (failure is not None and not failure.account_failure)):
                # 文本结果/内容策略拒绝/送达失败不是账号的错：只放回槽位，不计失败、不触发冷却或自动暂停。
                _release_image_account_without_failure(
                    token,
                    failure or image_failure("upstream_text_reply"),
                )
                return
            try:
                account_service.mark_image_result(
                    token,
                    success,
                    refresh_account=refresh_account,
                    failure_code=failure.code if failure is not None else "",
                    status_code=upstream_status,
                )
            except Exception as mark_error:  # noqa: BLE001 - 账号状态写失败也必须归还槽位
                logger.warning({
                    "event": "image_account_result_update_failed",
                    "call_id": request.call_id,
                    "account_email": account_email,
                    "error": diagnostic_excerpt(mark_error, 300),
                })
                try:
                    account_service.release_image_slot(token)
                except Exception as release_error:  # noqa: BLE001
                    logger.warning({
                        "event": "image_account_slot_release_failed",
                        "call_id": request.call_id,
                        "error": diagnostic_excerpt(release_error, 300),
                    })

        account_wait_ms = int((time.perf_counter() - account_wait_started) * 1000)
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        attempt_started = time.perf_counter()
        current_egress = {}
        current_fallback = False
        _monitor_image_stage(
            request,
            "image_account_lookup",
            account_wait_ms=account_wait_ms,
            account_email=account_email,
            account_found=bool(account),
            index=index,
            total=total,
        )
        if account_wait_ms >= 5000:
            logger.warning({
                "event": "image_account_wait_slow",
                "call_id": request.call_id,
                "account_wait_ms": account_wait_ms,
                "account_email": account_email,
                "index": index,
            })
        logger.debug({
            "event": "image_account_lookup",
            "call_id": request.call_id,
            "token_prefix": token[:12] + "..." if len(token) > 12 else token,
            "account_email": account_email,
            "account_found": bool(account),
            "account_wait_ms": account_wait_ms,
            "index": index,
        })
        backend: OpenAIBackendAPI | None = None
        egress_acquired = False
        try:
            egress_started = time.perf_counter()
            fallback_profile = None
            using_fallback_profile = fallback_retry_pending
            fallback_retry_pending = False
            if using_fallback_profile:
                fallback_profile = proxy_settings.get_fallback_profile(
                    upstream=True,
                    reserve_image_egress=True,
                )
                if fallback_profile is None:
                    raise ImageGenerationError(
                        "fallback proxy is not configured",
                        status_code=502,
                        error_type="server_error",
                        code="connection_failed",
                        account_email=account_email,
                    )
            deadline_kwargs: dict[str, Any] = (
                {"deadline_monotonic": request.deadline_monotonic} if request.deadline_monotonic > 0 else {}
            )
            backend = OpenAIBackendAPI(
                access_token=token,
                proxy_profile=fallback_profile,
                reserve_image_egress=fallback_profile is None,
                **deadline_kwargs,
            )
            backend.cancel_checker = lambda: _raise_if_request_cancelled(request)
            current_egress = _backend_egress_data(backend)
            current_fallback = using_fallback_profile
            if request.trace_image_perf:
                egress_data = _backend_egress_data(backend)
                if using_fallback_profile:
                    egress_data.update({
                        "fallback_retry": True,
                        "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                        "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    })
                _monitor_image_stage(
                    request,
                    "image_egress_waiting",
                    account_email=account_email,
                    index=index,
                    total=total,
                    **egress_data,
                )
            # 已在 get_profile 里预占的出口名额也要在失败时归还。
            egress_acquired = bool(getattr(backend.proxy_profile, "image_egress_reserved", False))
            egress_acquire_ms = proxy_settings.acquire_image_egress(backend.proxy_profile, **deadline_kwargs)
            egress_acquired = int(getattr(backend.proxy_profile, "image_concurrency_limit", 0) or 0) > 0
            egress_wait_ms = int((time.perf_counter() - egress_started) * 1000)
            if request.trace_image_perf:
                egress_data = _backend_egress_data(backend)
                if using_fallback_profile:
                    egress_data.update({
                        "fallback_retry": True,
                        "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                        "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    })
                _monitor_image_stage(
                    request,
                    "image_egress_ready",
                    egress_wait_ms=egress_wait_ms,
                    egress_acquire_ms=egress_acquire_ms,
                    account_email=account_email,
                    index=index,
                    total=total,
                    **egress_data,
                )
                logger.debug({
                    "event": "image_egress_ready",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "account_email": account_email,
                    "egress_wait_ms": egress_wait_ms,
                    "egress_acquire_ms": egress_acquire_ms,
                    **egress_data,
                })
            if request.progress_callback or request.trace_image_perf:
                backend.progress_callback = _image_progress_callback_with_monitor(
                    request,
                    index,
                    total,
                    lambda: account_email,
                )
            stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
            outputs: list[ImageOutput] = []
            stream_started = time.perf_counter()
            for output in stream_fn(backend, request, index, total):
                _raise_if_request_cancelled(request)
                if account_email and not output.account_email:
                    output.account_email = account_email
                # 上游用文字说"生图额度用完"：按额度耗尽处理（置限流、换号），不当文本结果返回。
                if output.kind == "message" and text_says_image_quota_exhausted(output.text):
                    raise ImageGenerationError(
                        output.text,
                        account_email=account_email,
                        conversation_id=output.conversation_id,
                        upstream_error=output.text,
                        raw_upstream_message=output.text,
                        failure=image_failure("image_quota_exhausted", raw_detail=output.text),
                    )
                # An upstream-side error reply is never a usable text result:
                # fail it (and switch accounts) even when messages are allowed.
                if output.kind == "message" and (
                        request.message_as_error or is_upstream_error_reply_text(output.text)
                ):
                    raise ImageGenerationError(
                        output.text or "Image generation was rejected by upstream policy.",
                        account_email=account_email,
                        conversation_id=output.conversation_id,
                        upstream_error=output.text or "",
                        raw_upstream_message=output.text or "",
                        failure=classify_image_message_text(output.text),
                    )
                emitted_for_token = True
                returned_message = output.kind == "message"
                returned_result = returned_result or output.kind == "result"
                outputs.append(output)
            stream_ms = int((time.perf_counter() - stream_started) * 1000)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_single_stream_done",
                    stream_ms=stream_ms,
                    returned_message=returned_message,
                    returned_result=returned_result,
                    account_email=account_email,
                    index=index,
                    total=total,
                )
                logger.info({
                    "event": "image_single_stream_done",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "stream_ms": stream_ms,
                    "returned_message": returned_message,
                    "returned_result": returned_result,
                    "account_email": account_email,
                })
            if returned_message:
                message_text = outputs[-1].text if outputs else ""
                # 上游给出的文字回复（内容策略拒绝等）是请求层面的结果，不记账号失败；
                # 只有我们自己合成的“结果取不回/响应不完整”提示才算上游/账号侧问题。
                synthetic_incomplete = message_text in _SYNTHETIC_INCOMPLETE_MESSAGES
                finalize_image_slot(
                    False,
                    refresh_account=synthetic_incomplete and should_refresh_image_account(message_text),
                    account_fault=synthetic_incomplete,
                )
                record_success("message")
                if outputs:
                    outputs[-1].image_attempts = list(image_attempts)
                if request.trace_image_perf:
                    _monitor_image_stage(
                        request,
                        "image_single_done",
                        total_ms=int((time.perf_counter() - single_started) * 1000),
                        status="message",
                        account_email=account_email,
                        index=index,
                        total=total,
                    )
                    logger.info({
                        "event": "image_single_done",
                        "call_id": request.call_id,
                        "model": request.model,
                        "index": index,
                        "total": total,
                        "total_ms": int((time.perf_counter() - single_started) * 1000),
                        "status": "message",
                        "account_email": account_email,
                    })
                return outputs
            if not returned_result:
                finalize_image_slot(False)
                if emitted_for_token:
                    conv_id = outputs[-1].conversation_id if outputs else ""
                    raise ImageGenerationError(
                        "upstream completed without generating images",
                        status_code=502,
                        error_type="server_error",
                        code="no_image_generated",
                        account_email=account_email,
                        conversation_id=conv_id,
                    )
                return outputs
            _cleanup_image_conversations_after_success(backend, outputs)
            finalize_image_slot(True)
            record_success("success")
            if outputs:
                outputs[-1].image_attempts = list(image_attempts)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_single_done",
                    total_ms=int((time.perf_counter() - single_started) * 1000),
                    status="success",
                    account_email=account_email,
                    index=index,
                    total=total,
                    result_size=_output_size_text(outputs),
                )
                logger.info({
                    "event": "image_single_done",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "total_ms": int((time.perf_counter() - single_started) * 1000),
                    "status": "success",
                    "account_email": account_email,
                    "result_size": _output_size_text(outputs),
                })
            return outputs
        except ImagePollTimeoutError as exc:
            if account_email:
                setattr(exc, "account_email", account_email)
            raw_error = str(exc)
            upstream_error = getattr(exc, "upstream_error", "") or getattr(exc, "last_task_error", "") or raw_error
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(upstream_error),
            )
            logger.warning({
                "event": "image_poll_timeout",
                "request_token": token,
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:200],
                "upstream_error": str(upstream_error)[:1000] if upstream_error else "",
                "last_conversation_snapshot": getattr(exc, "last_conversation_snapshot", None),
            })
            image_error = ImageGenerationError(
                raw_error,
                status_code=502,
                error_type="server_error",
                code="image_poll_timeout",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                raw_error=raw_error,
                upstream_error=str(upstream_error or ""),
                raw_upstream_message=str(getattr(exc, "last_assistant_text", "") or ""),
            )
            for attr in (
                "poll_attempts",
                "poll_timeout_secs",
                "last_task_error",
                "last_conversation_snapshot",
                "last_assistant_text",
            ):
                if hasattr(exc, attr):
                    setattr(image_error, attr, getattr(exc, attr))
            switch_account(image_error, classify_image_exception(image_error))
            raise (best_error or image_error) from exc
        except RequestCancelledError as exc:
            finalize_image_slot(False)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_cancelled",
                    status="cancelled",
                    account_email=account_email,
                    index=index,
                    total=total,
                )
            raise ImageGenerationError(
                str(exc) or "request cancelled by administrator",
                status_code=499,
                error_type="server_error",
                code="request_cancelled",
                account_email=account_email,
            ) from exc
        except ImageContentPolicyError as exc:
            finalize_image_slot(False, account_fault=False)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=str(exc),
                )
            logger.warning({
                "event": "image_stream_content_policy_error",
                "request_token": token,
                "account_email": account_email,
                "error": diagnostic_excerpt(exc, 1000),
                "index": index,
            })
            policy_error = ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                upstream_error=str(exc),
                raw_upstream_message=str(exc),
            )
            switch_account(policy_error, classify_image_exception(policy_error))
            raise (best_error or policy_error) from exc
        except ImageTextReplyError as exc:
            text_reply = str(exc) or "上游返回了文本回复而不是图片。"
            if text_says_image_quota_exhausted(text_reply):
                text_failure = image_failure("image_quota_exhausted", raw_detail=text_reply)
            else:
                text_failure = upstream_error_reply_failure(
                    image_failure("upstream_text_reply", raw_detail=text_reply).with_public_detail(text_reply)
                )
            finalize_image_slot(
                False,
                failure=text_failure,
                refresh_account=text_failure.code == "image_quota_exhausted",
            )
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_text_reply",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=text_reply,
                )
            logger.info({
                "event": "image_stream_text_reply",
                "request_token": token,
                "account_email": account_email,
                "conversation_id": getattr(exc, "conversation_id", ""),
                "message_preview": diagnostic_excerpt(text_reply, 1000),
                "index": index,
            })
            image_error = ImageGenerationError(
                text_reply,
                status_code=400,
                error_type="invalid_request_error",
                code="upstream_text_reply",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                raw_error=text_reply,
                upstream_error=str(getattr(exc, "upstream_error", "") or text_reply),
                raw_upstream_message=str(getattr(exc, "raw_upstream_message", "") or text_reply),
                failure=text_failure if text_failure.code in {"upstream_error_reply", "image_quota_exhausted"} else None,
            )
            for attr in (
                "upstream_message_preview",
                "last_conversation_snapshot",
                "last_assistant_text",
            ):
                if hasattr(exc, attr):
                    setattr(image_error, attr, getattr(exc, attr))
            if switch_account(image_error, text_failure):
                continue
            raise (best_error or image_error) from exc
        except ImageGenerationError as exc:
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            error_text = str(exc)
            failure = classify_image_exception(exc)
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(error_text),
                failure=failure,
            )
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=error_text,
                )
            logger.warning({
                "event": "image_stream_generation_error",
                "request_token": token,
                "account_email": account_email,
                "failure_code": failure.code,
                "error": diagnostic_excerpt(error_text, 1000),
                "index": index,
            })
            if switch_account(exc, failure):
                continue
            if best_error is not None and best_error is not exc:
                raise best_error from exc
            raise
        except ProxyReferenceUnavailableError as exc:
            # 代理引用解析不出（缺失/停用/无节点）：绝不回落直连，按明确失败码报错，不记账号失败。
            proxy_failure = image_failure("proxy_unavailable", raw_detail=str(exc))
            finalize_image_slot(False, failure=proxy_failure, account_fault=False)
            proxy_error = ImageGenerationError(
                str(exc) or "proxy is unavailable",
                account_email=account_email,
                failure=proxy_failure,
            )
            record_attempt(proxy_failure)
            final_error(proxy_error, proxy_failure)
            raise (best_error or proxy_error) from exc
        except ImageEgressDeadlineError as exc:
            # 等出口并发等到请求截止：本次没开始生成，不记账号失败，直接按繁忙超时报错。
            busy_failure = image_failure("account_busy_timeout", raw_detail=str(exc))
            finalize_image_slot(False, failure=busy_failure, account_fault=False)
            busy_error = ImageGenerationError(
                str(exc) or "image request deadline exceeded while waiting for egress capacity",
                account_email=account_email,
                failure=busy_failure,
            )
            record_attempt(busy_failure)
            final_error(busy_error, busy_failure)
            raise (best_error or busy_error) from exc
        except Exception as exc:
            last_error = str(exc)
            stream_error_ms = int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0
            http_timing = _backend_http_timing_data(backend)
            if request.trace_image_perf and stream_error_ms > 0:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=stream_error_ms,
                    stream_ms=stream_error_ms,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=last_error,
                    **http_timing,
                )
            logger.warning({
                "event": "image_stream_fail",
                "request_token": token,
                "account_email": account_email,
                "error": diagnostic_excerpt(last_error, 1000),
                "stream_error_ms": stream_error_ms,
                "index": index,
                **http_timing,
            })
            if not emitted_for_token and is_token_invalid_error(last_error):
                finalize_image_slot(False)
                refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                if refreshed_token and refreshed_token != token:
                    token = refreshed_token
                    continue
                account_service.handle_invalid_token(token, "image_stream", error=last_error)
                continue
            quick_timeout_retry_ms = min(30000, max(5000, int(config.image_stream_timeout_secs * 1000 * 0.2)))
            early_connection_failure = (
                not emitted_for_token
                and (stream_error_ms == 0 or stream_error_ms <= quick_timeout_retry_ms)
                and (is_tls_connection_error(last_error) or is_connection_timeout_error(last_error))
            )
            fallback_reference = proxy_settings.get_fallback_proxy_reference()
            if early_connection_failure and fallback_reference and not fallback_retry_used:
                fallback_retry_used = True
                fallback_retry_pending = True
                record_attempt(classify_image_exception(exc), retry_kind="egress_fallback")
                retry_token = token
                fallback_from_egress = _backend_egress_data(backend) if backend is not None else {}
                logger.warning({
                    "event": "image_stream_fallback_retry",
                    "request_token": token,
                    "account_email": account_email,
                    "index": index,
                    "fallback_proxy_configured": True,
                    "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                    "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    "stream_error_ms": stream_error_ms,
                    "error": last_error[:200],
                })
                if request.trace_image_perf:
                    _monitor_image_stage(
                        request,
                        "image_egress_fallback_retry",
                        account_email=account_email,
                        index=index,
                        total=total,
                        status="retrying",
                        fallback_from_egress_key=fallback_from_egress.get("egress_key", ""),
                        fallback_from_egress_label=fallback_from_egress.get("egress_label", ""),
                    )
                continue
            failure = classify_image_exception(exc)
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(last_error),
                failure=failure,
                upstream_status=int(exc.status_code) if isinstance(exc, UpstreamHTTPError) else 0,
            )
            stream_error = ImageGenerationError(
                image_stream_error_message(last_error),
                account_email=account_email,
                conversation_id=str(getattr(exc, "conversation_id", "") or ""),
                raw_error=last_error,
                upstream_error=last_error,
                failure=failure if isinstance(exc, ImageDownloadError) else None,
            )
            if switch_account(stream_error, failure):
                continue
            raise (best_error or stream_error) from exc
        finally:
            if egress_acquired and backend is not None:
                proxy_settings.release_image_egress(backend.proxy_profile)
            if backend is not None:
                backend.close()


def _select_image_pool_error(errors: dict[int, Exception]) -> Exception | None:
    """Select one deterministic failure across slots and retain every slot's attempts.

    Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """
    if not errors:
        return None

    def score(item: tuple[int, Exception]) -> tuple[int, int]:
        slot, exc = item
        return (image_failure_priority(classify_image_exception(exc)), -slot)

    _, selected = max(errors.items(), key=score)
    if getattr(selected, "failure", None) is None:
        try:
            setattr(selected, "failure", classify_image_exception(selected))
        except Exception:
            pass
    attempts: list[dict[str, Any]] = []
    for slot, exc in sorted(errors.items()):
        raw_attempts = getattr(exc, "image_attempts", None)
        if not isinstance(raw_attempts, (list, tuple)):
            continue
        for attempt_number, raw_attempt in enumerate(raw_attempts, start=1):
            if not isinstance(raw_attempt, dict):
                continue
            attempt = dict(raw_attempt)
            attempt.setdefault("slot", slot)
            attempt.setdefault("attempt", attempt_number)
            if attempt not in attempts:
                attempts.append(attempt)
    attempts.sort(key=lambda item: (int(item.get("slot") or 0), int(item.get("attempt") or 0)))
    if attempts:
        setattr(selected, "image_attempts", attempts)
    return selected


def _pool_failure_error(errors: dict[int, Exception], last_error: str, attempts: list[dict[str, Any]]) -> Exception:
    """Error raised when no slot produced an image."""
    selected = _select_image_pool_error(errors)
    if isinstance(selected, ImageGenerationError):
        if attempts and not selected.image_attempts:
            selected.image_attempts = list(attempts)
        return selected
    if not last_error:
        last_error = "no account in the pool could generate images — check account quota and rate-limit status"
    pool_error = ImageGenerationError(
        image_stream_error_message(last_error),
        conversation_id="",
        raw_error=last_error,
        upstream_error=last_error,
    )
    pool_error.image_attempts = list(attempts)
    return pool_error


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。

    整个请求共享一个截止时间（request.deadline_monotonic）：排队等号、等出口并发、
    断流恢复轮询都受它约束；串行模式下某一张失败不会中止后续，有成功就返回成功的。
    """
    if not is_supported_image_model(request.model):
        _monitor_image_stage(
            request,
            "image_local_rejected",
            local_reason="unsupported_model",
            status="failed",
        )
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))

    if request.deadline_monotonic <= 0:
        request.deadline_monotonic = time.monotonic() + max(0.001, float(config.image_request_timeout_secs))

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(replace(request), 1, 1)
        if not outputs:
            raise ImageGenerationError(
                "image generation completed without output",
                failure=image_failure("no_image_generated"),
            )
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        serial_errors: dict[int, Exception] = {}
        serial_failed_attempts: list[dict[str, Any]] = []
        serial_succeeded = False
        for index in range(1, request.n + 1):
            try:
                outputs = _generate_single_image(replace(request), index, request.n)
            except Exception as exc:
                serial_errors[index] = exc
                serial_failed_attempts.extend(getattr(exc, "image_attempts", None) or [])
                logger.warning({
                    "event": "image_serial_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })
                if isinstance(exc, RequestCancelledError) or getattr(exc, "code", "") == "request_cancelled":
                    break
                if time.monotonic() >= request.deadline_monotonic:
                    break
                continue
            if serial_failed_attempts and outputs:
                outputs[-1].image_attempts = [*outputs[-1].image_attempts, *serial_failed_attempts]
                serial_failed_attempts = []
            if any(output.kind == "result" and output.data for output in outputs):
                serial_succeeded = True
            for output in outputs:
                yield output
        if serial_succeeded:
            for index, exc in serial_errors.items():
                logger.warning({
                    "event": "image_serial_partial_failure",
                    "failed_index": index,
                    "error": str(exc)[:200],
                })
            if serial_failed_attempts:
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=request.n,
                    total=request.n,
                    image_attempts=list(serial_failed_attempts),
                )
            return
        if serial_errors:
            raise _pool_failure_error(serial_errors, "", serial_failed_attempts)
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures: dict[Any, int] = {}
    errors: dict[int, Exception] = {}
    processed_futures: set[Any] = set()
    emitted = False
    last_error = ""
    failed_slot_attempts: list[dict[str, Any]] = []
    executor = ThreadPoolExecutor(max_workers=request.n)

    def consume_future(future: Any) -> list[ImageOutput]:
        nonlocal emitted, last_error, failed_slot_attempts
        index = futures[future]
        processed_futures.add(future)
        try:
            outputs = future.result()
        except Exception as exc:
            errors[index] = exc
            failed_slot_attempts.extend(getattr(exc, "image_attempts", None) or [])
            last_error = str(exc)
            logger.warning({
                "event": "image_parallel_generation_error",
                "index": index,
                "error": last_error[:300],
            })
            if not emitted:
                logger.warning({
                    "event": "image_parallel_failure_before_success",
                    "failed_index": index,
                    "error": last_error[:200],
                })
            return []
        if failed_slot_attempts and outputs:
            # 先失败的槽位尝试记录挂到后面成功的输出上，日志层才能看到完整时间线。
            outputs[-1].image_attempts = [*outputs[-1].image_attempts, *failed_slot_attempts]
            failed_slot_attempts = []
        if outputs:
            emitted = True
        return outputs

    try:
        for index in range(1, request.n + 1):
            futures[executor.submit(_generate_single_image, replace(request), index, request.n)] = index

        # yield 结果：按完成顺序立即输出，不再等所有图片都结束后才返回成功结果。
        try:
            remaining = max(0.0, request.deadline_monotonic - time.monotonic())
            for future in as_completed(futures, timeout=remaining):
                for output in consume_future(future):
                    yield output
        except FuturesTimeoutError:
            pending = [future for future in futures if future not in processed_futures]
            done, _ = wait(pending, timeout=0.05)
            for future in done:
                for output in consume_future(future):
                    yield output
            for future in pending:
                if future in processed_futures:
                    continue
                future.cancel()
                errors[futures[future]] = ImageGenerationError(
                    f"image slot {futures[future]} exceeded the request deadline",
                    failure=image_failure("task_interrupted"),
                )
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    # 所有成功输出都已产出之后才失败的槽位：其尝试记录没有后续输出可挂，
    # 用一个不带文本的尾部输出带给日志层（对客户端不可见）。
    if emitted and failed_slot_attempts:
        yield ImageOutput(
            kind="progress",
            model=request.model,
            index=request.n,
            total=request.n,
            image_attempts=list(failed_slot_attempts),
        )

    # 如果有失败但也有成功，记录警告
    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })
        return

    raise _pool_failure_error(errors, last_error, failed_slot_attempts)


def _image_stream_payload(output: ImageOutput, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    item = {"type": event_type, **payload}
    if output.account_email:
        item["_account_email"] = output.account_email
    if output.conversation_id:
        item["_conversation_id"] = output.conversation_id
    if output.image_attempts:
        item["_image_attempts"] = list(output.image_attempts)
    return item


def stream_image_chunks(
    outputs: Iterable[ImageOutput],
    event_prefix: str = "image_generation",
    usage_builder: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    partial_images: object = 0,
) -> Iterator[dict[str, Any]]:
    prefix = str(event_prefix or "image_generation").strip() or "image_generation"
    emitted = False
    last_output: ImageOutput | None = None
    # ChatGPT Web only gives us final image bytes here. Emitting those bytes as a
    # synthetic partial_image makes some clients display the same image twice.
    for output in outputs:
        last_output = output
        if output.kind == "result":
            for item_index, item in enumerate(output.data):
                if not isinstance(item, dict):
                    continue
                b64_json = str(item.get("b64_json") or "").strip()
                image_url = str(item.get("url") or "").strip()
                if not b64_json and not image_url:
                    continue
                completed: dict[str, Any] = {}
                if b64_json:
                    completed["b64_json"] = b64_json
                if image_url:
                    completed["url"] = image_url
                if usage_builder:
                    usage = usage_builder([item])
                    if usage:
                        completed["usage"] = usage
                completed_payload = _image_stream_payload(output, f"{prefix}.completed", completed)
                if image_url:
                    completed_payload["_image_urls"] = [image_url]
                if item.get("width") and item.get("height"):
                    completed_payload["_image_result"] = {"width": item["width"], "height": item["height"]}
                emitted = True
                yield completed_payload
        elif output.kind == "message" and output.text:
            emitted = True
            yield _image_stream_payload(
                output,
                f"{prefix}.failed",
                {"error": {
                    "message": output.text,
                    "type": "image_generation_error",
                    "code": classify_image_message_text(output.text).code,
                }},
            )
        elif output.kind == "progress" and not output.text and output.image_attempts:
            # 仅供日志层收集尝试记录，LoggedCall 不会把它转发给客户端。
            yield {"_log_only": True, "_image_attempts": list(output.image_attempts)}
    if not emitted:
        # 一张图都没发出去时必须明确失败，否则客户端收到空流、日志却记成功。
        failed = {"error": {
            "message": "image generation returned no image",
            "type": "image_generation_error",
            "code": "no_image_generated",
        }}
        if last_output is not None:
            yield _image_stream_payload(last_output, f"{prefix}.failed", failed)
        else:
            yield {"type": f"{prefix}.failed", **failed}


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    attempts: list[dict[str, Any]] = []
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.image_attempts:
            attempts.extend(item for item in output.image_attempts if item not in attempts)
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    if attempts:
        result["_image_attempts"] = attempts
    return result

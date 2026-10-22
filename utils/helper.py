import base64
import hashlib
import json
import mimetypes
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END
from fastapi import HTTPException
from services.proxy_service import proxy_settings
from utils.log import logger
from utils.remote_image import fetch_public_image

# 2.5 系列是兼容名，走 ChatGPT Web 的 auto 路由，不强制特定画图引擎。
WEB_IMAGE_MODELS = (
    "gpt-image-2",
    "gpt-image-2.5",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
)
BASE_IMAGE_MODELS = {*WEB_IMAGE_MODELS, "codex-gpt-image-2"}
IMAGE_MODEL_PLAN_TYPES = ("plus", "team", "pro")
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
PREFIXED_CODEX_IMAGE_MODELS = {
    f"{plan_type}-{CODEX_IMAGE_MODEL}"
    for plan_type in IMAGE_MODEL_PLAN_TYPES
}
IMAGE_MODELS = BASE_IMAGE_MODELS | PREFIXED_CODEX_IMAGE_MODELS
PUBLIC_IMAGE_MODELS = BASE_IMAGE_MODELS | PREFIXED_CODEX_IMAGE_MODELS
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

SUPPORTED_JSON_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
MAX_JSON_IMAGE_BYTES = 10 * 1024 * 1024
MAX_JSON_EDIT_IMAGES = 10
DATA_URL_IMAGE_RE = re.compile(r"^data:(?P<mime>[-+./\w]+);base64,(?P<data>.*)$", re.DOTALL)
REMOTE_IMAGE_TIMEOUT_SECONDS = 20


def _image_extension(mime_type: str) -> str:
    image_type = mime_type.split("/", 1)[1].split(";", 1)[0].lower() if "/" in mime_type else "png"
    return "jpg" if image_type == "jpeg" else image_type or "png"


def _decode_json_image_string(value: str, index: int, filename: str | None = None, mime_type: str | None = None) -> tuple[bytes, str, str]:
    text = value.strip()
    if not text:
        raise HTTPException(status_code=400, detail={"error": "image file is empty"})
    match = DATA_URL_IMAGE_RE.match(text)
    if match:
        resolved_mime = (match.group("mime") or "image/png").lower()
        encoded = match.group("data")
    else:
        if text.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail={"error": "remote image URLs are not supported"})
        resolved_mime = (mime_type or "image/png").lower()
        encoded = text
    if resolved_mime == "image/jpg":
        resolved_mime = "image/jpeg"
    if resolved_mime not in SUPPORTED_JSON_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=400, detail={"error": "unsupported image mime type"})
    try:
        image_data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid base64 image data"}) from exc
    if not image_data:
        raise HTTPException(status_code=400, detail={"error": "image file is empty"})
    if len(image_data) > MAX_JSON_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail={"error": "image file is too large"})
    return image_data, filename or f"image_{index}.{_image_extension(resolved_mime)}", resolved_mime


def _extract_json_image_value(item: object) -> tuple[str, str | None, str | None]:
    if isinstance(item, str):
        return item, None, None
    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail={"error": "image entry must be a base64 string or object"})
    filename = str(item.get("filename") or item.get("file_name") or "").strip() or None
    mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip() or None
    value = item.get("b64_json") or item.get("base64")
    if not value:
        image_url = item.get("image_url") or item.get("url")
        if isinstance(image_url, dict):
            filename = filename or str(image_url.get("filename") or image_url.get("file_name") or "").strip() or None
            mime_type = mime_type or str(image_url.get("mime_type") or image_url.get("mimeType") or "").strip() or None
            value = image_url.get("url") or image_url.get("image_url")
        else:
            value = image_url
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail={"error": "image entry must include image data"})
    return value, filename, mime_type


def normalize_json_edit_images(image: object = None, images: object = None) -> list[tuple[bytes, str, str]]:
    raw_images = images if images is not None else image
    if raw_images is None:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    entries = raw_images if isinstance(raw_images, list) else [raw_images]
    if not entries:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    if len(entries) > MAX_JSON_EDIT_IMAGES:
        raise HTTPException(status_code=400, detail={"error": f"images supports up to {MAX_JSON_EDIT_IMAGES} items"})
    normalized = []
    for index, item in enumerate(entries, start=1):
        value, filename, mime_type = _extract_json_image_value(item)
        normalized.append(_decode_json_image_string(value, index, filename, mime_type))
    return normalized


def new_uuid() -> str:
    return str(uuid.uuid4())


def split_image_model(model: object) -> tuple[str | None, str | None]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None, None
    if normalized in BASE_IMAGE_MODELS:
        return None, normalized
    for plan_type in IMAGE_MODEL_PLAN_TYPES:
        prefix = f"{plan_type}-"
        if normalized.startswith(prefix):
            base_model = normalized[len(prefix):]
            if base_model == CODEX_IMAGE_MODEL:
                return plan_type, base_model
    return None, None


def is_supported_image_model(model: object) -> bool:
    _, base_model = split_image_model(model)
    return base_model is not None


def is_codex_image_model(model: object) -> bool:
    _, base_model = split_image_model(model)
    return base_model == CODEX_IMAGE_MODEL


def is_image_chat_request(body: dict[str, object]) -> bool:
    model = str(body.get("model") or "").strip()
    modalities = body.get("modalities")
    if is_supported_image_model(model):
        return True
    return isinstance(modalities, list) and "image" in {str(item or "").strip().lower() for item in modalities}


_UPSTREAM_BODY_LOG_LIMIT = 500
UPSTREAM_CREDENTIAL_SCOPES = frozenset({"account", "signed_asset", "public"})


class UpstreamHTTPError(RuntimeError):
    """Raised when an upstream HTTP call returns a non-2xx status.

    Carries structured fields (status_code, body, retry_after) so callers can
    branch on status code instead of string-matching on str(exc). The full
    body is preserved on the instance; the formatted message truncates it
    to keep log lines reasonable.
    """

    def __init__(
        self,
        context: str,
        status_code: int,
        body: Any,
        retry_after: int | None = None,
        credential_scope: str = "account",
    ) -> None:
        self.context = context
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        # account: 请求带的是账号凭据，401/403 可能说明账号失效；
        # signed_asset: CDN 签名 URL（上传/下载），401/403 只说明链接过期或被拒，与账号无关；
        # public: 不带凭据的公开页面。
        self.credential_scope = (
            credential_scope
            if credential_scope in UPSTREAM_CREDENTIAL_SCOPES
            else "account"
        )
        if isinstance(body, (dict, list)):
            try:
                body_str = json.dumps(body, ensure_ascii=False)
            except (TypeError, ValueError):
                body_str = repr(body)
        else:
            body_str = str(body)
        if len(body_str) > _UPSTREAM_BODY_LOG_LIMIT:
            body_str = body_str[:_UPSTREAM_BODY_LOG_LIMIT] + "…[truncated]"
        super().__init__(f"{context} failed: status={status_code}, body={body_str}")


_ERROR_BODY_READ_LIMIT = 64 * 1024


def read_error_body(response: Any) -> Any:
    """读取失败响应的正文，能解析成 JSON 就返回 JSON。

    stream=True 的响应 .text 是空的（正文还在流里），需要从 iter_content 读最多
    _ERROR_BODY_READ_LIMIT 字节，否则上游的结构化错误码（额度、内容策略、挑战页）会丢。
    """
    text: Any = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    if not isinstance(text, (str, bytes)):
        text = ""
    if not text and hasattr(response, "iter_content"):
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in response.iter_content():
                if not chunk:
                    continue
                data = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                chunks.append(data)
                size += len(data)
                if size >= _ERROR_BODY_READ_LIMIT:
                    break
        except Exception:
            pass
        text = b"".join(chunks)[:_ERROR_BODY_READ_LIMIT]
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    try:
        return json.loads(text) if text else text
    except (TypeError, ValueError):
        return text


def ensure_ok(
    response: requests.Response,
    context: str,
    *,
    credential_scope: str = "account",
) -> None:
    if 200 <= response.status_code < 300:
        return
    body = read_error_body(response)
    retry_after_header = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    retry_after: int | None = None
    if retry_after_header is not None:
        ra_str = str(retry_after_header).strip()
        if ra_str.isdigit():
            retry_after = int(ra_str)
    raise UpstreamHTTPError(
        context,
        response.status_code,
        body,
        retry_after=retry_after,
        credential_scope=credential_scope,
    )


def stream_error_body(exc: BaseException) -> dict:
    """流式错误帧：{message,type,param,code}，type/code 是结构化值，绝不是 Python 类名。

    带 to_openai_error 的异常（如 ImageGenerationError）用它自己的投影（已不含账号邮箱）；
    其余异常按图片失败分类得到 type/code，上游 HTTP 细节不外露。
    """
    if hasattr(exc, "to_openai_error"):
        try:
            return exc.to_openai_error()
        except Exception:  # noqa: BLE001 - 投影失败时退回通用分类
            pass
    error_type, code, message = "server_error", "upstream_error", str(exc)
    try:
        from services.image_failure import classify_image_exception

        failure = classify_image_exception(exc)
        error_type, code = failure.error_type, failure.code
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, UpstreamHTTPError) or any(
        marker in message.lower() for marker in ("backend-api/", "status=", "body=", "chatgpt.com")
    ):
        message = "Upstream request failed. Please try again later."
    return {"error": {"message": message, "type": error_type, "param": None, "code": code}}


def sse_json_stream(items) -> Iterator[str]:
    yield ": stream-open\n\n"
    try:
        for item in items:
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
    except Exception as exc:
        logger.warning({
            "event": "sse_stream_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        })
        yield f"data: {json.dumps(stream_error_body(exc), ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def image_sse_stream(items) -> Iterator[str]:
    try:
        for item in items:
            event = str(item.get("type") or "message") if isinstance(item, dict) else "message"
            yield f"event: {event}\n"
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
    except Exception as exc:
        logger.warning({
            "event": "image_sse_stream_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        })
        yield "event: error\n"
        yield f"data: {json.dumps(stream_error_body(exc), ensure_ascii=False)}\n\n"


def anthropic_sse_stream(items) -> Iterator[str]:
    try:
        for item in items:
            event = str(item.get("type") or "message_delta") if isinstance(item, dict) else "message_delta"
            yield f"event: {event}\n"
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
    except Exception as exc:
        logger.warning({
            "event": "anthropic_sse_stream_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        })
        error = {"type": "error", "error": {"type": "api_error", "message": str(exc)}}
        yield "event: error\n"
        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"


def _format_timeout_secs(value: float) -> str:
    if value >= 10:
        return f"{value:.0f}s"
    if value >= 1:
        return f"{value:.1f}s"
    return f"{value:.3f}s"


def iter_sse_payloads(
    response: requests.Response,
    max_duration_secs: float | None = None,
    cancel_checker: Callable[[], None] | None = None,
    stop_at: Callable[[], float | None] | None = None,
) -> Iterator[str]:
    """Yield SSE data payloads.

    ``max_duration_secs`` is a hard limit (raises TimeoutError). ``stop_at`` returns
    an optional monotonic time after which the stream is closed and iteration ends
    normally, so callers can hand off to another path without treating it as an error.
    """
    started_at = time.monotonic()
    timeout_secs = float(max_duration_secs or 0)
    pending: bytes | None = None
    aborted = False

    def _timeout_error() -> TimeoutError:
        return TimeoutError(f"SSE stream exceeded {_format_timeout_secs(timeout_secs)}")

    def _remaining_secs() -> float | None:
        if timeout_secs <= 0:
            return None
        return timeout_secs - (time.monotonic() - started_at)

    def _raise_if_timeout() -> None:
        if cancel_checker is not None:
            try:
                cancel_checker()
            except Exception:
                _abort_stream_nonblocking()
                raise
        remaining = _remaining_secs()
        if remaining is not None and remaining <= 0:
            _abort_stream_nonblocking()
            raise _timeout_error()

    def _soft_stop_reached() -> bool:
        if stop_at is None:
            return False
        deadline = stop_at()
        return deadline is not None and time.monotonic() >= deadline

    def _abort_stream_nonblocking() -> None:
        """Cancel curl_cffi streaming without waiting for its stream task.

        curl_cffi's Response.iter_content() blocks on an internal queue.get()
        without a timeout. Calling response.close() from a watchdog can block on
        stream_task.result(), so enforce our own deadline and only signal/close
        the underlying curl handle here.
        """
        nonlocal aborted
        aborted = True
        try:
            if getattr(response, "quit_now", None):
                response.quit_now.set()
        except Exception:
            pass
        try:
            if getattr(response, "curl", None):
                response.curl.close()
        except Exception:
            pass
        try:
            response._stream_closed = True
        except Exception:
            pass

    def _iter_chunks() -> Iterator[bytes]:
        stream_queue = getattr(response, "queue", None)
        if stream_queue is None:
            yield from _iter_response_content_with_deadline()
            return
        while True:
            _raise_if_timeout()
            if _soft_stop_reached():
                _abort_stream_nonblocking()
                return
            remaining = _remaining_secs()
            wait_secs = 1.0 if remaining is None else max(0.001, min(1.0, remaining))
            try:
                chunk = stream_queue.get(timeout=wait_secs)
            except queue.Empty:
                _raise_if_timeout()
                continue
            if isinstance(chunk, RequestException):
                try:
                    response.close()
                except Exception:
                    pass
                raise chunk
            if chunk is STREAM_END:
                break
            yield chunk

    def _iter_response_content_with_deadline() -> Iterator[bytes]:
        """Iterate response.iter_content() without letting a blocking read bypass our deadline.

        Most curl_cffi streaming responses expose ``response.queue`` and are handled above.
        Some proxy/transport combinations do not, and a direct ``iter_content()`` call can
        block past ``max_duration_secs``. Move the blocking read to a daemon producer so the
        consumer can keep checking cancellation/timeout and close the underlying stream.
        """
        item_queue: queue.Queue[object] = queue.Queue()
        done = object()

        def _produce() -> None:
            try:
                for item in response.iter_content():
                    item_queue.put(item)
            except Exception as exc:
                item_queue.put(exc)
            finally:
                item_queue.put(done)

        threading.Thread(target=_produce, name="sse-response-reader", daemon=True).start()
        while True:
            _raise_if_timeout()
            if _soft_stop_reached():
                _abort_stream_nonblocking()
                return
            remaining = _remaining_secs()
            wait_secs = 1.0 if remaining is None else max(0.001, min(1.0, remaining))
            try:
                item = item_queue.get(timeout=wait_secs)
            except queue.Empty:
                _raise_if_timeout()
                continue
            if item is done:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    try:
        for chunk in _iter_chunks():
            if pending is not None:
                chunk = pending + chunk
            lines = chunk.splitlines()
            pending = lines.pop() if lines and chunk and lines[-1] and lines[-1][-1] == chunk[-1] else None
            for raw_line in lines:
                _raise_if_timeout()
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload:
                    yield payload
        if pending:
            line = pending.decode("utf-8", errors="ignore") if isinstance(pending, bytes) else str(pending)
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    yield payload
    except Exception as exc:
        if timeout_secs > 0 and _remaining_secs() is not None and _remaining_secs() <= 0 and not isinstance(exc, TimeoutError):
            _abort_stream_nonblocking()
            raise _timeout_error() from exc
        raise
    finally:
        if not aborted:
            try:
                response.close()
            except Exception:
                pass


def save_images_from_text(text: str, prefix: str) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    matches = re.findall(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", text or "")
    saved_paths: list[Path] = []
    timestamp = int(time.time() * 1000)
    for index, data_url in enumerate(matches, start=1):
        header, encoded = data_url.split(",", 1)
        image_type = header.split(";")[0].removeprefix("data:image/").strip() or "png"
        extension = "jpg" if image_type == "jpeg" else image_type
        output_path = OUTPUT_DIR / f"{prefix}_{timestamp}_{index}.{extension}"
        output_path.write_bytes(base64.b64decode(encoded))
        saved_paths.append(output_path)
    return saved_paths


def anonymize_token(token: object) -> str:
    value = str(token or "").strip()
    if not value:
        return "token:empty"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"token:{digest}"


def extract_response_prompt(input_value: object) -> str:
    if isinstance(input_value, str):
        return input_value.strip()
    if isinstance(input_value, dict):
        role = str(input_value.get("role") or "").strip().lower()
        if role and role != "user":
            return ""
        return extract_prompt_from_message_content(input_value.get("content"))
    if not isinstance(input_value, list):
        return ""
    prompt_parts: list[str] = []
    for item in input_value:
        if isinstance(item, dict) and str(item.get("type") or "").strip() == "input_text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
            continue
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role and role != "user":
            continue
        prompt = extract_prompt_from_message_content(item.get("content"))
        if prompt:
            prompt_parts.append(prompt)
    return "\n".join(prompt_parts).strip()


def has_response_image_generation_tool(body: dict[str, object]) -> bool:
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and str(tool.get("type") or "").strip() == "image_generation":
                return True
    tool_choice = body.get("tool_choice")
    return isinstance(tool_choice, dict) and str(tool_choice.get("type") or "").strip() == "image_generation"


def extract_prompt_from_message_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
        elif item_type == "input_text":
            text = str(item.get("text") or item.get("input_text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _message_image_url(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("url") or value.get("image_url") or "").strip()
    return str(value or "").strip()


def _decode_message_image_url(value: object) -> tuple[bytes, str] | None:
    source = _message_image_url(value)
    if source.startswith("data:"):
        header, _, data = source.partition(",")
        mime = header.split(";")[0].removeprefix("data:") or "image/png"
        return base64.b64decode(data), mime
    if not source.startswith(("http://", "https://")):
        return None
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    remote = fetch_public_image(
        source,
        max_bytes=MAX_JSON_IMAGE_BYTES,
        timeout=REMOTE_IMAGE_TIMEOUT_SECONDS,
        user_agent="chatgpt2api vision fetcher",
        session_kwargs=proxy_settings.build_session_kwargs(),
    )
    image_data = remote.data
    mime = str(remote.content_type or "image/png").split(";", 1)[0].lower()
    guessed_mime = mimetypes.guess_type(remote.path)[0] or ""
    if mime and not mime.startswith("image/") and mime not in {"application/octet-stream", "binary/octet-stream"}:
        raise HTTPException(status_code=400, detail={"error": "image_url must point to an image"})
    if not mime.startswith("image/") and guessed_mime.startswith("image/"):
        mime = guessed_mime
    if not mime.startswith("image/"):
        mime = "image/png"
    return image_data, mime


def _decode_message_image_object(item: dict[str, object]) -> tuple[bytes, str] | None:
    data = item.get("data")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), str(item.get("mime") or item.get("mime_type") or "image/png")
    for key in ("image_url", "url"):
        image = _decode_message_image_url(item.get(key))
        if image:
            return image
    value = item.get("b64_json") or item.get("base64")
    if isinstance(value, str) and value.strip():
        image_data, _, mime = _decode_json_image_string(
            value,
            1,
            mime_type=str(item.get("mime") or item.get("mime_type") or item.get("mimeType") or "image/png"),
        )
        return image_data, mime
    source = item.get("source")
    if isinstance(source, dict) and str(source.get("type") or "") == "base64":
        encoded = str(source.get("data") or "")
        mime = str(source.get("media_type") or source.get("mime_type") or "image/png")
        image_data, _, resolved_mime = _decode_json_image_string(encoded, 1, mime_type=mime)
        return image_data, resolved_mime
    return None


def extract_image_from_message_content(content: object) -> list[tuple[bytes, str]]:
    if not isinstance(content, list):
        return []
    images = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "image_url":
            image = _decode_message_image_url(item.get("image_url") or item.get("url") or item)
            if image:
                images.append(image)
        elif item_type in {"input_image", "image"}:
            image = _decode_message_image_object(item)
            if image:
                images.append(image)
    return images


def extract_chat_image(body: dict[str, object]) -> list[tuple[bytes, str]]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        images = extract_image_from_message_content(message.get("content"))
        if images:
            return images
    return []


def extract_chat_prompt(body: dict[str, object]) -> str:
    direct_prompt = str(body.get("prompt") or "").strip()
    if direct_prompt:
        return direct_prompt
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    prompt_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        prompt = extract_prompt_from_message_content(message.get("content"))
        if prompt:
            prompt_parts.append(prompt)
    return "\n".join(prompt_parts).strip()


def parse_image_count(raw_value: object) -> int:
    try:
        value = int(raw_value or 1)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "n must be an integer"}) from exc
    if value < 1 or value > 4:
        raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
    return value


def build_chat_image_markdown_content(image_result: dict[str, object]) -> str:
    image_items = image_result.get("data") if isinstance(image_result.get("data"), list) else []
    markdown_images: list[str] = []
    for index, item in enumerate(image_items, start=1):
        if not isinstance(item, dict):
            continue
        image_url = str(item.get("url") or item.get("image_url") or "").strip()
        if image_url:
            markdown_images.append(f"![image_{index}]({image_url})")
            continue
        b64_json = str(item.get("b64_json") or "").strip()
        if b64_json:
            markdown_images.append(f"![image_{index}](data:image/png;base64,{b64_json})")
    return "\n\n".join(markdown_images) if markdown_images else "Image generation completed."

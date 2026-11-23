"""图片输出格式 output_format：校验与结果转换（png / jpeg / webp / gif，jpg 视为 jpeg）。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only)。转换放在放大之后，避免放大步骤把结果重新存成 PNG。

gif 不是本地转换出来的：官网生图模型看到提示词里的“生成格式为 gif。”会直接画出多帧动图
（2026-11-20 实测，frame_count / frame_duration_ms 也会照做），网关只负责把提示附上、原样保留 GIF 字节。
"""
from __future__ import annotations

import io
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

IMAGE_OUTPUT_FORMATS = ("png", "jpeg", "webp", "gif")
_ALIASES = {"jpg": "jpeg"}
_MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
# GIF 帧参数的允许范围；只在 output_format=gif 时生效。
FRAME_COUNT_RANGE = (2, 60)
FRAME_DURATION_MS_RANGE = (20, 5000)


def _invalid(param: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": f"invalid_{param}",
            }
        },
    )


def normalize_image_output_format(value: object) -> str | None:
    """空值返回 None（保持上游原样）；不支持的格式抛 400。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = _ALIASES.get(text, text)
    if normalized not in IMAGE_OUTPUT_FORMATS:
        raise _invalid("output_format", f"unsupported output_format: {text}; supported: png, jpeg, jpg, webp, gif")
    return normalized


def _normalize_frame_option(value: object, param: str, bounds: tuple[int, int]) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise _invalid(param, f"{param} must be an integer") from None
    low, high = bounds
    if not low <= number <= high:
        raise _invalid(param, f"{param} must be between {low} and {high}")
    return number


def image_output_options(source: Mapping[str, Any] | None) -> dict[str, Any]:
    """从请求体取出 output_format 与 GIF 帧参数（校验并归一化）；帧参数只在 gif 时保留。"""
    source = source or {}
    output_format = normalize_image_output_format(source.get("output_format"))
    frame_count = _normalize_frame_option(source.get("frame_count"), "frame_count", FRAME_COUNT_RANGE)
    frame_duration_ms = _normalize_frame_option(
        source.get("frame_duration_ms"), "frame_duration_ms", FRAME_DURATION_MS_RANGE
    )
    if output_format != "gif":
        frame_count = frame_duration_ms = None
    return {"output_format": output_format, "frame_count": frame_count, "frame_duration_ms": frame_duration_ms}


def gif_prompt_hints(output_format: object, frame_count: object = None, frame_duration_ms: object = None) -> list[str]:
    """要 GIF 时附在提示词末尾的几行（与上游同一写法）。"""
    if str(output_format or "").strip().lower() != "gif":
        return []
    lines = ["生成格式为 gif。"]
    if frame_count:
        lines.append(f"frame_count: {frame_count}")
    if frame_duration_ms:
        lines.append(f"frame_duration_ms: {frame_duration_ms}")
    return lines


def is_gif_bytes(image_data: bytes | None) -> bool:
    return bool(image_data) and bytes(image_data[:4]) == b"GIF8"


def image_output_mime_type(output_format: str | None) -> str:
    return _MIME_TYPES.get(str(output_format or ""), "image/png")


def _source_format(image_data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(image_data)) as image:
            return str(image.format or "").lower()
    except Exception:
        return ""


def convert_image_bytes(image_data: bytes, output_format: object) -> bytes:
    """把单张结果图转成目标格式；已是目标格式或未指定时原样返回，转换失败时保留原图并记日志。"""
    target = normalize_image_output_format(output_format)
    if not target or _source_format(image_data) == target:
        return image_data
    if target == "gif":
        # 动图只能由官网画出来；官网这次没给 GIF 就保留原图，不把单帧图硬转成 GIF。
        return image_data
    try:
        with Image.open(io.BytesIO(image_data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source.copy())
        output = io.BytesIO()
        if target == "jpeg":
            if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=95, subsampling=0)
        elif target == "webp":
            if image.mode == "P":
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.save(output, format="WEBP", quality=95, method=4)
        else:
            image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except Exception as exc:  # 生成好的图比格式更重要：转换失败退回原图。
        logger.warning("image output format conversion failed: target=%s error=%s", target, exc)
        return image_data

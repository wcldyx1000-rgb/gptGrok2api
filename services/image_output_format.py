"""图片输出格式 output_format：校验与结果转换（png / jpeg / webp，jpg 视为 jpeg）。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only)，不含其 GIF 输出；转换放在放大之后，避免放大步骤把结果重新存成 PNG。
"""
from __future__ import annotations

import io
import logging

from fastapi import HTTPException
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

IMAGE_OUTPUT_FORMATS = ("png", "jpeg", "webp")
_ALIASES = {"jpg": "jpeg"}
_MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def normalize_image_output_format(value: object) -> str | None:
    """空值返回 None（保持上游原样）；不支持的格式抛 400。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = _ALIASES.get(text, text)
    if normalized not in IMAGE_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"unsupported output_format: {text}; supported: png, jpeg, jpg, webp",
                    "type": "invalid_request_error",
                    "param": "output_format",
                    "code": "invalid_output_format",
                }
            },
        )
    return normalized


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

"""生图张数 n 的统一校验：非法值返回 400 + 结构化 code（invalid_n / n_too_large）。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
"""
from __future__ import annotations

from fastapi import HTTPException

MIN_IMAGE_COUNT = 1
MAX_IMAGE_COUNT = 4


def _n_error(message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"message": message, "type": "invalid_request_error", "param": "n", "code": code}},
    )


def parse_image_count(raw_value: object, *, maximum: int = MAX_IMAGE_COUNT) -> int:
    """解析 n：缺省为 1；必须是 1..maximum 的整数，否则抛 400。"""
    message = f"n must be an integer between {MIN_IMAGE_COUNT} and {maximum}"
    if raw_value is None or raw_value == "":
        return 1
    if isinstance(raw_value, bool):
        raise _n_error(message, "invalid_n")
    try:
        if isinstance(raw_value, float):
            if not raw_value.is_integer():
                raise ValueError(raw_value)
        value = int(raw_value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError) as exc:
        raise _n_error(message, "invalid_n") from exc
    if value < MIN_IMAGE_COUNT:
        raise _n_error(message, "invalid_n")
    if value > maximum:
        raise _n_error(message, "n_too_large")
    return value

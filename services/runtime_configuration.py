"""运行时线程容量等环境变量配置。

Ported from yukkcat/chatgpt2api (AGPL-3.0-only).
"""
from __future__ import annotations

import os

DEFAULT_THREAD_TOKENS = 120
# 生图请求单独的线程上限：排队等账号的生图请求会一直占着线程，和对话共用一个池时，
# 生图一多就把对话和其他接口都挤到入口排队。分开后生图最多占这么多，超出的在异步层排队、不占线程。
DEFAULT_IMAGE_THREAD_TOKENS = 80
IMAGE_THREAD_TOKENS_ENV = "GPTGROK2API_IMAGE_THREAD_TOKENS"
LEGACY_IMAGE_THREAD_TOKENS_ENV = "CHATGPT2API_IMAGE_THREAD_TOKENS"


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(str(os.getenv(name, "") or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def image_thread_tokens() -> int:
    """生图请求线程上限：优先读 GPTGROK2API_IMAGE_THREAD_TOKENS，其次兼容 CHATGPT2API_ 前缀。"""
    for name in (IMAGE_THREAD_TOKENS_ENV, LEGACY_IMAGE_THREAD_TOKENS_ENV):
        if str(os.getenv(name, "") or "").strip():
            return env_int(name, DEFAULT_IMAGE_THREAD_TOKENS, 1)
    return DEFAULT_IMAGE_THREAD_TOKENS

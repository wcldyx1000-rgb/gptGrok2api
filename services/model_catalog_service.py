from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.account_service import account_service
from services.config import config
from utils.helper import CODEX_IMAGE_MODEL, WEB_IMAGE_MODELS
from utils.log import logger


# 拉不到上游列表时的兜底，与官网免费账号选择器一致（Auto / Thinking mini / Instant）。
# 不在列表里的模型名（如旧的 gpt-5-6）仍会原样转发给上游，已有调用不受影响。
FALLBACK_CHAT_MODELS = [
    "auto",
    "gpt-6-t-mini",
    "gpt-6",
]
UPSTREAM_CHAT_MODELS_TTL_SECS = 1800.0
UPSTREAM_CHAT_MODELS_RETRY_SECS = 120.0


class _UpstreamChatModels:
    """官网选择器模型缓存。

    只由 lifespan 里启动的后台线程刷新（每 30 分钟，失败 2 分钟后重试）；
    get() 只读缓存，导入模块或单测里都不会触发网络请求。
    后台线程启动后（start()），若缓存意外过期（超过 TTL + 重试间隔仍无刷新，
    例如后台线程已死），get() 会自愈：在新的后台线程里拉取，永不阻塞请求。
    Self-healing idea ported from yukkcat/chatgpt2api (AGPL-3.0-only).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: list[str] = []
        self._started = False
        self._last_attempt_at = 0.0
        self._healing = False

    def get(self) -> list[str]:
        spawn = False
        with self._lock:
            models = list(self._models)
            if (
                self._started
                and not self._healing
                and time.monotonic() - self._last_attempt_at
                >= UPSTREAM_CHAT_MODELS_TTL_SECS + UPSTREAM_CHAT_MODELS_RETRY_SECS
            ):
                self._healing = True
                spawn = True
        if spawn:
            threading.Thread(target=self._heal, name="chat-models-heal", daemon=True).start()
        return models

    def _heal(self) -> None:
        try:
            self.refresh()
        finally:
            with self._lock:
                self._healing = False

    def set(self, models: list[str]) -> None:
        with self._lock:
            self._models = list(models)

    def refresh(self) -> bool:
        """拉取一次官网选择器；失败保留旧缓存并返回 False。"""
        models: list[str] = []
        with self._lock:
            self._last_attempt_at = time.monotonic()
        try:
            token = account_service.get_text_access_token()
            if token:
                from services.openai_backend_api import OpenAIBackendAPI

                with OpenAIBackendAPI(access_token=token) as backend:
                    models = backend.list_picker_models()
        except Exception as exc:
            logger.warning({"event": "upstream_chat_models_refresh_failed", "error": str(exc)[:200]})
        if models:
            self.set(models)
            return True
        return False

    def start(self, stop_event: threading.Event) -> threading.Thread:
        with self._lock:
            self._started = True
            self._last_attempt_at = time.monotonic()

        def run() -> None:
            while not stop_event.is_set():
                ok = self.refresh()
                wait = UPSTREAM_CHAT_MODELS_TTL_SECS if ok else UPSTREAM_CHAT_MODELS_RETRY_SECS
                if stop_event.wait(wait):
                    break

        thread = threading.Thread(target=run, name="chat-models-refresh", daemon=True)
        thread.start()
        return thread


upstream_chat_models = _UpstreamChatModels()

FALLBACK_IMAGE_MODELS = list(WEB_IMAGE_MODELS)


def _normalize_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _settings_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _configured_chat_models(settings: dict[str, Any]) -> list[str]:
    catalog = _settings_dict(settings.get("model_catalog"))
    explicit = _normalize_list(catalog.get("chat_models"))
    if explicit:
        return explicit

    combined: list[str] = []
    for key in ("base_chat_models", "specialized_chat_models", "image_capable_chat_models"):
        for model in _normalize_list(catalog.get(key)):
            if model not in combined:
                combined.append(model)
    return combined


def _configured_image_models(settings: dict[str, Any]) -> list[str]:
    image_generation = _settings_dict(settings.get("image_generation"))
    catalog = _settings_dict(settings.get("model_catalog"))
    for source in (
        image_generation.get("model_options"),
        catalog.get("image_api_models"),
        image_generation.get("supported_models"),
    ):
        models = _normalize_list(source)
        if models:
            return models
    return []


def _image_models_from_accounts(accounts: list[dict[str, Any]]) -> list[str]:
    available_accounts = [
        account
        for account in accounts
        if isinstance(account, dict) and account_service._is_image_account_available(account)
    ]
    if not available_accounts:
        return []

    models: list[str] = list(WEB_IMAGE_MODELS)
    codex_types = {
        normalized
        for account in available_accounts
        if account_service._normalize_source_type(account.get("source_type")) == "codex"
        and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if codex_types & {"Plus", "Team", "Pro"}:
        models.append(CODEX_IMAGE_MODEL)
    for plan_type in ("Plus", "Team", "Pro"):
        if plan_type in codex_types:
            models.append(f"{plan_type.lower()}-{CODEX_IMAGE_MODEL}")
    return models


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def get_model_catalog() -> dict[str, Any]:
    settings = config.get()
    configured_chat_models = _configured_chat_models(settings)
    configured_image_models = _configured_image_models(settings)

    if configured_chat_models:
        chat_source, chat_models = "config", configured_chat_models
    else:
        upstream_models = upstream_chat_models.get()
        chat_source = "upstream" if upstream_models else "fallback"
        chat_models = upstream_models or list(FALLBACK_CHAT_MODELS)

    if configured_image_models:
        image_source = "config"
        image_models = configured_image_models
    else:
        account_models = _image_models_from_accounts(account_service.list_accounts())
        image_source = "accounts" if account_models else "fallback"
        image_models = account_models or list(FALLBACK_IMAGE_MODELS)

    grok_catalog: dict[str, Any] = {}
    try:
        from services.grok_runtime import grok_runtime

        if grok_runtime.available:
            grok_catalog = grok_runtime.run_sync(grok_runtime.model_catalog, timeout=10)
    except Exception:
        grok_catalog = {}

    # Grok Build/CLI OAuth is a distinct bearer-token provider.  Its cached
    # model discovery is local-only here: model listing must never make an
    # upstream call or refresh a credential on the request path.
    try:
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        oauth_grok_items = xai_cli_oauth_service.model_items()
    except Exception:
        oauth_grok_items = []

    grok_chat_models = _normalize_list(grok_catalog.get("chat_models"))
    for item in oauth_grok_items:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            if model_id and model_id not in grok_chat_models:
                grok_chat_models.append(model_id)
    grok_image_models = _normalize_list(grok_catalog.get("image_models"))
    grok_image_edit_models = _normalize_list(grok_catalog.get("image_edit_models"))
    grok_video_models = _normalize_list(grok_catalog.get("video_models"))

    chat_models = _unique([*chat_models, *grok_chat_models])
    image_edit_models = _unique([*image_models, *grok_image_edit_models])
    image_models = _unique([*image_models, *grok_image_models])
    all_models = _unique([
        *chat_models,
        *image_models,
        *image_edit_models,
        *grok_video_models,
    ])

    grok_items = grok_catalog.get("items") if isinstance(grok_catalog.get("items"), list) else []
    all_grok_items: list[dict[str, Any]] = []
    grok_item_ids: set[str] = set()
    for item in [*grok_items, *oauth_grok_items]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in grok_item_ids:
            continue
        grok_item_ids.add(model_id)
        all_grok_items.append(dict(item))

    if grok_catalog and oauth_grok_items:
        grok_source = "runtime+oauth"
    elif grok_catalog:
        grok_source = "runtime"
    elif oauth_grok_items:
        grok_source = "oauth"
    else:
        grok_source = "unavailable"

    # Ported from yukkcat/chatgpt2api (AGPL-3.0-only): defaults / capabilities / revision.
    high_resolution_models = [
        model
        for model in image_models
        if model == CODEX_IMAGE_MODEL or model.endswith(f"-{CODEX_IMAGE_MODEL}")
    ]
    defaults = {
        "chat_model": "auto" if "auto" in chat_models else (chat_models[0] if chat_models else ""),
        "image_model": "gpt-image-2" if "gpt-image-2" in image_models else (image_models[0] if image_models else ""),
    }
    capabilities = {
        "image_upscale": bool(settings.get("image_upscale_enabled")),
        "high_resolution_image_models": high_resolution_models,
    }
    source = {"chat": chat_source, "image": image_source, "grok": grok_source}
    revision = hashlib.sha256(
        json.dumps(
            {
                "chat_models": chat_models,
                "image_models": image_models,
                "image_edit_models": image_edit_models,
                "video_models": grok_video_models,
                "defaults": defaults,
                "capabilities": capabilities,
                "source": source,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "object": "model_catalog",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "revision": revision,
        "defaults": defaults,
        "capabilities": capabilities,
        "chat_models": chat_models,
        "image_models": image_models,
        "image_edit_models": image_edit_models,
        "video_models": grok_video_models,
        "models": all_grok_items,
        "all_models": all_models,
        "source": source,
        "openai_models_endpoint": "/v1/models",
    }

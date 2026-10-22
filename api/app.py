from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import Event

from anyio.to_thread import current_default_thread_limiter
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import (
    accounts,
    ai,
    checkout,
    egress,
    grok,
    grok_admin,
    grok_oauth,
    grok_web,
    icloud_privacy_mail,
    image_tasks,
    prompts,
    register,
    system,
)
from api.errors import install_exception_handlers
from api.support import resolve_image_base_url, resolve_web_asset, start_limited_account_watcher
from app.platform.request_context import (
    reset_media_base_url,
    set_media_base_url,
)
from services.backup_service import backup_service
from services.genbox_push_service import shutdown_genbox_push_service, start_genbox_push_service
from services.config import config
from services.dashboard_metrics_service import dashboard_metrics_service
from services.retention_cleanup_service import start_retention_cleanup_scheduler
from services.account_relogin_service import account_relogin_service
from services.account_service import account_service
from services.image_warm_pool import image_warm_pool
from services.model_catalog_service import upstream_chat_models
from services.log_service import log_service
from services.image_task_service import image_task_service
from utils.access_log_redaction import install_access_log_redaction
from services import egress_slots
from services.free_proxy_scheduler import free_proxy_scheduler
from services.proxy_service import proxy_settings
from services.grok_runtime import grok_runtime
from services.register_service import register_service
from services.openai_survival_service import openai_survival_service
from services.realtime_monitor_service import realtime_monitor_service, thread_tokens
from utils.log import logger


def _configure_threadpool() -> None:
    tokens = thread_tokens()
    limiter = current_default_thread_limiter()
    previous = int(getattr(limiter, "total_tokens", 0) or 0)
    if previous != tokens:
        limiter.total_tokens = tokens
    realtime_monitor_service.set_threadpool(tokens=tokens, previous_tokens=previous)
    logger.info({
        "event": "runtime_threadpool_configured",
        "previous_tokens": previous,
        "tokens": tokens,
    })


_IMAGE_TASK_SHUTDOWN_WAIT_SECONDS = 25.0


def create_app() -> FastAPI:
    app_version = config.app_version
    install_access_log_redaction()

    @asynccontextmanager
    async def lifespan(host_app: FastAPI):
        _configure_threadpool()
        async with grok_runtime.lifespan(host_app):
            stop_event = Event()
            thread = start_limited_account_watcher(stop_event)
            # 日志 + 图片保留期统一清理（按小时，含启动时立即清理一次与低空间兜底）。
            cleanup_thread = start_retention_cleanup_scheduler(stop_event)
            register_service.start_grok_probe_scheduler(stop_event)
            openai_survival_thread = openai_survival_service.start_scheduler(stop_event)
            account_relogin_service.start(stop_event)
            try:
                # 上次进程退出时遗留的 AT 失效待核验账号，重新排进核验，避免一直被挡在调度之外。
                account_service.resume_pending_auth_verifications()
            except Exception as exc:
                logger.warning({"event": "resume_pending_auth_failed", "error": str(exc)})
            # 官网模型选择器：后台每 30 分钟刷新，读取失败沿用兜底列表。
            upstream_chat_models.start(stop_event)
            # 两者默认关闭：未启用时线程只空转等待，不会访问 mihomo 或抓取代理。
            egress_slots.reconciler.start(stop_event)
            free_proxy_scheduler.start(stop_event)
            try:
                proxy_settings.warm_up_on_start()
            except Exception as exc:
                logger.warning({"event": "clearance_warm_up_on_start_failed", "error": str(exc)})
            backup_service.start()
            start_genbox_push_service()
            log_service.start_store_migration()
            try:
                yield
            finally:
                stop_event.set()
                # 优雅停机：取消排队中的图片任务并等待运行中的任务收尾（有上限，
                # 与 uvicorn --timeout-graceful-shutdown 配合）。
                try:
                    await asyncio.wait_for(
                        run_in_threadpool(image_task_service.shutdown_cancel_pending_and_wait),
                        timeout=_IMAGE_TASK_SHUTDOWN_WAIT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning({"event": "image_task_shutdown_wait_timeout"})
                except Exception as exc:
                    logger.warning({"event": "image_task_shutdown_failed", "error": str(exc)})
                thread.join(timeout=1)
                cleanup_thread.join(timeout=1)
                register_service.stop_grok_probe_scheduler()
                image_warm_pool.shutdown()
                openai_survival_service.stop_scheduler()
                openai_survival_thread.join(timeout=1)
                account_relogin_service.stop(timeout=1)
                egress_slots.reconciler.stop()
                free_proxy_scheduler.stop()
                proxy_settings.shutdown()
                try:
                    dashboard_metrics_service.flush()
                except Exception as exc:
                    logger.error({"event": "dashboard_metrics_shutdown_flush_failed", "error": str(exc)})
                try:
                    log_service.close()
                except Exception as exc:
                    logger.error({"event": "log_store_shutdown_flush_failed", "error": str(exc)})
                backup_service.stop()
                try:
                    await run_in_threadpool(shutdown_genbox_push_service)
                except Exception as exc:
                    logger.warning({"event": "genbox_push_shutdown_failed", "error": type(exc).__name__})

    app = FastAPI(title="GPTGrok2API", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    grok.install_exception_handlers(app)

    @app.middleware("http")
    async def bind_embedded_media_base_url(request, call_next):
        """Expose the public host to embedded media URL resolvers.

        A streaming body starts after the route handler returns, so its iterator
        is wrapped as well. This keeps local image/video URLs bound to the
        request that created them without changing global runtime config.
        """
        base_url = resolve_image_base_url(request)
        token = set_media_base_url(base_url)
        try:
            response = await call_next(request)
        finally:
            reset_media_base_url(token)

        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is not None:
            async def scoped_body():
                stream_token = set_media_base_url(base_url)
                try:
                    async for chunk in body_iterator:
                        yield chunk
                finally:
                    reset_media_base_url(stream_token)

            response.body_iterator = scoped_body()
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(grok.create_router())
    app.include_router(grok_admin.create_router())
    app.include_router(grok_oauth.create_router())
    # Keep Grok2API's original admin pages usable without creating a second
    # admin credential; the same handlers are protected by host admin auth.
    app.include_router(grok_admin.create_router(prefix="/admin/api", include_in_schema=False))
    app.include_router(grok_web.create_router())
    app.include_router(accounts.create_router())
    app.include_router(checkout.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(prompts.create_router())
    app.include_router(register.create_router())
    app.include_router(egress.create_router())
    app.include_router(icloud_privacy_mail.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        response = FileResponse(asset)
        if asset.name == "index.html":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif full_path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return app

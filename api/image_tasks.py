from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from services.image_count import parse_image_count
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request
from services.image_task_service import ImageTaskQueueFullError, image_task_service
from services.log_service import LoggedCall
from services.provider_account_stats import get_image_account_stats


class ImageGenerationTaskRequest(BaseModel):
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: object = 1
    size: str | None = None
    quality: str = "auto"


class ResumePollRequest(BaseModel):
    extra_timeout_secs: float = Field(default=30.0, ge=5.0, le=120.0)


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


QUEUE_FULL_RETRY_AFTER_SECS = "5"


def _queue_full_http_error(exc: ImageTaskQueueFullError) -> HTTPException:
    """队列满：503 + code image_task_queue_full（服务容量问题，不是限流），并提示客户端稍后重试。"""
    return HTTPException(
        status_code=503,
        detail={"code": getattr(exc, "code", "image_task_queue_full"), "error": str(exc)},
        headers={"Retry-After": QUEUE_FULL_RETRY_AFTER_SECS},
    )


def _image_quota_payload(stats: dict) -> dict[str, object]:
    total_quota = max(0, int(stats.get("total_quota") or 0))
    unlimited = max(0, int(stats.get("unlimited_quota_count") or 0))
    unknown = max(0, int(stats.get("unknown_quota_count") or 0))
    return {
        "total_quota": total_quota,
        "unlimited_quota_count": unlimited,
        "unknown_quota_count": unknown,
        "active_accounts": max(0, int(stats.get("active") or 0)),
        "limited_accounts": max(0, int(stats.get("limited") or 0)),
        "abnormal_accounts": max(0, int(stats.get("abnormal") or 0)),
        "disabled_accounts": max(0, int(stats.get("disabled") or 0)),
        "providers": stats.get("providers") if isinstance(stats.get("providers"), dict) else {},
        "available": total_quota > 0 or unlimited > 0 or unknown > 0,
    }


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks")
    async def list_image_tasks(
        ids: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        # 不带 ids 时只返回最近 limit 条（按更新时间倒序），响应带 has_more。
        tasks = await run_in_threadpool(
            lambda: image_task_service.list_tasks(identity, _parse_task_ids(ids), limit=limit)
        )
        stats = await get_image_account_stats()
        if isinstance(tasks, dict):
            return {**tasks, "quota_summary": _image_quota_payload(stats)}
        return tasks

    @router.get("/api/image-tasks/quota")
    async def image_quota_summary(
        authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        stats = await get_image_account_stats()
        return _image_quota_payload(stats)

    @router.post("/api/image-tasks/generations")
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/generations", body.model, "文生图任务", request_text=body.prompt), body.prompt)
        try:
            return await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=body.client_task_id,
                prompt=body.prompt,
                model=body.model,
                n=parse_image_count(body.n),
                size=body.size,
                quality=body.quality,
                base_url=resolve_image_base_url(request),
            )
        except ImageTaskQueueFullError as exc:
            raise _queue_full_http_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-tasks/edits")
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        client_task_id = str(payload.get("client_task_id") or "").strip()
        if not client_task_id:
            raise HTTPException(status_code=400, detail={"error": "client_task_id is required"})
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/edits", model, "图生图任务", request_text=prompt), prompt)
        # 先预占队列名额，再读取上传图片，最后提交；任何失败路径都释放名额。
        reservation = None
        try:
            reservation = await run_in_threadpool(image_task_service.reserve_submission)
            images = await read_image_sources(image_sources)
            masks = await read_image_sources(mask_sources) if mask_sources else None
            return await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=model,
                n=payload.get("n", 1),
                size=payload["size"],
                quality=payload["quality"],
                base_url=resolve_image_base_url(request),
                images=images,
                masks=masks,
                reservation=reservation,
            )
        except ImageTaskQueueFullError as exc:
            raise _queue_full_http_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        finally:
            if reservation is not None:
                reservation.rollback()  # 已提交的名额 rollback 为 no-op

    @router.post("/api/image-tasks/{task_id}/resume-poll")
    async def resume_image_poll(
        task_id: str,
        body: ResumePollRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                image_task_service.resume_poll,
                identity,
                task_id,
                body.extra_timeout_secs,
            )
        except ImageTaskQueueFullError as exc:
            raise _queue_full_http_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    return router

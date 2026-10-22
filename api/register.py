from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from api.support import require_admin
from services.register_service import (
    GrokAccountChatTestError,
    OutlookPoolConflict,
    OutlookPoolNotFound,
    register_service,
)
from services.openai_survival_service import openai_survival_service


class RegisterConfigRequest(BaseModel):
    target: Literal["openai", "grok"] | None = None
    grok: dict | None = None
    mail: dict | None = None
    checkout: dict | None = None
    sub2api_sync: dict | None = None
    agent_identity_archive: dict | None = None
    egress: dict | None = None
    confirm_refresh_token: bool | None = None
    proxy: str | None = None
    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None


class OutlookPoolResetRequest(BaseModel):
    scope: str | None = None


class OutlookPoolRetryRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=128)
    mailbox_ids: list[str] = Field(min_length=1)


class OutlookMailboxActionRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=128)
    mailbox_id: str = Field(min_length=1, max_length=128)


class GptMailStatusRequest(BaseModel):
    provider: dict | None = None
    force: bool | None = None


class DomainMailSetupRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=128)


class OpenAISurvivalConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=1440)
    concurrency: int | None = Field(default=None, ge=1, le=8)
    refresh_codex_rt: bool | None = None


class GrokAccountIdsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class GrokAccountDeleteRequest(GrokAccountIdsRequest):
    delete_upstream: bool = False


class GrokAccountDisabledRequest(GrokAccountIdsRequest):
    disabled: bool


class GrokProbePollingRequest(BaseModel):
    enabled: bool


class GrokAccountChatTestRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1_200)
    model: str | None = Field(default=None, max_length=128)


class GrokAccountExportRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    format: Literal["sub2api", "cpa"] = "sub2api"


def _grok_account_ids(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register")
    async def get_register_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.get_public()}

    @router.post("/api/register")
    async def update_register_config(body: RegisterConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.update(body.model_dump(exclude_none=True))}

    @router.post("/api/register/start")
    async def start_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.start()}

    @router.post("/api/register/stop")
    async def stop_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.stop()}

    @router.post("/api/register/checkout-retries/stop")
    async def stop_checkout_retries(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.stop_checkout_retries()}

    @router.post("/api/register/checkout-history/clear")
    async def clear_checkout_history(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return register_service.clear_checkout_history()

    @router.post("/api/register/reset")
    async def reset_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset()}

    @router.post("/api/register/outlook-pool/reset")
    async def reset_outlook_pool(body: OutlookPoolResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset_outlook_pool(body.scope or "all")}

    @router.post("/api/register/outlook-pool/retry-selected")
    async def retry_selected_outlook_mailboxes(
        body: OutlookPoolRetryRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return {
                "register": register_service.retry_outlook_failed(
                    body.provider_id,
                    body.mailbox_ids,
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _outlook_pool_error(exc: ValueError) -> HTTPException:
        if isinstance(exc, OutlookPoolNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, OutlookPoolConflict):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @router.get("/api/register/outlook-pool/entries")
    async def list_outlook_pool_entries(
        provider_id: str = Query(min_length=1, max_length=128),
        q: str = Query(default="", max_length=200),
        state: str = Query(default="all", max_length=32),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                register_service.outlook_pool_entries,
                provider_id,
                query=q,
                state=state,
                page=page,
                page_size=page_size,
            )
        except ValueError as exc:
            raise _outlook_pool_error(exc) from exc

    @router.post("/api/register/outlook-pool/mailboxes/reset")
    async def reset_outlook_pool_mailbox(
        body: OutlookMailboxActionRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(register_service.reset_outlook_mailbox, body.provider_id, body.mailbox_id)
        except ValueError as exc:
            raise _outlook_pool_error(exc) from exc

    @router.post("/api/register/outlook-pool/mailboxes/delete")
    async def delete_outlook_pool_mailbox(
        body: OutlookMailboxActionRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(register_service.delete_outlook_mailbox, body.provider_id, body.mailbox_id)
        except ValueError as exc:
            raise _outlook_pool_error(exc) from exc

    @router.post("/api/register/gptmail/status")
    async def get_gptmail_status(body: GptMailStatusRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"status": register_service.gptmail_status(body.provider, force=bool(body.force))}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/register/gptmail/refresh-key")
    async def refresh_gptmail_public_key(body: GptMailStatusRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"status": register_service.refresh_gptmail_public_key(body.provider, force=body.force is not False)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/register/domain-mail/cf-setup")
    async def setup_domain_mail_routing(body: DomainMailSetupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(register_service.domain_mail_cf_setup, body.provider_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/register/openai/survival")
    async def get_openai_survival(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"survival": openai_survival_service.status()}

    @router.post("/api/register/openai/survival")
    async def update_openai_survival(
        body: OpenAISurvivalConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            status = openai_survival_service.update_config(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"survival": status}

    @router.post("/api/register/openai/survival/run")
    async def run_openai_survival(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "started": openai_survival_service.run_now(),
            "survival": openai_survival_service.status(),
        }

    @router.get("/api/register/grok/accounts")
    async def list_grok_accounts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
        keyword: str = "",
        status: str = "all",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        view = await run_in_threadpool(
            register_service.grok_accounts_view,
            keyword=keyword,
            status=status,
        )
        items = view.get("items") if isinstance(view.get("items"), list) else []
        total = len(items)
        start = (page - 1) * page_size
        return {
            "count": total,
            "total": total,
            "all_total": int(view.get("all_total") or 0),
            "page": page,
            "page_size": page_size,
            "items": items[start:start + page_size],
            "summary": view.get("summary") if isinstance(view.get("summary"), dict) else {},
            "runtime_available": bool(view.get("runtime_available")),
            "runtime_error": str(view.get("runtime_error") or ""),
            "probe_scheduler": register_service.grok_probe_scheduler_status(),
        }

    @router.post("/api/register/grok/probe-polling")
    async def set_grok_probe_polling(
        body: GrokProbePollingRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {"probe_scheduler": register_service.set_grok_probe_scheduler_enabled(body.enabled)}

    @router.post("/api/register/grok/accounts/runtime/snapshot")
    async def refresh_grok_runtime_snapshot(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(register_service.refresh_grok_runtime_snapshot)

    @router.get("/api/register/grok/accounts/{account_id}/credentials")
    async def get_grok_account_login_credentials(
        account_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        credentials = await run_in_threadpool(register_service.grok_account_login_credentials, account_id)
        if credentials is None:
            raise HTTPException(status_code=404, detail={"error": "Grok 账号不存在或已删除"})
        if not str(credentials.get("password") or "").strip():
            raise HTTPException(status_code=409, detail={"error": "该 Grok 账号未保存登录密码"})
        return JSONResponse(
            credentials,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.post("/api/register/grok/accounts/sync")
    async def sync_grok_accounts(
        body: GrokAccountIdsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        return await run_in_threadpool(register_service.sync_grok_accounts, ids)

    @router.post("/api/register/grok/accounts/oauth/authorize")
    async def authorize_grok_accounts_oauth(
        body: GrokAccountIdsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        return await run_in_threadpool(register_service.authorize_grok_accounts_oauth, ids)

    @router.post("/api/register/grok/accounts/runtime/refresh")
    async def refresh_grok_accounts_runtime(
        body: GrokAccountIdsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        return await run_in_threadpool(register_service.refresh_grok_accounts_runtime, ids)

    @router.post("/api/register/grok/accounts/runtime/verify")
    async def verify_grok_accounts_runtime(
        body: GrokAccountIdsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        return await run_in_threadpool(register_service.verify_grok_accounts_runtime, ids)

    @router.post("/api/register/grok/accounts/{account_id}/runtime/chat-test")
    async def chat_test_grok_account_runtime(
        account_id: str,
        body: GrokAccountChatTestRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                register_service.chat_test_grok_account,
                account_id,
                prompt=body.prompt,
                model=body.model,
            )
        except GrokAccountChatTestError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"error": str(exc)}) from exc

    @router.post("/api/register/grok/accounts/runtime/disabled")
    async def set_grok_accounts_disabled(
        body: GrokAccountDisabledRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        return await run_in_threadpool(register_service.set_grok_accounts_disabled, ids, body.disabled)

    @router.delete("/api/register/grok/accounts")
    async def delete_grok_accounts(
        body: GrokAccountDeleteRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        if not ids:
            raise HTTPException(status_code=400, detail={"error": "ids is required"})
        try:
            return await run_in_threadpool(
                register_service.delete_grok_accounts,
                ids,
                delete_upstream=body.delete_upstream,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.get("/api/register/grok/accounts/export")
    async def export_grok_accounts(
        format: Literal["sub2api", "cpa"] = "sub2api",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            if format == "cpa":
                return Response(
                    register_service.export_grok_accounts_cpa(),
                    media_type="application/zip",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="grok-accounts-cpa-{timestamp}.zip"',
                    },
                )
            payload = register_service.export_grok_accounts_sub2api()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="grok-accounts-sub2api-{timestamp}.json"',
            },
        )

    @router.post("/api/register/grok/accounts/export")
    async def export_selected_grok_accounts(
        body: GrokAccountExportRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        ids = _grok_account_ids(body.ids)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            if body.format == "cpa":
                return Response(
                    register_service.export_grok_accounts_cpa(ids or None),
                    media_type="application/zip",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="grok-accounts-cpa-{timestamp}.zip"',
                    },
                )
            payload = register_service.export_grok_accounts_sub2api(ids or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="grok-accounts-sub2api-{timestamp}.json"',
            },
        )

    @router.get("/api/register/events")
    async def register_events(
        token: str = "",
        authorization: str | None = Header(default=None),
    ):
        # 推荐使用 Authorization 头（前端已改为 fetch 流式读取）；
        # query 的 token 仅为旧客户端兼容保留，密钥会进入访问日志，已不再被前端使用。
        require_admin(authorization if authorization else f"Bearer {token}")

        async def stream():
            last = ""
            while True:
                payload = json.dumps(register_service.get_public(), ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router

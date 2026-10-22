from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services import egress_check, egress_quota, egress_slots
from services.free_proxy_scheduler import free_proxy_scheduler
from services.proxy_service import proxy_settings
from services.register_service import register_service


class EgressCheckRequest(BaseModel):
    proxy: str = ""
    use_cache: bool = False


class EgressSlotSettingsRequest(BaseModel):
    enabled: bool | None = None
    controller_url: str | None = None
    proxy_template: str | None = None
    secret: str | None = None
    switch_hold_hours: int | None = None
    slot_capacity: int | None = None


class EgressSlotLocaleRequest(BaseModel):
    timezone: str = ""
    language: str = ""


class EgressSlotCountRequest(BaseModel):
    count: int


class EgressSourceRequest(BaseModel):
    kind: str
    name: str
    url: str = ""
    text: str = ""


class EgressSlotAssignRequest(BaseModel):
    node: str | None = None


class EgressChecksRequest(BaseModel):
    names: list[str] = Field(default_factory=list)


class EgressSwitchRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    target: str = ""


class FreeProxySettingsRequest(BaseModel):
    enabled: bool | None = None
    protocol: str | None = None
    limit: int | None = None
    test_limit: int | None = None
    max_nodes: int | None = None
    interval_minutes: int | None = None
    allow_datacenter: bool | None = None


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": str(exc)})


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/egress/check")
    async def check_egress(body: EgressCheckRequest, authorization: str | None = Header(default=None)):
        """检测注册出口：不传 proxy 时按注册设置展开全部出口。"""
        require_admin(authorization)
        reference = body.proxy.strip() or str(register_service.get().get("proxy") or "")
        settings = (register_service.get().get("egress") or {})
        return await run_in_threadpool(
            egress_check.preflight,
            reference,
            block_unsupported=bool(settings.get("block_unsupported", True)),
            use_cache=body.use_cache,
        )

    @router.get("/api/egress/health")
    async def egress_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "disabled_nodes": proxy_settings.node_health_snapshot(),
            "quota_usage": await run_in_threadpool(egress_quota.snapshot),
        }

    @router.get("/api/egress/slots")
    async def slots_snapshot(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(egress_slots.snapshot)

    @router.post("/api/egress/slots/settings")
    async def slots_settings(body: EgressSlotSettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.update_settings, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots/count")
    async def slots_count(body: EgressSlotCountRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.set_slot_count, body.count)
        except (ValueError, egress_slots.ControllerError) as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots/sources")
    async def slots_add_source(body: EgressSourceRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.add_source, body.kind, body.name, body.url, body.text)
        except (ValueError, egress_slots.ControllerError) as exc:
            raise _bad_request(exc) from exc

    @router.delete("/api/egress/slots/sources/{source_id}")
    async def slots_remove_source(source_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.remove_source, source_id)
        except (ValueError, egress_slots.ControllerError) as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots/sources/{source_id}/refresh")
    async def slots_refresh_source(source_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.refresh_source, source_id)
        except (ValueError, egress_slots.ControllerError) as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots/{slot}/locale")
    async def slots_locale(slot: int, body: EgressSlotLocaleRequest, authorization: str | None = Header(default=None)):
        """设置槽位的时区和语言；两项都留空表示清除。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.set_slot_locale, slot, body.timezone, body.language)
        except ValueError as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots/{slot}")
    async def slots_assign(slot: int, body: EgressSlotAssignRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.set_slot, slot, body.node)
        except (ValueError, egress_slots.ControllerError) as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots-checks")
    async def slots_checks(body: EgressChecksRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.start_checks, body.names or None)
        except ValueError as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/slots-use-for-register")
    async def slots_use_for_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.use_for_register)
        except ValueError as exc:
            raise _bad_request(exc) from exc

    @router.get("/api/egress/accounts")
    async def slots_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(egress_slots.overview)

    @router.post("/api/egress/accounts/switch")
    async def slots_switch(body: EgressSwitchRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(egress_slots.request_switch, body.access_tokens, body.target)
        except ValueError as exc:
            raise _bad_request(exc) from exc

    @router.post("/api/egress/accounts/cancel-switch")
    async def slots_cancel_switch(body: EgressSwitchRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(egress_slots.cancel_switch, body.access_tokens)

    @router.get("/api/egress/free-proxy")
    async def free_proxy_status(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return free_proxy_scheduler.status()

    @router.post("/api/egress/free-proxy")
    async def free_proxy_update(body: FreeProxySettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return free_proxy_scheduler.update(body.model_dump(exclude_none=True))

    @router.post("/api/egress/free-proxy/run")
    async def free_proxy_run(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(free_proxy_scheduler.run_now)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    return router


from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.protocol.error_response import anthropic_error_response, openai_error_response


def _is_openai_compatible_path(path: str) -> bool:
    return (
        path == "/v1"
        or path.startswith("/v1/")
        or path == "/grok/v1"
        or path.startswith("/grok/v1/")
    )


def _is_anthropic_messages_path(path: str) -> bool:
    return path in {"/v1/messages", "/grok/v1/messages"}


def _compatible_error_response(
    request: Request,
    detail: object,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if _is_anthropic_messages_path(request.url.path):
        return anthropic_error_response(detail, status_code, headers=headers)
    return openai_error_response(detail, status_code, headers=headers)


def _validation_param_and_code(errors: list[dict]) -> tuple[str | None, str]:
    """取第一条校验错误的字段路径（去掉 body/query 前缀）和 OpenAI 风格 code。"""
    for item in errors:
        if not isinstance(item, dict):
            continue
        loc = [str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path", "header")]
        code = "missing_required_parameter" if item.get("type") == "missing" else "invalid_value"
        return (".".join(loc) or None), code
    return None, "invalid_value"


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.detail, exc.status_code, exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": jsonable_encoder(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if _is_openai_compatible_path(request.url.path):
            # OpenAI 兼容路由的参数校验失败统一为 400 invalid_request_error（管理 API 仍保持 422）。
            errors = exc.errors()
            if _is_anthropic_messages_path(request.url.path):
                return anthropic_error_response(errors, 400)
            param, code = _validation_param_and_code(errors)
            return openai_error_response(
                errors, 400, error_type="invalid_request_error", code=code, param=param
            )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

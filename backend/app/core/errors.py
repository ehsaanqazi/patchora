"""Consistent JSON error responses.

Every error response has the shape::

    {"error": {"code": "not_found", "message": "...", "details": {...}, "request_id": "..."}}
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger, redact

logger = get_logger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


def error_body(code: str, message: str, request_id: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": request_id}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.warning("application error", extra={"code": exc.code, "error": redact(exc.message)})
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, redact(exc.message), _request_id(request), exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "Request validation failed", _request_id(request),
                               {"errors": errors}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "authentication_required",
                403: "permission_denied"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail), _request_id(request)),
        )

    @app.exception_handler(OperationalError)
    async def _db_error(request: Request, exc: OperationalError) -> JSONResponse:
        locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        logger.error("database error", extra={"locked": locked})
        return JSONResponse(
            status_code=503,
            content=error_body(
                "database_busy" if locked else "database_error",
                "The database is busy, please retry." if locked else "A database error occurred.",
                _request_id(request),
            ),
            headers={"Retry-After": "1"} if locked else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "An unexpected error occurred.", _request_id(request)),
        )

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.models import (
    AuditEvent,
    BrowserSession,
    Owner,
    ProviderCredential,
    WorkerHeartbeat,
    now,
)
from app.platform.security import (
    CredentialVault,
    authenticate,
    hashed,
    login,
    reserve_login_attempt,
    utc,
)
from app.platform.works import router as works_router
from app.platform.asset_tools import router as asset_tools_router
from app.platform.tasks import router as tasks_router
from app.platform.budgets import router as budgets_router
from app.platform.quotas import router as quotas_router
from app.platform.memory import router as memory_router
from app.platform.executors import router as executors_router
from app.platform.agents import (
    router as agent_router,
    owner_router as agent_owner_router,
)
from app.platform.agent_security import authenticate_agent

logger = logging.getLogger("novelos.platform")
PUBLIC_PATHS = {"/healthz", "/readyz", "/api/v1/auth/status", "/api/v1/auth/login"}


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    provider_type: str = Field(pattern="^(model_api|external_agent|detector)$")
    secret: SecretStr = Field(min_length=1, max_length=16384)


def error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        content={
            "error": {"code": code, "message": message},
            "detail": message,
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


def create_app(
    settings: PlatformSettings | None = None, database: PlatformDatabase | None = None
) -> FastAPI:
    settings = settings or PlatformSettings.from_env()
    settings.validate()
    db = database or PlatformDatabase(settings)
    vault = CredentialVault(settings.master_key)

    @asynccontextmanager
    async def lifespan(_app):
        db.require_current()  # Startup checks schema; only the CLI migrates it.
        yield
        if database is None:
            db.engine.dispose()

    app = FastAPI(title="NovelOS 控制平台", version="1.0.0-alpha.1", lifespan=lifespan)
    app.state.database = db
    app.state.settings = settings
    app.include_router(works_router)
    app.include_router(asset_tools_router)
    app.include_router(tasks_router)
    app.include_router(budgets_router)
    app.include_router(quotas_router)
    app.include_router(memory_router)
    app.include_router(executors_router)
    app.include_router(agent_router)
    app.include_router(agent_owner_router)

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        request.state.request_id = uuid4().hex
        try:
            if request.url.path.startswith("/api/agent/v1/"):

                def lookup_agent():
                    with db.sessions() as session:
                        return authenticate_agent(
                            session, request.headers.get("authorization", "")
                        )

                agent = await run_in_threadpool(lookup_agent)
                if agent is None:
                    return error(
                        request,
                        401,
                        "AGENT_AUTH_REQUIRED",
                        "需要有效的任务专属 Agent 令牌",
                    )
                request.state.agent = agent
            elif request.url.path not in PUBLIC_PATHS:

                def lookup():
                    with db.sessions() as session:
                        return authenticate(
                            session, request.cookies.get(settings.session_cookie, "")
                        )

                principal = await run_in_threadpool(lookup)
                if principal is None:
                    return error(request, 401, "AUTH_REQUIRED", "请先登录")
                request.state.principal = principal
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    csrf = request.headers.get("x-csrf-token", "")
                    if not csrf or not secrets.compare_digest(
                        hashed(csrf), principal["csrf_hash"]
                    ):
                        return error(
                            request, 403, "CSRF_INVALID", "请求校验失败，请刷新后重试"
                        )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if (
                    origin is not None and origin != settings.public_origin
                ) or request.headers.get("sec-fetch-site") == "cross-site":
                    return error(request, 403, "ORIGIN_DENIED", "请求来源不被允许")
                chunks = []
                length = 0
                async for chunk in request.stream():
                    length += len(chunk)
                    limit = (
                        8 * 1024 * 1024
                        if request.url.path.startswith(
                            ("/api/v1/works/", "/api/v1/tasks/", "/api/agent/v1/")
                        )
                        else 65536
                    )
                    if length > limit:
                        return error(request, 413, "BODY_TOO_LARGE", "请求内容过大")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await call_next(request)
        except Exception as exc:
            # Database exception text can contain values; log only type and correlation ID.
            logger.error(
                "request_failed id=%s type=%s",
                request.state.request_id,
                type(exc).__name__,
            )
            response = error(
                request, 500, "INTERNAL_ERROR", "操作失败，请按请求编号查看运行记录"
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return error(request, exc.status_code, "REQUEST_REJECTED", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        # Pydantic errors may echo submitted secrets. Never return their input fields.
        return error(request, 422, "INVALID_INPUT", "提交内容格式不正确，请检查输入")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready(request: Request):
        try:
            db.require_current()
        except (SQLAlchemyError, RuntimeError):
            return error(request, 503, "NOT_READY", "服务尚未就绪")
        return {"status": "ready"}

    @app.get("/api/v1/auth/status")
    def auth_status(request: Request):
        with db.sessions() as session:
            principal = authenticate(
                session, request.cookies.get(settings.session_cookie, "")
            )
            return {
                "initialized": session.get(Owner, "owner") is not None,
                "authenticated": principal is not None,
                "username": principal["username"] if principal else None,
            }

    @app.post("/api/v1/auth/login")
    def sign_in(body: LoginInput, request: Request, response: Response):
        with db.sessions() as session:
            client = request.client.host if request.client else "unknown"
            if not reserve_login_attempt(session, settings, client):
                raise HTTPException(429, "登录尝试过多，请稍后再试")
            result = login(
                session, settings, body.username, body.password.get_secret_value()
            )
            if result is None:
                raise HTTPException(401, "用户名或密码错误")
            old = request.cookies.get(settings.session_cookie)
            if old:
                session.execute(
                    delete(BrowserSession).where(
                        BrowserSession.token_hash == hashed(old)
                    )
                )
                session.commit()
        token, csrf = result
        for name, value, http_only in (
            (settings.session_cookie, token, True),
            (settings.csrf_cookie, csrf, False),
        ):
            response.set_cookie(
                name,
                value,
                httponly=http_only,
                secure=settings.secure_cookie,
                samesite="strict",
                path="/",
                max_age=settings.session_seconds,
            )
        return {"authenticated": True}

    @app.post("/api/v1/auth/logout")
    def sign_out(request: Request, response: Response):
        with db.sessions.begin() as session:
            session.execute(
                delete(BrowserSession).where(
                    BrowserSession.token_hash == request.state.principal["token_hash"]
                )
            )
            session.add(AuditEvent(actor="owner", action="logout"))
        response.delete_cookie(
            settings.session_cookie,
            path="/",
            secure=settings.secure_cookie,
            samesite="strict",
        )
        response.delete_cookie(
            settings.csrf_cookie,
            path="/",
            secure=settings.secure_cookie,
            samesite="strict",
        )
        return {"authenticated": False}

    @app.get("/api/v1/runtime")
    def runtime():
        with db.sessions() as session:
            workers = list(session.scalars(select(WorkerHeartbeat)))
        return {
            "stage": "M3-in-progress",
            "schema_revision": db.require_current(),
            "database": db.engine.dialect.name,
            "environment": settings.environment,
            "workers": [
                {
                    "id": worker.id,
                    "state": worker.state,
                    "online": worker.state != "stopped"
                    and utc(worker.last_seen_at) > now() - timedelta(seconds=45),
                }
                for worker in workers
            ],
            "capabilities": {
                "credentials": True,
                "work_assets": True,
                "task_execution": True,
                "external_execution": True,
                "agent_gateway": "offline_v1",
                "provider_api_execution": False,
                "model_api_configuration": True,
                "model_protocol_testing": settings.environment == "test",
                "local_task_execution": True,
                "paid_execution": False,
                "work_budgets": True,
                "executor_routing": "local_only",
                "legacy_import": "read_only_cli",
                "asset_projections": True,
                "finalization": False,
            },
        }

    @app.get("/api/v1/credentials")
    def list_credentials():
        with db.sessions() as session:
            return [
                {
                    "id": item.id,
                    "name": item.name,
                    "provider_type": item.provider_type,
                    "created_at": item.created_at,
                }
                for item in session.scalars(
                    select(ProviderCredential).order_by(ProviderCredential.created_at)
                )
            ]

    @app.post("/api/v1/credentials", status_code=201)
    def save_credential(body: CredentialInput):
        with db.sessions() as session:
            item = ProviderCredential(
                name=body.name,
                provider_type=body.provider_type,
                ciphertext=vault.encrypt(body.secret.get_secret_value()),
            )
            session.add(item)
            try:
                session.flush()
                session.add(
                    AuditEvent(
                        actor="owner", action="credential.created", target_id=item.id
                    )
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                raise HTTPException(409, "凭证名称已存在") from None
            return {
                "id": item.id,
                "name": item.name,
                "provider_type": item.provider_type,
            }

    @app.delete("/api/v1/credentials/{credential_id}", status_code=204)
    def remove_credential(credential_id: str):
        with db.sessions.begin() as session:
            item = session.get(ProviderCredential, credential_id)
            if item is None:
                raise HTTPException(404, "凭证不存在")
            session.delete(item)
            session.add(
                AuditEvent(
                    actor="owner", action="credential.deleted", target_id=credential_id
                )
            )

    return app

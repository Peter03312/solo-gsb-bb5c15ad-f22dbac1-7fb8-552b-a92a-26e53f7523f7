"""FastAPI application: HTTP API for dataset derivative permission checks."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings, settings as default_settings
from .database import Database
from .schemas import (
    BasisEntry,
    BlockerEntry,
    CreateResponse,
    ErrorDetail,
    LicenseChangeRequest,
    LicenseChangeResponse,
    PermissionResponse,
    VersionCreateRequest,
    VersionResponse,
)
from .seed import seed_demo_data
from .service import InvalidTimeError, LicenseService, ServiceError

logger = logging.getLogger("license_service")


class AppState:
    def __init__(self, db: Database, service: LicenseService) -> None:
        self.db = db
        self.service = service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        db = Database(settings.db_path)
        if settings.init_db:
            db.init_schema()
            logger.info("数据库 schema 已就绪：%s", settings.db_path)
        if settings.seed_demo and settings.init_db:
            seed_demo_data(db)
        if settings.echo:
            logger.info(
                "配置 DB_PATH=%s HOST=%s PORT=%s INIT_DB=%s SEED_DEMO=%s",
                settings.db_path,
                settings.host,
                settings.port,
                settings.init_db,
                settings.seed_demo,
            )
        app.state.app_state = AppState(db, LicenseService(db))
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="数据集派生许可判定服务",
        version="1.0.0",
        description=(
            "登记数据集版本与其固定父版本引用、登记按 UTC 时刻生效的允许用途变更，"
            "并基于自身及全部祖先的最近一次有效变更的用途交集判定可否使用。"
        ),
        lifespan=lifespan,
    )

    def service_from(request: Request) -> LicenseService:
        return request.app.state.app_state.service  # type: ignore[no-any-return]

    # -- error handling ---------------------------------------------------

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        detail = ErrorDetail(error=exc.code, message=exc.message, details=exc.details)
        return JSONResponse(status_code=exc.status_code, content=detail.model_dump())

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
        detail = ErrorDetail(
            error="invalid_time",
            message="无法解析带时区的 UTC 时刻：" + str(exc),
        )
        return JSONResponse(status_code=422, content=detail.model_dump())

    # -- health & meta ----------------------------------------------------

    @app.get("/health", tags=["meta"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- versions ---------------------------------------------------------

    @app.post(
        "/versions",
        response_model=CreateResponse,
        status_code=201,
        tags=["versions"],
        summary="登记数据集版本并固定父版本引用（不可修改）",
    )
    def create_version(payload: VersionCreateRequest, request: Request) -> CreateResponse:
        version = service_from(request).register_version(
            version_id=payload.version_id,
            parent_ids=payload.parent_ids,
            note=payload.note,
        )
        return CreateResponse(replayed=False, version=VersionResponse(**version))

    @app.get(
        "/versions",
        response_model=list[VersionResponse],
        tags=["versions"],
        summary="列出所有版本",
    )
    def list_versions(request: Request) -> list[VersionResponse]:
        return [VersionResponse(**v) for v in service_from(request).list_versions()]

    @app.get(
        "/versions/{version_id}",
        response_model=VersionResponse,
        tags=["versions"],
        summary="查询版本详情（含父版本与许可变更）",
    )
    def get_version(version_id: str, request: Request) -> VersionResponse:
        return VersionResponse(**service_from(request).get_version(version_id))

    # -- license changes --------------------------------------------------

    @app.post(
        "/license-changes",
        response_model=CreateResponse,
        status_code=201,
        tags=["license-changes"],
        summary="登记按 UTC 时刻生效的允许用途变更（幂等）",
        responses={
            200: {"description": "相同标识与内容的重复提交，返回已存在结果"},
            201: {"description": "新变更已创建"},
            409: {"description": "标识相同内容不同，或同版本同时刻存在不同变更"},
        },
    )
    def create_change(
        payload: LicenseChangeRequest, request: Request
    ) -> JSONResponse | CreateResponse:
        change, replayed = service_from(request).submit_change(
            change_id=payload.change_id,
            version_id=payload.version_id,
            effective_at=payload.effective_at,
            allowed_uses=payload.allowed_uses,
        )
        body = CreateResponse(replayed=replayed, change=LicenseChangeResponse(**change))
        if replayed:
            # 200 OK makes idempotent replays distinguishable from creation.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/license-changes/{change_id}",
        response_model=LicenseChangeResponse,
        tags=["license-changes"],
        summary="按标识查询许可变更",
    )
    def get_change(change_id: str, request: Request) -> LicenseChangeResponse:
        return LicenseChangeResponse(**service_from(request).get_change(change_id))

    # -- permission check -------------------------------------------------

    @app.get(
        "/permissions/check",
        response_model=PermissionResponse,
        tags=["permissions"],
        summary="判定给定版本在某 UTC 时刻是否可用于某用途",
        responses={422: {"description": "时刻缺少时区或无法解析"}},
    )
    def check_permission(
        request: Request,
        version_id: str = Query(..., description="被查询的数据集版本"),
        use: str = Query(..., description="拟使用的用途"),
        at: str = Query(..., alias="at", description="UTC 时刻（带时区），如 2026-07-01T00:00:00Z"),
    ) -> PermissionResponse:
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidTimeError(
                "at 参数必须是带时区的 ISO 8601 时刻，例如 2026-07-01T00:00:00Z"
            ) from exc
        if parsed.tzinfo is None:
            raise InvalidTimeError("at 参数必须带时区信息（例如结尾的 Z 或 +00:00）")
        result = service_from(request).check_permission(version_id, use, parsed)
        return PermissionResponse(
            version_id=result["version_id"],
            use=result["use"],
            at=result["at"],
            allowed=result["allowed"],
            closure=result["closure"],
            basis=[BasisEntry(**b) for b in result["basis"]],
            blockers=[BlockerEntry(**b) for b in result["blockers"]],
            allowed_uses_intersection=result["allowed_uses_intersection"],
        )

    return app


app = create_app()

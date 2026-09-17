"""FastAPI 应用入口"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .lfs_routes import router as lfs_router
from .metadata import MetadataStore
from .qiniu_client import QiniuClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 启动时校验配置
    try:
        settings.validate_qiniu()
    except RuntimeError as e:
        logger.error("配置错误: %s", e)
        raise

    # 初始化客户端
    app.state.settings = settings
    app.state.qiniu = QiniuClient(settings)
    app.state.metadata = MetadataStore(settings.metadata_db_path)

    stats = app.state.metadata.stats()
    logger.info(
        "Git LFS server started: api=%s, bucket=%s, objects=%d, total=%d bytes",
        settings.api_base,
        settings.qiniu_bucket,
        stats["count"],
        stats["total_bytes"],
    )

    yield

    logger.info("Git LFS server shutting down")


app = FastAPI(
    title="Git LFS Server (Qiniu)",
    description="A simple Git LFS server backed by Qiniu Cloud Object Storage",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS：LFS 客户端主要是 git CLI，但浏览器端工具（如 gitpod）也会用到
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(lfs_router)


# ---- 健康检查 ----

@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/api/v1/stats", include_in_schema=False)
async def stats(request: Request):
    """管理员用：本地元数据统计"""
    return request.app.state.metadata.stats()


# ---- 全局错误处理 ----

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Git LFS 客户端对错误格式不挑剔，但用统一格式便于排查
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail, "status_code": exc.status_code},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"message": "validation error", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"message": "internal server error", "detail": str(exc)},
    )

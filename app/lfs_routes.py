"""Git LFS Batch API v1 路由

参考规范: https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md

路由约定:
- POST /api/v1/objects/batch        -> batch 上传/下载 URL 协商
- GET  /api/v1/storage/{oid}        -> 下载对象
- PUT  /api/v1/storage/{oid}        -> 上传对象
- HEAD /api/v1/storage/{oid}        -> 探测对象是否存在
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import AsyncIterator, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import authenticate
from .config import Settings, get_settings
from .metadata import MetadataStore
from .models import (
    LFSBatchRequest,
    LFSBatchResponse,
    LFSObjectActions,
    LFSObjectAction,
    LFSObjectError,
    LFSObjectResponse,
)
from .qiniu_client import QiniuClient, QiniuError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# SHA256 = 64 个十六进制字符
_OID_RE = re.compile(r"^[a-fA-F0-9]{64}$")


# ---- 依赖 ----

def get_qiniu(request: Request) -> QiniuClient:
    return request.app.state.qiniu


def get_metadata(request: Request) -> MetadataStore:
    return request.app.state.metadata


# ---- 辅助 ----

def _is_valid_oid(oid: str) -> bool:
    return bool(oid) and bool(_OID_RE.match(oid))


async def _stream_request_body(request: Request) -> AsyncIterator[bytes]:
    """异步迭代请求 body"""
    async for chunk in request.stream():
        if chunk:
            yield chunk


# ---- 路由 ----

@router.get("/info/lfs/objects/basic", include_in_schema=False)
async def basic_info() -> Response:
    """Git LFS spec v1 兼容：声明 server 支持 basic transfer"""
    return Response(status_code=status.HTTP_200_OK)


@router.post(
    "/objects/batch",
    response_model=LFSBatchResponse,
    response_model_exclude_none=True,
)
async def batch(
    body: LFSBatchRequest,
    settings: Settings = Depends(get_settings),
    qiniu: QiniuClient = Depends(get_qiniu),
    metadata: MetadataStore = Depends(get_metadata),
    user: str = Depends(authenticate),
) -> LFSBatchResponse:
    """批量协商：返回每个对象的 upload/download URL

    - upload: 若对象已存在，actions 为空（客户端跳过）
    - download: 若对象不存在，返回 error=404
    """
    base = settings.api_base
    expires = 3600
    objects_out: list[LFSObjectResponse] = []

    for obj in body.objects:
        # 参数校验
        if not _is_valid_oid(obj.oid) or obj.size < 0:
            objects_out.append(LFSObjectResponse(
                oid=obj.oid,
                size=obj.size,
                error=LFSObjectError(code=400, message="invalid oid or size"),
            ))
            continue

        if obj.size > settings.max_object_size:
            objects_out.append(LFSObjectResponse(
                oid=obj.oid,
                size=obj.size,
                error=LFSObjectError(
                    code=413,
                    message=f"object size exceeds limit {settings.max_object_size}",
                ),
            ))
            continue

        try:
            if body.operation == "upload":
                # 已存在则让客户端跳过（actions 为空对象）
                if qiniu.exists(obj.oid):
                    objects_out.append(LFSObjectResponse(
                        oid=obj.oid,
                        size=obj.size,
                        authenticated=True,
                        actions=LFSObjectActions(),  # 无 upload 动作 -> 跳过
                    ))
                else:
                    objects_out.append(LFSObjectResponse(
                        oid=obj.oid,
                        size=obj.size,
                        authenticated=True,
                        actions=LFSObjectActions(
                            upload=LFSObjectAction(
                                href=f"{base}/storage/{obj.oid}",
                                expires_in=expires,
                                header={"Content-Type": "application/octet-stream"},
                            ),
                        ),
                    ))
            elif body.operation == "download":
                stat = await asyncio.to_thread(qiniu.stat, obj.oid)
                if stat is None:
                    objects_out.append(LFSObjectResponse(
                        oid=obj.oid,
                        size=obj.size,
                        error=LFSObjectError(code=404, message="object not found"),
                    ))
                else:
                    actual_size = stat["size"] or obj.size
                    objects_out.append(LFSObjectResponse(
                        oid=obj.oid,
                        size=actual_size,
                        authenticated=True,
                        actions=LFSObjectActions(
                            download=LFSObjectAction(
                                href=f"{base}/storage/{obj.oid}",
                                expires_in=expires,
                            ),
                        ),
                    ))
        except Exception as e:
            logger.exception("batch handler error oid=%s op=%s", obj.oid, body.operation)
            objects_out.append(LFSObjectResponse(
                oid=obj.oid,
                size=obj.size,
                error=LFSObjectError(code=500, message=str(e)),
            ))

    return LFSBatchResponse(transfer="basic", objects=objects_out)


@router.put(
    "/storage/{oid}",
    status_code=status.HTTP_200_OK,
    response_class=Response,
)
async def upload_object(
    oid: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    qiniu: QiniuClient = Depends(get_qiniu),
    metadata: MetadataStore = Depends(get_metadata),
    user: str = Depends(authenticate),
) -> Response:
    """接收客户端上传的对象，写入七牛云"""
    if not _is_valid_oid(oid):
        raise HTTPException(status_code=400, detail="invalid oid")

    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            cl = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if cl > settings.max_object_size:
            raise HTTPException(status_code=413, detail="object too large")

    # 流式写到临时文件 + 增量计算 SHA256（不把整个文件装内存）
    import tempfile, os
    with tempfile.NamedTemporaryFile(prefix="lfs-upload-", delete=False) as tmp:
        tmp_path = tmp.name
        sha = hashlib.sha256()
        received = 0
        try:
            async for chunk in _stream_request_body(request):
                received += len(chunk)
                if received > settings.max_object_size:
                    raise HTTPException(status_code=413, detail="object too large")
                sha.update(chunk)
                tmp.write(chunk)
            tmp.flush()
        except HTTPException:
            os.unlink(tmp_path)
            raise
        except Exception as e:
            os.unlink(tmp_path)
            logger.exception("upload stream error")
            raise HTTPException(status_code=500, detail=str(e))

    actual_oid = sha.hexdigest()
    if actual_oid != oid:
        os.unlink(tmp_path)
        raise HTTPException(
            status_code=400,
            detail=f"hash mismatch: client declared {oid}, actual {actual_oid}",
        )

    try:
        etag = await asyncio.to_thread(qiniu.upload_file, oid, tmp_path, received)
    except QiniuError as e:
        os.unlink(tmp_path)
        logger.error("qiniu upload failed oid=%s err=%s", oid, e)
        raise HTTPException(status_code=502, detail=f"storage error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    metadata.add(oid, received, etag=etag, request_ip=request.client.host if request.client else None)

    # Git LFS 客户端期望 PUT 返回 200/201，且空 body
    return Response(status_code=status.HTTP_200_OK)


@router.get("/storage/{oid}")
async def download_object(
    oid: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    qiniu: QiniuClient = Depends(get_qiniu),
    user: str = Depends(authenticate),
) -> StreamingResponse:
    """从七牛云流式读取对象返回给客户端"""
    if not _is_valid_oid(oid):
        raise HTTPException(status_code=400, detail="invalid oid")

    stat = await asyncio.to_thread(qiniu.stat, oid)
    if stat is None:
        raise HTTPException(status_code=404, detail="object not found")

    def iter_qiniu() -> Iterator[bytes]:
        for chunk in qiniu.download_stream(oid):
            yield chunk

    return StreamingResponse(
        iter_qiniu(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(stat["size"]),
            "ETag": f'"{stat["hash"]}"' if stat.get("hash") else "",
        },
    )


@router.head("/storage/{oid}", include_in_schema=False)
async def head_object(
    oid: str,
    qiniu: QiniuClient = Depends(get_qiniu),
    user: str = Depends(authenticate),
) -> Response:
    if not _is_valid_oid(oid):
        raise HTTPException(status_code=400, detail="invalid oid")
    stat = await asyncio.to_thread(qiniu.stat, oid)
    if stat is None:
        raise HTTPException(status_code=404)
    return Response(status_code=200, headers={"Content-Length": str(stat["size"])})


@router.delete("/storage/{oid}", include_in_schema=False)
async def delete_object(
    oid: str,
    qiniu: QiniuClient = Depends(get_qiniu),
    metadata: MetadataStore = Depends(get_metadata),
    user: str = Depends(authenticate),
) -> JSONResponse:
    """管理用：删除对象（不暴露给客户端）"""
    if not _is_valid_oid(oid):
        raise HTTPException(status_code=400, detail="invalid oid")
    ok = await asyncio.to_thread(qiniu.delete, oid)
    metadata.delete(oid)
    return JSONResponse({"oid": oid, "deleted_in_storage": ok})

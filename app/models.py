"""Git LFS Batch API 数据模型（v1 协议）"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---- 请求 ----

Transfer = Literal["basic", "stasded"]


class LFSObject(BaseModel):
    """LFS 对象（由 SHA256 OID 和字节数标识）"""
    oid: str = Field(..., description="对象的 SHA256 哈希（小写十六进制，64 字符）")
    size: int = Field(..., ge=0, description="对象字节数")


class LFSBatchRequest(BaseModel):
    """POST /objects/batch 请求体"""
    operation: Literal["upload", "download"]
    transfers: list[Transfer] = Field(default_factory=lambda: ["basic"])
    objects: list[LFSObject]
    hash_algo: Optional[str] = "sha256"


# ---- 响应 ----

HeaderMap = dict[str, str]


class LFSObjectAction(BaseModel):
    """单个对象的某个动作（upload / download / verify）"""
    href: str
    expires_in: int = 3600
    header: Optional[HeaderMap] = None


class LFSObjectActions(BaseModel):
    """对象可执行的动作集合"""
    upload: Optional[LFSObjectAction] = None
    download: Optional[LFSObjectAction] = None
    verify: Optional[LFSObjectAction] = None


class LFSObjectError(BaseModel):
    code: int
    message: str


class LFSObjectResponse(BaseModel):
    """batch 接口中单个对象的响应"""
    oid: str
    size: int
    authenticated: Optional[bool] = None
    actions: Optional[LFSObjectActions] = None
    error: Optional[LFSObjectError] = None


class LFSBatchResponse(BaseModel):
    """POST /objects/batch 响应体"""
    transfer: Transfer = "basic"
    objects: list[LFSObjectResponse]


# ---- 错误 ----


class ErrorResponse(BaseModel):
    """通用错误响应"""
    message: str
    request_id: Optional[str] = None
    documentation_url: Optional[str] = None
    details: Optional[dict[str, Any]] = None

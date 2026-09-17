"""七牛云对象存储客户端

- 上传：使用 ResumableUploader，支持大文件分块、断点续传
- 下载：私有空间生成带签名的临时 URL，公开空间直接用源站域名
- 探测：使用 BucketManager.stat（HEAD 语义）
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Iterator, Optional

import qiniu
import requests
from qiniu import Auth, BucketManager

from .config import Settings

logger = logging.getLogger(__name__)


class QiniuError(RuntimeError):
    """七牛云操作失败"""


class QiniuClient:
    """封装 Git LFS 所需的七牛云操作"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.auth = Auth(settings.qiniu_access_key, settings.qiniu_secret_key)
        self.bucket = settings.qiniu_bucket
        self.bucket_manager = BucketManager(self.auth)

    # ---- Key 规则 ----

    def _key(self, oid: str) -> str:
        """按 oid 前两位分片，便于大桶下的横向扩展和 CDN 缓存"""
        return f"{self.settings.storage_prefix}/{oid[:2]}/{oid}"

    # ---- 上传 ----

    def upload_token(self, oid: str, expires: int = 3600) -> str:
        return self.auth.upload_token(self.bucket, self._key(oid), expires)

    def upload_file(self, oid: str, file_path: str, size: int) -> str:
        """上传本地文件到七牛云，返回 ETag"""
        from qiniu import put_file

        token = self.upload_token(oid)
        ret, info = put_file(
            token,
            self._key(oid),
            file_path,
            mime_type="application/octet-stream",
            check_crc=True,
        )
        if info.status_code != 200:
            raise QiniuError(f"upload failed: status={info.status_code} text={info.text_body!r}")
        return ret.get("hash", "") if isinstance(ret, dict) else ""

    def upload_stream(self, oid: str, chunks: Iterator[bytes], expected_size: int) -> str:
        """流式上传：边接收 chunk 边写入临时文件，最后整体上传

        使用临时文件而非内存 buffer，避免大文件把 server 内存吃满
        """
        with tempfile.NamedTemporaryFile(prefix="lfs-upload-", delete=False) as tmp:
            tmp_path = tmp.name
            received = 0
            try:
                for chunk in chunks:
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > self.settings.max_object_size:
                        raise QiniuError(f"object exceeds max size {self.settings.max_object_size}")
                    tmp.write(chunk)
            except Exception:
                os.unlink(tmp_path)
                raise

        if expected_size and received != expected_size:
            os.unlink(tmp_path)
            raise QiniuError(
                f"size mismatch: expected={expected_size} actual={received}"
            )

        try:
            return self.upload_file(oid, tmp_path, received)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ---- 下载 ----

    def public_url(self, oid: str) -> str:
        return f"https://{self.settings.qiniu_bucket_domain}/{self._key(oid)}"

    def download_url(self, oid: str, expires: int = 3600) -> str:
        """私有空间生成带签名的临时 URL；公开空间返回源站 URL"""
        url = self.public_url(oid)
        if self.settings.qiniu_bucket_private:
            return self.auth.private_download_url(url, expires=expires)
        return url

    def download_stream(self, oid: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """流式读取对象内容，用于代理下载"""
        url = self.download_url(oid, expires=3600)
        with requests.get(url, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk

    # ---- 探测 ----

    def stat(self, oid: str) -> Optional[dict]:
        """HEAD 对象，返回 {size, hash, mime, ...} 或 None"""
        ret, info = self.bucket_manager.stat(self.bucket, self._key(oid))
        if info.status_code == 200 and isinstance(ret, dict):
            return {
                "size": ret.get("fsize", 0),
                "hash": ret.get("hash", ""),
                "mime": ret.get("mimeType", "application/octet-stream"),
                "put_time": ret.get("putTime", 0),
            }
        if info.status_code == 612:  # Not Found
            return None
        # 其他错误：打日志，但当作不存在
        logger.warning("qiniu stat non-200: oid=%s status=%s body=%s",
                       oid, info.status_code, info.text_body)
        return None

    def exists(self, oid: str) -> bool:
        return self.stat(oid) is not None

    def get_size(self, oid: str) -> Optional[int]:
        s = self.stat(oid)
        return s["size"] if s else None

    def delete(self, oid: str) -> bool:
        ret, info = self.bucket_manager.delete(self.bucket, self._key(oid))
        return info.status_code == 200 or info.status_code == 612

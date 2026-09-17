"""对象元数据存储：记录每个 OID 的本地信息

存在的原因：
1. Qiniu 的 stat API 有 QPS 限制，本地缓存加速 batch 接口
2. 即使 Qiniu 上文件被删/丢失，本地元数据保留作审计
3. 记录上传时间、大小，便于做策略（TTL、限额等）
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class MetadataStore:
    """线程安全的 SQLite 元数据存储"""

    def __init__(self, db_path: str = "./lfs_metadata.db") -> None:
        self.db_path = db_path
        # check_same_thread=False + 自管 lock 让多线程访问安全
        self._lock = threading.Lock()
        # 确保父目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL 模式提升并发读写性能
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    oid          TEXT PRIMARY KEY,
                    size         INTEGER NOT NULL,
                    uploaded_at  TEXT    NOT NULL,
                    etag         TEXT,
                    request_ip   TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_objects_uploaded_at ON objects(uploaded_at)"
            )
            conn.commit()

    def add(self, oid: str, size: int, etag: Optional[str] = None, request_ip: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO objects (oid, size, uploaded_at, etag, request_ip)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(oid) DO UPDATE SET
                    size        = excluded.size,
                    uploaded_at = excluded.uploaded_at,
                    etag        = COALESCE(excluded.etag, objects.etag)
                """,
                (oid, size, now, etag, request_ip),
            )
            conn.commit()

    def get(self, oid: str) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT oid, size, uploaded_at, etag FROM objects WHERE oid = ?",
                (oid,),
            ).fetchone()
            if row is None:
                return None
            return {"oid": row["oid"], "size": row["size"], "uploaded_at": row["uploaded_at"], "etag": row["etag"]}

    def exists(self, oid: str) -> bool:
        return self.get(oid) is not None

    def delete(self, oid: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM objects WHERE oid = ?", (oid,))
            conn.commit()
            return cur.rowcount > 0

    def stats(self) -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(size), 0) AS total FROM objects"
            ).fetchone()
            return {"count": row["cnt"], "total_bytes": row["total"]}

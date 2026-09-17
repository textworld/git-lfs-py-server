"""Smoke tests for the Git LFS server.

Runs an in-process FastAPI server on a random port using a fake Qiniu client
(fakes the qiniu SDK), so no real cloud credentials are needed.

Usage:
    cd git-lfs-py-server
    python -m tests.test_smoke
"""
from __future__ import annotations

import hashlib
import os
import socket
import sys
import threading
import time

# 把项目根加进 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _setup_env() -> None:
    os.environ.update({
        "QINIU_ACCESS_KEY": "test_ak",
        "QINIU_SECRET_KEY": "test_sk",
        "QINIU_BUCKET": "test-bucket",
        "QINIU_BUCKET_DOMAIN": "test.qiniucdn.com",
        "QINIU_BUCKET_PRIVATE": "true",
        "LFS_USER": "testuser",
        "LFS_PASSWORD": "testpass",
        "SERVER_BASE_URL": "http://127.0.0.1:{port}",
        "METADATA_DB_PATH": "/tmp/_lfs_smoke.db",
    })
    if os.path.exists("/tmp/_lfs_smoke.db"):
        os.unlink("/tmp/_lfs_smoke.db")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _patch_qiniu() -> None:
    """用 fake QiniuClient 替换真实 qiniu 操作"""
    import app.qiniu_client as qc

    class FakeQiniu:
        def __init__(self, settings):
            self.settings = settings
            self.store: dict[str, bytes] = {}

        def upload_token(self, oid, expires=3600):
            return "fake-token"

        def upload_file(self, oid, file_path, size):
            with open(file_path, "rb") as f:
                self.store[oid] = f.read()
            return "fake-etag"

        def download_url(self, oid, expires=3600):
            return f"https://fake/{oid}"

        def download_stream(self, oid, chunk_size=64 * 1024):
            data = self.store.get(oid)
            if data is None:
                raise RuntimeError("not found")
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        def stat(self, oid):
            if oid in self.store:
                return {
                    "size": len(self.store[oid]),
                    "hash": hashlib.md5(self.store[oid]).hexdigest(),
                    "mime": "application/octet-stream",
                    "put_time": 0,
                }
            return None

        def exists(self, oid):
            return oid in self.store

        def get_size(self, oid):
            return len(self.store[oid]) if oid in self.store else None

        def delete(self, oid):
            return self.store.pop(oid, None) is not None

    qc.QiniuClient = FakeQiniu


def _wait_ready(port: int, timeout: float = 5.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def main() -> int:
    _setup_env()
    _patch_qiniu()

    import uvicorn
    from app.main import app

    port = _free_port()
    os.environ["SERVER_BASE_URL"] = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        if not _wait_ready(port):
            print("Server failed to start")
            return 1

        import json
        import urllib.request
        from urllib.error import HTTPError

        base = f"http://127.0.0.1:{port}/api/v1"
        auth_header = "Basic " + __import__("base64").b64encode(b"testuser:testpass").decode()

        passed = 0

        def check(name: str, ok: bool):
            nonlocal passed
            print(f"  {'PASS' if ok else 'FAIL'} {name}")
            if ok:
                passed += 1

        def http(method: str, path: str, *, body=None, data: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, bytes]:
            req = urllib.request.Request(base + path, method=method)
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            if body is not None:
                req.add_header("Content-Type", "application/json")
                data = json.dumps(body).encode()
            if data is not None:
                req.data = data
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except HTTPError as e:
                return e.code, e.read()

        # healthz
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as r:
            check("healthz 200", r.status == 200)

        # basic info
        s, _ = http("GET", "/info/lfs/objects/basic")
        check("basic info 200", s == 200)

        # no-auth
        s, _ = http("POST", "/objects/batch", body={"operation": "download", "objects": []})
        check("no-auth 401", s == 401)

        # 上传 512KB 随机数据
        payload = os.urandom(512 * 1024)
        oid = hashlib.sha256(payload).hexdigest()

        # batch upload (auth)
        req2 = urllib.request.Request(base + "/objects/batch", method="POST")
        req2.add_header("Authorization", auth_header)
        req2.add_header("Content-Type", "application/json")
        req2.data = json.dumps({
            "operation": "upload",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": len(payload)}],
        }).encode()
        with urllib.request.urlopen(req2) as r:
            data = json.loads(r.read())
        check("batch upload 200", r.status == 200)
        check("upload action present", bool(data["objects"][0].get("actions", {}).get("upload")))

        # PUT storage
        req3 = urllib.request.Request(base + f"/storage/{oid}", method="PUT", data=payload)
        req3.add_header("Authorization", auth_header)
        with urllib.request.urlopen(req3) as r:
            check("PUT 200", r.status == 200)

        # batch upload 再次：应跳过
        req2.data = json.dumps({
            "operation": "upload",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": len(payload)}],
        }).encode()
        with urllib.request.urlopen(req2) as r:
            data = json.loads(r.read())
        check("skip when exists", not data["objects"][0].get("actions", {}).get("upload"))

        # batch download
        req2.data = json.dumps({
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": len(payload)}],
        }).encode()
        with urllib.request.urlopen(req2) as r:
            data = json.loads(r.read())
        check("batch download 200", r.status == 200)
        check("download action present", bool(data["objects"][0].get("actions", {}).get("download")))

        # GET storage
        req4 = urllib.request.Request(base + f"/storage/{oid}")
        req4.add_header("Authorization", auth_header)
        with urllib.request.urlopen(req4) as r:
            downloaded = r.read()
        check("GET 200", r.status == 200)
        check("download size matches", len(downloaded) == len(payload))
        check("download content matches OID", hashlib.sha256(downloaded).hexdigest() == oid)

        # 错误：missing
        req2.data = json.dumps({
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": "0" * 64, "size": 100}],
        }).encode()
        with urllib.request.urlopen(req2) as r:
            data = json.loads(r.read())
        check("missing -> 404 in batch", data["objects"][0]["error"]["code"] == 404)

        # 错误：hash mismatch
        s, _ = http("PUT", f"/storage/{'f' * 64}", data=b"x",
                    headers={"Authorization": auth_header})
        check("hash mismatch 400", s == 400)

        # 错误：invalid oid
        s, _ = http("PUT", "/storage/not-valid", data=b"x",
                    headers={"Authorization": auth_header})
        check("invalid oid 400", s == 400)

        # HEAD
        req5 = urllib.request.Request(base + f"/storage/{oid}", method="HEAD")
        req5.add_header("Authorization", auth_header)
        try:
            urllib.request.urlopen(req5)
            check("HEAD existing 200", True)
        except HTTPError as e:
            check("HEAD existing 200", e.code == 200)

        req6 = urllib.request.Request(base + f"/storage/{'0' * 64}", method="HEAD")
        req6.add_header("Authorization", auth_header)
        try:
            urllib.request.urlopen(req6)
            check("HEAD missing 404", False)
        except HTTPError as e:
            check("HEAD missing 404", e.code == 404)

        # DELETE
        s, _ = http("DELETE", f"/storage/{oid}", headers={"Authorization": auth_header})
        check("DELETE 200", s == 200)

        # after delete -> 404
        req2.data = json.dumps({
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": len(payload)}],
        }).encode()
        with urllib.request.urlopen(req2) as r:
            data = json.loads(r.read())
        check("after delete 404", data["objects"][0]["error"]["code"] == 404)

        print(f"\n=== {passed} checks passed ===")
        return 0 if passed >= 18 else 1
    finally:
        server.should_exit = True
        thread.join(timeout=3)


if __name__ == "__main__":
    sys.exit(main())

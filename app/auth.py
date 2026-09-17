"""HTTP Basic 认证

Git LFS 客户端（git-lfs CLI）支持通过 URL 嵌入凭据：
    https://user:password@host/api/v1

所以用 HTTP Basic 即可，无需复杂的 token 体系。
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Settings, get_settings

_security = HTTPBasic(auto_error=True)


def authenticate(
    credentials: HTTPBasicCredentials = Depends(_security),
    settings: Settings = Depends(get_settings),
) -> str:
    """校验 Basic Auth；返回用户名"""
    user_ok = secrets.compare_digest(credentials.username, settings.lfs_user)
    pass_ok = secrets.compare_digest(credentials.password, settings.lfs_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Git LFS"'},
        )
    return credentials.username

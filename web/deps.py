from __future__ import annotations

from fastapi import HTTPException, Request

from web.session import SessionUser, read_session


def optional_user(request: Request) -> SessionUser | None:
    return read_session(request)


def require_user(request: Request) -> SessionUser:
    u = read_session(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    return u


def require_admin(request: Request) -> SessionUser:
    u = read_session(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return u

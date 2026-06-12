from __future__ import annotations

import time

from fastapi import HTTPException, Request

from web.session import SessionUser, read_session

_admin_cache: dict[int, tuple[bool, float]] = {}
_ADMIN_TTL = 300  # re-check Discord roles every 5 minutes


async def _live_is_admin(discord_id: int) -> bool:
    """Return live admin status, hitting Discord at most once per _ADMIN_TTL seconds."""
    cached = _admin_cache.get(discord_id)
    if cached and time.monotonic() - cached[1] < _ADMIN_TTL:
        return cached[0]
    from web import discord_api
    try:
        member = await discord_api.get_member(discord_id)
        admin = await discord_api.check_admin(member) if member is not None else False
    except Exception:
        admin = False
    _admin_cache[discord_id] = (admin, time.monotonic())
    return admin


def optional_user(request: Request) -> SessionUser | None:
    return read_session(request)


def require_user(request: Request) -> SessionUser:
    u = read_session(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    return u


async def require_admin(request: Request) -> SessionUser:
    u = read_session(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    if not await _live_is_admin(u.discord_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return u

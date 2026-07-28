from __future__ import annotations

import time

from fastapi import HTTPException, Request

from web import config
from web.database import set_request_guild
from web.session import SessionUser, read_session


async def _session_or_none(request: Request) -> SessionUser | None:
    """Read the session cookie, discarding it if it's bound to a different guild
    than the currently configured GUILD_ID.

    A session minted before GUILD_ID was set (or set to a different value) would
    otherwise keep working against the wrong server's database for the rest of
    its 7-day cookie lifetime — this makes a GUILD_ID change take effect for
    existing sessions immediately instead of silently doing nothing until they
    happen to expire.

    Also re-checks Tribute-lock status on every call so a lock applied mid-session
    takes effect immediately rather than waiting out the session cookie's 7-day
    lifetime.
    """
    u = read_session(request)
    if u is not None and config.GUILD_ID and u.guild_id != config.GUILD_ID:
        return None
    if u is not None:
        from bot.database.engine import get_tribute_lock, TRIBUTE_LOCK_MESSAGE
        from web.database import set_request_guild, get_db
        set_request_guild(u.guild_id or 0)
        async with get_db() as db:
            if await get_tribute_lock(db, u.guild_id or 0, u.discord_id) is not None:
                raise HTTPException(status_code=403, detail=TRIBUTE_LOCK_MESSAGE)
    return u

_admin_cache: dict[tuple[int, int | None], tuple[bool, float]] = {}
_ADMIN_TTL = 300  # re-check Discord roles every 5 minutes


async def _live_is_admin(discord_id: int, guild_id: int | None = None) -> bool:
    """Return live admin status, hitting Discord at most once per _ADMIN_TTL seconds.

    ``guild_id`` should be the guild the caller's session/token is actually bound
    to; omitting it falls back to config.GUILD_ID (fine in single-guild setups,
    wrong in true multi-guild ones where it would check the wrong server).
    """
    cache_key = (discord_id, guild_id)
    cached = _admin_cache.get(cache_key)
    if cached and time.monotonic() - cached[1] < _ADMIN_TTL:
        return cached[0]
    from web import discord_api
    try:
        member = await discord_api.get_member(discord_id, guild_id=guild_id)
        admin = await discord_api.check_admin(member, guild_id=guild_id) if member is not None else False
    except Exception:
        admin = False
    _admin_cache[cache_key] = (admin, time.monotonic())
    return admin


async def optional_user(request: Request) -> SessionUser | None:
    u = await _session_or_none(request)
    if u:
        set_request_guild(u.guild_id or 0)
    return u


async def require_user(request: Request) -> SessionUser:
    u = await _session_or_none(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    set_request_guild(u.guild_id or 0)
    return u


async def require_admin(request: Request) -> SessionUser:
    u = await _session_or_none(request)
    if u is None:
        raise HTTPException(status_code=401, detail="Login required")
    set_request_guild(u.guild_id or 0)
    if not await _live_is_admin(u.discord_id, u.guild_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return u

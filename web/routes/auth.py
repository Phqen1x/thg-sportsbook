from __future__ import annotations

import secrets
import time

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

from web import config, discord_api
from web.database import available_guilds
from web.session import SessionUser, clear_session, set_session

router = APIRouter(tags=["auth"])

_pending: dict[str, float] = {}
_STATE_TTL = 600  # OAuth state tokens expire after 10 minutes
_MAX_PENDING = 500  # cap to prevent unbounded growth under repeated /auth/login spam


def _oauth_url(state: str) -> str:
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    })


@router.get("/auth/login")
async def login():
    now = time.monotonic()
    expired = [k for k, t in _pending.items() if now - t > _STATE_TTL]
    for k in expired:
        del _pending[k]
    if len(_pending) >= _MAX_PENDING:
        oldest = sorted(_pending, key=lambda k: _pending[k])
        for k in oldest[: _MAX_PENDING // 2]:
            del _pending[k]
    state = secrets.token_urlsafe(16)
    _pending[state] = now
    return RedirectResponse(_oauth_url(state))


@router.get("/auth/callback")
async def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    now = time.monotonic()
    if error or not code or state not in _pending or now - _pending[state] > _STATE_TTL:
        _pending.pop(state, None)
        return RedirectResponse("/?error=Login+failed.+Please+try+again.")
    del _pending[state]
    try:
        tokens = await discord_api.exchange_code(code)
        user_data = await discord_api.get_user(tokens["access_token"])
        uid = int(user_data["id"])
        member = await discord_api.get_member(uid)
        if member is None:
            return RedirectResponse("/?error=You+must+be+a+server+member+to+log+in.")
        if not await discord_api.can_use_bot(member, uid):
            return RedirectResponse("/?error=You+don't+have+permission+to+use+this+bot!")
        admin = await discord_api.check_admin(member)
        guilds = available_guilds()
        # Auto-select guild if there's only one option; otherwise let the user pick.
        guild_id: int | None = guilds[0] if len(guilds) == 1 else None
        user = SessionUser(
            discord_id=uid,
            username=user_data.get("global_name") or user_data.get("username", "Unknown"),
            avatar=user_data.get("avatar"),
            is_admin=admin,
            guild_id=guild_id,
        )
        dest = "/" if guild_id else "/select-server"
        resp = RedirectResponse(dest)
        set_session(resp, user)
        return resp
    except Exception:
        return RedirectResponse("/?error=Login+failed.+Please+try+again.")


@router.get("/auth/logout")
async def logout():
    resp = RedirectResponse("/")
    clear_session(resp)
    return resp

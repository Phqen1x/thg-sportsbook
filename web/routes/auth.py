from __future__ import annotations

import secrets
import time

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

from web import config, discord_api
from web.session import SessionUser, clear_session, set_session

router = APIRouter(tags=["auth"])

_pending: dict[str, float] = {}
_STATE_TTL = 600  # OAuth state tokens expire after 10 minutes


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
        admin = await discord_api.check_admin(member)
        user = SessionUser(
            discord_id=uid,
            username=user_data.get("global_name") or user_data.get("username", "Unknown"),
            avatar=user_data.get("avatar"),
            is_admin=admin,
        )
        resp = RedirectResponse("/")
        set_session(resp, user)
        return resp
    except Exception:
        return RedirectResponse("/?error=Login+failed.+Please+try+again.")


@router.get("/auth/logout")
async def logout():
    resp = RedirectResponse("/")
    clear_session(resp)
    return resp

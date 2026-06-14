"""Token-based auth for the Discord Activity (embedded SPA).

The Activity runs inside Discord's iframe where third-party cookies and full-page
OAuth redirects are unavailable, so the cookie-session flow in ``web/session.py``
cannot be used. Instead the SPA performs the Embedded App SDK handshake, the
backend exchanges the OAuth code (server-side, with the client secret) and verifies
admin status (server-side, with the bot token), then mints a short-lived **signed**
bearer token that the SPA sends on every API call.

Identity and admin status are always re-derived from the verified signature here —
nothing the client sends is trusted.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from web import config
from web.session import SessionUser

_ser = URLSafeTimedSerializer(config.WEB_SECRET_KEY, salt="sb-activity-v1")
MAX_AGE = 60 * 60 * 2  # 2 hours


def mint_token(user: SessionUser) -> str:
    """Sign a short-lived bearer token carrying the server-verified identity."""
    return _ser.dumps(json.dumps(asdict(user)))


def verify_token(raw: str) -> SessionUser | None:
    """Return the user iff the token's signature is valid and unexpired."""
    try:
        payload = _ser.loads(raw, max_age=MAX_AGE)
        return SessionUser(**json.loads(payload))
    except (BadSignature, SignatureExpired, Exception):
        return None


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


def bearer_user(request: Request) -> SessionUser:
    """FastAPI dependency: require a valid activity token."""
    raw = _token_from_request(request)
    user = verify_token(raw) if raw else None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    from web.database import set_request_guild
    set_request_guild(user.guild_id or 0)
    return user


async def bearer_admin(request: Request) -> SessionUser:
    """FastAPI dependency: require a valid activity token with current admin rights.

    ``is_admin`` in the token reflects mint-time status; we re-validate live against
    Discord (via the same 5-minute cache used by the cookie session path) so that a
    revoked admin role takes effect within one cache TTL rather than the full token TTL.
    """
    user = bearer_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    from web.deps import _live_is_admin
    if not await _live_is_admin(user.discord_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from web import config

_ser = URLSafeTimedSerializer(config.WEB_SECRET_KEY, salt="sb-session-v1")
_SECURE_COOKIE = bool(config.WEB_SSL_CERTFILE)
COOKIE = "sb_session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days


@dataclass
class SessionUser:
    discord_id: int
    username: str
    avatar: str | None
    is_admin: bool

    @property
    def avatar_url(self) -> str:
        if self.avatar:
            return f"https://cdn.discordapp.com/avatars/{self.discord_id}/{self.avatar}.png"
        return f"https://cdn.discordapp.com/embed/avatars/{self.discord_id % 5}.png"


def set_session(response: Response, user: SessionUser) -> None:
    signed = _ser.dumps(json.dumps(asdict(user)))
    response.set_cookie(COOKIE, signed, max_age=MAX_AGE, httponly=True, samesite="lax", secure=_SECURE_COOKIE)


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE)


def read_session(request: Request) -> SessionUser | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        payload = _ser.loads(raw, max_age=MAX_AGE)
        return SessionUser(**json.loads(payload))
    except (BadSignature, SignatureExpired, Exception):
        return None

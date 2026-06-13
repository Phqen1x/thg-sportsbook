from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from web import config

_ser = URLSafeTimedSerializer(config.WEB_SECRET_KEY, salt="sb-csrf-v1")
_MAX_AGE = 60 * 60 * 8  # 8 hours


def make_csrf(session_cookie: str) -> str:
    """Return a signed CSRF token bound to the given session cookie value."""
    return _ser.dumps(session_cookie[:32] if session_cookie else "anon")


def verify_csrf(token: str | None, session_cookie: str) -> bool:
    """Return True iff the CSRF token is valid and matches the session cookie."""
    if not token:
        return False
    expected = session_cookie[:32] if session_cookie else "anon"
    try:
        return _ser.loads(token, max_age=_MAX_AGE) == expected
    except (BadSignature, SignatureExpired, Exception):
        return False

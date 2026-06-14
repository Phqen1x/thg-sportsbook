from __future__ import annotations

import hashlib
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web import config
from web.csrf import make_csrf, verify_csrf
from web.routes import activity, auth, public, member, admin
from web.session import COOKIE

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
ACTIVITY_DIR = HERE / "activity"

def _static_version() -> str:
    """Short hash of activity static files for cache-busting."""
    h = hashlib.md5()
    for name in ("static/app.js", "static/style.css"):
        f = ACTIVITY_DIR / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:8]

def _web_static_version() -> str:
    """Short hash of web static files for cache-busting."""
    h = hashlib.md5()
    for name in ("style.css", "app.js"):
        f = HERE / "static" / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:8]

_VERSION = _static_version()
_WEB_VERSION = _web_static_version()

# Discord embeds the Activity in an iframe served from *.discordsays.com and
# proxies all traffic, so we must permit framing by Discord (and never send
# X-Frame-Options). connect/img/script sources cover the SDK + Discord CDN avatars.
_ACTIVITY_CSP = (
    "frame-ancestors https://discord.com https://*.discord.com https://*.discordsays.com; "
    "default-src 'self' https://*.discordsays.com; "
    "connect-src 'self' https://*.discordsays.com https://discord.com wss://*.discordsays.com; "
    "img-src 'self' https://cdn.discordapp.com https://*.discordsays.com data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://*.discordsays.com; "
    "font-src 'self' https://fonts.gstatic.com https://*.discordsays.com; "
    "script-src 'self' 'unsafe-inline' https://*.discordsays.com"
)

_DASHBOARD_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.discordapp.com data:; "
    "script-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'"
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if config.WEB_SECRET_KEY == "dev-secret-change-me":
        raise RuntimeError(
            "WEB_SECRET_KEY is set to the insecure default. "
            "Set a strong random value in your .env before running in production."
        )
    from bot.database.engine import init_db
    from web.database import available_guilds
    for gid in available_guilds():
        await init_db(gid)
    yield


def fmt_odds(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def fmt_chips(n: int) -> str:
    return f"{n:,}"


def _render_activity_index(in_discord: bool) -> HTMLResponse:
    """Serve the SPA shell, injecting a proxy-aware asset base + runtime config.

    Inside Discord all requests must be routed through the ``/.proxy`` prefix; the
    ``<base>`` href makes every relative asset URL resolve correctly, and
    ``window.__ACTIVITY__`` tells ``app.js`` which prefix to use for API calls.
    """
    html = (ACTIVITY_DIR / "index.html").read_text(encoding="utf-8")
    proxy = "/.proxy" if in_discord else ""
    base = f"{proxy}/activity/"
    html = (
        html.replace("%%BASE%%", base)
        .replace("%%PROXY%%", proxy)
        .replace("%%CLIENT_ID%%", config.DISCORD_CLIENT_ID)
        .replace("%%VERSION%%", _VERSION)
    )
    resp = HTMLResponse(html)
    resp.headers["Content-Security-Policy"] = _ACTIVITY_CSP
    return resp


def create_app() -> FastAPI:
    app = FastAPI(title="Panem Sportsbook", docs_url=None, redoc_url=None, lifespan=_lifespan)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["fmt_odds"] = fmt_odds
    templates.env.filters["fmt_chips"] = fmt_chips
    templates.env.globals["abs"] = abs
    templates.env.globals["static_v"] = _WEB_VERSION
    templates.env.globals["csrf_token"] = lambda req: make_csrf(req.cookies.get(COOKIE, ""))

    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.mount("/activity/static", StaticFiles(directory=str(ACTIVITY_DIR / "static")), name="activity-static")

    app.include_router(activity.router)
    app.include_router(auth.router)
    app.include_router(public.router)
    app.include_router(member.router)
    app.include_router(admin.router)

    # Innermost: intercept the Discord Activity iframe launch at the root path.
    @app.middleware("http")
    async def activity_root(request: Request, call_next):
        if request.url.path == "/" and "frame_id" in request.query_params:
            return _render_activity_index(in_discord=True)
        return await call_next(request)

    # Middle: CSRF validation for all form POST requests.
    # Filters by Content-Type so JSON API calls (/api/*) are naturally excluded.
    # request.body() caches in request._body so route handlers can still read the form.
    @app.middleware("http")
    async def csrf_check(request: Request, call_next):
        ct = request.headers.get("content-type", "")
        if request.method == "POST" and "application/x-www-form-urlencoded" in ct:
            body = await request.body()
            form = parse_qs(body.decode(errors="replace"), keep_blank_values=True)
            token = (form.get("_csrf") or [None])[0]
            session_cookie = request.cookies.get(COOKIE, "")
            if not verify_csrf(token, session_cookie):
                return Response("CSRF validation failed", status_code=403)
        return await call_next(request)

    # Outermost: attach security headers to every response.
    # Activity responses carry their own CSP (permitting Discord iframe embedding), so
    # we only add the dashboard CSP + X-Frame-Options when no CSP is already present.
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = _DASHBOARD_CSP
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/activity", response_class=HTMLResponse)
    async def activity_index(request: Request):
        in_discord = "frame_id" in request.query_params
        return _render_activity_index(in_discord=in_discord)

    @app.exception_handler(401)
    async def auth_redirect(request: Request, exc: HTTPException):
        # API clients (the Activity) expect JSON; browsers get the login redirect.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail or "Authentication required"}, status_code=401)
        return RedirectResponse(f"/auth/login")

    @app.exception_handler(403)
    async def forbidden(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail or "Forbidden"}, status_code=403)
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "user": None, "code": 403, "message": "You don't have permission to view this page."},
            status_code=403,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail or "Not found"}, status_code=404)
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "user": None, "code": 404, "message": "Page not found."},
            status_code=404,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        log.error(
            "Unhandled exception on %s %s\n%s",
            request.method, request.url.path,
            traceback.format_exc(),
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "user": None, "code": 500, "message": "An unexpected error occurred. Please try again."},
            status_code=500,
        )

    return app


app = create_app()

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from web import config

# Per-request guild context — set by auth dependencies before any DB access.
_guild_ctx: contextvars.ContextVar[int] = contextvars.ContextVar("web_guild_id", default=0)

# Per-path factory cache — keyed by resolved DB path so multiple guilds each get
# their own engine without re-creating on every request.
_factory_cache: dict[str, async_sessionmaker[AsyncSession]] = {}


def set_request_guild(guild_id: int) -> None:
    """Set the guild database context for the current asyncio task."""
    _guild_ctx.set(guild_id)


def get_request_guild() -> int:
    """Return the active guild ID for this request, falling back to config."""
    return _guild_ctx.get() or (config.GUILD_ID or 0)


def _db_path() -> str:
    gid = _guild_ctx.get() or config.GUILD_ID
    if not gid:
        raise RuntimeError("No guild context set for this request")
    return str(Path(config.DB_PATH).parent / f"sportsbook_{gid}.db")


def _factory_for(path: str) -> async_sessionmaker[AsyncSession]:
    if path not in _factory_cache:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
        _factory_cache[path] = async_sessionmaker(engine, expire_on_commit=False)
    return _factory_cache[path]


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    # Lazily create/migrate/seed the guild's schema on first touch from this
    # process, mirroring the bot's own get_session(). Without this, a guild
    # whose sqlite file was never created by a bot command (nobody has run any
    # bot command there yet — e.g. the Games haven't been set up) 500s on every
    # Activity endpoint with "no such table", surfacing as a bare "Internal
    # Server Error" instead of the tables just... not existing yet.
    gid = _guild_ctx.get() or config.GUILD_ID
    if gid:
        from bot.database.engine import init_db
        await init_db(gid)
    async with _factory_for(_db_path())() as session:
        yield session


def available_guilds() -> list[int]:
    """Return guild IDs that have an initialised per-guild database file.

    If GUILD_ID is configured and its DB exists, only that guild is returned so
    the auth flow auto-selects it rather than showing a multi-server picker.
    """
    base = Path(config.DB_PATH)
    result = []
    for p in base.parent.glob("sportsbook_*.db"):
        suffix = p.stem[len("sportsbook_"):]
        if suffix.isdigit():
            result.append(int(suffix))
    if config.GUILD_ID and config.GUILD_ID in result:
        return [config.GUILD_ID]
    return result

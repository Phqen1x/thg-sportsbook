from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from web import config

_engine = create_async_engine(f"sqlite+aiosqlite:///{config.DB_PATH}", echo=False)
_factory = async_sessionmaker(_engine, expire_on_commit=False)


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _factory() as session:
        yield session

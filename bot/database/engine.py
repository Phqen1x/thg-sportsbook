import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select, func

from bot import config
from bot.database.models import Base, BettingPhase, GameSetting

engine = create_async_engine(
    f"sqlite+aiosqlite:///{config.DB_PATH}",
    echo=False,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_defaults()


async def _seed_defaults() -> None:
    defaults = {
        "cashout_allowed": json.dumps(config.CASHOUT_ALLOWED),
        "cashout_rate": json.dumps(config.CASHOUT_RATE),
        "game_active": json.dumps(False),
        "default_chips": json.dumps(config.DEFAULT_CHIPS),
        "current_phase_id": json.dumps(None),
    }
    async with get_session() as session:
        for key, value in defaults.items():
            existing = await session.get(GameSetting, key)
            if existing is None:
                session.add(GameSetting(key=key, value=value))

        phase_count = await session.scalar(
            select(func.count()).select_from(BettingPhase)
        )
        if phase_count == 0:
            default_phases = [
                ("Pre-Games",  "Markets open before tributes enter the arena",   0),
                ("Bloodbath",  "The initial cornucopia bloodbath",                1),
                ("Mid-Games",  "Main gameplay — arena events and hunts",         2),
                ("Finale",     "Final tributes; victor is imminent",              3),
            ]
            for name, desc, order in default_phases:
                session.add(BettingPhase(name=name, description=desc, sort_order=order))


async def get_setting(key: str) -> str | None:
    async with get_session() as session:
        row = await session.get(GameSetting, key)
        return row.value if row else None


async def set_setting(key: str, value) -> None:
    async with get_session() as session:
        row = await session.get(GameSetting, key)
        if row:
            row.value = json.dumps(value)
        else:
            session.add(GameSetting(key=key, value=json.dumps(value)))

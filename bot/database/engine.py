import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select, func, text

from bot import config
from bot.database.models import Base, BettingPhase, GameSetting, MarketTemplate

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


@asynccontextmanager
async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    """Lightweight session for read-only queries — no explicit transaction begin/commit."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_schema()
    await _seed_defaults()


async def _migrate_schema() -> None:
    async with engine.begin() as conn:
        rows = await conn.execute(text("PRAGMA table_info(market_templates)"))
        existing = {row[1] for row in rows.fetchall()}
        if "type_key" not in existing:
            await conn.execute(text("ALTER TABLE market_templates ADD COLUMN type_key VARCHAR(30)"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_market_templates_type_key ON market_templates (type_key)"
            ))
        if "is_builtin" not in existing:
            await conn.execute(text(
                "ALTER TABLE market_templates ADD COLUMN is_builtin BOOLEAN NOT NULL DEFAULT 0"
            ))

        rows = await conn.execute(text("PRAGMA table_info(tributes)"))
        trib_cols = {row[1] for row in rows.fetchall()}
        if "discord_user_id" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN discord_user_id BIGINT"))
        if "member_joined_at" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN member_joined_at DATETIME"))


_BUILTIN_MARKET_TYPES = [
    ("TRIBUTE_WINS",       "Tribute Wins (Victor)",        "HARD",     "Tribute wins the entire Hunger Games and is declared Victor."),
    ("TRIBUTE_PLACEMENT",  "Tribute Placement (Exact)",    "HARD",     "Tribute finishes in a specific exact placement position."),
    ("TRIBUTE_TOP_N",      "Tribute Top-N Finish",         "MODERATE", "Tribute finishes within the top N tributes remaining."),
    ("TRIBUTE_KILLS",      "Top Killer",                   "HARD",     "Tribute gets the most kills of any tribute in the Games."),
    ("KILL_EVENT",         "Kill Event (A kills B)",        "HARD",     "One specific tribute kills another specific tribute."),
    ("DEATH_CAUSE",        "Death Cause",                  "MODERATE", "Tribute dies by a specific cause (natural, mutt, tribute, or Gamemakers)."),
    ("FIRST_BLOOD",        "First Blood",                  "MODERATE", "Tribute gets the very first kill of the Games."),
    ("BLOODBATH_SURVIVOR", "Bloodbath Survivor",           "EASY",     "Tribute survives the opening Cornucopia bloodbath."),
    ("SPONSOR_EVENT",      "Sponsor Event (Custom)",       "MODERATE", "Custom sponsor-driven event with a user-defined label."),
    ("KILLS_OU",           "Kills Over/Under",             "MODERATE", "Tribute gets more or fewer kills than a set line (e.g. over/under 1.5)."),
    ("PLACEMENT_OU",       "Placement Over/Under",         "MODERATE", "Tribute finishes with a better or worse placement than a set line."),
]


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

        for type_key, name, difficulty, description in _BUILTIN_MARKET_TYPES:
            existing = await session.execute(
                select(MarketTemplate).where(MarketTemplate.type_key == type_key)
            )
            if existing.scalars().first() is None:
                session.add(MarketTemplate(
                    name=name,
                    description=description,
                    difficulty=difficulty,
                    type_key=type_key,
                    is_builtin=True,
                    active=True,
                ))


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

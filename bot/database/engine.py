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
from bot.database.models import Base, BettingPhase, DistrictRecord, GameSetting, MarketTemplate

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
        trib_col_rows = rows.fetchall()
        trib_cols = {row[1] for row in trib_col_rows}
        if "discord_user_id" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN discord_user_id BIGINT"))
        if "member_joined_at" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN member_joined_at DATETIME"))
        if "sade_participant" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN sade_participant BOOLEAN NOT NULL DEFAULT 0"))
        if "sade_champion" not in trib_cols:
            await conn.execute(text("ALTER TABLE tributes ADD COLUMN sade_champion BOOLEAN NOT NULL DEFAULT 0"))

        # Make tributes.training_score nullable (SQLite requires full table recreation)
        ts_col = next((c for c in trib_col_rows if c[1] == "training_score"), None)
        if ts_col and ts_col[3] == 1:  # notnull == 1
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            await conn.execute(text("DROP TABLE IF EXISTS tributes_new"))
            await conn.execute(text("""
                CREATE TABLE tributes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100) NOT NULL,
                    district INTEGER NOT NULL,
                    gender VARCHAR(2) NOT NULL,
                    training_score INTEGER,
                    face_claim VARCHAR(500),
                    kills INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(10) NOT NULL DEFAULT 'ALIVE',
                    death_cause VARCHAR(200),
                    killed_by_id INTEGER REFERENCES tributes(id),
                    placement INTEGER,
                    alliance_id INTEGER REFERENCES alliances(id),
                    discord_user_id BIGINT,
                    member_joined_at DATETIME,
                    sade_participant BOOLEAN NOT NULL DEFAULT 0,
                    sade_champion BOOLEAN NOT NULL DEFAULT 0,
                    non_binary BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await conn.execute(text("""
                INSERT INTO tributes_new
                SELECT id, name, district, gender, training_score, face_claim,
                       IFNULL(kills, 0), IFNULL(status, 'ALIVE'), death_cause,
                       killed_by_id, placement, alliance_id, discord_user_id,
                       member_joined_at,
                       IFNULL(sade_participant, 0), IFNULL(sade_champion, 0),
                       IFNULL(non_binary, 0),
                       IFNULL(created_at, CURRENT_TIMESTAMP)
                FROM tributes
            """))
            await conn.execute(text("DROP TABLE tributes"))
            await conn.execute(text("ALTER TABLE tributes_new RENAME TO tributes"))
            await conn.execute(text("PRAGMA foreign_keys = ON"))

        # Add tributes.kill_boost (accumulated kill-quality multiplier). Checked
        # after the recreation above so it is added regardless of that path.
        rows = await conn.execute(text("PRAGMA table_info(tributes)"))
        trib_cols = {row[1] for row in rows.fetchall()}
        if "kill_boost" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN kill_boost FLOAT NOT NULL DEFAULT 1.0"
            ))

        # Migrate district_records from per-tribute rows to per-district aggregate rows
        rows = await conn.execute(text("PRAGMA table_info(district_records)"))
        dr_cols = {row[1] for row in rows.fetchall()}
        if "tribute_name" in dr_cols:
            await conn.execute(text("DROP TABLE district_records"))
            await conn.run_sync(Base.metadata.tables["district_records"].create)
            dr_cols = set()

        _DR_NEW_COLS = [
            ("victor_male_count",              "INTEGER"),
            ("victor_female_count",            "INTEGER"),
            ("victor_nonbinary_count",         "INTEGER"),
            ("runner_up_finishes",             "INTEGER"),
            ("runner_up_male",                 "INTEGER"),
            ("runner_up_female",               "INTEGER"),
            ("runner_up_nonbinary",            "INTEGER"),
            ("male_kills",                     "INTEGER"),
            ("female_kills",                   "INTEGER"),
            ("nonbinary_kills",                "INTEGER"),
            ("avg_placement_last5",            "INTEGER"),
            ("bloodbath_kills",                "INTEGER"),
            ("bloodbath_deaths",               "INTEGER"),
            ("avg_training_score",             "INTEGER"),
            ("avg_training_score_male",        "INTEGER"),
            ("avg_training_score_female",      "INTEGER"),
            ("avg_training_score_nonbinary",   "INTEGER"),
            ("manmade_arena_wins",             "INTEGER"),
            ("reputation",                     "INTEGER"),
        ]
        for col, typ in _DR_NEW_COLS:
            if col not in dr_cols:
                await conn.execute(text(f"ALTER TABLE district_records ADD COLUMN {col} {typ}"))

        # Make markets.tribute_a_id nullable (SQLite requires full table recreation)
        rows = await conn.execute(text("PRAGMA table_info(markets)"))
        mkt_cols = rows.fetchall()
        tribute_a_col = next((c for c in mkt_cols if c[1] == "tribute_a_id"), None)
        if tribute_a_col and tribute_a_col[3] == 1:  # notnull == 1
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            await conn.execute(text("DROP TABLE IF EXISTS markets_new"))
            await conn.execute(text("""
                CREATE TABLE markets_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type VARCHAR(30) NOT NULL,
                    label VARCHAR(200) NOT NULL,
                    tribute_a_id INTEGER REFERENCES tributes(id),
                    tribute_b_id INTEGER REFERENCES tributes(id),
                    cause VARCHAR(100),
                    placement_num INTEGER,
                    top_n INTEGER,
                    ou_line REAL,
                    ou_side VARCHAR(5),
                    phase_id INTEGER REFERENCES betting_phases(id),
                    odds INTEGER NOT NULL,
                    odds_override BOOLEAN NOT NULL DEFAULT 0,
                    status VARCHAR(10) NOT NULL DEFAULT 'CLOSED',
                    result BOOLEAN,
                    cashout_allowed BOOLEAN,
                    cashout_rate REAL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await conn.execute(text("""
                INSERT INTO markets_new
                SELECT id, type, label, tribute_a_id, tribute_b_id, cause,
                       placement_num, top_n, ou_line, ou_side, phase_id, odds,
                       IFNULL(odds_override, 0),
                       IFNULL(status, 'CLOSED'),
                       result, cashout_allowed, cashout_rate,
                       IFNULL(created_at, CURRENT_TIMESTAMP)
                FROM markets
            """))
            await conn.execute(text("DROP TABLE markets"))
            await conn.execute(text("ALTER TABLE markets_new RENAME TO markets"))
            await conn.execute(text("PRAGMA foreign_keys = ON"))

        # Add Final 8 / Final 5 phases; push Finale to sort_order 5
        final8_count = (await conn.execute(text(
            "SELECT COUNT(*) FROM betting_phases WHERE name = 'Final 8'"
        ))).scalar()
        if final8_count == 0:
            await conn.execute(text(
                "UPDATE betting_phases SET sort_order = 5 WHERE name = 'Finale' AND sort_order = 3"
            ))
            await conn.execute(text(
                "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                "VALUES ('Final 8', 'The top 8 tributes remain', 3)"
            ))
            await conn.execute(text(
                "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                "VALUES ('Final 5', 'The top 5 tributes remain', 4)"
            ))


_BUILTIN_MARKET_TYPES = [
    ("TRIBUTE_WINS",            "Tribute Wins (Victor)",                "HARD",      "Tribute wins the entire Hunger Games and is declared Victor."),
    ("TRIBUTE_PLACEMENT",       "Tribute Placement (Exact)",            "HARD",      "Tribute finishes in a specific exact placement position."),
    ("TRIBUTE_TOP_N",           "Tribute Top-N Finish",                 "MODERATE",  "Tribute finishes within the top N tributes remaining."),
    ("TRIBUTE_KILLS",           "Top Killer",                           "HARD",      "Tribute gets the most kills of any tribute in the Games."),
    ("KILL_EVENT",              "Kill Event (A kills B)",               "HARD",      "One specific tribute kills another specific tribute."),
    ("DEATH_CAUSE",             "Death Cause",                          "MODERATE",  "Tribute dies by a specific cause (natural, mutt, tribute, or Gamemakers)."),
    ("FIRST_BLOOD",             "First Blood",                          "MODERATE",  "Tribute gets the very first kill of the Games (Pre-Games/Bloodbath only)."),
    ("BLOODBATH_SURVIVOR",      "Bloodbath Survivor",                   "EASY",      "Tribute survives the opening Cornucopia bloodbath (Pre-Games/Bloodbath only)."),
    ("SPONSOR_EVENT",           "Sponsor Event (Custom)",               "MODERATE",  "Custom sponsor-driven event with a user-defined label."),
    ("KILLS_OU",                "Kills Over/Under",                     "MODERATE",  "Tribute gets more or fewer kills than a set line (e.g. over/under 1.5)."),
    ("PLACEMENT_OU",            "Placement Over/Under",                 "MODERATE",  "Tribute finishes with a better or worse placement than a set line."),
    ("MAKES_FINAL_8",           "Makes Final 8",                        "MODERATE",  "Tribute is still alive when the Final 8 phase begins."),
    ("MISSES_FINAL_8",          "Eliminated Before Final 8",            "MODERATE",  "Tribute is eliminated before the Final 8 phase begins."),
    ("MAKES_FINAL_5",           "Makes Final 5",                        "MODERATE",  "Tribute is still alive when the Final 5 phase begins."),
    ("MISSES_FINAL_5",          "Eliminated Before Final 5",            "MODERATE",  "Tribute is eliminated before the Final 5 phase begins."),
    ("MAKES_FINALE",            "Makes the Finale",                     "HARD",      "Tribute is still alive when the Finale phase begins."),
    ("MISSES_FINALE",           "Eliminated Before Finale",             "MODERATE",  "Tribute is eliminated before the Finale phase begins."),
    ("ARENA_TYPE",              "Arena Type (Pre-Games)",               "EASY",      "Bet on whether the arena is Artificial or Natural. Resolves when Pre-Games ends."),
    ("EXACT_TRAINING_SCORE",    "Exact Training Score",                 "HARD",      "Guess a tribute's exact training score (1–12). Resolves when Pre-Games ends."),
    ("COMBINED_DISTRICT_SCORE", "Combined District Training Score",     "VERY_HARD", "Guess the combined training scores of both district tributes. Resolves when Pre-Games ends."),
    ("TRAINING_SCORE_OU",       "Training Score Over/Under",            "EASY",      "Bet over or under on a tribute's training score. Resolves when Pre-Games ends."),
    # ── District-level markets ─────────────────────────────────────────────────
    ("DISTRICT_VICTOR",         "District Victor",                      "HARD",      "Bet on which district the victor will come from. Resolves at game end."),
    ("DISTRICT_KILLS_OU",       "District Total Kills Over/Under",      "MODERATE",  "Bet over or under on a district's combined kill total. Resolves at game end."),
    ("DISTRICT_BOTH_BLOODBATH", "District Both Survive Bloodbath",      "MODERATE",  "Both district tributes survive the opening bloodbath. Resolves when Bloodbath ends."),
    ("DISTRICT_BOTH_FINAL_8",   "District Both Make Final 8",           "HARD",      "Both district tributes are alive when the Final 8 phase begins."),
    ("DISTRICT_ONE_FINAL_8",    "District At Least One Makes Final 8",  "MODERATE",  "At least one district tribute is alive when the Final 8 phase begins."),
    ("DISTRICT_BOTH_FINAL_5",   "District Both Make Final 5",           "VERY_HARD", "Both district tributes are alive when the Final 5 phase begins."),
    ("DISTRICT_ONE_FINAL_5",    "District At Least One Makes Final 5",  "MODERATE",  "At least one district tribute is alive when the Final 5 phase begins."),
    ("DISTRICT_BOTH_FINALE",    "District Both Make the Finale",        "VERY_HARD", "Both district tributes are alive when the Finale phase begins."),
    ("DISTRICT_ONE_FINALE",     "District At Least One Makes Finale",   "HARD",      "At least one district tribute is alive when the Finale phase begins."),
    # ── Alliance-level markets ─────────────────────────────────────────────────
    ("ALLIANCE_VICTOR",         "Alliance Victor",                      "HARD",      "A member of the alliance wins the Games. Resolves at game end."),
    ("ALLIANCE_KILLS_OU",       "Alliance Total Kills Over/Under",      "MODERATE",  "Bet over or under on the alliance's combined kill total. Resolves at game end."),
    ("ALLIANCE_ALL_BLOODBATH",  "Alliance All Survive Bloodbath",       "MODERATE",  "All alliance members survive the opening bloodbath. Resolves when Bloodbath ends."),
    ("ALLIANCE_ALL_FINAL_8",    "Alliance All Make Final 8",            "HARD",      "All alliance members are alive when the Final 8 phase begins."),
    ("ALLIANCE_ONE_FINAL_8",    "Alliance At Least One Makes Final 8",  "MODERATE",  "At least one alliance member is alive when the Final 8 phase begins."),
    ("ALLIANCE_ALL_FINAL_5",    "Alliance All Make Final 5",            "VERY_HARD", "All alliance members are alive when the Final 5 phase begins."),
    ("ALLIANCE_ONE_FINAL_5",    "Alliance At Least One Makes Final 5",  "MODERATE",  "At least one alliance member is alive when the Final 5 phase begins."),
    ("ALLIANCE_ALL_FINALE",     "Alliance All Make the Finale",         "VERY_HARD", "All alliance members are alive when the Finale phase begins."),
    ("ALLIANCE_ONE_FINALE",     "Alliance At Least One Makes Finale",   "HARD",      "At least one alliance member is alive when the Finale phase begins."),
]


async def _seed_defaults() -> None:
    defaults = {
        "cashout_allowed": json.dumps(config.CASHOUT_ALLOWED),
        "cashout_rate": json.dumps(config.CASHOUT_RATE),
        "game_active": json.dumps(False),
        "default_chips": json.dumps(config.DEFAULT_CHIPS),
        "current_phase_id": json.dumps(None),
        "num_games": json.dumps(0),
        "arena_artificial_count": json.dumps(0),
        "arena_natural_count": json.dumps(0),
        "arena_type": json.dumps(None),
    }
    async with get_session() as session:
        for key, value in defaults.items():
            existing = await session.get(GameSetting, key)
            if existing is None:
                session.add(GameSetting(key=key, value=value))

        for d in range(1, 13):
            if await session.get(DistrictRecord, d) is None:
                session.add(DistrictRecord(district=d))

        phase_count = await session.scalar(
            select(func.count()).select_from(BettingPhase)
        )
        if phase_count == 0:
            default_phases = [
                ("Pre-Games",  "Markets open before tributes enter the arena",   0),
                ("Bloodbath",  "The initial cornucopia bloodbath",                1),
                ("Mid-Games",  "Main gameplay — arena events and hunts",         2),
                ("Final 8",    "The top 8 tributes remain",                       3),
                ("Final 5",    "The top 5 tributes remain",                       4),
                ("Finale",     "Final tributes; victor is imminent",              5),
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

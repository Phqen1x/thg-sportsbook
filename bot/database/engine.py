import contextvars
import fcntl
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncGenerator, Iterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select, func, text

from bot import config
from bot.database.models import Base, BettingPhase, DistrictRecord, GameSetting, MarketTemplate

_guild_id_ctx: contextvars.ContextVar[int] = contextvars.ContextVar("guild_id", default=0)

_engines: dict[int, AsyncEngine] = {}
_factories: dict[int, async_sessionmaker[AsyncSession]] = {}


def _db_path_for_guild(guild_id: int) -> Path:
    return Path(config.DB_PATH).parent / f"sportsbook_{guild_id}.db"


def _ensure_engine(guild_id: int) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    if guild_id not in _engines:
        db_path = _db_path_for_guild(guild_id)
        eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        factory = async_sessionmaker(
            eng,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        _engines[guild_id] = eng
        _factories[guild_id] = factory
    return _engines[guild_id], _factories[guild_id]


def _effective_guild_id(guild_id: int) -> int:
    """Collapse the live Discord snowflake to the configured single-guild pin.

    When GUILD_ID is set this deployment serves exactly one guild, so every live
    interaction (whatever server it physically came from) must resolve to that
    guild's DB / rows / settings — matching the web app. When unset, the live id
    is used unchanged (true multi-guild behaviour)."""
    return config.GUILD_ID or guild_id


def set_guild_context(guild_id: int) -> None:
    """Set the guild_id for the current async task context."""
    _guild_id_ctx.set(_effective_guild_id(guild_id))


def current_guild_id() -> int:
    """The effective guild id for the current context — the single source of
    truth for guild_id row stamps, query filters, and settings-key prefixes.

    The GUILD_ID pin is applied at *read* time, not just when set_guild_context
    runs: some DB access happens in tasks that never set the context (e.g.
    on_app_command_completion → post_audit_log dispatches in a separate task, so
    the command's contextvar does not propagate). Without read-time pinning
    those fall back to the contextvar default 0 and spawn sportsbook_0.db."""
    return _effective_guild_id(_guild_id_ctx.get())


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    _, factory = _ensure_engine(current_guild_id())
    async with factory() as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    """Lightweight session for read-only queries — no explicit transaction begin/commit."""
    _, factory = _ensure_engine(current_guild_id())
    async with factory() as session:
        yield session


@contextmanager
def _init_lock(guild_id: int) -> Iterator[None]:
    """Cross-process exclusive lock so the bot and web services never run
    create_all/migrate/seed against the same per-guild DB concurrently (which
    races on CREATE TABLE / duplicate-key INSERTs). The loser blocks until the
    winner finishes, then finds the schema already present and skips it."""
    db_path = _db_path_for_guild(guild_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.parent / f".init-{guild_id}.lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


async def init_db(guild_id: int) -> None:
    guild_id = _effective_guild_id(guild_id)
    token = _guild_id_ctx.set(guild_id)
    try:
        with _init_lock(guild_id):
            eng, _ = _ensure_engine(guild_id)
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await _migrate_schema()
            await _seed_defaults()
    finally:
        _guild_id_ctx.reset(token)


async def _migrate_schema() -> None:
    eng, _ = _ensure_engine(current_guild_id())
    async with eng.begin() as conn:
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

        # Add tributes.kill_boost. Checked after the recreation above so it is
        # added regardless of that path.
        rows = await conn.execute(text("PRAGMA table_info(tributes)"))
        trib_cols = {row[1] for row in rows.fetchall()}
        if "kill_boost" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN kill_boost FLOAT NOT NULL DEFAULT 0.0"
            ))

        # One-time migration: switch kill_boost from multiplicative (neutral=1.0)
        # to additive (neutral=0.0) semantics. Marked in game_settings.
        kb_marker = await conn.execute(
            text("SELECT value FROM game_settings WHERE key = 'kill_boost_format'")
        )
        if kb_marker.fetchone() is None:
            await conn.execute(text("UPDATE tributes SET kill_boost = 0.0"))
            await conn.execute(text(
                "INSERT OR IGNORE INTO game_settings (key, value) "
                "VALUES ('kill_boost_format', '\"v2\"')"
            ))
        if "debilitation_level" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN debilitation_level VARCHAR(30)"
            ))
        # Tribute age (12–18); nullable so pre-existing tributes stay neutral.
        if "age" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN age INTEGER"
            ))
        if "times_played" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN times_played INTEGER NOT NULL DEFAULT 0"
            ))
        if "highest_placement" not in trib_cols:
            await conn.execute(text(
                "ALTER TABLE tributes ADD COLUMN highest_placement INTEGER"
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
            ("funding_level",                  "VARCHAR(20)"),
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

        # Phase migrations only apply to pre-existing databases. A brand-new DB
        # has an empty betting_phases table here (it is seeded with the full,
        # current phase set in _seed_defaults), so skip all of this to avoid
        # partially populating the table and short-circuiting that seed.
        phase_rows = (await conn.execute(text(
            "SELECT COUNT(*) FROM betting_phases"
        ))).scalar()
        if phase_rows:
            # Add Final 8 / Final 5 phases to legacy DBs that predate them.
            final8_count = (await conn.execute(text(
                "SELECT COUNT(*) FROM betting_phases WHERE name = 'Final 8'"
            ))).scalar()
            if final8_count == 0:
                await conn.execute(text(
                    "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                    "VALUES ('Final 8', 'The top 8 tributes remain', 3)"
                ))
                await conn.execute(text(
                    "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                    "VALUES ('Final 5', 'The top 5 tributes remain', 4)"
                ))

        # Restructure phases: rename Mid-Games → Arena, insert the two sponsor
        # phases, and drop the Finale phase. Guarded on Mid-Games so this only
        # touches pre-existing databases (a fresh DB is seeded with the new
        # phase set directly in _seed_defaults).
        midgames_exists = (await conn.execute(text(
            "SELECT COUNT(*) FROM betting_phases WHERE name = 'Mid-Games'"
        ))).scalar()
        if midgames_exists:
            await conn.execute(text(
                "UPDATE betting_phases SET name = 'Arena', "
                "description = 'The arena opens — all remaining markets go live', "
                "sort_order = 3 WHERE name = 'Mid-Games'"
            ))
            await conn.execute(text(
                "UPDATE betting_phases SET sort_order = 5 WHERE name = 'Final 8'"
            ))
            await conn.execute(text(
                "UPDATE betting_phases SET sort_order = 6 WHERE name = 'Final 5'"
            ))
            await conn.execute(text(
                "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                "VALUES ('Sponsors Open', 'Sponsorship window opens — funded districts surge', 2)"
            ))
            await conn.execute(text(
                "INSERT OR IGNORE INTO betting_phases (name, description, sort_order) "
                "VALUES ('Sponsors Closing', 'Sponsorship window closes — funding edge fades', 4)"
            ))
            # Detach any markets pinned to the Finale phase, then remove it.
            await conn.execute(text(
                "UPDATE markets SET phase_id = NULL WHERE phase_id IN "
                "(SELECT id FROM betting_phases WHERE name = 'Finale')"
            ))
            await conn.execute(text("DELETE FROM betting_phases WHERE name = 'Finale'"))

        # Purge removed market types (finale milestones + sponsor events). Only
        # delete markets that carry no bets/pending legs so existing wagers are
        # never silently dropped; obsolete templates are always removed so the
        # types can no longer be created.
        _removed_types = (
            "'MAKES_FINALE','MISSES_FINALE',"
            "'DISTRICT_BOTH_FINALE','DISTRICT_ONE_FINALE',"
            "'ALLIANCE_ALL_FINALE','ALLIANCE_ONE_FINALE',"
            "'GAMES_PARTNER_FINALE','SPONSOR_EVENT'"
        )
        await conn.execute(text(
            f"DELETE FROM markets WHERE type IN ({_removed_types}) "
            "AND id NOT IN (SELECT market_id FROM bets) "
            "AND id NOT IN (SELECT market_id FROM pending_parlay_legs)"
        ))
        await conn.execute(text(
            f"DELETE FROM market_templates WHERE type_key IN ({_removed_types})"
        ))

        # Add alliance_id to modifier_assignments for alliance-scoped modifiers
        rows = await conn.execute(text("PRAGMA table_info(modifier_assignments)"))
        ma_cols = {row[1] for row in rows.fetchall()}
        if "alliance_id" not in ma_cols:
            await conn.execute(text(
                "ALTER TABLE modifier_assignments ADD COLUMN "
                "alliance_id INTEGER REFERENCES alliances(id) ON DELETE CASCADE"
            ))

        # Add parlays.is_public for the public tailing board (default: listed)
        rows = await conn.execute(text("PRAGMA table_info(parlays)"))
        parlay_cols = {row[1] for row in rows.fetchall()}
        if parlay_cols and "is_public" not in parlay_cols:
            await conn.execute(text(
                "ALTER TABLE parlays ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 1"
            ))

        # ── Multi-server isolation: add guild_id to all user-scoped tables ────────
        # When migrating a guild-specific DB (created by copying sportsbook.db),
        # stamp all existing rows with the real guild_id so they remain visible
        # to guild-scoped queries.  The legacy/fallback DB (guild_id=0) keeps 0.
        _gid = current_guild_id()

        # users: recreate with composite PK (guild_id, discord_id).
        rows = await conn.execute(text("PRAGMA table_info(users)"))
        user_cols = {row[1] for row in rows.fetchall()}
        if "guild_id" not in user_cols:
            await conn.execute(text("PRAGMA foreign_keys = OFF"))
            await conn.execute(text("DROP TABLE IF EXISTS users_new"))
            await conn.execute(text("""
                CREATE TABLE users_new (
                    guild_id BIGINT NOT NULL DEFAULT 0,
                    discord_id BIGINT NOT NULL,
                    username VARCHAR(100) NOT NULL,
                    chips INTEGER NOT NULL DEFAULT 1000,
                    total_wagered INTEGER NOT NULL DEFAULT 0,
                    total_won INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, discord_id)
                )
            """))
            await conn.execute(
                text("""
                    INSERT INTO users_new
                    SELECT :gid, discord_id, username, chips,
                           IFNULL(total_wagered, 0), IFNULL(total_won, 0),
                           IFNULL(created_at, CURRENT_TIMESTAMP)
                    FROM users
                """),
                {"gid": _gid},
            )
            await conn.execute(text("DROP TABLE users"))
            await conn.execute(text("ALTER TABLE users_new RENAME TO users"))
            await conn.execute(text("PRAGMA foreign_keys = ON"))

        # bets / parlays / pending_parlay_legs / betting_restrictions: add guild_id
        rows = await conn.execute(text("PRAGMA table_info(bets)"))
        if "guild_id" not in {row[1] for row in rows.fetchall()}:
            await conn.execute(text(
                "ALTER TABLE bets ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0"
            ))
            if _gid:
                await conn.execute(
                    text("UPDATE bets SET guild_id = :gid WHERE guild_id = 0"),
                    {"gid": _gid},
                )

        rows = await conn.execute(text("PRAGMA table_info(parlays)"))
        if "guild_id" not in {row[1] for row in rows.fetchall()}:
            await conn.execute(text(
                "ALTER TABLE parlays ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0"
            ))
            if _gid:
                await conn.execute(
                    text("UPDATE parlays SET guild_id = :gid WHERE guild_id = 0"),
                    {"gid": _gid},
                )

        rows = await conn.execute(text("PRAGMA table_info(pending_parlay_legs)"))
        if "guild_id" not in {row[1] for row in rows.fetchall()}:
            await conn.execute(text(
                "ALTER TABLE pending_parlay_legs ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0"
            ))
            if _gid:
                await conn.execute(
                    text("UPDATE pending_parlay_legs SET guild_id = :gid WHERE guild_id = 0"),
                    {"gid": _gid},
                )

        rows = await conn.execute(text("PRAGMA table_info(betting_restrictions)"))
        if "guild_id" not in {row[1] for row in rows.fetchall()}:
            await conn.execute(text(
                "ALTER TABLE betting_restrictions ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0"
            ))
            if _gid:
                await conn.execute(
                    text("UPDATE betting_restrictions SET guild_id = :gid WHERE guild_id = 0"),
                    {"gid": _gid},
                )

        # Rename game_settings key: active_phase_id → current_phase_id (key renamed in lemonade)
        old_phase_row = (await conn.execute(
            text("SELECT value FROM game_settings WHERE key='active_phase_id'")
        )).fetchone()
        if old_phase_row:
            await conn.execute(text(
                "INSERT OR REPLACE INTO game_settings (key, value) VALUES ('current_phase_id', :v)"
            ), {"v": old_phase_row[0]})
            await conn.execute(text("DELETE FROM game_settings WHERE key='active_phase_id'"))

        # Backfill any guild_id=0 rows left by an earlier migration run (e.g. if
        # the bot was updated after the guild DB was first created with old code).
        if _gid:
            # users has a composite PK (guild_id, discord_id) so promote only rows
            # that have no existing guild-specific counterpart, then drop duplicates.
            await conn.execute(
                text("""
                    UPDATE users SET guild_id = :gid
                    WHERE guild_id = 0
                    AND discord_id NOT IN (
                        SELECT discord_id FROM users WHERE guild_id = :gid
                    )
                """),
                {"gid": _gid},
            )
            await conn.execute(
                text("DELETE FROM users WHERE guild_id = 0"),
            )
            # Simple tables: just reassign the 0 rows.
            for _table in ("bets", "parlays", "pending_parlay_legs", "betting_restrictions"):
                await conn.execute(
                    text(f"UPDATE {_table} SET guild_id = :gid WHERE guild_id = 0"),  # noqa: S608
                    {"gid": _gid},
                )


_BUILTIN_MARKET_TYPES = [
    ("TRIBUTE_WINS",            "Tribute Wins (Victor)",                "HARD",      "Tribute wins the entire Hunger Games and is declared Victor."),
    ("TRIBUTE_PLACEMENT",       "Tribute Placement (Exact)",            "HARD",      "Tribute finishes in a specific exact placement position."),
    ("TRIBUTE_TOP_N",           "Tribute Top-N Finish",                 "MODERATE",  "Tribute finishes within the top N tributes remaining."),
    ("TRIBUTE_KILLS",           "Top Killer",                           "HARD",      "Tribute gets the most kills of any tribute in the Games."),
    ("KILL_EVENT",              "Kill Event (A kills B)",               "HARD",      "One specific tribute kills another specific tribute."),
    ("DEATH_CAUSE",             "Death Cause",                          "MODERATE",  "Tribute dies by a specific cause (natural, mutt, tribute, or Gamemakers)."),
    ("FIRST_BLOOD",             "First Blood",                          "MODERATE",  "Tribute gets the very first kill of the Games (Pre-Games/Bloodbath only)."),
    ("BLOODBATH_SURVIVOR",      "Bloodbath Survivor",                   "EASY",      "Tribute survives the opening Cornucopia bloodbath (Pre-Games/Bloodbath only)."),
    ("KILLS_OU",                "Kills Over/Under",                     "MODERATE",  "Tribute gets more or fewer kills than a set line (e.g. over/under 1.5)."),
    ("PLACEMENT_OU",            "Placement Over/Under",                 "MODERATE",  "Tribute finishes with a better or worse placement than a set line."),
    ("MAKES_FINAL_8",           "Makes Final 8",                        "MODERATE",  "Tribute is still alive when the Final 8 phase begins."),
    ("MISSES_FINAL_8",          "Eliminated Before Final 8",            "MODERATE",  "Tribute is eliminated before the Final 8 phase begins."),
    ("MAKES_FINAL_5",           "Makes Final 5",                        "MODERATE",  "Tribute is still alive when the Final 5 phase begins."),
    ("MISSES_FINAL_5",          "Eliminated Before Final 5",            "MODERATE",  "Tribute is eliminated before the Final 5 phase begins."),
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
    # ── Alliance-level markets ─────────────────────────────────────────────────
    ("ALLIANCE_VICTOR",         "Alliance Victor",                      "HARD",      "A member of the alliance wins the Games. Resolves at game end."),
    ("ALLIANCE_KILLS_OU",       "Alliance Total Kills Over/Under",      "MODERATE",  "Bet over or under on the alliance's combined kill total. Resolves at game end."),
    ("ALLIANCE_ALL_BLOODBATH",  "Alliance All Survive Bloodbath",       "MODERATE",  "All alliance members survive the opening bloodbath. Resolves when Bloodbath ends."),
    ("ALLIANCE_ALL_FINAL_8",    "Alliance All Make Final 8",            "HARD",      "All alliance members are alive when the Final 8 phase begins."),
    ("ALLIANCE_ONE_FINAL_8",    "Alliance At Least One Makes Final 8",  "MODERATE",  "At least one alliance member is alive when the Final 8 phase begins."),
    ("ALLIANCE_ALL_FINAL_5",    "Alliance All Make Final 5",            "VERY_HARD", "All alliance members are alive when the Final 5 phase begins."),
    ("ALLIANCE_ONE_FINAL_5",    "Alliance At Least One Makes Final 5",  "MODERATE",  "At least one alliance member is alive when the Final 5 phase begins."),
    ("FIRST_ALLIANCE_WIPED",    "First Alliance to be Wiped",           "HARD",      "This alliance is the first to have one or fewer living members. Resolves manually."),
    ("ALLIANCE_MOST_KILLS",     "Alliance Gets Most Kills",             "HARD",      "This alliance is credited with the most kills of any alliance. Resolves at game end."),
    ("EXACT_ALLIANCE_KILLS",    "Alliance Exact Kill Count",            "VERY_HARD", "This alliance is credited with exactly the guessed number of kills (top_n). Resolves at game end."),
    # ── New tribute-level markets ──────────────────────────────────────────────
    ("TRIBUTE_RUNNER_UP",       "Tribute Runner-Up (2nd Place)",        "HARD",      "Tribute finishes exactly 2nd — the runner-up to the Victor. Auto-resolves at game end."),
    ("FIRST_TRIBUTE_TO_DIE",    "First Tribute to Die",                 "MODERATE",  "This tribute is the very first to die in the Games. Auto-resolves on the first death."),
    ("FIRST_IN_ALLIANCE_DEATH", "First in Alliance to Die",             "MODERATE",  "This tribute is the first member of their alliance to die. Requires alliance_id. Auto-resolves on first alliance death."),
    ("HIGHEST_TRAINING_SCORE",  "Highest Training Score",               "HARD",      "This tribute receives the highest individual training score. Auto-resolves when Pre-Games ends."),
    ("LOWEST_TRAINING_SCORE",   "Lowest Training Score",                "HARD",      "This tribute receives the lowest individual training score. Auto-resolves when Pre-Games ends."),
    ("TRIBUTE_KILLED_BLOODBATH","Killed in Bloodbath",                  "MODERATE",  "Tribute is killed during the opening Cornucopia bloodbath. Auto-resolves when Arena phase begins."),
    # ── New district-level markets ─────────────────────────────────────────────
    ("DISTRICT_HIGHEST_SCORE",  "District Highest Combined Score",      "HARD",      "This district has the highest combined training score total. Auto-resolves when Pre-Games ends."),
    ("FIRST_DISTRICT_WIPE",     "First District Wipe",                  "MODERATE",  "This district is the first to have all members eliminated. Auto-resolves on the wipe-triggering death."),
    ("DISTRICT_WIPED_BLOODBATH","District Wiped in Bloodbath",          "HARD",      "Both district tributes are killed during the opening bloodbath. Auto-resolves when Arena phase begins."),
    # ── New alliance-level markets ─────────────────────────────────────────────
    ("ALLIANCE_RUNNER_UP",      "Alliance Produces Runner-Up",          "HARD",      "A member of this alliance finishes 2nd overall. Auto-resolves at game end."),
    ("ALLIANCE_WIPED_BLOODBATH","Alliance Wiped in Bloodbath",          "VERY_HARD", "All alliance members are killed during the opening bloodbath. Auto-resolves when Arena phase begins."),
    # ── Game-level prop markets ────────────────────────────────────────────────
    ("BLOODBATH_KILLS_OU",      "Bloodbath Kills Over/Under",           "MODERATE",  "Over or under on total tribute-on-tribute kills scored during the bloodbath. Auto-resolves when Arena phase begins."),
    ("BLOODBATH_DEATHS_OU",     "Bloodbath Deaths Over/Under",          "MODERATE",  "Over or under on total tribute deaths in the bloodbath. Auto-resolves when Bloodbath phase ends."),
    ("EXACT_BLOODBATH_DEATHS",  "Exact Bloodbath Deaths",               "VERY_HARD", "Guess the exact number of bloodbath deaths (placement_num). Auto-resolves when Bloodbath phase ends."),
    ("BLOODBATH_NO_DEATHS",     "Bloodbath Contains No Deaths",         "LONGSHOT",  "Nobody dies in the bloodbath — a bloodless Cornucopia. Auto-resolves when Bloodbath ends."),
    ("ANY_BB_DOUBLE_KILL",      "Any Tribute Records 2+ Bloodbath Kills","HARD",     "At least one tribute records two or more kills during the bloodbath. Resolves manually."),
    ("ARENA_TRAP_DEATHS_OU",    "Arena Trap Deaths Over/Under",         "MODERATE",  "Over or under on deaths from Gamemaker traps/arena hazards. Resolves manually at game end."),
    ("ARENA_ENV_DEATHS_OU",     "Arena Environmental Deaths Over/Under","MODERATE",  "Over or under on deaths from natural causes or mutts combined. Resolves manually at game end."),
    ("ARENA_IS_NATURAL",        "Arena is a Natural Arena",             "EASY",      "The arena is a natural environment (wilderness, jungle, etc.). Auto-resolves when Pre-Games ends."),
    ("ARENA_IS_ARTIFICIAL",     "Arena is an Artificial Arena",         "EASY",      "The arena is man-made (industrial, tech hazards, etc.). Auto-resolves when Pre-Games ends."),
    ("NUM_TENS_OU",             "Number of 10+ Scores Over/Under",      "MODERATE",  "Over or under on how many tributes receive a training score of 10 or higher. Auto-resolves when Pre-Games ends."),
    ("SOLO_TRIBUTES_OU",        "Solo (Unallied) Tributes Over/Under",  "MODERATE",  "Over or under on how many tributes enter with no alliance. Auto-resolves when Pre-Games ends."),
    ("GAMES_DURATION",          "Games Duration (Days)",                "HARD",      "Guess how many days the Games last (placement_num = guessed days 5–10). Resolves manually at game end."),
    ("GAMES_FEAST",             "Games Features a Feast",               "MODERATE",  "The Games will feature a Cornucopia feast event. Resolves manually."),
    ("GAMES_BETRAYAL",          "Games Features a Betrayal",            "MODERATE",  "The Games will feature a notable alliance betrayal. Resolves manually."),
    ("DISTRICT_PARTNER_KILL",   "Games Features a District Partner Kill","HARD",     "Any tribute kills their own district partner during the Games. Auto-resolves at game end."),
]


async def _seed_defaults() -> None:
    defaults = {
        "cashout_allowed": json.dumps(config.CASHOUT_ALLOWED),
        "cashout_rate": json.dumps(config.CASHOUT_RATE),
        "game_active": json.dumps(False),
        "default_chips": json.dumps(config.DEFAULT_CHIPS),
        "current_phase_id": json.dumps(None),
        "sponsor_state": json.dumps(None),
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
                ("Pre-Games",        "Only District Victor and tribute score markets are open", 0),
                ("Bloodbath",        "The initial cornucopia bloodbath — bloodbath markets open", 1),
                ("Sponsors Open",    "Sponsorship window opens — funded districts surge",         2),
                ("Arena",            "The arena opens — all remaining markets go live",           3),
                ("Sponsors Closing", "Sponsorship window closes — funding edge fades",            4),
                ("Final 8",          "The top 8 tributes remain",                                 5),
                ("Final 5",          "The top 5 tributes remain",                                 6),
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


async def get_guild_setting(guild_id: int, key: str) -> str | None:
    return await get_setting(f"{guild_id}:{key}")


async def set_guild_setting(guild_id: int, key: str, value) -> None:
    await set_setting(f"{guild_id}:{key}", value)

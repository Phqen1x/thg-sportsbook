from __future__ import annotations

import contextvars
import json
import logging
import math
import random
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, or_, select

from bot.database.engine import get_session, get_read_session, set_setting, get_setting, current_guild_id
from bot.database.models import (
    Alliance,
    Bet,
    BettingPhase,
    BettingRestriction,
    DistrictRecord,
    GameSetting,
    Market,
    MarketTemplate,
    Modifier,
    ModifierAssignment,
    Parlay,
    ParlayTemplate,
    ParlayTemplateLeg,
    PendingParlayLeg,
    Tribute,
    TributeLock,
    User,
)
from bot.odds.calculator import (
    combined_american,
    implied_probability,
    parlay_combined_probability,
    straight_payout,
    parlay_payout,
)
from bot.odds.defaults import (
    AGE_NEUTRAL,
    ALLIANCE_FACTOR_ALPHA,
    DEFAULT_FALLBACK_ODDS,
    HIST_ALPHA,
    HIST_KILL_ALPHA,
    KILL_BOOST_SCALE,
    MODIFIER_TEMPER,
    age_factor,
    alliance_default_odds,
    apply_group_influence,
    death_cause_share,
    debilitation_factor,
    debilitation_label,
    default_odds,
    district_default_odds,
    district_pair_kill_factor,
    funding_factor,
    funding_label,
    game_default_odds,
    kill_boost_dr_factor,
    kill_quality_multiplier,
    prior_experience_factor,
    reputation_factor,
    SADE_CHAMPION_FACTOR,
    strength_hurts,
)
from bot.imaging.base import (
    buf_to_discord_file,
    get_active_theme,
    get_theme_by_name,
    list_theme_names,
    render_async,
    set_active_theme,
)
from bot.imaging.bet_slip import (
    ParlayLegData,
    render_parlay_result_slip,
    render_straight_result_slip,
)
from bot.utils.checks import is_admin
from bot.utils.formatters import fmt_chips, fmt_odds, safe_defer
from bot.utils.market_view import MarketPageView, MarketTypePageView, sort_markets
# Leg-compatibility validation lives in the betting cog; reused here so admin and
# auto-generated parlays obey the same conflict rules as member-built ones.
from bot.cogs.betting import _parlay_conflict, tribute_lookup_for_markets, PARLAY_PAYOUT_CAP

log = logging.getLogger("capitol.admin")

# Contextvar that accumulates bet-result notifications while markets are resolved.
# Command handlers set this up before their session block; _resolve_market pushes into it.
_notif_buffer: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "_notif_buffer", default=None
)


@asynccontextmanager
async def _collect_notifications() -> AsyncGenerator[list[dict], None]:
    notifs: list[dict] = []
    token = _notif_buffer.set(notifs)
    try:
        yield notifs
    finally:
        _notif_buffer.reset(token)


async def _send_resolution_dms(bot: commands.Bot, notifications: list[dict]) -> None:
    for notif in notifications:
        try:
            user = bot.get_user(notif["user_id"]) or await bot.fetch_user(notif["user_id"])
            if user is None:
                continue
            status = notif["status"]
            if notif["type"] == "straight":
                buf = await render_async(
                    render_straight_result_slip,
                    notif["market_label"],
                    notif["odds"],
                    notif["wager"],
                    notif["payout"],
                    status,
                )
                file = buf_to_discord_file(buf, "bet_result.png")
                if status == "WON":
                    msg = (
                        f"Your bet on **{notif['market_label']}** has **WON**! "
                        f"You've been paid **{fmt_chips(notif['payout'])}**."
                    )
                elif status == "LOST":
                    msg = f"Your bet on **{notif['market_label']}** has **LOST**. Better luck next time!"
                else:
                    msg = (
                        f"Your bet on **{notif['market_label']}** has been **VOIDED**. "
                        f"Your **{fmt_chips(notif['wager'])}** wager has been refunded."
                    )
                await user.send(content=msg, file=file)
            elif notif["type"] == "parlay":
                legs = [
                    ParlayLegData(
                        leg_num=i + 1,
                        market_label=leg["market_label"],
                        odds=leg["odds"],
                        status=leg["status"],
                    )
                    for i, leg in enumerate(notif["legs"])
                ]
                buf = await render_async(
                    render_parlay_result_slip, legs, notif["wager"], notif["payout"], status
                )
                file = buf_to_discord_file(buf, "parlay_result.png")
                if status == "WON":
                    msg = (
                        f"Your parlay has **WON**! "
                        f"You've been paid **{fmt_chips(notif['payout'])}**."
                    )
                elif status == "LOST":
                    msg = "Your parlay has **LOST**. Better luck next time!"
                else:
                    msg = (
                        f"Your parlay has been **VOIDED**. "
                        f"Your **{fmt_chips(notif['wager'])}** wager has been refunded."
                    )
                poster_username = notif.get("original_poster_username")
                if poster_username:
                    msg += f"\n(Tailed from **{poster_username}**'s board listing.)"
                await user.send(content=msg, file=file)
            elif notif["type"] == "tail_notify":
                verb = {"WON": "won", "LOST": "lost", "VOIDED": "been voided"}.get(status, status.lower())
                msg = (
                    f"**{notif['tailer_username']}** tailed your Parlay #{notif['parlay_id']} "
                    f"off the board — it just **{verb}**."
                )
                await user.send(content=msg)
        except discord.Forbidden:
            log.warning("Cannot DM user %s — DMs disabled", notif["user_id"])
        except Exception:
            log.exception("Failed to send result DM to user %s", notif["user_id"])


MARKET_TYPES = [
    app_commands.Choice(name="Tribute Wins (Victor)", value="TRIBUTE_WINS"),
    app_commands.Choice(name="Tribute Placement (Exact)", value="TRIBUTE_PLACEMENT"),
    app_commands.Choice(name="Tribute Top-N Finish", value="TRIBUTE_TOP_N"),
    app_commands.Choice(name="Tribute Runner-Up (2nd Place)", value="TRIBUTE_RUNNER_UP"),
    app_commands.Choice(name="First Tribute to Die", value="FIRST_TRIBUTE_TO_DIE"),
    app_commands.Choice(name="Top Killer", value="TRIBUTE_KILLS"),
    app_commands.Choice(name="Kill Event (A kills B)", value="KILL_EVENT"),
    app_commands.Choice(name="Death Cause", value="DEATH_CAUSE"),
    app_commands.Choice(name="First Blood", value="FIRST_BLOOD"),
    app_commands.Choice(name="Bloodbath Survivor", value="BLOODBATH_SURVIVOR"),
    app_commands.Choice(name="Killed in Bloodbath", value="TRIBUTE_KILLED_BLOODBATH"),
    app_commands.Choice(name="First in Alliance to Die", value="FIRST_IN_ALLIANCE_DEATH"),
    app_commands.Choice(name="Highest Training Score", value="HIGHEST_TRAINING_SCORE"),
    app_commands.Choice(name="Lowest Training Score", value="LOWEST_TRAINING_SCORE"),
    app_commands.Choice(name="Kills Over/Under", value="KILLS_OU"),
    app_commands.Choice(name="Placement Over/Under", value="PLACEMENT_OU"),
    app_commands.Choice(name="Makes Final 8", value="MAKES_FINAL_8"),
    app_commands.Choice(name="Eliminated Before Final 8", value="MISSES_FINAL_8"),
    app_commands.Choice(name="Makes Final 5", value="MAKES_FINAL_5"),
    app_commands.Choice(name="Eliminated Before Final 5", value="MISSES_FINAL_5"),
    app_commands.Choice(name="Arena Type (Pre-Games)", value="ARENA_TYPE"),
    app_commands.Choice(name="Exact Training Score", value="EXACT_TRAINING_SCORE"),
    app_commands.Choice(
        name="Combined District Score", value="COMBINED_DISTRICT_SCORE"
    ),
    app_commands.Choice(name="Training Score Over/Under", value="TRAINING_SCORE_OU"),
    # ── Game-level prop markets ────────────────────────────────────────────────
    app_commands.Choice(name="Bloodbath Kills Over/Under", value="BLOODBATH_KILLS_OU"),
    app_commands.Choice(name="Bloodbath Deaths Over/Under", value="BLOODBATH_DEATHS_OU"),
    app_commands.Choice(name="Exact Bloodbath Deaths", value="EXACT_BLOODBATH_DEATHS"),
    app_commands.Choice(name="Bloodbath Contains No Deaths", value="BLOODBATH_NO_DEATHS"),
    app_commands.Choice(
        name="Any Tribute Records 2+ BB Kills", value="ANY_BB_DOUBLE_KILL"
    ),
    app_commands.Choice(name="Arena Trap Deaths Over/Under", value="ARENA_TRAP_DEATHS_OU"),
    app_commands.Choice(
        name="Arena Environmental Deaths Over/Under", value="ARENA_ENV_DEATHS_OU"
    ),
    app_commands.Choice(name="Arena is a Natural Arena", value="ARENA_IS_NATURAL"),
    app_commands.Choice(
        name="Arena is an Artificial Arena", value="ARENA_IS_ARTIFICIAL"
    ),
    app_commands.Choice(name="Number of 10s Awarded Over/Under", value="NUM_TENS_OU"),
    app_commands.Choice(name="Solo Tributes Over/Under", value="SOLO_TRIBUTES_OU"),
    app_commands.Choice(name="Games Duration (Days)", value="GAMES_DURATION"),
    app_commands.Choice(name="Games — Features a Feast", value="GAMES_FEAST"),
    app_commands.Choice(name="Games — Features a Betrayal", value="GAMES_BETRAYAL"),
    app_commands.Choice(
        name="Games — District Partner vs. Partner Kill", value="DISTRICT_PARTNER_KILL"
    ),
    # ── District-partner comparison markets ───────────────────────────────────
    app_commands.Choice(
        name="Scores Higher Than District Partner", value="PARTNER_SCORE_HIGHER"
    ),
    app_commands.Choice(
        name="Scores Lower Than District Partner", value="PARTNER_SCORE_LOWER"
    ),
    app_commands.Choice(
        name="Places Higher Than District Partner", value="PARTNER_PLACE_HIGHER"
    ),
    app_commands.Choice(
        name="Places Lower Than District Partner", value="PARTNER_PLACE_LOWER"
    ),
    # ── District-level markets ─────────────────────────────────────────────────
    app_commands.Choice(name="District Victor", value="DISTRICT_VICTOR"),
    app_commands.Choice(
        name="District Highest Combined Score", value="DISTRICT_HIGHEST_SCORE"
    ),
    app_commands.Choice(name="First District Wipe", value="FIRST_DISTRICT_WIPE"),
    app_commands.Choice(
        name="District Total Kills Over/Under", value="DISTRICT_KILLS_OU"
    ),
    app_commands.Choice(
        name="District Both Survive Bloodbath", value="DISTRICT_BOTH_BLOODBATH"
    ),
    app_commands.Choice(
        name="District Wiped in Bloodbath", value="DISTRICT_WIPED_BLOODBATH"
    ),
    app_commands.Choice(
        name="District Both Make Final 8", value="DISTRICT_BOTH_FINAL_8"
    ),
    app_commands.Choice(
        name="District At Least One Makes Final 8", value="DISTRICT_ONE_FINAL_8"
    ),
    app_commands.Choice(
        name="District Both Make Final 5", value="DISTRICT_BOTH_FINAL_5"
    ),
    app_commands.Choice(
        name="District At Least One Makes Final 5", value="DISTRICT_ONE_FINAL_5"
    ),
    # ── Alliance-level markets ─────────────────────────────────────────────────
    app_commands.Choice(name="Alliance Victor", value="ALLIANCE_VICTOR"),
    app_commands.Choice(
        name="Alliance Total Kills Over/Under", value="ALLIANCE_KILLS_OU"
    ),
    app_commands.Choice(name="Alliance All Make Final 8", value="ALLIANCE_ALL_FINAL_8"),
    app_commands.Choice(
        name="Alliance At Least One Makes Final 8", value="ALLIANCE_ONE_FINAL_8"
    ),
    app_commands.Choice(name="Alliance All Make Final 5", value="ALLIANCE_ALL_FINAL_5"),
    app_commands.Choice(
        name="Alliance At Least One Makes Final 5", value="ALLIANCE_ONE_FINAL_5"
    ),
    app_commands.Choice(
        name="First Alliance to be Wiped", value="FIRST_ALLIANCE_WIPED"
    ),
    app_commands.Choice(name="Alliance Gets Most Kills", value="ALLIANCE_MOST_KILLS"),
    app_commands.Choice(
        name="Alliance Exact Kill Count", value="EXACT_ALLIANCE_KILLS"
    ),
    app_commands.Choice(
        name="Alliance Produces Runner-Up (2nd Place)", value="ALLIANCE_RUNNER_UP"
    ),
]

# Milestone market constants used for parlay validation
_MAKES_MILESTONES = {"MAKES_FINAL_8", "MAKES_FINAL_5"}
_ALL_MILESTONES = {
    "MAKES_FINAL_8",
    "MISSES_FINAL_8",
    "MAKES_FINAL_5",
    "MISSES_FINAL_5",
}
_MILESTONE_GROUP = {
    "MAKES_FINAL_8": "FINAL_8",
    "MISSES_FINAL_8": "FINAL_8",
    "MAKES_FINAL_5": "FINAL_5",
    "MISSES_FINAL_5": "FINAL_5",
}

# Market types that resolve when their respective phase is entered
_PHASE_ENTRY_MARKETS: dict[str, tuple[str, str]] = {
    "Final 8": ("MAKES_FINAL_8", "MISSES_FINAL_8"),
    "Final 5": ("MAKES_FINAL_5", "MISSES_FINAL_5"),
}

# Pre-Games prop markets — resolve when Pre-Games ends
_PREGAMES_PROP_TYPES = {
    "ARENA_TYPE",
    "EXACT_TRAINING_SCORE",
    "COMBINED_DISTRICT_SCORE",
    "TRAINING_SCORE_OU",
    "ARENA_IS_NATURAL",
    "ARENA_IS_ARTIFICIAL",
    "NUM_TENS_OU",
    "SOLO_TRIBUTES_OU",
    "PARTNER_SCORE_HIGHER",
    "PARTNER_SCORE_LOWER",
}
# The subset of _PREGAMES_OPEN_TYPES that does NOT carry forward into Bloodbath —
# training-score/scouting props are only meaningful before the Games start.
# (District Victor and Tribute Wins are pre-game markets too, but they run the
# whole game, so they're excluded here and stay open into Bloodbath and beyond.)
_PREGAMES_EXCLUSIVE_TYPES = _PREGAMES_PROP_TYPES | {
    "HIGHEST_TRAINING_SCORE",
    "LOWEST_TRAINING_SCORE",
    "DISTRICT_HIGHEST_SCORE",
}
# Bloodbath markets — resolve/void when Bloodbath ends
_BLOODBATH_RESOLVE_TYPES = {"BLOODBATH_SURVIVOR"}
_BLOODBATH_VOID_TYPES = {"FIRST_BLOOD"}

# ── Phase-gated market availability ───────────────────────────────────────────
# Markets open progressively as the Games advance. During Pre-Games the District
# and individual-Tribute victor moneylines plus the training-score / scouting
# props are live. Bloodbath opens every market type except the pre-game-exclusive
# props/scores and all alliance-level markets (see _BLOODBATH_OPEN_TYPES below,
# computed after _ALLIANCE_MARKET_TYPES is defined) — alliance markets stay closed
# until Arena. Bloodbath markets resolve the instant Arena begins (see
# game_set_phase), and every market type is open from Arena onward — including
# through Sponsors Open/Closing, which only affect funding-modifier strength
# (_set_sponsor_state), never which types are open.
_PREGAMES_OPEN_TYPES = _PREGAMES_PROP_TYPES | {
    "HIGHEST_TRAINING_SCORE",
    "LOWEST_TRAINING_SCORE",
    "DISTRICT_HIGHEST_SCORE",
    "DISTRICT_VICTOR",
    "TRIBUTE_WINS",
}


def _open_types_for_phase(phase_name: str | None) -> set[str] | None:
    """Market types that should be OPEN during ``phase_name``.

    Returns ``None`` when there is no type restriction — every market is open
    (Arena onward, including the sponsor-open, sponsor-closing, and final phases).
    """
    if phase_name == "Pre-Games":
        return _PREGAMES_OPEN_TYPES
    if phase_name == "Bloodbath":
        return _BLOODBATH_OPEN_TYPES
    return None


def _market_should_open(market: "Market", phase_id: int | None, allowed: set[str] | None) -> bool:
    """Whether ``market`` should be OPEN in the phase identified by ``phase_id``.

    A market pinned to an explicit phase opens only in that phase; otherwise it is
    gated by the phase's allowed-type set (``allowed=None`` means all types open).
    """
    if market.phase_id is not None:
        return market.phase_id == phase_id
    return allowed is None or market.type in allowed


async def _set_sponsor_state(session, state: str | None) -> None:
    """Persist the sponsorship-window state ("open" | "closed" | None) within the
    given session so a following _recalculate_markets sees it. Drives the funding
    modifier strength via funding_factor."""
    row = await session.get(GameSetting, "sponsor_state")
    if row is not None:
        row.value = json.dumps(state)
    else:
        session.add(GameSetting(key="sponsor_state", value=json.dumps(state)))

# District-level market types
_DISTRICT_MARKET_TYPES = {
    "DISTRICT_VICTOR",
    "DISTRICT_KILLS_OU",
    "DISTRICT_BOTH_BLOODBATH",
    "DISTRICT_WIPED_BLOODBATH",
    "DISTRICT_BOTH_FINAL_8",
    "DISTRICT_ONE_FINAL_8",
    "DISTRICT_BOTH_FINAL_5",
    "DISTRICT_ONE_FINAL_5",
    "DISTRICT_HIGHEST_SCORE",
    "FIRST_DISTRICT_WIPE",
}
# District milestone markets that resolve at phase entry
_DISTRICT_PHASE_ENTRY: dict[str, tuple[str, ...]] = {
    "Final 8": ("DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8"),
    "Final 5": ("DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5"),
}

# Alliance-level market types (placement_num = alliance_id, cause = alliance name)
_ALLIANCE_MARKET_TYPES = {
    "ALLIANCE_VICTOR",
    "ALLIANCE_KILLS_OU",
    "ALLIANCE_ALL_FINAL_8",
    "ALLIANCE_ONE_FINAL_8",
    "ALLIANCE_ALL_FINAL_5",
    "ALLIANCE_ONE_FINAL_5",
    "FIRST_ALLIANCE_WIPED",
    "ALLIANCE_MOST_KILLS",
    "EXACT_ALLIANCE_KILLS",
    "ALLIANCE_RUNNER_UP",
}

# Bloodbath opens every built-in market type except the pre-game-exclusive props
# (training scores, arena type, partner-score comparisons) and all alliance-level
# markets, which stay closed until Arena begins.
_BLOODBATH_OPEN_TYPES = (
    {c.value for c in MARKET_TYPES} - _PREGAMES_EXCLUSIVE_TYPES - _ALLIANCE_MARKET_TYPES
)

# Game-level prop markets (no tribute, district, or alliance — whole-game props)
_GAME_PROP_TYPES = {
    "BLOODBATH_KILLS_OU",
    "BLOODBATH_DEATHS_OU",
    "EXACT_BLOODBATH_DEATHS",
    "BLOODBATH_NO_DEATHS",
    "ANY_BB_DOUBLE_KILL",
    "ARENA_TRAP_DEATHS_OU",
    "ARENA_ENV_DEATHS_OU",
    "ARENA_IS_NATURAL",
    "ARENA_IS_ARTIFICIAL",
    "NUM_TENS_OU",
    "SOLO_TRIBUTES_OU",
    "GAMES_DURATION",
    "GAMES_FEAST",
    "GAMES_BETRAYAL",
    "DISTRICT_PARTNER_KILL",
}
_ALLIANCE_PHASE_ENTRY: dict[str, tuple[str, ...]] = {
    "Final 8": ("ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8"),
    "Final 5": ("ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5"),
}

BUILT_IN_TYPE_VALUES = {c.value for c in MARKET_TYPES}

DIFFICULTY_ODDS: dict[str, int] = {
    "EASY": -200,
    "MODERATE": +100,
    "HARD": +300,
    "VERY_HARD": +700,
    "LONGSHOT": +1500,
}

# AI-generated parlays are labelled by their actual combined American odds:
#   Safe     : combined < +500
#   Balanced : +500 <= combined <= +3000
#   Longshot : combined > +3000
def classify_parlay_tier(combined_odds: int) -> str:
    if combined_odds < 500:
        return "SAFE"
    if combined_odds <= 3000:
        return "BALANCED"
    return "LONGSHOT"


DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy      (≈ -200 odds)", value="EASY"),
    app_commands.Choice(name="Moderate  (≈ +100 odds)", value="MODERATE"),
    app_commands.Choice(name="Hard      (≈ +300 odds)", value="HARD"),
    app_commands.Choice(name="Very Hard (≈ +700 odds)", value="VERY_HARD"),
    app_commands.Choice(name="Longshot  (≈ +1500 odds)", value="LONGSHOT"),
]

_DEATH_CAUSES = ["Natural Causes", "Mutt", "Another Tribute", "Trap/Event"]

# Values must match _DEATH_CAUSES exactly so autocomplete selection maps 1:1 to
# the cause stored on each DEATH_CAUSE market.
_DEATH_CAUSE_SUGGESTIONS = _DEATH_CAUSES

# Each district fields at most two tributes, and never two of the same displayed
# gender — one each of male / female / non-binary. Enforced on tribute creation,
# edits, and bulk imports.
_GENDER_WORD = {"M": "male", "F": "female", "NB": "non-binary"}


def _district_slot_error(
    roster: list["Tribute"],
    district: int,
    display_gender: str,
    exclude_id: int | None = None,
) -> str | None:
    """Return an error string if placing a tribute whose displayed gender is
    `display_gender` into `district` would break the roster rules (max two
    tributes, no repeated displayed gender), else None. `roster` is the full
    tribute list; `exclude_id` skips the tribute being edited in place."""
    peers = [t for t in roster if t.district == district and t.id != exclude_id]
    if len(peers) >= 2:
        return f"District {district} already has two tributes — that is the maximum."
    if any(p.display_gender == display_gender for p in peers):
        word = _GENDER_WORD.get(display_gender, display_gender)
        return f"District {district} already has a {word} tribute."
    return None


# ── Autocomplete helpers ──────────────────────────────────────────────────────


async def tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).order_by(
                Tribute.district, Tribute.non_binary, Tribute.gender
            )
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def alive_tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute)
            .where(Tribute.status == "ALIVE")
            .order_by(Tribute.district, Tribute.non_binary, Tribute.gender)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def dead_tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute)
            .where(Tribute.status == "DEAD")
            .order_by(Tribute.district, Tribute.non_binary, Tribute.gender)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} (DEAD)"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def unallied_tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Tributes not yet in any alliance — a tribute can only belong to one, so
    those already allied are hidden from the alliance-add picker."""
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute)
            .where(Tribute.alliance_id == None)  # noqa: E711
            .order_by(Tribute.district, Tribute.non_binary, Tribute.gender)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def open_market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Market)
            .where(Market.status.in_(["OPEN", "CLOSED"]))
            .order_by(Market.id)
        )
        markets = result.scalars().all()
    choices = []
    for m in markets:
        label = f"[{m.status}] {m.label}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
    return choices[:25]


async def public_parlay_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Pending, publicly-tailable member parlays — for admin per-parlay cashout overrides."""
    async with get_read_session() as session:
        result = await session.execute(
            select(Parlay)
            .where(Parlay.status == "PENDING", Parlay.is_public == True)  # noqa: E712
            .order_by(Parlay.id.desc())
            .limit(100)
        )
        parlays = result.scalars().all()
    choices = []
    for p in parlays:
        label = f"Parlay #{p.id} — {fmt_chips(p.total_wager)} wager"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(p.id)))
    return choices[:25]


async def overridden_market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Market)
            .where(Market.status.in_(["OPEN", "CLOSED"]), Market.odds_override == True)
            .order_by(Market.id)
        )
        markets = result.scalars().all()
    choices = []
    for m in markets:
        label = f"[{m.status}] {m.label}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
    return choices[:25]


async def death_cause_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    for cause in _DEATH_CAUSE_SUGGESTIONS:
        if current.lower() in cause.lower():
            choices.append(app_commands.Choice(name=cause, value=cause))
    # If the user typed something that doesn't match any suggestion, surface it as a custom entry
    if current and not any(current.lower() == c.value.lower() for c in choices):
        choices.append(app_commands.Choice(name=f'Custom: "{current}"', value=current))
    return choices[:25]


async def parlay_template_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(ParlayTemplate).order_by(ParlayTemplate.id.desc())
        )
        templates = result.scalars().all()
    choices = []
    for t in templates:
        flag = "" if t.active else " (hidden)"
        label = f"#{t.id} [{t.source}] {t.name}{flag}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(t.id)))
    return choices[:25]


async def phase_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(BettingPhase).order_by(BettingPhase.sort_order)
        )
        phases = result.scalars().all()
    choices = []
    for p in phases:
        if current.lower() in p.name.lower():
            choices.append(app_commands.Choice(name=p.name, value=str(p.id)))
    return choices[:25]


async def alliance_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(Alliance).order_by(Alliance.name))
        alliances = result.scalars().all()
    choices = []
    for a in alliances:
        if current.lower() in a.name.lower():
            choices.append(app_commands.Choice(name=a.name, value=str(a.id)))
    return choices[:25]


async def market_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(MarketTemplate)
            .where(MarketTemplate.active == True)
            .order_by(MarketTemplate.is_builtin.desc(), MarketTemplate.name)
        )
        templates = result.scalars().all()
    choices = []
    for t in templates:
        label = t.name if t.is_builtin else f"[Custom] {t.name}"
        value = t.type_key if t.type_key else f"CUSTOM_{t.id}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=value))
    return choices[:25]


async def market_template_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(MarketTemplate).order_by(MarketTemplate.id)
        )
        templates = result.scalars().all()
    choices = []
    for t in templates:
        label = f"{'[INACTIVE] ' if not t.active else ''}{t.name}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(t.id)))
    return choices[:25]


async def modifier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(Modifier).order_by(Modifier.id))
        mods = result.scalars().all()
    choices = []
    for m in mods:
        label = f"{m.label} (×{m.weight})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
    return choices[:25]


async def modifier_assignment_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        assign_result = await session.execute(
            select(ModifierAssignment, Modifier.label, Modifier.weight)
            .join(Modifier, ModifierAssignment.modifier_id == Modifier.id)
            .order_by(ModifierAssignment.id)
        )
        rows = assign_result.all()
        trib_result = await session.execute(select(Tribute))
        tributes = {t.id: t for t in trib_result.scalars().all()}
        alliance_result = await session.execute(select(Alliance))
        alliances = {a.id: a for a in alliance_result.scalars().all()}
    choices = []
    for assignment, mod_label, weight in rows:
        if assignment.tribute_id is not None:
            t = tributes.get(assignment.tribute_id)
            scope = (
                f"D{t.district}{t.display_gender} {t.name}"
                if t
                else f"Tribute #{assignment.tribute_id}"
            )
        elif assignment.alliance_id is not None:
            a = alliances.get(assignment.alliance_id)
            scope = a.name if a else f"Alliance #{assignment.alliance_id}"
        else:
            scope = f"District {assignment.district}"
        label = f"{mod_label} → {scope} (×{weight})"
        if current.lower() in label.lower():
            choices.append(
                app_commands.Choice(name=label[:100], value=str(assignment.id))
            )
    return choices[:25]


# ── Seniority ────────────────────────────────────────────────────────────────


def _seniority_factor(joined_at: datetime | None) -> float:
    """Probability multiplier based on how long a member has been in the server."""
    if joined_at is None:
        return 1.0
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - joined_at).days
    if days < 30:
        return 0.5
    years = days / 365.25
    if years < 1:
        return 1.0
    return round(1.0 + 0.1 * int(years), 10)


def _district_historical_factor(
    record: DistrictRecord | None,
    all_records: list[DistrictRecord],
    num_games: int,
    for_kills: bool = False,
    gender: str | None = None,
) -> float:
    """
    Multiplicative probability factor for a district derived from aggregate historical stats.

    for_kills=False (default): win/survival factor.  High kill rate is treated as a
    penalty — aggressive districts get into more fights and are less likely to win.
    Uses HIST_ALPHA.

    for_kills=True: kill-market factor.  Only kill-related columns are used, all as
    positive signals.  Uses HIST_KILL_ALPHA.

    gender: when provided ("M" or "F"), gender-specific sub-fields are used in place
    of the aggregate equivalents where available (e.g. victor_male_count instead of
    wins, male_kills instead of total_kills), producing tribute-level accuracy.
    Falls back to the aggregate field when the gender-specific one is None.
    """
    if record is None or num_games <= 0:
        return 1.0

    components: list[float] = []

    # Clamp each component's ratio so a single extreme (e.g. a district with 0
    # of a penalty stat, which would otherwise divide by zero) can't dominate.
    RATIO_MIN, RATIO_MAX = 0.2, 5.0

    def _component(
        d_val: float | None, g_vals: list[float], invert: bool = False
    ) -> None:
        if d_val is None or not g_vals:
            return
        g_avg = sum(g_vals) / len(g_vals)
        if g_avg <= 0:
            return
        if invert:
            # A zero stat is the best possible outcome for an inverted (penalty)
            # metric; treat it as the max ratio instead of dividing by zero.
            ratio = RATIO_MAX if d_val <= 0 else (g_avg / d_val)
        else:
            ratio = d_val / g_avg
        components.append(max(RATIO_MIN, min(RATIO_MAX, ratio)))

    bb_set = [r for r in all_records if r.bloodbath_kills is not None]
    bb_death_set = [r for r in all_records if r.bloodbath_deaths is not None]

    # Resolve gender-specific or aggregate kill counts once for reuse below.
    if gender == "M" and record.male_kills is not None:
        kills_val = record.male_kills / num_games
        kills_g_vals = [
            r.male_kills / num_games for r in all_records if r.male_kills is not None
        ]
    elif gender == "F" and record.female_kills is not None:
        kills_val = record.female_kills / num_games
        kills_g_vals = [
            r.female_kills / num_games
            for r in all_records
            if r.female_kills is not None
        ]
    else:
        kills_set = [r for r in all_records if r.total_kills is not None]
        kills_val = (
            record.total_kills / num_games if record.total_kills is not None else None
        )
        kills_g_vals = [r.total_kills / num_games for r in kills_set]

    if for_kills:
        # Kill markets: high historical kills → positive factor.
        # Use gender-specific kill counts when available so male/female tributes
        # are priced against their gender's own historical kill rates.
        _component(kills_val, kills_g_vals)
        _component(
            (
                record.bloodbath_kills / num_games
                if record.bloodbath_kills is not None
                else None
            ),
            [r.bloodbath_kills / num_games for r in bb_set],
        )
        _component(
            float(record.kill_record) if record.kill_record is not None else None,
            [float(r.kill_record) for r in all_records if r.kill_record is not None],
        )
    else:
        # Win/survival markets: high kill rate is a penalty (more fights → less likely to win).
        # Use gender-specific kill counts so a gender's kill-rate history only penalises
        # tributes of that gender.
        _component(kills_val, kills_g_vals, invert=True)
        _component(
            (
                record.bloodbath_kills / num_games
                if record.bloodbath_kills is not None
                else None
            ),
            [r.bloodbath_kills / num_games for r in bb_set],
            invert=True,
        )
        # High bloodbath death rate (tributes from this district dying in the bloodbath)
        # is a survival penalty — compare each district against the field average.
        _component(
            (
                record.bloodbath_deaths / num_games
                if record.bloodbath_deaths is not None
                else None
            ),
            [r.bloodbath_deaths / num_games for r in bb_death_set],
            invert=True,
        )
        _component(
            record.avg_placement,
            [r.avg_placement for r in all_records if r.avg_placement is not None],
            invert=True,
        )
        # Use gender-specific victor counts when available so a male tribute's odds
        # reflect his gender's historical win rate, not the district's aggregate.
        if gender == "M" and record.victor_male_count is not None:
            wins_val: float | None = float(record.victor_male_count)
            wins_g_vals = [
                float(r.victor_male_count)
                for r in all_records
                if r.victor_male_count is not None
            ]
        elif gender == "F" and record.victor_female_count is not None:
            wins_val = float(record.victor_female_count)
            wins_g_vals = [
                float(r.victor_female_count)
                for r in all_records
                if r.victor_female_count is not None
            ]
        else:
            wins_val = float(record.wins) if record.wins is not None else None
            wins_g_vals = [float(r.wins) for r in all_records if r.wins is not None]
        _component(wins_val, wins_g_vals)
        _component(
            float(record.top8_finishes) if record.top8_finishes is not None else None,
            [
                float(r.top8_finishes)
                for r in all_records
                if r.top8_finishes is not None
            ],
        )
        _component(
            float(record.top5_finishes) if record.top5_finishes is not None else None,
            [
                float(r.top5_finishes)
                for r in all_records
                if r.top5_finishes is not None
            ],
        )
        _component(
            float(record.kill_record) if record.kill_record is not None else None,
            [float(r.kill_record) for r in all_records if r.kill_record is not None],
        )
        # Use gender-specific runner-up counts when available.
        if gender == "M" and record.runner_up_male is not None:
            ru_val: float | None = record.runner_up_male / num_games
            ru_g_vals = [
                r.runner_up_male / num_games
                for r in all_records
                if r.runner_up_male is not None
            ]
        elif gender == "F" and record.runner_up_female is not None:
            ru_val = record.runner_up_female / num_games
            ru_g_vals = [
                r.runner_up_female / num_games
                for r in all_records
                if r.runner_up_female is not None
            ]
        else:
            runner_up_set = [r for r in all_records if r.runner_up_finishes is not None]
            ru_val = (
                record.runner_up_finishes / num_games
                if record.runner_up_finishes is not None
                else None
            )
            ru_g_vals = [r.runner_up_finishes / num_games for r in runner_up_set]
        _component(ru_val, ru_g_vals)
        _component(
            record.avg_placement_last5,
            [
                r.avg_placement_last5
                for r in all_records
                if r.avg_placement_last5 is not None
            ],
            invert=True,
        )

    if not components:
        return 1.0

    raw_factor = sum(components) / len(components)
    alpha = HIST_KILL_ALPHA if for_kills else HIST_ALPHA
    return 1.0 + (raw_factor - 1.0) * alpha


# How much a district's historical arena-type win preference nudges odds when the
# current game arena matches or opposes that preference.  Max swing ±5% (ratio 0 or 1).
HIST_ARENA_ALPHA = 0.10


def _arena_preference_factor(
    record: DistrictRecord | None, arena_type: str | None
) -> float:
    """Win-odds multiplier based on whether a district's historical wins favour the
    current arena type.

    Districts whose past wins skew toward artificial arenas get a small boost when
    the current game is set to ARTIFICIAL (and a small penalty in NATURAL arenas),
    and vice-versa.  Neutral when wins are evenly split (ratio 0.5) or when either
    ``wins`` or ``manmade_arena_wins`` is unset.  Max swing: ±HIST_ARENA_ALPHA/2.
    """
    if record is None or arena_type is None:
        return 1.0
    if record.wins is None or record.wins <= 0 or record.manmade_arena_wins is None:
        return 1.0
    manmade_ratio = record.manmade_arena_wins / record.wins
    if arena_type == "ARTIFICIAL":
        deviation = manmade_ratio - 0.5
    elif arena_type == "NATURAL":
        deviation = 0.5 - manmade_ratio
    else:
        return 1.0
    return 1.0 + deviation * HIST_ARENA_ALPHA


# ── Odds helpers ──────────────────────────────────────────────────────────────


async def _kill_quality_for_victim(session, victim: Tribute) -> float:
    """Kill-quality multiplier for killing ``victim``.

    Reflects the victim's win odds relative to the field: killing a favourite is
    worth more than killing a long-shot. Prefers the victim's live "to win"
    market odds (what bettors actually saw); falls back to computed win odds.
    The victim is excluded explicitly (rather than relying on their status
    already being DEAD) since sessions run with autoflush=False — a pending
    status="DEAD" write isn't visible to this query until flushed.
    """
    alive_count = await session.scalar(
        select(func.count()).select_from(Tribute).where(
            Tribute.status == "ALIVE", Tribute.id != victim.id
        )
    )
    field_size = (alive_count or 0) + 1  # surviving field + the victim themselves

    win_mkt = await session.execute(
        select(Market)
        .where(Market.tribute_a_id == victim.id, Market.type == "TRIBUTE_WINS")
        .order_by(Market.id.desc())
    )
    mkt = win_mkt.scalars().first()
    if mkt is not None:
        victim_prob = implied_probability(mkt.odds)
    else:
        trib_result = await session.execute(select(Tribute))
        all_tributes = trib_result.scalars().all()
        victim_prob = implied_probability(
            default_odds("TRIBUTE_WINS", victim, all_tributes)
        )

    return kill_quality_multiplier(victim_prob, 1.0 / max(1, field_size))


def _compute_odds(
    market_type: str,
    trib_a: Tribute | None,
    all_tributes: list[Tribute],
    trib_b: Tribute | None = None,
    placement_num: int | None = None,
    top_n: int | None = None,
    ou_line: float | None = None,
    ou_side: str | None = None,
    modifier_factor: float = 1.0,
    cause: str | None = None,
    arena_type: str | None = None,
    hist_avg_score: float | None = None,
    base_odds_override: int | None = None,
    district_record: "DistrictRecord | None" = None,
    all_district_records: "list[DistrictRecord] | None" = None,
    num_games: int = 0,
) -> int:
    from bot.odds.calculator import american_to_decimal, prob_to_american

    if base_odds_override is not None:
        base = base_odds_override
    else:
        base = default_odds(
            market_type,
            trib_a,
            all_tributes,
            tribute_b=trib_b,
            placement_num=placement_num,
            top_n=top_n,
            ou_line=ou_line,
            ou_side=ou_side,
            hist_avg_score=hist_avg_score,
        )
    if trib_a is None:
        return base
    district_mates = [
        t for t in all_tributes if t.district == trib_a.district and t.id != trib_a.id
    ]
    alliance_mates = [
        t
        for t in all_tributes
        if trib_a.alliance_id
        and t.alliance_id == trib_a.alliance_id
        and t.id != trib_a.id
    ]
    if market_type == "DEATH_CAUSE":
        # Death cause is anchored to the tribute's win probability: a tribute
        # that is P% to win is (1-P)% to die, and that death budget is split
        # across the causes so they sum back to it — not four flat longshots.
        # Reuse the win market's own pricing pipeline to get P(win).
        win_base = default_odds("TRIBUTE_WINS", trib_a, all_tributes)
        win_odds = apply_group_influence(
            win_base,
            "TRIBUTE_WINS",
            trib_a,
            district_mates,
            alliance_mates,
            all_tributes,
        )
        p_win = 1.0 / american_to_decimal(win_odds)
        if modifier_factor != 1.0:
            p_win = max(0.01, min(0.99, p_win * modifier_factor))
        share = death_cause_share(
            cause,
            1.0 - p_win,
            arena_type,
            district_record,
            all_district_records,
            num_games,
        )
        return prob_to_american(max(0.005, min(0.99, share)))
    odds = apply_group_influence(
        base,
        market_type,
        trib_a,
        district_mates,
        alliance_mates,
        all_tributes,
        ou_side=ou_side,
    )
    if modifier_factor != 1.0:
        dec = american_to_decimal(odds)
        prob = 1.0 / dec
        if strength_hurts(market_type, ou_side):
            # "Yes" side is a bet against the tribute, so a strength-boosting
            # modifier must push it down: scale the complement and invert.
            comp_prob = 1.0 - prob
            adj_comp = max(0.01, min(0.99, comp_prob * modifier_factor))
            adj_prob = max(0.01, min(0.99, 1.0 - adj_comp))
        else:
            adj_prob = max(0.01, min(0.99, prob * modifier_factor))
        odds = prob_to_american(adj_prob)
    if market_type == "KILL_EVENT" and trib_b is not None:
        factor = district_pair_kill_factor(trib_a, trib_b)
        if factor != 1.0:
            dec = american_to_decimal(odds)
            prob = 1.0 / dec
            adj_prob = max(0.01, min(0.99, prob * factor))
            odds = prob_to_american(adj_prob)
    return odds


async def _recalculate_markets(session) -> None:
    # Recompute every non-overridden, unresolved market (both CLOSED pre-game and
    # OPEN live markets). Victor/placement/etc. odds are field-relative, so they
    # must be re-priced against the *current* roster — otherwise a market created
    # while the field was still being filled keeps the stale odds it was born with
    # (e.g. the first tribute added gets priced as if alone → 99% to win).
    result = await session.execute(
        select(Market).where(
            Market.status.in_(["OPEN", "CLOSED"]), Market.odds_override == False
        )
    )
    markets = result.scalars().all()
    trib_result = await session.execute(select(Tribute))
    all_tributes = trib_result.scalars().all()

    # Build raw per-tribute modifier factors from direct/district/alliance assignments
    assign_result = await session.execute(
        select(ModifierAssignment, Modifier.weight).join(
            Modifier, ModifierAssignment.modifier_id == Modifier.id
        )
    )
    raw_factors: dict[int, float] = {}
    for assignment, weight in assign_result.all():
        if assignment.tribute_id is not None:
            raw_factors[assignment.tribute_id] = (
                raw_factors.get(assignment.tribute_id, 1.0) * weight
            )
        elif assignment.alliance_id is not None:
            for t in all_tributes:
                if t.alliance_id == assignment.alliance_id:
                    raw_factors[t.id] = raw_factors.get(t.id, 1.0) * weight
        elif assignment.district is not None:
            for t in all_tributes:
                if t.district == assignment.district:
                    raw_factors[t.id] = raw_factors.get(t.id, 1.0) * weight

    # Apply debilitation level as a multiplicative debuff on top of other modifiers
    for t in all_tributes:
        dw = debilitation_factor(t.debilitation_level)
        if dw != 1.0:
            raw_factors[t.id] = raw_factors.get(t.id, 1.0) * dw

    # Build a base-odds map for custom market types (difficulty baseline used as starting odds)
    tmpl_result = await session.execute(select(MarketTemplate))
    custom_bases: dict[str, int] = {
        f"CUSTOM_{t.id}": (
            t.default_odds
            if t.default_odds is not None
            else DIFFICULTY_ODDS[t.difficulty]
        )
        for t in tmpl_result.scalars().all()
    }

    # Load district aggregate records, global game count, and arena type for historical/death factors
    hist_result = await session.execute(select(DistrictRecord))
    all_dr = list(hist_result.scalars().all())
    num_games_row = await session.get(GameSetting, "num_games")
    num_games = int(json.loads(num_games_row.value)) if num_games_row else 0
    arena_type_row = await session.get(GameSetting, "arena_type")
    arena_type = json.loads(arena_type_row.value) if arena_type_row else None
    # Sponsorship window state scales the district funding factor (Sponsors Open
    # amplifies it, Sponsors Closing cuts it to a residual).
    sponsor_state_row = await session.get(GameSetting, "sponsor_state")
    sponsor_state = json.loads(sponsor_state_row.value) if sponsor_state_row else None
    age_row = await session.get(GameSetting, "victor_avg_age")
    global_age_neutral = float(json.loads(age_row.value)) if age_row else float(AGE_NEUTRAL)
    dr_map = {r.district: r for r in all_dr}
    # Manually-set district reputation factor (applies to both win and kill markets)
    dist_rep_factor: dict[int, float] = {
        d: reputation_factor(dr_map[d].reputation) if d in dr_map else 1.0
        for d in range(1, 13)
    }
    # District funding level factor (applies to both win and kill markets).
    # Scaled by the sponsorship-window state.
    dist_funding_factor: dict[int, float] = {
        d: funding_factor(dr_map[d].funding_level, sponsor_state) if d in dr_map else 1.0
        for d in range(1, 13)
    }

    # Per-tribute "circumstantial" strength factors — everything that makes a
    # tribute strong or weak on their own, *before* alliance influence: sponsor
    # modifiers, seniority, the gender-aware district history factor (placement +
    # bloodbath record), the arena-preference factor, district reputation, and the
    # accumulated kill-quality boost. Two dicts: win/survival vs. kill markets.
    alive = [t for t in all_tributes if t.status == "ALIVE"]
    solo_win_factors: dict[int, float] = {}
    solo_kill_factors: dict[int, float] = {}
    for t in alive:
        own = raw_factors.get(t.id, 1.0)
        seniority = _seniority_factor(t.member_joined_at)
        rep = dist_rep_factor.get(t.district, 1.0)
        fund = dist_funding_factor.get(t.district, 1.0)
        # Age factor: anchored to historical victor age average where available.
        age = age_factor(t.age, global_age_neutral)
        # Convert additive kill-boost sum to a multiplier. Neutral (0 kills) → 1.0.
        kboost = max(0.5, 1.0 + (t.kill_boost or 0.0) * KILL_BOOST_SCALE)
        prior_exp = prior_experience_factor(t.times_played, t.highest_placement)
        sade_champ = SADE_CHAMPION_FACTOR if t.sade_champion else 1.0
        hist_win = _district_historical_factor(
            dr_map.get(t.district), all_dr, num_games, for_kills=False, gender=t.gender
        )
        hist_kill = _district_historical_factor(
            dr_map.get(t.district), all_dr, num_games, for_kills=True, gender=t.gender
        )
        arena_pref = _arena_preference_factor(dr_map.get(t.district), arena_type)
        solo_win_factors[t.id] = (
            own * seniority * age * hist_win * arena_pref * rep * fund * kboost * prior_exp * sade_champ
        )
        solo_kill_factors[t.id] = own * seniority * age * hist_kill * rep * fund * kboost * prior_exp * sade_champ

    # The field-average circumstantial strength is the neutral benchmark each ally
    # is judged against. An ally exactly at the average neither helps nor hurts.
    avg_win = (
        sum(solo_win_factors.values()) / len(solo_win_factors)
        if solo_win_factors
        else 1.0
    )
    avg_kill = (
        sum(solo_kill_factors.values()) / len(solo_kill_factors)
        if solo_kill_factors
        else 1.0
    )

    # Fold in alliance influence: a signed, size-scaled sum of each ally's
    # circumstantial surplus/deficit vs. the field. Allies who are objectively
    # weak — poor placement history, bloodbath-prone, cursed by modifiers, low
    # reputation — pull a tribute's odds DOWN even if the tribute is strong, and a
    # bigger alliance of such allies drags harder; strong allies do the reverse.
    # The net multiplier is clamped so an extreme pack can't run odds away.
    tribute_win_factors: dict[int, float] = {}
    tribute_kill_factors: dict[int, float] = {}
    for t in alive:
        win = solo_win_factors[t.id]
        kill = solo_kill_factors[t.id]
        if t.alliance_id is not None:
            allies = [
                m for m in alive if m.alliance_id == t.alliance_id and m.id != t.id
            ]
            if allies:
                win_nudge = ALLIANCE_FACTOR_ALPHA * sum(
                    solo_win_factors[m.id] - avg_win for m in allies
                )
                kill_nudge = ALLIANCE_FACTOR_ALPHA * sum(
                    solo_kill_factors[m.id] - avg_kill for m in allies
                )
                win *= max(0.4, min(2.5, 1.0 + win_nudge))
                kill *= max(0.4, min(2.5, 1.0 + kill_nudge))
        tribute_win_factors[t.id] = win
        tribute_kill_factors[t.id] = kill

    # Field-average of the FINAL per-tribute factors (solo strength + alliance
    # nudge). Each market's modifier is divided by this so it acts relative to the
    # field: a tribute with the average boost set is priced off the base model
    # unchanged, keeping the roster off the ±9900 rail even when most tributes
    # share a large modifier (e.g. a district-wide career boost).
    avg_win_factor = (
        sum(tribute_win_factors.values()) / len(tribute_win_factors)
        if tribute_win_factors
        else 1.0
    )
    avg_kill_factor = (
        sum(tribute_kill_factors.values()) / len(tribute_kill_factors)
        if tribute_kill_factors
        else 1.0
    )

    _KILL_MARKET_TYPES = {"TRIBUTE_KILLS", "KILLS_OU", "FIRST_BLOOD", "KILL_EVENT"}

    for market in markets:
        if market.tribute_a_id is None:
            continue  # global markets (e.g. ARENA_TYPE) have no tribute-driven odds
        trib_a = next((t for t in all_tributes if t.id == market.tribute_a_id), None)
        trib_b = (
            next((t for t in all_tributes if t.id == market.tribute_b_id), None)
            if market.tribute_b_id
            else None
        )
        if trib_a:
            is_kill = market.type in _KILL_MARKET_TYPES
            raw_factor = (
                tribute_kill_factors.get(trib_a.id, 1.0)
                if is_kill
                else tribute_win_factors.get(trib_a.id, 1.0)
            )
            # The modifier is applied to odds RELATIVE to the field average, not as
            # an absolute multiplier. A tribute carrying the field-average set of
            # boosts is left unchanged (×1.0); only above/below-average tributes
            # move. This is what keeps the field off the ±9900 cap: an absolute
            # multiplier applied to every tribute (e.g. a 2× career boost shared by
            # most of the roster) would otherwise push the whole field toward the
            # rail and pin the un-boosted handful at the opposite cap.
            field_avg_factor = avg_kill_factor if is_kill else avg_win_factor
            # Temper the relative modifier with an exponent < 1 so a single large
            # assignment (e.g. a 2× career boost or a 0.2× curse) can't on its own
            # drive a tribute to the rail. relative=1 (average) stays 1; extremes
            # are pulled toward 1.
            relative = raw_factor / max(field_avg_factor, 0.01)
            mfactor = relative ** MODIFIER_TEMPER
            # Resolve historical average training score for this tribute's district/gender
            # so pregame training markets (HIGHEST_TRAINING_SCORE, etc.) can differentiate
            # districts before actual scores are revealed.
            _dr = dr_map.get(trib_a.district)
            _hist_score: float | None = None
            if _dr is not None:
                if trib_a.gender == "M" and _dr.avg_training_score_male is not None:
                    _hist_score = float(_dr.avg_training_score_male)
                elif trib_a.gender == "F" and _dr.avg_training_score_female is not None:
                    _hist_score = float(_dr.avg_training_score_female)
                elif _dr.avg_training_score is not None:
                    _hist_score = float(_dr.avg_training_score)
            market.odds = _compute_odds(
                market.type,
                trib_a,
                all_tributes,
                trib_b=trib_b,
                placement_num=market.placement_num,
                top_n=market.top_n,
                ou_line=market.ou_line,
                ou_side=market.ou_side,
                modifier_factor=mfactor,
                cause=market.cause,
                arena_type=arena_type,
                hist_avg_score=_hist_score,
                base_odds_override=custom_bases.get(market.type),
                district_record=dr_map.get(trib_a.district),
                all_district_records=all_dr,
                num_games=num_games,
            )

    # ── District-level markets (no tribute_a_id, district stored in placement_num) ─
    dist_market_result = await session.execute(
        select(Market).where(
            Market.status.in_(["OPEN", "CLOSED"]),
            Market.odds_override == False,
            Market.type.in_(_DISTRICT_MARKET_TYPES),
        )
    )
    for market in dist_market_result.scalars().all():
        d = market.placement_num
        if d is None:
            continue
        d_tributes = [t for t in all_tributes if t.district == d]
        market.odds = district_default_odds(
            market.type,
            d_tributes,
            all_tributes,
            ou_line=market.ou_line,
            ou_side=market.ou_side,
            win_factors=tribute_win_factors,
            kill_factors=tribute_kill_factors,
        )

    # ── Alliance-level markets (placement_num = alliance_id) ───────────────────
    alliance_market_result = await session.execute(
        select(Market).where(
            Market.status.in_(["OPEN", "CLOSED"]),
            Market.odds_override == False,
            Market.type.in_(_ALLIANCE_MARKET_TYPES),
        )
    )
    for market in alliance_market_result.scalars().all():
        aid = market.placement_num
        if aid is None:
            continue
        members = [t for t in all_tributes if t.alliance_id == aid]
        market.odds = alliance_default_odds(
            market.type,
            members,
            all_tributes,
            ou_line=market.ou_line,
            ou_side=market.ou_side,
            win_factors=solo_win_factors,
            kill_factors=solo_kill_factors,
            top_n=market.top_n,
        )

    # ── Game-level prop markets (no tribute/district/alliance) ─────────────────
    game_prop_result = await session.execute(
        select(Market).where(
            Market.status.in_(["OPEN", "CLOSED"]),
            Market.odds_override == False,
            Market.type.in_(_GAME_PROP_TYPES),
        )
    )
    for market in game_prop_result.scalars().all():
        market.odds = game_default_odds(
            market.type,
            all_tributes,
            placement_num=market.placement_num,
            ou_line=market.ou_line,
            ou_side=market.ou_side,
        )


async def _resolve_market(session, market: Market, result: bool | None) -> dict:
    market.status = "RESOLVED"
    market.result = result
    bet_result = await session.execute(
        select(Bet).where(Bet.market_id == market.id, Bet.status == "PENDING")
    )
    bets = bet_result.scalars().all()
    resolved_count = 0
    credits_issued = 0
    buf = _notif_buffer.get()
    for bet in bets:
        if result is None:
            bet.status = "VOIDED"
            _ur = await session.execute(
                select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id)
            )
            user = _ur.scalar_one_or_none()
            if user:
                user.chips += bet.wager
            if buf is not None and bet.parlay_id is None:
                buf.append({
                    "type": "straight",
                    "user_id": bet.user_id,
                    "status": "VOIDED",
                    "market_label": market.label,
                    "odds": bet.odds_at_placement,
                    "wager": bet.wager,
                    "payout": bet.wager,
                })
        elif result is True and bet.parlay_id is None:
            bet.status = "WON"
            _ur = await session.execute(
                select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id)
            )
            user = _ur.scalar_one_or_none()
            if user:
                user.chips += bet.payout_if_win
                user.total_won += bet.payout_if_win
            credits_issued += bet.payout_if_win
            if buf is not None:
                buf.append({
                    "type": "straight",
                    "user_id": bet.user_id,
                    "status": "WON",
                    "market_label": market.label,
                    "odds": bet.odds_at_placement,
                    "wager": bet.wager,
                    "payout": bet.payout_if_win,
                })
        elif result is False and bet.parlay_id is None:
            bet.status = "LOST"
            if buf is not None:
                buf.append({
                    "type": "straight",
                    "user_id": bet.user_id,
                    "status": "LOST",
                    "market_label": market.label,
                    "odds": bet.odds_at_placement,
                    "wager": bet.wager,
                    "payout": 0,
                })
        elif bet.parlay_id is not None:
            bet.status = (
                "WON" if result is True else ("VOIDED" if result is None else "LOST")
            )
            parlay_notifs = await _check_parlay(session, bet.parlay_id)
            if buf is not None:
                buf.extend(parlay_notifs)
        resolved_count += 1

    # An auto-generated featured parlay is considered "resolved" the moment any
    # of its legs settles — drop it from the tailing board so it can't be tailed
    # with a dead leg. Admin-built templates persist (they simply stop showing
    # once a leg is no longer OPEN, and admins can delete them manually).
    leg_rows = await session.execute(
        select(ParlayTemplateLeg).where(ParlayTemplateLeg.market_id == market.id)
    )
    stale_ids = {leg.template_id for leg in leg_rows.scalars().all()}
    for tid in stale_ids:
        tpl = await session.get(ParlayTemplate, tid)
        if tpl and tpl.source == "AUTO":
            await session.delete(tpl)

    return {"resolved": resolved_count, "credits": credits_issued}


async def _resolve_score_completion_markets(
    session, all_tributes: list["Tribute"], tribute_map: dict[int, "Tribute"]
) -> tuple[int, int]:
    """Resolve HIGHEST/LOWEST_TRAINING_SCORE and DISTRICT_HIGHEST_SCORE once every
    tribute who can still receive one has a training score.

    A tribute killed before ever being scored counts as a null score: they don't
    block this from resolving, and they're excluded from the max/min/district
    totals below (their own markets are voided separately at kill time).
    """
    still_pending = any(
        tr.training_score is None and tr.status != "DEAD" for tr in all_tributes
    )
    if still_pending:
        return 0, 0
    scored = [tr for tr in all_tributes if tr.training_score is not None]
    if not scored:
        return 0, 0

    resolved = 0
    credits = 0
    max_score = max(tr.training_score for tr in scored)
    min_score = min(tr.training_score for tr in scored)
    for mtype, target in (
        ("HIGHEST_TRAINING_SCORE", max_score),
        ("LOWEST_TRAINING_SCORE", min_score),
    ):
        hl_result = await session.execute(
            select(Market).where(
                Market.type == mtype,
                Market.status.in_(["OPEN", "CLOSED"]),
            )
        )
        for mkt in hl_result.scalars().all():
            trib = tribute_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
            result = bool(trib and trib.training_score == target)
            stats = await _resolve_market(session, mkt, result)
            resolved += 1
            credits += stats.get("credits", 0)

    dist_scores: dict[int, int] = {}
    for tr in scored:
        dist_scores[tr.district] = dist_scores.get(tr.district, 0) + tr.training_score
    if dist_scores:
        max_dist_score = max(dist_scores.values())
        dhs_result = await session.execute(
            select(Market).where(
                Market.type == "DISTRICT_HIGHEST_SCORE",
                Market.status.in_(["OPEN", "CLOSED"]),
            )
        )
        for mkt in dhs_result.scalars().all():
            d = mkt.placement_num
            result = d is not None and dist_scores.get(d, 0) == max_dist_score
            stats = await _resolve_market(session, mkt, result)
            resolved += 1
            credits += stats.get("credits", 0)

    return resolved, credits


# ── Auto-generated featured parlays ────────────────────────────────────────────
# Three tailable parlays are (re)generated at the start of each phase from the
# markets open at that moment, spanning a spread of risk/payout profiles. They
# carry no frozen odds — the legs are live markets, so the tailing board always
# shows current prices. They're cleared on the next phase and removed leg-by-leg
# as their markets resolve (see _resolve_market).

AUTO_PARLAY_MAX_WAGER = 1_000
_MIN_PARLAY_LEGS = 3
_MAX_PARLAY_LEGS = 8

# Temporary kill switch: when False, AI-generated featured parlays are stored
# with a blank description so admins can write their own via
# `/admin parlay set-description`.  The model still picks legs and titles; we
# just drop its description text.  Flip back to True to restore AI blurbs.
_AI_PARLAY_DESCRIPTIONS_ENABLED = False

# Temporary kill switch: when False, all AUTO/AI featured-parlay generation is
# disabled at the source, so every caller — automatic (game start, phase
# transitions) and manual (/admin parlay generate, /admin parlay ai-generate) —
# becomes a no-op. Admin-authored parlay templates (source="ADMIN") are
# unaffected. Flip back to True to re-enable.
_AUTO_PARLAY_GENERATION_ENABLED = False


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _parlay_prob(legs: list[Market]) -> float:
    return parlay_combined_probability([m.odds for m in legs])


def _parlay_within_cap(legs: list[Market]) -> bool:
    payout = parlay_payout(AUTO_PARLAY_MAX_WAGER, [m.odds for m in legs])
    return payout <= PARLAY_PAYOUT_CAP


def _pick_themed_legs(
    candidates: list[Market],
    max_legs: int = _MAX_PARLAY_LEGS,
    min_legs: int = _MIN_PARLAY_LEGS,
    *,
    worst_odds_first: bool = False,
    tribute_by_id: dict[int, "Tribute"] | None = None,
) -> list[Market]:
    """Greedily pick conflict-free, cap-safe legs.

    By default sorts probability high→low (best odds first). Pass worst_odds_first=True
    to pick the most difficult markets — used for LONGSHOT fallback. ``tribute_by_id``
    is forwarded to _parlay_conflict for district-aware rules.
    """
    sorted_cands = sorted(
        candidates,
        key=lambda m: implied_probability(m.odds),
        reverse=not worst_odds_first,
    )
    for target in range(min(max_legs, len(sorted_cands)), min_legs - 1, -1):
        chosen: list[Market] = []
        for m in sorted_cands:
            if _parlay_conflict(chosen, m, tribute_by_id) is not None:
                continue
            chosen.append(m)
            if not _parlay_within_cap(chosen):
                chosen.pop()
                continue
            if len(chosen) == target:
                break
        if len(chosen) >= min_legs:
            return chosen
    return []


def _dedup_markets(markets: list[Market]) -> list[Market]:
    seen: set[int] = set()
    result = []
    for m in markets:
        if m.id not in seen:
            seen.add(m.id)
            result.append(m)
    return result


async def _generate_auto_parlays(session, phase_id: int | None, count: int = 3) -> int:
    """(Re)generate up to `count` themed auto parlays. Returns how many were made.

    Each parlay is built around a central entity — one tribute, one alliance, or one
    district — so every leg in the parlay is narratively coherent. Legs: 3–8.
    """
    if not _AUTO_PARLAY_GENERATION_ENABLED:
        return 0
    old = await session.execute(
        select(ParlayTemplate).where(ParlayTemplate.source == "AUTO")
    )
    for tpl in old.scalars().all():
        await session.delete(tpl)
    await session.flush()

    mkt_result = await session.execute(select(Market).where(Market.status == "OPEN"))
    open_markets = list(mkt_result.scalars().all())
    if len(open_markets) < _MIN_PARLAY_LEGS:
        return 0

    # Load tribute/district/alliance context
    tid_set: set[int] = set()
    for m in open_markets:
        if m.tribute_a_id:
            tid_set.add(m.tribute_a_id)
        if m.tribute_b_id:
            tid_set.add(m.tribute_b_id)

    tributes_map: dict[int, Tribute] = {}
    if tid_set:
        t_rows = await session.execute(select(Tribute).where(Tribute.id.in_(tid_set)))
        tributes_map = {t.id: t for t in t_rows.scalars().all()}

    districts = {t.district for t in tributes_map.values()}
    district_records: dict[int, DistrictRecord] = {}
    if districts:
        dr_rows = await session.execute(
            select(DistrictRecord).where(DistrictRecord.district.in_(districts))
        )
        district_records = {dr.district: dr for dr in dr_rows.scalars().all()}

    alliance_ids = {t.alliance_id for t in tributes_map.values() if t.alliance_id}
    alliance_names: dict[int, str] = {}
    alliance_members: dict[int, set[int]] = {}
    if alliance_ids:
        a_rows = await session.execute(
            select(Alliance).where(Alliance.id.in_(alliance_ids))
        )
        alliance_names = {a.id: a.name for a in a_rows.scalars().all()}
        for t in tributes_map.values():
            if t.alliance_id:
                alliance_members.setdefault(t.alliance_id, set()).add(t.id)

    # Build themed groupings: (theme_type, theme_key, candidate_markets)
    # theme_key is int for tribute/alliance/district/veterans/kill_leaders,
    # tuple[int,int] for cross_district and rival_alliances.
    from itertools import combinations as _combinations
    groupings: list[tuple[str, object, list[Market]]] = []

    # Tribute-centric: every market where this tribute appears on either side
    by_tribute: dict[int, list[Market]] = {}
    for m in open_markets:
        for tid in (m.tribute_a_id, m.tribute_b_id):
            if tid and tid in tributes_map:
                by_tribute.setdefault(tid, []).append(m)
    for tid, mkts in by_tribute.items():
        deduped = _dedup_markets(mkts)
        if len(deduped) >= _MIN_PARLAY_LEGS:
            groupings.append(("tribute", tid, deduped))

    # Alliance-centric: every market touching any member of this alliance
    for aid, member_ids in alliance_members.items():
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in member_ids or m.tribute_b_id in member_ids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("alliance", aid, mkts))

    # District-centric: every market touching any tribute from this district
    for district in districts:
        dist_tids = {t.id for t in tributes_map.values() if t.district == district}
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in dist_tids or m.tribute_b_id in dist_tids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("district", district, mkts))

    # Cross-district: pair two districts together — pools more markets and frames
    # the parlay as an inter-district rivalry rather than a single-tribute showcase.
    district_list = sorted(districts)
    for d1, d2 in _combinations(district_list, 2):
        d1_tids = {t.id for t in tributes_map.values() if t.district == d1}
        d2_tids = {t.id for t in tributes_map.values() if t.district == d2}
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in d1_tids | d2_tids or m.tribute_b_id in d1_tids | d2_tids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("cross_district", (d1, d2), mkts))

    # Rival alliances: two alliances pooled — frames the parlay as an alliance war.
    aid_list = sorted(alliance_ids)
    for a1, a2 in _combinations(aid_list, 2):
        a1_tids = alliance_members.get(a1, set())
        a2_tids = alliance_members.get(a2, set())
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in a1_tids | a2_tids or m.tribute_b_id in a1_tids | a2_tids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("rival_alliances", (a1, a2), mkts))

    # Veterans: tributes with prior-game experience — the returning-class narrative.
    vet_tids = {t.id for t in tributes_map.values() if t.times_played and t.times_played >= 1}
    if vet_tids:
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in vet_tids or m.tribute_b_id in vet_tids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("veterans", 0, mkts))

    # Kill leaders: current-game tributes with 2+ kills — the bloodbath narrative.
    killer_tids = {t.id for t in tributes_map.values() if t.kills >= 2}
    if killer_tids:
        mkts = _dedup_markets([
            m for m in open_markets
            if m.tribute_a_id in killer_tids or m.tribute_b_id in killer_tids
        ])
        if len(mkts) >= _MIN_PARLAY_LEGS:
            groupings.append(("kill_leaders", 0, mkts))

    if not groupings:
        return 0

    # Distribute `count` slots across tiers as evenly as possible.
    # SAFE gets extras first, then BALANCED, so LONGSHOT is never short-changed.
    n_each = count // 3
    rem = count % 3
    tier_allocations: list[tuple[str, int, int, int]] = [
        ("SAFE",     n_each + (1 if rem >= 1 else 0), 2, 4),
        ("BALANCED", n_each + (1 if rem >= 2 else 0), 4, 6),
        ("LONGSHOT", n_each,                           6, 8),
    ]

    # Hard cap: at most 1 tribute-centric parlay regardless of count.
    # Creative themes (cross_district, rival_alliances, veterans, kill_leaders)
    # always have priority; tribute is last resort only.
    import math as _math
    max_tribute = max(1, count // 5)
    tribute_count = 0
    used_tribute_keys: set[int] = set()

    TierEntry = tuple[str, object, list[Market], str]  # theme_type, theme_key, legs, tier_name
    selected: list[TierEntry] = []
    used_leg_sets: set[frozenset[int]] = set()

    _CREATIVE_THEMES = {"cross_district", "rival_alliances", "veterans", "kill_leaders"}
    _THEME_PRIORITY = {
        "cross_district": 0, "rival_alliances": 0,
        "veterans": 1, "kill_leaders": 1,
        "alliance": 2, "district": 2,
        "tribute": 3,
    }

    def _tribute_allowed(theme_type: str, theme_key: object) -> bool:
        if theme_type != "tribute":
            return True
        assert isinstance(theme_key, int)
        return theme_key not in used_tribute_keys and tribute_count < max_tribute

    for tier_name, n_slots, min_l, max_l in tier_allocations:
        for _slot in range(n_slots):
            tier_cands: list[tuple[str, object, list[Market]]] = []
            for theme_type, theme_key, mkts in groupings:
                if not _tribute_allowed(theme_type, theme_key):
                    continue
                legs = _pick_themed_legs(mkts, max_legs=max_l, min_legs=min_l, tribute_by_id=tributes_map)
                if len(legs) >= min_l:
                    leg_key = frozenset(l.id for l in legs)
                    if leg_key not in used_leg_sets:
                        tier_cands.append((theme_type, theme_key, legs))

            # LONGSHOT fallback: relax to worst-odds if no grouping has 6+ legs
            if not tier_cands and tier_name == "LONGSHOT":
                for theme_type, theme_key, mkts in groupings:
                    if not _tribute_allowed(theme_type, theme_key):
                        continue
                    legs = _pick_themed_legs(
                        mkts, max_legs=_MAX_PARLAY_LEGS, min_legs=_MIN_PARLAY_LEGS,
                        worst_odds_first=True, tribute_by_id=tributes_map,
                    )
                    if len(legs) >= _MIN_PARLAY_LEGS:
                        leg_key = frozenset(l.id for l in legs)
                        if leg_key not in used_leg_sets:
                            tier_cands.append((theme_type, theme_key, legs))

            # Last resort: allow a tribute even if cap is technically exceeded,
            # rather than leaving a slot completely empty.
            if not tier_cands:
                for theme_type, theme_key, mkts in groupings:
                    if theme_type == "tribute":
                        assert isinstance(theme_key, int)
                        if theme_key in used_tribute_keys:
                            continue
                    legs = _pick_themed_legs(mkts, max_legs=max_l, min_legs=min_l, tribute_by_id=tributes_map)
                    if len(legs) >= min_l:
                        leg_key = frozenset(l.id for l in legs)
                        if leg_key not in used_leg_sets:
                            tier_cands.append((theme_type, theme_key, legs))

            if not tier_cands:
                break

            # Sort: creative/non-tribute themes first, then market-type diversity, then leg count.
            tier_cands.sort(
                key=lambda c: (
                    _THEME_PRIORITY.get(c[0], 9),
                    -len({m.type for m in c[2]}),
                    -len(c[2]),
                ),
            )
            best_type, best_key, best_legs = tier_cands[0]
            selected.append((best_type, best_key, best_legs, tier_name))
            used_leg_sets.add(frozenset(l.id for l in best_legs))
            if best_type == "tribute":
                tribute_count += 1
                assert isinstance(best_key, int)
                used_tribute_keys.add(best_key)

    # Persist
    created = 0
    for theme_type, theme_key, legs, difficulty in selected:

        if theme_type == "tribute":
            assert isinstance(theme_key, int)
            t = tributes_map[theme_key]
            fname = t.name.split()[0]
            dr = district_records.get(t.district)
            parts: list[str] = []
            if t.sade_champion:
                parts.append(f"{fname} has already worn the victor's crown — SADE experience the odds can't capture")
            if t.times_played and t.times_played >= 2:
                parts.append(f"{fname} has {t.times_played} prior games of experience")
            elif t.times_played == 1:
                parts.append(f"{fname} isn't new to this — one prior game under their belt")
            if t.training_score and t.training_score >= 9:
                parts.append(f"a training score of {t.training_score} puts them in elite territory")
            elif t.training_score:
                parts.append(f"training score: {t.training_score}")
            if t.kills and t.kills >= 3:
                parts.append(f"{t.kills} kills already makes {fname} one of the most dangerous in the arena")
            elif t.kills:
                parts.append(f"{t.kills} {'kill' if t.kills == 1 else 'kills'} this game")
            if t.alliance_id and t.alliance_id in alliance_names:
                parts.append(f"running with {alliance_names[t.alliance_id]}")
            if dr and dr.wins:
                parts.append(
                    f"District {t.district} has {dr.wins} all-time "
                    f"{'victory' if dr.wins == 1 else 'victories'}"
                )
            desc = ". ".join(parts).capitalize() + "." if parts else f"Everything riding on {fname}."
            if t.sade_champion:
                name = f"{fname}: Double Victor"
            elif t.times_played and t.times_played >= 2:
                name = f"{fname}: Veteran's Slate"
            elif t.kills and t.kills >= 3:
                name = f"{fname}: Blood Trail"
            elif t.training_score and t.training_score >= 9:
                name = f"{fname}: Top of the Class"
            else:
                name = f"All In on {fname}"

        elif theme_type == "alliance":
            assert isinstance(theme_key, int)
            aname = alliance_names.get(theme_key, "The Alliance")
            member_tids = alliance_members.get(theme_key, set())
            members = [tributes_map[tid] for tid in member_tids if tid in tributes_map]
            names_str = " & ".join(t.name.split()[0] for t in members[:3])
            combined_kills = sum(t.kills for t in members)
            vets = [t for t in members if t.times_played]
            champs = [t for t in members if t.sade_champion]
            parts = [f"{names_str} march under the {aname} banner"]
            if champs:
                parts.append(f"{champs[0].name.split()[0]} is a former SADE victor — the experience shows")
            if combined_kills:
                parts.append(
                    f"{combined_kills} combined {'kill' if combined_kills == 1 else 'kills'} between them"
                )
            if vets:
                parts.append(
                    f"{len(vets)} returning {'tribute' if len(vets) == 1 else 'tributes'} "
                    f"with prior arena experience"
                )
            # District record flavor for the alliance's home districts
            alliance_districts = {tributes_map[tid].district for tid in member_tids if tid in tributes_map}
            for d in sorted(alliance_districts)[:2]:
                dr = district_records.get(d)
                if dr and dr.wins:
                    parts.append(f"District {d} has sent {dr.wins} victor{'s' if dr.wins != 1 else ''} before")
                elif dr and dr.top8_finishes:
                    parts.append(f"District {d} has {dr.top8_finishes} historical top-8 finishes")
            desc = ". ".join(parts).capitalize() + "."
            # Name varies by alliance strength
            top_score = max((t.training_score or 0) for t in members) if members else 0
            if champs:
                name = f"{aname}: Returning Champions"
            elif top_score >= 10:
                name = f"{aname}: Trained to Kill"
            elif combined_kills >= 3:
                name = f"{aname}: Blood Pact"
            else:
                name = f"{aname} Solidarity"

        elif theme_type == "district":
            assert isinstance(theme_key, int)
            dr = district_records.get(theme_key)
            dist_tids = {t.id for t in tributes_map.values() if t.district == theme_key}
            dist_tributes = [tributes_map[tid] for tid in dist_tids if tid in tributes_map]
            score_count = sum(1 for t in dist_tributes if t.training_score)
            score_sum = sum(t.training_score or 0 for t in dist_tributes)
            avg_score = score_sum // score_count if score_count else None
            parts = []
            if dr and dr.wins and dr.wins >= 3:
                parts.append(f"District {theme_key} has {dr.wins} all-time victories — this is what a dynasty looks like")
            elif dr and dr.wins:
                parts.append(f"District {theme_key} has tasted victory before — {dr.wins} time{'s' if dr.wins != 1 else ''}")
            else:
                parts.append(f"District {theme_key} is hungry and has everything to prove")
            if dr and dr.top5_finishes:
                parts.append(f"{dr.top5_finishes} top-5 finishes in the historical record")
            elif dr and dr.top8_finishes:
                parts.append(f"{dr.top8_finishes} top-8 finishes historically")
            if dr and dr.total_kills:
                parts.append(f"{dr.total_kills} all-time kills out of this district")
            if avg_score:
                parts.append(f"current tributes average a {avg_score} training score")
            if dr and dr.reputation and dr.reputation <= 2:
                parts.append("one of Panem's most feared districts — the Capitol watches closely")
            elif dr and dr.funding_level in ("rich", "well_funded"):
                parts.append(f"Capitol funding gives District {theme_key} an edge in preparation")
            desc = ". ".join(parts).capitalize() + "."
            if dr and dr.wins and dr.wins >= 3:
                name = f"District {theme_key}: Dynasty"
            elif dr and dr.total_kills and dr.total_kills >= 10:
                name = f"District {theme_key}: Killing Ground"
            elif dr and dr.runner_up_finishes and dr.runner_up_finishes >= 2:
                name = f"District {theme_key}: So Close"
            else:
                name = f"District {theme_key} Double Down"

        elif theme_type == "cross_district":
            assert isinstance(theme_key, tuple)
            d1, d2 = theme_key
            dr1 = district_records.get(d1)
            dr2 = district_records.get(d2)
            d1_tids = {t.id for t in tributes_map.values() if t.district == d1}
            d2_tids = {t.id for t in tributes_map.values() if t.district == d2}
            d1_tributes = [tributes_map[tid] for tid in d1_tids if tid in tributes_map]
            d2_tributes = [tributes_map[tid] for tid in d2_tids if tid in tributes_map]
            d1_kills = sum(t.kills for t in d1_tributes)
            d2_kills = sum(t.kills for t in d2_tributes)
            parts = [f"Districts {d1} and {d2} collide — back them both and let the arena decide"]
            if dr1 and dr1.wins and dr2 and dr2.wins:
                parts.append(
                    f"District {d1} carries {dr1.wins} historical {'win' if dr1.wins == 1 else 'wins'}, "
                    f"District {d2} has {dr2.wins}"
                )
            elif dr1 and dr1.wins:
                parts.append(
                    f"District {d1} has {dr1.wins} historical {'victory' if dr1.wins == 1 else 'victories'} "
                    f"— District {d2} is still searching for its first"
                )
            elif dr2 and dr2.wins:
                parts.append(
                    f"District {d2} holds {dr2.wins} historical {'victory' if dr2.wins == 1 else 'victories'} "
                    f"— District {d1} is still writing its story"
                )
            combined_kills = d1_kills + d2_kills
            if combined_kills:
                parts.append(f"{combined_kills} kills between these two districts so far this game")
            if dr1 and dr1.avg_placement and dr2 and dr2.avg_placement:
                better_d = d1 if dr1.avg_placement <= dr2.avg_placement else d2
                better_avg = min(dr1.avg_placement, dr2.avg_placement)
                parts.append(f"District {better_d} averages a placement of {better_avg} historically")
            desc = ". ".join(parts).capitalize() + "."
            d1_wins = dr1.wins or 0 if dr1 else 0
            d2_wins = dr2.wins or 0 if dr2 else 0
            if d1_wins + d2_wins >= 4:
                name = f"Districts {d1} & {d2}: Legacy Wager"
            elif d1_kills + d2_kills >= 4:
                name = f"Districts {d1} & {d2}: Bloodbath Stakes"
            else:
                name = f"Districts {d1} & {d2}: Divided Loyalties"

        elif theme_type == "rival_alliances":
            assert isinstance(theme_key, tuple)
            a1, a2 = theme_key
            a1_name = alliance_names.get(a1, f"Alliance {a1}")
            a2_name = alliance_names.get(a2, f"Alliance {a2}")
            a1_members = [tributes_map[tid] for tid in alliance_members.get(a1, set()) if tid in tributes_map]
            a2_members = [tributes_map[tid] for tid in alliance_members.get(a2, set()) if tid in tributes_map]
            a1_kills = sum(t.kills for t in a1_members)
            a2_kills = sum(t.kills for t in a2_members)
            a1_vets = [t for t in a1_members if t.times_played]
            a2_vets = [t for t in a2_members if t.times_played]
            a1_top_score = max((t.training_score or 0) for t in a1_members) if a1_members else 0
            a2_top_score = max((t.training_score or 0) for t in a2_members) if a2_members else 0
            parts = [f"{a1_name} and {a2_name} are the last two alliances standing — these bets span them both"]
            if a1_kills or a2_kills:
                stronger = a1_name if a1_kills >= a2_kills else a2_name
                parts.append(f"{stronger} leads the body count so far")
            if a1_vets or a2_vets:
                total_vets = len(a1_vets) + len(a2_vets)
                parts.append(f"{total_vets} veteran{'s' if total_vets != 1 else ''} across both alliances bring prior-game experience")
            if a1_top_score and a2_top_score:
                elite = a1_name if a1_top_score >= a2_top_score else a2_name
                parts.append(f"{elite} fields the higher-trained tributes")
            # District record flavor
            all_member_districts = {
                tributes_map[tid].district
                for tids in [alliance_members.get(a1, set()), alliance_members.get(a2, set())]
                for tid in tids if tid in tributes_map
            }
            win_districts = [
                d for d in sorted(all_member_districts)
                if district_records.get(d) and district_records[d].wins
            ]
            if win_districts:
                d = win_districts[0]
                parts.append(f"District {d} has {district_records[d].wins} all-time {'victory' if district_records[d].wins == 1 else 'victories'} backing this fight")
            desc = ". ".join(parts).capitalize() + "."
            combined_kills = a1_kills + a2_kills
            if combined_kills >= 4:
                name = f"{a1_name} vs {a2_name}: War of Banners"
            elif a1_vets or a2_vets:
                name = f"{a1_name} vs {a2_name}: Experienced Hands"
            else:
                name = f"{a1_name} vs {a2_name}: Alliance Clash"

        elif theme_type == "veterans":
            vet_list = sorted(
                [t for t in tributes_map.values() if t.times_played and t.times_played >= 1],
                key=lambda t: (-(t.times_played or 0), -(t.training_score or 0)),
            )
            vet_names = " & ".join(t.name.split()[0] for t in vet_list[:3])
            combined_kills = sum(t.kills for t in vet_list)
            champs = [t for t in vet_list if t.sade_champion]
            best_placement = min(
                (t.highest_placement for t in vet_list if t.highest_placement), default=None
            )
            parts = [f"{vet_names} have been here before — arena experience money can't buy"]
            if champs:
                parts.append(f"{champs[0].name.split()[0]} is a former victor walking in with a target on their back")
            if best_placement:
                parts.append(f"best prior finish among the group: {best_placement}{_ordinal(best_placement)} place")
            if combined_kills:
                parts.append(f"{combined_kills} kills already this game across the returning class")
            # Alliance context for vets
            vet_alliances = {alliance_names[t.alliance_id] for t in vet_list if t.alliance_id and t.alliance_id in alliance_names}
            if vet_alliances:
                parts.append(f"running with: {', '.join(sorted(vet_alliances))}")
            desc = ". ".join(parts).capitalize() + "."
            max_times = max(t.times_played or 0 for t in vet_list) if vet_list else 0
            if champs:
                name = "The Victor's Return"
            elif max_times >= 3:
                name = "Grizzled Veterans"
            elif len(vet_list) >= 4:
                name = "The Returning Class"
            else:
                name = "Experience Counts"

        else:  # kill_leaders
            killers = sorted(
                [t for t in tributes_map.values() if t.kills >= 2],
                key=lambda t: (-t.kills, -(t.training_score or 0)),
            )
            killer_names = " & ".join(t.name.split()[0] for t in killers[:3])
            total_kills = sum(t.kills for t in killers)
            parts = [f"{killer_names} have already drawn blood — these are the arena's most dangerous tributes"]
            top_killer = killers[0] if killers else None
            if top_killer:
                dr = district_records.get(top_killer.district)
                parts.append(
                    f"{top_killer.name.split()[0]} leads with {top_killer.kills} kills"
                    + (
                        f", representing District {top_killer.district} "
                        f"({dr.total_kills or 0} all-time kills from that district)"
                        if dr and dr.total_kills else ""
                    )
                )
            if len(killers) >= 2:
                parts.append(f"{total_kills} combined kills between this group")
            # Alliance context
            killer_alliances = {
                alliance_names[t.alliance_id]
                for t in killers if t.alliance_id and t.alliance_id in alliance_names
            }
            if killer_alliances:
                parts.append(f"hunting under: {', '.join(sorted(killer_alliances))}")
            desc = ". ".join(parts).capitalize() + "."
            if total_kills >= 8:
                name = "Massacre in Progress"
            elif total_kills >= 5:
                name = "Blood on Their Hands"
            else:
                name = "The Killers' Column"

        tpl = ParlayTemplate(
            name=name, description=desc, source="AUTO",
            difficulty=difficulty, phase_id=phase_id,
            active=True,
        )
        session.add(tpl)
        await session.flush()
        for i, m in enumerate(legs):
            session.add(ParlayTemplateLeg(template_id=tpl.id, market_id=m.id, sort_order=i))
        created += 1

    return created


def _bundle_markets_by_subject(open_markets, tribute_by_id):
    """Group OPEN markets by subject using structured fields (no label parsing).

    Returns ``{(kind, ident): [Market, ...]}`` where ``kind`` is ``"D"``
    (district) or ``"A"`` (alliance).  A market lands in a district bundle when
    it references a tribute from that district (via tribute_a/tribute_b) or is a
    ``DISTRICT_*`` market for it; alliance bundles come from ``ALLIANCE_*``
    markets.  A cross-district versus market lands in both districts' bundles.
    Bloodbath/global markets with no single subject are skipped, so every bundle
    stays coherent — the model can only ever build a parlay from one subject.

    Intra-district versus markets — where both tributes belong to the SAME
    district (e.g. "D4F places higher than D4M") — are excluded from that
    district's bundle, so a district-focused parlay never backs a tribute
    against their own district partner.
    """
    bundles: dict[tuple[str, int], list] = {}
    for m in open_markets:
        ta = tribute_by_id.get(m.tribute_a_id) if m.tribute_a_id else None
        tb = tribute_by_id.get(m.tribute_b_id) if m.tribute_b_id else None
        # A market pits district-mates against each other when both tributes are
        # set and share a district. Such a market only maps to that one district,
        # so we drop it from that bundle entirely.
        intra_district_versus = (
            ta is not None and tb is not None and ta.district == tb.district
        )
        keys: list[tuple[str, int]] = []
        for t in (ta, tb):
            if t is not None:
                keys.append(("D", t.district))
        if m.type.startswith("DISTRICT_") and m.placement_num is not None:
            keys.append(("D", m.placement_num))
        if m.type.startswith("ALLIANCE_") and m.placement_num is not None:
            keys.append(("A", m.placement_num))
        for key in dict.fromkeys(keys):  # dedupe, preserve order
            if intra_district_versus and key == ("D", ta.district):
                continue  # never offer partner-vs-partner legs to a district parlay
            bundles.setdefault(key, []).append(m)
    return bundles


def _tribute_payload(t) -> dict:
    return {
        "district": t.district,
        "name": t.name,
        "gender": t.display_gender,
        "age": t.age,
        "kills": t.kills,
        "training_score": t.training_score,
        "times_played": t.times_played,
        "debilitation_level": t.debilitation_level,
    }


# Theme-locked angles: each maps to the market types whose legs actually support
# that story.  A parlay's title is written from its angle, so the angle must only
# be used when the subject really has enough legs of that theme — otherwise the
# model writes e.g. a "Bloodbath" title over "Makes Final 8" legs.  We derive the
# angle from the subject's own markets (see ``_assign_theme``) and show the model
# ONLY that theme's markets, so title and legs match by construction.
_ANGLE_THEMES: dict[str, dict] = {
    "BLOODBATH": {
        "angle": "surviving the opening bloodbath and the early-arena chaos",
        "types": {
            "BLOODBATH_SURVIVOR", "DISTRICT_BOTH_BLOODBATH",
            "FIRST_BLOOD", "BLOODBATH_KILLS_OU",
        },
    },
    "FINALS": {
        "angle": "late-game endurance, grinding into the final 8 and final 5",
        "types": {
            "MAKES_FINAL_8", "MAKES_FINAL_5",
            "DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8",
            "DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5",
            "ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8",
            "ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5",
        },
    },
    "KILLS": {
        "angle": "raw aggression and stacking up kills",
        "types": {
            "TRIBUTE_KILLS", "KILLS_OU", "KILL_EVENT", "DISTRICT_KILLS_OU",
            "DISTRICT_PARTNER_KILL", "ALLIANCE_MOST_KILLS", "ALLIANCE_KILLS_OU",
        },
    },
    "TRAINING": {
        "angle": "training-score pedigree and Career-style polish",
        "types": {
            "HIGHEST_TRAINING_SCORE", "TRAINING_SCORE_OU",
            "DISTRICT_HIGHEST_SCORE", "COMBINED_DISTRICT_SCORE",
        },
    },
    "VICTORY": {
        "angle": "a deep run at the crown and a high placement ceiling",
        "types": {
            "TRIBUTE_WINS", "TRIBUTE_TOP_N", "TRIBUTE_RUNNER_UP", "TRIBUTE_PLACEMENT",
            "PLACEMENT_OU", "DISTRICT_VICTOR", "ALLIANCE_VICTOR", "ALLIANCE_RUNNER_UP",
            "PARTNER_PLACE_HIGHER", "PARTNER_SCORE_HIGHER",
        },
    },
}

# Framing angles that make no event-timing claim, so they stay coherent with
# whatever legs the model picks.  Used only when no single theme has enough
# markets to stand on its own (see ``_assign_theme``).
_FREE_ANGLES = [
    "a district's historical dynasty reasserting itself",
    "underdog defiance against long odds",
    "momentum from current form and standout tributes",
    "coordinated alliance muscle",
]

_TYPE_TO_THEME = {
    t: theme for theme, spec in _ANGLE_THEMES.items() for t in spec["types"]
}


def _assign_theme(markets, used_themes: set[str]):
    """Pick a coherent angle for one subject and the markets that support it.

    Groups the subject's (already POS-filtered) markets by narrative theme.  A
    theme qualifies only when it has at least 3 markets across at least 2 distinct
    market types — enough to build a varied 3-leg parlay from that theme alone.
    When one qualifies we lock the parlay to it: the returned markets are just
    that theme's, so every leg the model can pick matches the title it writes.
    ``used_themes`` is consulted (and updated) so different subjects in a batch
    get different themes.  If nothing qualifies we fall back to a framing angle
    that makes no event-timing claim and hand back all markets.

    Returns ``(angle_text, shown_markets)``.
    """
    buckets: dict[str, list] = {}
    for m in markets:
        theme = _TYPE_TO_THEME.get(m.type)
        if theme:
            buckets.setdefault(theme, []).append(m)

    candidates = [
        theme for theme, ms in buckets.items()
        if len(ms) >= 3 and len({m.type for m in ms}) >= 2
    ]
    if candidates:
        random.shuffle(candidates)
        # Stable sort keeps the shuffle order within each group but (1) pulls
        # not-yet-used themes ahead of already-used ones for batch variety, then
        # (2) prefers richer buckets so the model has room to build a 4-5 leg
        # parlay instead of being boxed into the 3-leg minimum by a thin theme.
        candidates.sort(key=lambda th: (th in used_themes, -len(buckets[th])))
        theme = candidates[0]
        used_themes.add(theme)
        return _ANGLE_THEMES[theme]["angle"], buckets[theme]

    return random.choice(_FREE_ANGLES), markets

# Market types whose outcome is unambiguously GOOD for the subject tribute /
# district / alliance (they advance, survive, win, out-perform).
_POS_MARKET_TYPES = {
    "TRIBUTE_WINS", "TRIBUTE_TOP_N", "TRIBUTE_RUNNER_UP", "HIGHEST_TRAINING_SCORE",
    "TRIBUTE_KILLS", "KILL_EVENT", "FIRST_BLOOD", "BLOODBATH_SURVIVOR",
    "MAKES_FINAL_8", "MAKES_FINAL_5", "PARTNER_SCORE_HIGHER", "PARTNER_PLACE_HIGHER",
    "DISTRICT_VICTOR", "DISTRICT_HIGHEST_SCORE", "DISTRICT_BOTH_BLOODBATH",
    "DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8", "DISTRICT_BOTH_FINAL_5",
    "DISTRICT_ONE_FINAL_5", "ALLIANCE_VICTOR",
    "ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8", "ALLIANCE_ALL_FINAL_5",
    "ALLIANCE_ONE_FINAL_5", "ALLIANCE_MOST_KILLS", "ALLIANCE_RUNNER_UP",
}
# Market types whose outcome is unambiguously BAD for the subject (they die,
# are eliminated, get wiped, under-perform).
_NEG_MARKET_TYPES = {
    "FIRST_TRIBUTE_TO_DIE", "LOWEST_TRAINING_SCORE", "DEATH_CAUSE",
    "TRIBUTE_KILLED_BLOODBATH", "FIRST_IN_ALLIANCE_DEATH", "MISSES_FINAL_8",
    "MISSES_FINAL_5", "PARTNER_SCORE_LOWER", "PARTNER_PLACE_LOWER",
    "FIRST_DISTRICT_WIPE", "DISTRICT_WIPED_BLOODBATH",
    "FIRST_ALLIANCE_WIPED",
}
# Over/Under types where OVER is the "doing well" side (more kills, higher score).
_OU_OVER_IS_GOOD = {"KILLS_OU", "DISTRICT_KILLS_OU", "ALLIANCE_KILLS_OU", "TRAINING_SCORE_OU"}


def _market_polarity(m, subject_district, tribute_by_id) -> str:
    """Classify a market as "POS", "NEG", or "NEUTRAL" for the subject.

    POS  = pays out when the subject does well (survives, advances, out-kills).
    NEG  = pays out when the subject does poorly (dies, is eliminated, wiped).
    NEUTRAL = game/arena props or exact-value bets with no clear direction.

    For head-to-head markets (e.g. "A kills B") the base polarity is from
    tribute_a's perspective; when the subject district is on the B side, it is
    inverted so "A kills B" reads as NEG for B's parlay.
    """
    t = m.type
    if t in _POS_MARKET_TYPES:
        base = "POS"
    elif t in _NEG_MARKET_TYPES:
        base = "NEG"
    elif t in _OU_OVER_IS_GOOD:
        base = "POS" if m.ou_side == "OVER" else "NEG" if m.ou_side == "UNDER" else "NEUTRAL"
    elif t == "PLACEMENT_OU":  # lower placement number is better
        base = "POS" if m.ou_side == "UNDER" else "NEG" if m.ou_side == "OVER" else "NEUTRAL"
    elif t == "TRIBUTE_PLACEMENT":  # exact finish; top-8 is good
        base = "POS" if (m.placement_num or 99) <= 8 else "NEG"
    else:
        base = "NEUTRAL"

    if base in ("POS", "NEG") and subject_district is not None and m.tribute_a_id and m.tribute_b_id:
        ta = tribute_by_id.get(m.tribute_a_id)
        tb = tribute_by_id.get(m.tribute_b_id)
        if ta and tb and tb.district == subject_district and ta.district != subject_district:
            base = "NEG" if base == "POS" else "POS"
    return base


def _refine_parlay_legs(market_ids, market_by_id):
    """Clean a selected leg set so the parlay is internally consistent.

    - Over/under guard: never keep both the OVER and UNDER of the same market
      type for the same tribute (e.g. "over 0.5 kills" and "under 0.5 kills"),
      which could never both win. First-selected side wins.
    - Variety cap: at most three legs of the same market type, so a parlay isn't
      an endless stack of one bet while still allowing, say, three different
      tributes to each "Survive the Bloodbath" (distinct bets, same type).

    Order is preserved. Directional consistency (no "dies"/"eliminated" legs in a
    backing parlay) is handled upstream by filtering candidates to non-NEG
    polarity, so this only tidies what's left.
    """
    kept: list[int] = []
    seen_side: dict[tuple, str] = {}
    type_counts: dict[str, int] = {}
    for mid in market_ids:
        m = market_by_id.get(mid)
        if m is None:
            continue
        if m.ou_side in ("OVER", "UNDER"):
            key = (m.type, m.tribute_a_id, m.tribute_b_id)
            prev = seen_side.get(key)
            if prev is not None and prev != m.ou_side:
                log.debug("Dropping leg %d: over/under conflict for %s", mid, key)
                continue
            seen_side.setdefault(key, m.ou_side)
        if type_counts.get(m.type, 0) >= 3:
            log.debug("Dropping leg %d: already 3 legs of type %s", mid, m.type)
            continue
        type_counts[m.type] = type_counts.get(m.type, 0) + 1
        kept.append(mid)
    return kept


# Aggregate district/alliance markets that summarise an event across a whole
# district or alliance, mapped to the individual-tribute market type covering the
# same event.  A "both/all/one make final 8" bet is entailed by (and perfectly
# correlated with) the individual "makes final 8" legs; likewise a district/
# alliance victor is guaranteed once one of its tributes is bet to win.  Pairing
# them just double-books the same outcome, so we keep the granular individual
# legs and drop the redundant aggregate.
_AGGREGATE_EVENT = {
    "DISTRICT_BOTH_FINAL_8": "MAKES_FINAL_8",
    "DISTRICT_ONE_FINAL_8": "MAKES_FINAL_8",
    "DISTRICT_BOTH_FINAL_5": "MAKES_FINAL_5",
    "DISTRICT_ONE_FINAL_5": "MAKES_FINAL_5",
    "DISTRICT_BOTH_BLOODBATH": "BLOODBATH_SURVIVOR",
    "DISTRICT_VICTOR": "TRIBUTE_WINS",
    "ALLIANCE_ALL_FINAL_8": "MAKES_FINAL_8",
    "ALLIANCE_ONE_FINAL_8": "MAKES_FINAL_8",
    "ALLIANCE_ALL_FINAL_5": "MAKES_FINAL_5",
    "ALLIANCE_ONE_FINAL_5": "MAKES_FINAL_5",
    "ALLIANCE_VICTOR": "TRIBUTE_WINS",
}
# Individual leg types that entail an aggregate above (kept in sync with the map).
_AGGREGATE_INDIVIDUAL_TYPES = set(_AGGREGATE_EVENT.values())


def _drop_redundant_aggregates(leg_ids, market_by_id, tribute_by_id):
    """Drop whole-district/alliance legs that individual-tribute legs already cover.

    If any individual leg for the same event and the same district/alliance is in
    the slip, the aggregate ("D10 Both Make Final 8", or a district victor once a
    D-tribute is bet to win) is redundant with it, so we remove the aggregate and
    keep the individual legs.  ``placement_num`` carries the district number on
    ``DISTRICT_*`` markets and the alliance id on ``ALLIANCE_*`` markets (see
    ``_bundle_markets_by_subject``).
    """
    legs = [(mid, market_by_id[mid]) for mid in leg_ids if mid in market_by_id]
    indiv_district: set[tuple[str, int]] = set()
    indiv_alliance: set[tuple[str, int]] = set()
    for _mid, m in legs:
        if m.type in _AGGREGATE_INDIVIDUAL_TYPES:
            t = tribute_by_id.get(m.tribute_a_id)
            if t is not None:
                indiv_district.add((m.type, t.district))
                if t.alliance_id is not None:
                    indiv_alliance.add((m.type, t.alliance_id))

    kept: list[int] = []
    for mid, m in legs:
        event = _AGGREGATE_EVENT.get(m.type)
        if event is not None and m.placement_num is not None:
            covered = (
                (m.type.startswith("DISTRICT_") and (event, m.placement_num) in indiv_district)
                or (m.type.startswith("ALLIANCE_") and (event, m.placement_num) in indiv_alliance)
            )
            if covered:
                log.debug("Dropping redundant aggregate leg %d (%s)", mid, m.type)
                continue
        kept.append(mid)
    return kept


def _placement_threshold(m) -> int | None:
    """Best finishing position that still wins a single-tribute placement leg.

    Returns the position cut-off (lower = harder/more specific) so nested legs can
    be compared: winning (1) implies top-5 (5) implies top-8 (8).  ``None`` for
    non-placement markets.  Only single-tribute markets qualify.
    """
    if not m.tribute_a_id or m.tribute_b_id:
        return None
    t = m.type
    if t == "TRIBUTE_WINS":
        return 1
    if t == "TRIBUTE_RUNNER_UP":
        return 2
    if t == "TRIBUTE_PLACEMENT":
        return m.placement_num or 2
    if t == "MAKES_FINAL_5":
        return 5
    if t == "MAKES_FINAL_8":
        return 8
    if t == "TRIBUTE_TOP_N":
        return m.top_n or 3
    if t == "PLACEMENT_OU" and m.ou_side == "UNDER" and m.ou_line is not None:
        return int(math.floor(m.ou_line))
    return None


def _drop_nested_placement_legs(leg_ids, market_by_id):
    """Keep at most one placement leg per tribute — the most specific one.

    A tribute can't independently "make the final 5" and "make the final 8": the
    harder finish implies the easier one, so stacking them just inflates the odds
    for no added risk (and exact-placement legs that can't co-occur make the
    parlay unwinnable).  We keep the smallest-threshold leg (best finish) per
    tribute and drop the rest; ties keep the first in leg order.
    """
    thresh: dict[int, int] = {}
    trib: dict[int, int] = {}
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None:
            continue
        th = _placement_threshold(m)
        if th is not None:
            thresh[mid] = th
            trib[mid] = m.tribute_a_id

    keeper: dict[int, int] = {}  # tribute_id -> mid to keep
    for mid in leg_ids:
        if mid not in thresh:
            continue
        tid = trib[mid]
        if tid not in keeper or thresh[mid] < thresh[keeper[tid]]:
            keeper[tid] = mid

    kept: list[int] = []
    for mid in leg_ids:
        if mid in thresh and keeper.get(trib[mid]) != mid:
            log.debug("Dropping nested placement leg %d (tribute %d)", mid, trib[mid])
            continue
        kept.append(mid)
    return kept


def _drop_nested_ou_legs(leg_ids, market_by_id):
    """Keep at most one over/under leg per tribute + market type + side.

    Two lines on the same side of the same stat nest: "over 1.5 kills" already
    implies "over 0.5 kills", so holding both just inflates the odds for no added
    risk (the lower line pays out for free once the higher one hits).  We keep the
    single dominant line per (type, tribute pair, side) group — the higher line
    for OVER, the lower for UNDER — and drop the rest; ties keep the first in leg
    order.
    """
    grp: dict[int, tuple] = {}
    line: dict[int, float] = {}
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None or m.ou_side not in ("OVER", "UNDER") or m.ou_line is None:
            continue
        grp[mid] = (m.type, m.tribute_a_id, m.tribute_b_id, m.ou_side)
        line[mid] = m.ou_line

    keeper: dict[tuple, int] = {}  # group -> mid to keep
    for mid in leg_ids:
        g = grp.get(mid)
        if g is None:
            continue
        cur = keeper.get(g)
        if cur is None:
            keeper[g] = mid
            continue
        # OVER: the higher line is the stronger, non-redundant bet; UNDER: lower.
        if (g[3] == "OVER" and line[mid] > line[cur]) or (
            g[3] == "UNDER" and line[mid] < line[cur]
        ):
            keeper[g] = mid

    kept: list[int] = []
    for mid in leg_ids:
        if mid in grp and keeper.get(grp[mid]) != mid:
            log.debug("Dropping nested over/under leg %d (group %s)", mid, grp[mid])
            continue
        kept.append(mid)
    return kept


# Markets only one tribute in the whole field can win: the first kill, the most
# kills, and the crown.  Backing two different tributes for the same one-of-field
# outcome can never all pay out, so a parlay may hold at most one leg of each.
_ONE_OF_FIELD_TYPES = {"FIRST_BLOOD", "TRIBUTE_KILLS", "TRIBUTE_WINS"}


def _drop_duplicate_one_of_field_legs(leg_ids, market_by_id):
    """Keep at most one leg per one-of-the-field market type.

    Only a single tribute can take the first kill / most kills / win, so two such
    legs make the parlay unwinnable.  For each type we keep the most-favoured leg
    (smallest American odds; ties keep the first in leg order) and drop the rest.
    """
    keeper: dict[str, int] = {}
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None or m.type not in _ONE_OF_FIELD_TYPES:
            continue
        cur = keeper.get(m.type)
        if cur is None or m.odds < market_by_id[cur].odds:
            keeper[m.type] = mid

    kept: list[int] = []
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is not None and m.type in _ONE_OF_FIELD_TYPES and keeper.get(m.type) != mid:
            log.debug("Dropping duplicate one-of-field leg %d (%s)", mid, m.type)
            continue
        kept.append(mid)
    return kept


def _kill_over_floor(m) -> int | None:
    """Minimum kill count required for a kills OVER leg to win (None otherwise)."""
    if m.ou_side != "OVER" or m.ou_line is None:
        return None
    return int(math.floor(m.ou_line)) + 1


def _drop_duplicate_district_score_legs(leg_ids, market_by_id):
    """Keep at most one "District combined score = N" leg per district.

    A district's combined training score is one exact number, so two different
    guesses for the same tribute pair can never both win. For each pair we keep
    the most-favoured leg (smallest American odds; ties keep the first in leg
    order) and drop the rest.
    """
    keeper: dict[frozenset, int] = {}
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None or m.type != "COMBINED_DISTRICT_SCORE":
            continue
        if m.tribute_a_id is None or m.tribute_b_id is None:
            continue
        pair = frozenset((m.tribute_a_id, m.tribute_b_id))
        cur = keeper.get(pair)
        if cur is None or m.odds < market_by_id[cur].odds:
            keeper[pair] = mid

    kept: list[int] = []
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if (
            m is not None
            and m.type == "COMBINED_DISTRICT_SCORE"
            and m.tribute_a_id is not None
            and m.tribute_b_id is not None
        ):
            pair = frozenset((m.tribute_a_id, m.tribute_b_id))
            if keeper.get(pair) != mid:
                log.debug("Dropping duplicate district-score leg %d (pair %s)", mid, pair)
                continue
        kept.append(mid)
    return kept


_DISTRICT_SCORE_FLOOR = 2  # lowest possible combined training score; see COMBINED_DISTRICT_SCORE creation


def _drop_district_score_floor_vs_highest_legs(leg_ids, market_by_id, tribute_by_id):
    """Drop one side when a district's rock-bottom combined-score guess (2, the
    floor of the 2-24 range) and that same district's "highest combined score"
    bet are both in the leg set — the lowest possible total can't also be the
    highest, so keep whichever leg has the better odds and drop the other.
    """
    def _district_of(m):
        if m.tribute_a_id is None:
            return None
        t = tribute_by_id.get(m.tribute_a_id)
        return t.district if t else None

    floor_by_district: dict[int, int] = {}
    highest_by_district: dict[int, int] = {}
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None:
            continue
        if m.type == "COMBINED_DISTRICT_SCORE" and m.placement_num == _DISTRICT_SCORE_FLOOR:
            d = _district_of(m)
            if d is not None:
                floor_by_district[d] = mid
        elif m.type == "DISTRICT_HIGHEST_SCORE" and m.placement_num is not None:
            highest_by_district[m.placement_num] = mid

    drop: set[int] = set()
    for d, floor_mid in floor_by_district.items():
        highest_mid = highest_by_district.get(d)
        if highest_mid is None:
            continue
        loser = (
            floor_mid if market_by_id[floor_mid].odds > market_by_id[highest_mid].odds
            else highest_mid
        )
        log.debug(
            "Dropping district-score-floor/highest conflict leg %d (district %d)", loser, d
        )
        drop.add(loser)

    if not drop:
        return leg_ids
    return [mid for mid in leg_ids if mid not in drop]


def _drop_redundant_kill_legs(leg_ids, market_by_id, tribute_by_id):
    """Drop kill legs whose outcome is already guaranteed by other legs.

    - A tribute's "over N.5 kills" leg is redundant when the slip already bets
      that tribute to kill enough named victims to clear the line (kills of X and
      Y guarantee 2 kills, so "over 1.5" adds no risk).
    - A district/alliance "total kills over N.5" leg is redundant when the summed
      guaranteed kills of its members (from their own over-lines and named-victim
      legs) already exceed the line — e.g. both D10 tributes over 1.5 forces >=4
      district kills, so "D10 total over 2.5" can never fail if they win.

    In every case the granular per-tribute legs are kept and the entailed
    aggregate/over leg is removed.
    """
    legs = [(mid, market_by_id[mid]) for mid in leg_ids if mid in market_by_id]

    # Distinct named victims per killer -> a guaranteed kill floor for that killer.
    victims: dict[int, set[int]] = {}
    for _mid, m in legs:
        if m.type == "KILL_EVENT" and m.tribute_a_id and m.tribute_b_id:
            victims.setdefault(m.tribute_a_id, set()).add(m.tribute_b_id)
    eff_floor: dict[int, int] = {tid: len(v) for tid, v in victims.items()}
    for _mid, m in legs:
        if m.type == "KILLS_OU" and m.tribute_a_id:
            of = _kill_over_floor(m)
            if of is not None:
                eff_floor[m.tribute_a_id] = max(eff_floor.get(m.tribute_a_id, 0), of)

    # Summed guaranteed kills per district / alliance (for the aggregate O/U legs).
    district_sum: dict[int, int] = {}
    alliance_sum: dict[int, int] = {}
    for tid, fl in eff_floor.items():
        t = tribute_by_id.get(tid)
        if t is None:
            continue
        district_sum[t.district] = district_sum.get(t.district, 0) + fl
        if t.alliance_id is not None:
            alliance_sum[t.alliance_id] = alliance_sum.get(t.alliance_id, 0) + fl

    drop: set[int] = set()
    for mid, m in legs:
        if m.type == "KILLS_OU" and m.ou_side == "OVER" and m.tribute_a_id:
            of = _kill_over_floor(m)
            if of is not None and len(victims.get(m.tribute_a_id, ())) >= of:
                log.debug("Dropping redundant kills-over leg %d (guaranteed by victims)", mid)
                drop.add(mid)
        elif m.ou_side == "OVER" and m.ou_line is not None and m.placement_num is not None:
            need = int(math.floor(m.ou_line)) + 1
            if m.type == "DISTRICT_KILLS_OU" and district_sum.get(m.placement_num, 0) >= need:
                log.debug("Dropping redundant district kills-over leg %d", mid)
                drop.add(mid)
            elif m.type == "ALLIANCE_KILLS_OU" and alliance_sum.get(m.placement_num, 0) >= need:
                log.debug("Dropping redundant alliance kills-over leg %d", mid)
                drop.add(mid)

    return [mid for mid in leg_ids if mid not in drop]


def _join_names(names: list[str]) -> str:
    """Join tribute names for a title: 'A', 'A & B', or 'A, B & C'."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return ", ".join(names[:-1]) + f" & {names[-1]}"


def _ensure_title_names_all_tributes(name, leg_ids, market_by_id, tribute_by_id, subject_label):
    """Rewrite a title that headlines only one of several tributes it bets on.

    When a district-scoped parlay's legs back two or more of that district's
    tributes but the model's title names just one, we expand the named tribute to
    the full list (e.g. "Torque's Blood Harvest" -> "Torque & Fizzeé's Blood
    Harvest"), keeping the same name form the model used.  Titles that name no
    tribute (purely thematic/district titles) or already name all of them are
    left untouched.  Alliance parlays keep their title — the alliance name in the
    subject already covers every member.
    """
    if not subject_label.startswith("District "):
        return name
    try:
        subj_district = int(subject_label.split()[1])
    except (IndexError, ValueError):
        return name

    involved: list = []
    seen: set[int] = set()
    for mid in leg_ids:
        m = market_by_id.get(mid)
        if m is None:
            continue
        for tid in (m.tribute_a_id, m.tribute_b_id):
            if not tid:
                continue
            t = tribute_by_id.get(tid)
            if t is not None and t.district == subj_district and t.id not in seen:
                seen.add(t.id)
                involved.append(t)
    if len(involved) < 2:
        return name

    def name_tokens(t):
        parts = [p for p in re.split(r"[\s\-]+", t.name) if len(p) >= 3]
        return parts or [t.name]

    matched_form = None  # "first" or "last" — mirror whatever form the title used
    mentioned: list = []
    for t in involved:
        parts = name_tokens(t)
        for i, tok in enumerate(parts):
            if re.search(rf"\b{re.escape(tok)}\b", name, re.IGNORECASE):
                mentioned.append(t)
                if matched_form is None:
                    matched_form = "first" if (i == 0 and len(parts) > 1) else "last"
                break
    if not mentioned or len(mentioned) == len(involved):
        return name

    def form_name(t):
        parts = name_tokens(t)
        return parts[0] if matched_form == "first" else parts[-1]

    joined = _join_names([form_name(t) for t in involved])
    for tok in name_tokens(mentioned[0]):
        hit = re.search(rf"\b{re.escape(tok)}\b", name, re.IGNORECASE)
        if hit:
            return (name[: hit.start()] + joined + name[hit.end():]).strip()
    return name


def _district_record_payload(dr) -> dict:
    return {
        "district": dr.district,
        "reputation": dr.reputation,
        "funding_level": dr.funding_level,
        "wins": dr.wins,
        "avg_placement": dr.avg_placement,
        "avg_placement_last5": dr.avg_placement_last5,
        "top8_finishes": dr.top8_finishes,
        "top5_finishes": dr.top5_finishes,
        "total_kills": dr.total_kills,
        "bloodbath_kills": dr.bloodbath_kills,
        "kill_record": dr.kill_record,
        "avg_training_score": dr.avg_training_score,
    }


async def _try_generate_ai_parlays(
    session, phase_id: int | None, count: int = 3, *, replace_existing: bool = True
) -> int:
    """Attempt AI-powered featured parlay generation via Lemonade.

    When ``replace_existing`` is True (the default, used by phase transitions),
    the current AUTO templates are cleared before the new ones are written so the
    board always reflects the latest phase.  Pass False to keep the existing AUTO
    parlays and append the new batch alongside them.  Writes new templates with
    source="AUTO".  Returns the count created, or 0 if Lemonade is unreachable,
    no lore is stored, or the model returns nothing usable.  All exceptions
    are logged and swallowed so callers can fall back to the algorithmic path.
    """
    if not _AUTO_PARLAY_GENERATION_ENABLED:
        return 0
    lore_raw = await get_setting("lemonade_district_lore")
    if not lore_raw:
        return 0

    try:
        from bot import config as _cfg
        from bot.lemonade import LemonadeClient, generate_ai_parlays_for_subjects

        client = LemonadeClient(base_url=_cfg.LEMONADE_BASE_URL, model=_cfg.LEMONADE_MODEL)
        if not await client.health():
            log.debug("Lemonade not reachable at %s; skipping AI parlays", _cfg.LEMONADE_BASE_URL)
            return 0

        lore_text: str = json.loads(lore_raw)

        mkt_rows = await session.execute(select(Market).where(Market.status == "OPEN"))
        open_markets = list(mkt_rows.scalars().all())
        if len(open_markets) < 3:
            return 0

        # Load ALL tributes: markets can reference eliminated tributes, and we
        # need every tribute's district to bundle markets by subject.
        all_tributes = list((await session.execute(select(Tribute))).scalars().all())
        tribute_by_id = {t.id: t for t in all_tributes}
        alive_by_district: dict[int, list] = {}
        for t in all_tributes:
            if t.status == "ALIVE":
                alive_by_district.setdefault(t.district, []).append(t)

        record_by_district = {
            dr.district: dr
            for dr in (await session.execute(select(DistrictRecord))).scalars().all()
        }
        alliance_by_id = {
            a.id: a for a in (await session.execute(select(Alliance))).scalars().all()
        }

        # Hard constraints in Python: bundle each market to its subject, then
        # keep only "backing" markets (drop NEG-polarity legs like dies /
        # eliminated / places-poorly) so a positively-themed parlay can never
        # include a leg that pays out when the subject fails.
        bundles = _bundle_markets_by_subject(open_markets, tribute_by_id)
        pos_bundles: dict[tuple[str, int], list] = {}
        for (kind, ident), ms in bundles.items():
            subj_district = ident if kind == "D" else None
            kept = [
                m for m in ms
                if _market_polarity(m, subj_district, tribute_by_id) != "NEG"
            ]
            if len(kept) >= 3:
                pos_bundles[(kind, ident)] = kept

        # Rank by richness, then randomise within the strong pool so different
        # subjects get featured each run instead of the same districts every time.
        eligible = sorted(
            pos_bundles.items(),
            key=lambda kv: (-len(kv[1]), sum(abs(m.odds) for m in kv[1])),
        )
        pool = eligible[: max(count * 3, count + 4)]
        random.shuffle(pool)

        tiers = ["SAFE", "BALANCED", "LONGSHOT"]
        random.shuffle(tiers)
        subjects: list[dict] = []
        # Angle is derived from each subject's own markets so the title the model
        # writes always matches its legs; used_themes spreads themes across the
        # batch so subjects don't all pitch the same story.
        used_themes: set[str] = set()
        # Over-provision subjects so a few model failures don't starve the batch.
        for idx, (key, ms) in enumerate(pool[: count + 3]):
            kind, ident = key
            angle_text, shown_markets = _assign_theme(ms, used_themes)
            if kind == "D":
                label = f"District {ident}"
                subj_tributes = alive_by_district.get(ident, [])
                recs = [record_by_district[ident]] if ident in record_by_district else []
            else:  # alliance
                a = alliance_by_id.get(ident)
                label = f"Alliance: {a.name}" if a else f"Alliance {ident}"
                subj_tributes = [
                    t for t in all_tributes
                    if t.alliance_id == ident and t.status == "ALIVE"
                ]
                member_districts = {t.district for t in subj_tributes}
                recs = [
                    record_by_district[d]
                    for d in sorted(member_districts) if d in record_by_district
                ]
            subjects.append({
                "subject_label": label,
                "lore": lore_text,
                "markets": [{"id": m.id, "label": m.label, "odds": m.odds} for m in shown_markets],
                "tributes": [_tribute_payload(t) for t in subj_tributes],
                "district_records": [_district_record_payload(dr) for dr in recs],
                "target_tier": tiers[idx % len(tiers)],
                "angle": angle_text,
            })

        if not subjects:
            log.debug("No subjects with >= 3 open markets; skipping AI parlays")
            return 0

        # Don't cap at `count` here: post-processing below can still drop a slip
        # (e.g. conflict filtering leaves it under the 3-leg minimum), so we take
        # every successful suggestion and stop once `count` have actually been
        # written.  Subjects are over-provisioned above to feed this.
        suggestions = await generate_ai_parlays_for_subjects(
            client,
            subjects,
            limit=None,
            num_ctx=_cfg.LEMONADE_CTX_SIZE,
            timeout=_cfg.LEMONADE_TIMEOUT,
        )

        if not suggestions:
            log.warning("Lemonade returned no valid parlay suggestions")
            return 0

        if replace_existing:
            old_rows = await session.execute(
                select(ParlayTemplate).where(ParlayTemplate.source == "AUTO")
            )
            for tpl in old_rows.scalars().all():
                await session.delete(tpl)
            await session.flush()

        market_odds = {m.id: m.odds for m in open_markets}
        market_by_id = {m.id: m for m in open_markets}
        created = 0
        for sug in suggestions:
            # Tidy the leg set (over/under conflicts, market-type variety cap,
            # then drop district/alliance aggregates that individual legs already
            # cover) before anything else, since it changes the legs and odds.
            leg_ids = _refine_parlay_legs(sug["market_ids"], market_by_id)
            leg_ids = _drop_nested_ou_legs(leg_ids, market_by_id)
            leg_ids = _drop_redundant_aggregates(leg_ids, market_by_id, tribute_by_id)
            leg_ids = _drop_nested_placement_legs(leg_ids, market_by_id)
            leg_ids = _drop_duplicate_one_of_field_legs(leg_ids, market_by_id)
            leg_ids = _drop_duplicate_district_score_legs(leg_ids, market_by_id)
            leg_ids = _drop_district_score_floor_vs_highest_legs(leg_ids, market_by_id, tribute_by_id)
            leg_ids = _drop_redundant_kill_legs(leg_ids, market_by_id, tribute_by_id)
            if len(leg_ids) < 3:
                log.warning(
                    "Parlay '%s' has fewer than 3 legs after conflict filtering; skipping",
                    sug.get("name"),
                )
                continue
            # Rewrite the title if it headlines only one of several tributes the
            # (now-final) legs actually back.
            final_name = _ensure_title_names_all_tributes(
                sug["name"], leg_ids, market_by_id, tribute_by_id,
                sug.get("subject_label", ""),
            )
            # Tier is assigned in code from true combined odds — the model is
            # never trusted to do the odds arithmetic.
            leg_odds = [market_odds[mid] for mid in leg_ids if mid in market_odds]
            tier = classify_parlay_tier(combined_american(leg_odds)) if leg_odds else "BALANCED"
            tpl = ParlayTemplate(
                name=final_name,
                description=sug["description"] if _AI_PARLAY_DESCRIPTIONS_ENABLED else "",
                source="AUTO",
                difficulty=tier,
                phase_id=phase_id,
                active=True,
            )
            session.add(tpl)
            await session.flush()
            for order, mid in enumerate(leg_ids):
                session.add(ParlayTemplateLeg(
                    template_id=tpl.id, market_id=mid, sort_order=order,
                ))
            created += 1
            if created >= count:
                break

        log.info("AI generated %d featured parlay(s) via Lemonade", created)
        return created

    except Exception:
        log.exception("AI parlay generation failed; caller may fall back to algorithmic")
        return 0


async def _check_parlay(session, parlay_id: int) -> list[dict]:
    """Check whether a parlay has fully resolved and settle it if so.

    Returns the notification dicts to send when the parlay finalizes
    (WON/LOST/VOIDED): the owner's own result notif, plus — if this parlay was
    created by tailing another member's board listing — a second notif so the
    original poster learns their parlay got tailed and how it turned out.
    Returns an empty list while the parlay is still pending.
    """
    parlay = await session.get(Parlay, parlay_id)
    if parlay is None or parlay.status != "PENDING":
        return []

    async def _finalize(owner_notif: dict) -> list[dict]:
        notifs = [owner_notif]
        if parlay.tailed_from_user_id is not None:
            _tur = await session.execute(
                select(User).where(
                    User.guild_id == parlay.guild_id, User.discord_id == parlay.user_id
                )
            )
            tailer = _tur.scalar_one_or_none()
            notifs.append({
                "type": "tail_notify",
                "user_id": parlay.tailed_from_user_id,
                "status": owner_notif["status"],
                "tailer_username": tailer.username if tailer else "A member",
                "parlay_id": parlay.tailed_from_parlay_id,
            })
            _pour = await session.execute(
                select(User).where(
                    User.guild_id == parlay.guild_id,
                    User.discord_id == parlay.tailed_from_user_id,
                )
            )
            poster = _pour.scalar_one_or_none()
            owner_notif["original_poster_username"] = poster.username if poster else "a member"
        return notifs

    leg_result = await session.execute(select(Bet).where(Bet.parlay_id == parlay_id))
    legs = leg_result.scalars().all()
    statuses = [leg.status for leg in legs]
    if "LOST" in statuses:
        parlay.status = "LOST"
        leg_data = []
        for leg in legs:
            mkt = await session.get(Market, leg.market_id)
            leg_data.append({
                "market_label": mkt.label if mkt else f"Market #{leg.market_id}",
                "odds": leg.odds_at_placement,
                "status": leg.status,
            })
        return await _finalize({
            "type": "parlay",
            "user_id": parlay.user_id,
            "status": "LOST",
            "wager": parlay.total_wager,
            "payout": 0,
            "legs": leg_data,
        })
    if any(s == "PENDING" for s in statuses):
        active_legs = [l for l in legs if l.status != "VOIDED"]
        if len(active_legs) < len(legs):
            parlay.total_payout = parlay_payout(
                parlay.total_wager, [l.odds_at_placement for l in active_legs]
            )
        return []
    active_legs = [l for l in legs if l.status != "VOIDED"]
    if all(l.status == "WON" for l in active_legs):
        parlay.status = "WON"
        _pur = await session.execute(
            select(User).where(User.guild_id == parlay.guild_id, User.discord_id == parlay.user_id)
        )
        user = _pur.scalar_one_or_none()
        if user:
            user.chips += parlay.total_payout
            user.total_won += parlay.total_payout
        leg_data = []
        for leg in legs:
            mkt = await session.get(Market, leg.market_id)
            leg_data.append({
                "market_label": mkt.label if mkt else f"Market #{leg.market_id}",
                "odds": leg.odds_at_placement,
                "status": leg.status,
            })
        return await _finalize({
            "type": "parlay",
            "user_id": parlay.user_id,
            "status": "WON",
            "wager": parlay.total_wager,
            "payout": parlay.total_payout,
            "legs": leg_data,
        })
    elif all(l.status == "VOIDED" for l in legs):
        parlay.status = "WON"
        _pur = await session.execute(
            select(User).where(User.guild_id == parlay.guild_id, User.discord_id == parlay.user_id)
        )
        user = _pur.scalar_one_or_none()
        if user:
            user.chips += parlay.total_wager
        leg_data = []
        for leg in legs:
            mkt = await session.get(Market, leg.market_id)
            leg_data.append({
                "market_label": mkt.label if mkt else f"Market #{leg.market_id}",
                "odds": leg.odds_at_placement,
                "status": leg.status,
            })
        return await _finalize({
            "type": "parlay",
            "user_id": parlay.user_id,
            "status": "VOIDED",
            "wager": parlay.total_wager,
            "payout": parlay.total_wager,
            "legs": leg_data,
        })
    return []


async def _unresolve_market(
    session, market: Market, reverted_parlays: set[int]
) -> dict:
    """Reverse the resolution of a market, undoing all bet payouts.

    Parlay-level reversal is deduplicated via ``reverted_parlays``; pass the
    same set across all markets being reverted in one operation so a parlay
    whose legs span multiple reverted markets is only unwound once.
    """
    bet_result = await session.execute(
        select(Bet).where(Bet.market_id == market.id, Bet.status != "PENDING")
    )
    bets = bet_result.scalars().all()
    unresolved_count = 0
    chips_reclaimed = 0

    for bet in bets:
        if bet.parlay_id is not None:
            if bet.parlay_id not in reverted_parlays:
                reverted_parlays.add(bet.parlay_id)
                parlay = await session.get(Parlay, bet.parlay_id)
                if parlay and parlay.status in ("WON", "LOST"):
                    _pur = await session.execute(
                        select(User).where(
                            User.guild_id == parlay.guild_id,
                            User.discord_id == parlay.user_id,
                        )
                    )
                    p_user = _pur.scalar_one_or_none()
                    if parlay.status == "WON":
                        leg_res = await session.execute(
                            select(Bet).where(Bet.parlay_id == bet.parlay_id)
                        )
                        all_legs = leg_res.scalars().all()
                        if all(l.status == "VOIDED" for l in all_legs):
                            if p_user:
                                p_user.chips -= parlay.total_wager
                        else:
                            if p_user:
                                p_user.chips -= parlay.total_payout
                                p_user.total_won -= parlay.total_payout
                    parlay.status = "PENDING"
            bet.status = "PENDING"
        elif bet.status == "WON":
            _ur = await session.execute(
                select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id)
            )
            user = _ur.scalar_one_or_none()
            if user:
                user.chips -= bet.payout_if_win
                user.total_won -= bet.payout_if_win
            chips_reclaimed += bet.payout_if_win
            bet.status = "PENDING"
        elif bet.status == "LOST":
            bet.status = "PENDING"
        elif bet.status == "VOIDED":
            _ur = await session.execute(
                select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id)
            )
            user = _ur.scalar_one_or_none()
            if user:
                user.chips -= bet.wager
            chips_reclaimed += bet.wager
            bet.status = "PENDING"
        unresolved_count += 1

    market.status = "OPEN"
    market.result = None
    return {"unresolved": unresolved_count, "chips_reclaimed": chips_reclaimed}


# ── Type-level bulk resolution helpers ─────────────────────────────────────────
# Every market type can be resolved by one of these helpers (or the per-market
# `/admin market resolve`), guaranteeing no market type is ever left un-settleable.


async def _resolve_markets_of_type(
    session, market_type: str, result: bool | None
) -> dict:
    """Resolve every OPEN/CLOSED market of a single type to the same outcome.

    Returns the number of markets/bets settled and chips paid out.
    """
    res = await session.execute(
        select(Market).where(
            Market.type == market_type,
            Market.status.in_(["OPEN", "CLOSED"]),
        )
    )
    markets = res.scalars().all()
    total_bets = 0
    total_credits = 0
    for m in markets:
        stats = await _resolve_market(session, m, result)
        total_bets += stats["resolved"]
        total_credits += stats["credits"]
    return {"markets": len(markets), "bets": total_bets, "credits": total_credits}


def _placement_market_result(market: Market, placement: int) -> bool:
    """Win/loss for a placement-driven market given the tribute's final placement."""
    if market.type == "TRIBUTE_PLACEMENT":
        return placement == market.placement_num
    if market.type == "TRIBUTE_TOP_N":
        return market.top_n is not None and placement <= market.top_n
    if market.type == "TRIBUTE_RUNNER_UP":
        return placement == 2
    # PLACEMENT_OU — lower placement number is a better finish
    line = market.ou_line if market.ou_line is not None else 0.5
    return placement > line if market.ou_side == "OVER" else placement <= line


# Fixed line for every tribute's Placement Over/Under market — previously this
# was computed from the field size *at the moment that tribute's markets were
# generated* (e.g. during a staggered reaping reveal), so different tributes
# ended up with different, inconsistent lines. Every tribute now gets the same
# "Places Over 12.5" / "Places Under 12.5" pair regardless of field size.
PLACEMENT_OU_LINE = 12.5

_PLACEMENT_MARKET_TYPES = (
    "TRIBUTE_PLACEMENT",
    "TRIBUTE_TOP_N",
    "PLACEMENT_OU",
    "TRIBUTE_RUNNER_UP",
)


async def _resolve_placement_markets(session) -> dict:
    """Settle all placement-based markets from each tribute's recorded placement.

    Tributes still ALIVE (placement is NULL) are left OPEN so this can be run
    mid-game without prematurely voiding undecided markets. Markets whose tribute
    no longer exists are voided.
    """
    trib_res = await session.execute(select(Tribute))
    tributes = {t.id: t for t in trib_res.scalars().all()}
    res = await session.execute(
        select(Market).where(
            Market.type.in_(_PLACEMENT_MARKET_TYPES),
            Market.status.in_(["OPEN", "CLOSED"]),
        )
    )
    resolved = 0
    skipped = 0
    for m in res.scalars().all():
        t = tributes.get(m.tribute_a_id) if m.tribute_a_id else None
        if t is None:
            await _resolve_market(session, m, None)  # tribute gone — void
            resolved += 1
        elif t.placement is None:
            skipped += 1  # still alive / undecided — leave open
        else:
            await _resolve_market(
                session, m, _placement_market_result(m, t.placement)
            )
            resolved += 1
    return {"resolved": resolved, "skipped": skipped}


async def _resolve_top_killer_markets(session) -> dict:
    """Settle TRIBUTE_KILLS (most kills) markets from final kill totals.

    The tribute(s) with the highest kill count win; ties produce multiple
    winners. If no kills were recorded at all, the markets are voided.
    """
    trib_res = await session.execute(select(Tribute))
    tributes = list(trib_res.scalars().all())
    max_kills = max((t.kills for t in tributes), default=0)
    res = await session.execute(
        select(Market).where(
            Market.type == "TRIBUTE_KILLS",
            Market.status.in_(["OPEN", "CLOSED"]),
        )
    )
    resolved = 0
    for m in res.scalars().all():
        if max_kills == 0:
            await _resolve_market(session, m, None)  # nobody killed — void
        else:
            t = next((x for x in tributes if x.id == m.tribute_a_id), None)
            await _resolve_market(session, m, bool(t and t.kills == max_kills))
        resolved += 1
    return {"resolved": resolved, "max_kills": max_kills}


async def _resolve_partner_place_markets(session) -> dict:
    """Settle PARTNER_PLACE_HIGHER / PARTNER_PLACE_LOWER markets from final placements.

    Both tributes must have a recorded placement for the market to resolve.
    Markets where either tribute is still alive are left open.
    """
    trib_res = await session.execute(select(Tribute))
    tributes = {t.id: t for t in trib_res.scalars().all()}
    res = await session.execute(
        select(Market).where(
            Market.type.in_(["PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"]),
            Market.status.in_(["OPEN", "CLOSED"]),
        )
    )
    resolved = 0
    skipped = 0
    for m in res.scalars().all():
        ta = tributes.get(m.tribute_a_id) if m.tribute_a_id else None
        tb = tributes.get(m.tribute_b_id) if m.tribute_b_id else None
        if ta is None or tb is None:
            await _resolve_market(session, m, None)
            resolved += 1
        elif ta.placement is None or tb.placement is None:
            skipped += 1
        else:
            # Lower placement number = better finish (1st beats 2nd)
            if m.type == "PARTNER_PLACE_HIGHER":
                result = ta.placement < tb.placement
            else:
                result = ta.placement > tb.placement
            await _resolve_market(session, m, result)
            resolved += 1
    return {"resolved": resolved, "skipped": skipped}


def _tribute_label(t: Tribute) -> str:
    """Compact identifier used across stats output: e.g. 'D4M Finnick'."""
    return f"D{t.district}{t.display_gender} {t.name}"


def _chunk_lines_into_fields(
    embed: discord.Embed, name: str, lines: list[str], empty: str = "—"
) -> None:
    """Add ``lines`` to ``embed`` under ``name``, splitting across multiple
    fields so no single field exceeds Discord's 1024-character limit."""
    if not lines:
        embed.add_field(name=name, value=empty, inline=False)
        return
    buf: list[str] = []
    length = 0
    first = True
    for line in lines:
        if length + len(line) + 1 > 1000:
            embed.add_field(
                name=name if first else f"{name} (cont.)",
                value="\n".join(buf),
                inline=False,
            )
            buf, length, first = [], 0, False
        buf.append(line)
        length += len(line) + 1
    if buf:
        embed.add_field(
            name=name if first else f"{name} (cont.)",
            value="\n".join(buf),
            inline=False,
        )


async def _reply(interaction: discord.Interaction, *args, **kwargs) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)
    except discord.NotFound:
        log.warning("Interaction expired before response could be sent.")


# ── Auto-market creation ──────────────────────────────────────────────────────


async def _auto_create_tribute_markets(
    session, new_tribute: Tribute, all_tributes: list[Tribute]
) -> int:
    """Creates all standard markets when a tribute is added. Returns count created."""

    # Pre-Games phase ID — stamped on training-score markets so phase-gating works
    pre_phase_result = await session.execute(
        select(BettingPhase).order_by(BettingPhase.sort_order).limit(1)
    )
    pre_phase_obj = pre_phase_result.scalars().first()
    pre_phase_id = pre_phase_obj.id if pre_phase_obj else None

    # Historical training score average for this tribute's district/gender
    dr_result = await session.execute(
        select(DistrictRecord)
        .where(DistrictRecord.district == new_tribute.district)
        .limit(1)
    )
    dr = dr_result.scalars().first()
    if dr:
        hist_avg = (
            dr.avg_training_score_male
            if new_tribute.gender == "M"
            else (
                dr.avg_training_score_female
                if new_tribute.gender == "F"
                else dr.avg_training_score
            )
        )
    else:
        hist_avg = None

    def _add(
        type_: str,
        trib_a: Tribute | None,
        trib_b: Tribute | None = None,
        cause: str | None = None,
        placement_num: int | None = None,
        top_n: int | None = None,
        ou_line: float | None = None,
        ou_side: str | None = None,
        phase_id: int | None = None,
        h_avg: float | None = None,
    ) -> int:
        odds = _compute_odds(
            type_,
            trib_a,
            all_tributes,
            trib_b=trib_b,
            placement_num=placement_num,
            top_n=top_n,
            ou_line=ou_line,
            ou_side=ou_side,
            hist_avg_score=h_avg,
        )
        if type_ == "DEATH_CAUSE":
            odds = DEFAULT_FALLBACK_ODDS
        label = _build_label(
            type_, trib_a, trib_b, cause, placement_num, top_n, ou_line, ou_side
        )
        m = Market(
            type=type_,
            label=label,
            tribute_a_id=trib_a.id if trib_a else None,
            tribute_b_id=trib_b.id if trib_b else None,
            cause=cause,
            placement_num=placement_num,
            top_n=top_n,
            ou_line=ou_line,
            ou_side=ou_side,
            phase_id=phase_id,
            odds=odds,
            status="CLOSED",
        )
        session.add(m)
        return 1

    created = 0

    # ── All-phase single-tribute markets ──────────────────────────────────────
    created += _add("TRIBUTE_WINS", new_tribute)
    created += _add("TRIBUTE_KILLS", new_tribute)
    created += _add("FIRST_BLOOD", new_tribute)
    created += _add("BLOODBATH_SURVIVOR", new_tribute)
    created += _add("TRIBUTE_KILLED_BLOODBATH", new_tribute)

    for cause in _DEATH_CAUSES:
        created += _add("DEATH_CAUSE", new_tribute, cause=cause)

    for line in [0.5, 1.5]:
        for side in ["OVER", "UNDER"]:
            created += _add("KILLS_OU", new_tribute, ou_line=line, ou_side=side)

    for side in ["OVER", "UNDER"]:
        created += _add("PLACEMENT_OU", new_tribute, ou_line=PLACEMENT_OU_LINE, ou_side=side)

    # ── Milestone markets (auto-resolve when respective phase begins) ──────────
    for mtype in (
        "MAKES_FINAL_8",
        "MISSES_FINAL_8",
        "MAKES_FINAL_5",
        "MISSES_FINAL_5",
    ):
        created += _add(mtype, new_tribute)

    # ── Pre-Games only: training score prop markets ───────────────────────────
    for guessed_score in range(1, 13):
        created += _add(
            "EXACT_TRAINING_SCORE",
            new_tribute,
            placement_num=guessed_score,
            h_avg=hist_avg,
            phase_id=pre_phase_id,
        )

    for side in ["OVER", "UNDER"]:
        created += _add(
            "TRAINING_SCORE_OU",
            new_tribute,
            ou_line=6.5,
            ou_side=side,
            h_avg=hist_avg,
            phase_id=pre_phase_id,
        )

    created += _add("HIGHEST_TRAINING_SCORE", new_tribute, phase_id=pre_phase_id, h_avg=hist_avg)
    created += _add("LOWEST_TRAINING_SCORE", new_tribute, phase_id=pre_phase_id, h_avg=hist_avg)

    # ── Kill-event pairs ──────────────────────────────────────────────────────
    others = [t for t in all_tributes if t.id != new_tribute.id]
    for other in others:
        created += _add("KILL_EVENT", new_tribute, trib_b=other)
        created += _add("KILL_EVENT", other, trib_b=new_tribute)

    # ── Combined district score + district markets (when second tribute is added)
    district_partners = [
        t
        for t in all_tributes
        if t.district == new_tribute.district and t.id != new_tribute.id
    ]
    if len(district_partners) == 1:
        partner = district_partners[0]
        for guessed_sum in range(2, 25):
            created += _add(
                "COMBINED_DISTRICT_SCORE",
                new_tribute,
                trib_b=partner,
                placement_num=guessed_sum,
                phase_id=pre_phase_id,
            )

        # District-partner comparison markets (8 total: higher/lower score/place for each)
        for mtype in ("PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"):
            created += _add(mtype, new_tribute, trib_b=partner, phase_id=pre_phase_id)
            created += _add(mtype, partner, trib_b=new_tribute, phase_id=pre_phase_id)
        for mtype in ("PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"):
            created += _add(mtype, new_tribute, trib_b=partner)
            created += _add(mtype, partner, trib_b=new_tribute)

        d = new_tribute.district

        def _add_district(
            type_: str,
            ou_line_: float | None = None,
            ou_side_: str | None = None,
            phase_id_: int | None = None,
        ) -> int:
            d_tributes = [t for t in all_tributes if t.district == d]
            odds = district_default_odds(
                type_, d_tributes, all_tributes, ou_line=ou_line_, ou_side=ou_side_
            )
            label = _build_label(type_, None, None, None, d, None, ou_line_, ou_side_)
            m = Market(
                type=type_,
                label=label,
                tribute_a_id=None,
                tribute_b_id=None,
                placement_num=d,
                ou_line=ou_line_,
                ou_side=ou_side_,
                phase_id=phase_id_,
                odds=odds,
                status="CLOSED",
            )
            session.add(m)
            return 1

        created += _add_district("DISTRICT_VICTOR")
        for line in [0.5, 1.5, 2.5]:
            for side in ["OVER", "UNDER"]:
                created += _add_district(
                    "DISTRICT_KILLS_OU", ou_line_=line, ou_side_=side
                )
        created += _add_district("DISTRICT_BOTH_BLOODBATH")
        created += _add_district("DISTRICT_WIPED_BLOODBATH")
        created += _add_district("DISTRICT_HIGHEST_SCORE", phase_id_=pre_phase_id)
        for mtype in (
            "DISTRICT_BOTH_FINAL_8",
            "DISTRICT_ONE_FINAL_8",
            "DISTRICT_BOTH_FINAL_5",
            "DISTRICT_ONE_FINAL_5",
        ):
            created += _add_district(mtype)

    return created


async def _backfill_missing_markets(session) -> dict[str, int]:
    """
    Scan all existing tributes and markets, then create any auto-generated markets
    that are absent because the market type was added after tribute creation.

    Returns a dict mapping market_type → count of markets created.

    Detection strategy: each expected market is described by a normalised key
    derived from its type and distinguishing parameters; if that key is absent
    from the set of existing market keys the market is created fresh.

    PLACEMENT_OU is keyed only by (type, tribute_id, ou_side) — not the line —
    since every tribute uses the same fixed PLACEMENT_OU_LINE. If any OVER/UNDER
    market of that type already exists for the tribute, we leave it alone.
    COMBINED_DISTRICT_SCORE is keyed by (type, min_id, max_id, guessed_sum)
    so a/b ordering in the DB doesn't matter.
    District markets are keyed by (type, district_num, ou_line, ou_side).
    """
    all_t_result = await session.execute(select(Tribute))
    all_tributes = list(all_t_result.scalars().all())

    all_m_result = await session.execute(
        select(Market).where(Market.status.in_(["OPEN", "CLOSED"]))
    )
    all_markets = list(all_m_result.scalars().all())

    dr_result = await session.execute(select(DistrictRecord))
    dr_map = {r.district: r for r in dr_result.scalars().all()}

    pre_phase_result = await session.execute(
        select(BettingPhase).order_by(BettingPhase.sort_order).limit(1)
    )
    pre_phase_obj = pre_phase_result.scalars().first()
    pre_phase_id = pre_phase_obj.id if pre_phase_obj else None

    # ── Build existing-market key sets ─────────────────────────────────────────
    # simple: (type, tribute_a_id) — single-tribute, no extra params
    existing_simple: set[tuple[str, int | None]] = set()
    # keyed: (type, ta_id [, tb_id] [, extra...]) — extra params distinguish variants
    existing_keyed: set[tuple] = set()
    # district: (type, district_num, ou_line, ou_side)
    existing_district: set[tuple] = set()

    _SIMPLE_TYPES = {
        "TRIBUTE_WINS",
        "TRIBUTE_KILLS",
        "FIRST_BLOOD",
        "BLOODBATH_SURVIVOR",
        "TRIBUTE_KILLED_BLOODBATH",
        "MAKES_FINAL_8",
        "MISSES_FINAL_8",
        "MAKES_FINAL_5",
        "MISSES_FINAL_5",
        "HIGHEST_TRAINING_SCORE",
        "LOWEST_TRAINING_SCORE",
    }

    for m in all_markets:
        ta, tb = m.tribute_a_id, m.tribute_b_id
        if m.type in _SIMPLE_TYPES:
            existing_simple.add((m.type, ta))
        elif m.type == "DEATH_CAUSE":
            existing_keyed.add(("DEATH_CAUSE", ta, m.cause))
        elif m.type == "KILLS_OU":
            existing_keyed.add(("KILLS_OU", ta, m.ou_line, m.ou_side))
        elif m.type == "PLACEMENT_OU":
            existing_keyed.add(("PLACEMENT_OU", ta, m.ou_side))
        elif m.type == "EXACT_TRAINING_SCORE":
            existing_keyed.add(("EXACT_TRAINING_SCORE", ta, m.placement_num))
        elif m.type == "TRAINING_SCORE_OU":
            existing_keyed.add(("TRAINING_SCORE_OU", ta, m.ou_line, m.ou_side))
        elif m.type == "KILL_EVENT":
            existing_keyed.add(("KILL_EVENT", ta, tb))
        elif m.type == "COMBINED_DISTRICT_SCORE":
            if ta is not None and tb is not None:
                existing_keyed.add(
                    (
                        "COMBINED_DISTRICT_SCORE",
                        min(ta, tb),
                        max(ta, tb),
                        m.placement_num,
                    )
                )
        elif m.type in (
            "PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER",
            "PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER",
        ):
            if ta is not None and tb is not None:
                existing_keyed.add((m.type, ta, tb))
        elif m.type in _DISTRICT_MARKET_TYPES:
            existing_district.add((m.type, m.placement_num, m.ou_line, m.ou_side))

    created_counts: dict[str, int] = {}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _hist_avg(tribute: Tribute) -> float | None:
        dr = dr_map.get(tribute.district)
        if dr is None:
            return None
        if tribute.gender == "M":
            return dr.avg_training_score_male
        elif tribute.gender == "F":
            return dr.avg_training_score_female
        return dr.avg_training_score

    def _make(
        type_: str,
        trib_a: Tribute | None = None,
        trib_b: Tribute | None = None,
        cause: str | None = None,
        placement_num: int | None = None,
        top_n: int | None = None,
        ou_line: float | None = None,
        ou_side: str | None = None,
        phase_id: int | None = None,
        h_avg: float | None = None,
    ) -> None:
        odds = _compute_odds(
            type_,
            trib_a,
            all_tributes,
            trib_b=trib_b,
            placement_num=placement_num,
            top_n=top_n,
            ou_line=ou_line,
            ou_side=ou_side,
            hist_avg_score=h_avg,
        )
        if type_ == "DEATH_CAUSE":
            odds = DEFAULT_FALLBACK_ODDS
        label = _build_label(
            type_, trib_a, trib_b, cause, placement_num, top_n, ou_line, ou_side
        )
        session.add(
            Market(
                type=type_,
                label=label,
                tribute_a_id=trib_a.id if trib_a else None,
                tribute_b_id=trib_b.id if trib_b else None,
                cause=cause,
                placement_num=placement_num,
                top_n=top_n,
                ou_line=ou_line,
                ou_side=ou_side,
                phase_id=phase_id,
                odds=odds,
                status="CLOSED",
            )
        )
        created_counts[type_] = created_counts.get(type_, 0) + 1

    def _make_district(
        type_: str,
        district: int,
        ou_line: float | None = None,
        ou_side: str | None = None,
        phase_id: int | None = None,
    ) -> None:
        d_tributes = [t for t in all_tributes if t.district == district]
        odds = district_default_odds(
            type_, d_tributes, all_tributes, ou_line=ou_line, ou_side=ou_side
        )
        label = _build_label(type_, None, None, None, district, None, ou_line, ou_side)
        session.add(
            Market(
                type=type_,
                label=label,
                tribute_a_id=None,
                tribute_b_id=None,
                placement_num=district,
                ou_line=ou_line,
                ou_side=ou_side,
                phase_id=phase_id,
                odds=odds,
                status="CLOSED",
            )
        )
        created_counts[type_] = created_counts.get(type_, 0) + 1

    def _make_alliance(
        alliance_id: int,
        alliance_name: str,
        type_: str,
        ou_line: float | None = None,
        ou_side: str | None = None,
    ) -> None:
        members = [t for t in all_tributes if t.alliance_id == alliance_id]
        odds = alliance_default_odds(
            type_, members, all_tributes, ou_line=ou_line, ou_side=ou_side
        )
        label = _build_label(
            type_, None, None, alliance_name, alliance_id, None, ou_line, ou_side
        )
        session.add(
            Market(
                type=type_,
                label=label,
                tribute_a_id=None,
                tribute_b_id=None,
                cause=alliance_name,
                placement_num=alliance_id,
                ou_line=ou_line,
                ou_side=ou_side,
                phase_id=None,
                odds=odds,
                status="CLOSED",
            )
        )
        created_counts[type_] = created_counts.get(type_, 0) + 1

    # ── Per-tribute markets ────────────────────────────────────────────────────
    _PREGAMES_SIMPLE = {"HIGHEST_TRAINING_SCORE", "LOWEST_TRAINING_SCORE"}
    for tribute in all_tributes:
        h_avg = _hist_avg(tribute)
        tid = tribute.id

        for mtype in _SIMPLE_TYPES:
            if (mtype, tid) not in existing_simple:
                _make(
                    mtype,
                    tribute,
                    phase_id=pre_phase_id if mtype in _PREGAMES_SIMPLE else None,
                    h_avg=h_avg if mtype in _PREGAMES_SIMPLE else None,
                )
                existing_simple.add((mtype, tid))

        for cause in _DEATH_CAUSES:
            key = ("DEATH_CAUSE", tid, cause)
            if key not in existing_keyed:
                _make("DEATH_CAUSE", tribute, cause=cause)
                existing_keyed.add(key)

        for line in [0.5, 1.5]:
            for side in ["OVER", "UNDER"]:
                key = ("KILLS_OU", tid, line, side)
                if key not in existing_keyed:
                    _make("KILLS_OU", tribute, ou_line=line, ou_side=side)
                    existing_keyed.add(key)

        for side in ["OVER", "UNDER"]:
            key = ("PLACEMENT_OU", tid, side)
            if key not in existing_keyed:
                _make("PLACEMENT_OU", tribute, ou_line=PLACEMENT_OU_LINE, ou_side=side)
                existing_keyed.add(key)

        for score in range(1, 13):
            key = ("EXACT_TRAINING_SCORE", tid, score)
            if key not in existing_keyed:
                _make(
                    "EXACT_TRAINING_SCORE",
                    tribute,
                    placement_num=score,
                    h_avg=h_avg,
                    phase_id=pre_phase_id,
                )
                existing_keyed.add(key)

        for side in ["OVER", "UNDER"]:
            key = ("TRAINING_SCORE_OU", tid, 6.5, side)
            if key not in existing_keyed:
                _make(
                    "TRAINING_SCORE_OU",
                    tribute,
                    ou_line=6.5,
                    ou_side=side,
                    h_avg=h_avg,
                    phase_id=pre_phase_id,
                )
                existing_keyed.add(key)

    # ── Kill-event pairs ───────────────────────────────────────────────────────
    for i, ta in enumerate(all_tributes):
        for tb in all_tributes[i + 1 :]:
            for killer, victim in [(ta, tb), (tb, ta)]:
                key = ("KILL_EVENT", killer.id, victim.id)
                if key not in existing_keyed:
                    _make("KILL_EVENT", killer, trib_b=victim)
                    existing_keyed.add(key)

    # ── District-pair markets ──────────────────────────────────────────────────
    seen_districts: set[int] = set()
    for tribute in all_tributes:
        d = tribute.district
        if d in seen_districts:
            continue
        members = [t for t in all_tributes if t.district == d]
        if len(members) < 2:
            continue
        seen_districts.add(d)

        ta, tb = members[0], members[1]
        min_id, max_id = min(ta.id, tb.id), max(ta.id, tb.id)

        for guessed_sum in range(2, 25):
            key = ("COMBINED_DISTRICT_SCORE", min_id, max_id, guessed_sum)
            if key not in existing_keyed:
                _make(
                    "COMBINED_DISTRICT_SCORE",
                    ta,
                    trib_b=tb,
                    placement_num=guessed_sum,
                    phase_id=pre_phase_id,
                )
                existing_keyed.add(key)

        for mtype in ("PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"):
            for subj, partner in ((ta, tb), (tb, ta)):
                key = (mtype, subj.id, partner.id)
                if key not in existing_keyed:
                    _make(mtype, subj, trib_b=partner, phase_id=pre_phase_id)
                    existing_keyed.add(key)

        for mtype in ("PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"):
            for subj, partner in ((ta, tb), (tb, ta)):
                key = (mtype, subj.id, partner.id)
                if key not in existing_keyed:
                    _make(mtype, subj, trib_b=partner)
                    existing_keyed.add(key)

        if ("DISTRICT_VICTOR", d, None, None) not in existing_district:
            _make_district("DISTRICT_VICTOR", d)
            existing_district.add(("DISTRICT_VICTOR", d, None, None))

        for line in [0.5, 1.5, 2.5]:
            for side in ["OVER", "UNDER"]:
                key = ("DISTRICT_KILLS_OU", d, line, side)
                if key not in existing_district:
                    _make_district("DISTRICT_KILLS_OU", d, ou_line=line, ou_side=side)
                    existing_district.add(key)

        if ("DISTRICT_BOTH_BLOODBATH", d, None, None) not in existing_district:
            _make_district("DISTRICT_BOTH_BLOODBATH", d)
            existing_district.add(("DISTRICT_BOTH_BLOODBATH", d, None, None))

        if ("DISTRICT_WIPED_BLOODBATH", d, None, None) not in existing_district:
            _make_district("DISTRICT_WIPED_BLOODBATH", d)
            existing_district.add(("DISTRICT_WIPED_BLOODBATH", d, None, None))

        if ("DISTRICT_HIGHEST_SCORE", d, None, None) not in existing_district:
            _make_district("DISTRICT_HIGHEST_SCORE", d, phase_id=pre_phase_id)
            existing_district.add(("DISTRICT_HIGHEST_SCORE", d, None, None))

        for mtype in (
            "DISTRICT_BOTH_FINAL_8",
            "DISTRICT_ONE_FINAL_8",
            "DISTRICT_BOTH_FINAL_5",
            "DISTRICT_ONE_FINAL_5",
        ):
            key = (mtype, d, None, None)
            if key not in existing_district:
                _make_district(mtype, d)
                existing_district.add(key)

    # ── Alliance markets (any alliance with ≥ 2 members) ────────────────────────
    # Key: (type, alliance_id, ou_line, ou_side) — cause (alliance name) not used for dedup
    existing_alliance: set[tuple] = set()
    for m in all_markets:
        if m.type in _ALLIANCE_MARKET_TYPES:
            existing_alliance.add((m.type, m.placement_num, m.ou_line, m.ou_side))

    a_result = await session.execute(select(Alliance))
    alliances = list(a_result.scalars().all())
    for alliance in alliances:
        members = [t for t in all_tributes if t.alliance_id == alliance.id]
        if len(members) < 2:
            continue
        aid = alliance.id
        aname = alliance.name

        if ("ALLIANCE_VICTOR", aid, None, None) not in existing_alliance:
            _make_alliance(aid, aname, "ALLIANCE_VICTOR")
            existing_alliance.add(("ALLIANCE_VICTOR", aid, None, None))

        for line in [3.5, 4.5, 5.5]:
            for side in ["OVER", "UNDER"]:
                key = ("ALLIANCE_KILLS_OU", aid, line, side)
                if key not in existing_alliance:
                    _make_alliance(
                        aid, aname, "ALLIANCE_KILLS_OU", ou_line=line, ou_side=side
                    )
                    existing_alliance.add(key)

        for mtype in (
            "ALLIANCE_ALL_FINAL_8",
            "ALLIANCE_ONE_FINAL_8",
            "ALLIANCE_ALL_FINAL_5",
            "ALLIANCE_ONE_FINAL_5",
        ):
            key = (mtype, aid, None, None)
            if key not in existing_alliance:
                _make_alliance(aid, aname, mtype)
                existing_alliance.add(key)

    return created_counts


async def _start_game() -> dict:
    """Shared game-start logic behind both the ``/game start`` Discord command
    and the web Activity's admin "Start Game" action, so the two stay in sync
    instead of drifting. Always begins in the lowest sort_order phase
    (Pre-Games), seeds ARENA_TYPE prop markets, backfills anything voided by a
    prior game cycle, and opens whatever the opening phase allows.

    Returns ``{"error": "already_active"}`` if a game is already running, else
    ``{"opened": int, "phase_name": str | None, "auto_parlays": int}``.
    """
    game_active_raw = await get_setting("game_active")
    if json.loads(game_active_raw or "false"):
        return {"error": "already_active"}

    async with get_session() as session:
        # Always start in the first phase (lowest sort_order = Pre-Games)
        phase_result = await session.execute(
            select(BettingPhase).order_by(BettingPhase.sort_order).limit(1)
        )
        pre_phase = phase_result.scalars().first()
        pre_phase_id = pre_phase.id if pre_phase else None
        pre_phase_name = pre_phase.name if pre_phase else None

        # Create ARENA_TYPE prop markets for this game (idempotent)
        art_row = await session.get(GameSetting, "arena_artificial_count")
        nat_row = await session.get(GameSetting, "arena_natural_count")
        art_count = int(json.loads(art_row.value)) if art_row else 0
        nat_count = int(json.loads(nat_row.value)) if nat_row else 0
        total_arena = art_count + nat_count

        from bot.odds.calculator import prob_to_american as _p2a

        for arena_cause in ("ARTIFICIAL", "NATURAL"):
            exists = await session.execute(
                select(Market).where(
                    Market.type == "ARENA_TYPE",
                    Market.cause == arena_cause,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            if not exists.scalars().first():
                if total_arena > 0:
                    count = art_count if arena_cause == "ARTIFICIAL" else nat_count
                    raw_prob = count / total_arena
                    arena_odds = _p2a(min(0.95, raw_prob * 1.05))
                else:
                    arena_odds = -110
                arena_label = f"Arena Type — {'Artificial' if arena_cause == 'ARTIFICIAL' else 'Natural'}"
                session.add(
                    Market(
                        type="ARENA_TYPE",
                        label=arena_label,
                        tribute_a_id=None,
                        cause=arena_cause,
                        phase_id=pre_phase_id,
                        odds=arena_odds,
                        status="CLOSED",
                    )
                )

        # Recreate any per-tribute markets that were resolved (voided) in a
        # prior game cycle — backfill skips resolved rows, so they're gone now.
        await _backfill_missing_markets(session)

        # Open only the markets available in the opening phase (Pre-Games:
        # District Victor + training-score/scouting props). Everything else
        # opens progressively via /game set_phase.
        allowed_types = _open_types_for_phase(pre_phase_name)
        mkt_result = await session.execute(
            select(Market).where(Market.status == "CLOSED")
        )
        opened = 0
        for m in mkt_result.scalars().all():
            if _market_should_open(m, pre_phase_id, allowed_types):
                m.status = "OPEN"
                opened += 1

        # A fresh game starts before the sponsorship window opens.
        await _set_sponsor_state(session, None)

        # Seed the tailing board with three featured parlays for this phase.
        auto_made = (
            await _try_generate_ai_parlays(session, pre_phase_id)
            or await _generate_auto_parlays(session, pre_phase_id)
        )

    await set_setting("game_active", True)
    if pre_phase_id is not None:
        await set_setting("current_phase_id", pre_phase_id)

    return {"opened": opened, "phase_name": pre_phase_name, "auto_parlays": auto_made}


async def _auto_create_alliance_markets(
    session, alliance: Alliance, all_tributes: list[Tribute]
) -> int:
    """Create all standard markets for a newly multi-member alliance. Returns count created."""
    members = [t for t in all_tributes if t.alliance_id == alliance.id]
    aid = alliance.id
    aname = alliance.name

    def _add(
        type_: str,
        ou_line: float | None = None,
        ou_side: str | None = None,
    ) -> int:
        members_here = [t for t in all_tributes if t.alliance_id == aid]
        odds = alliance_default_odds(
            type_, members_here, all_tributes, ou_line=ou_line, ou_side=ou_side
        )
        label = _build_label(type_, None, None, aname, aid, None, ou_line, ou_side)
        session.add(
            Market(
                type=type_,
                label=label,
                tribute_a_id=None,
                tribute_b_id=None,
                cause=aname,
                placement_num=aid,
                ou_line=ou_line,
                ou_side=ou_side,
                odds=odds,
                status="CLOSED",
            )
        )
        return 1

    created = 0
    created += _add("ALLIANCE_VICTOR")
    for line in [3.5, 4.5, 5.5]:
        for side in ["OVER", "UNDER"]:
            created += _add("ALLIANCE_KILLS_OU", ou_line=line, ou_side=side)
    for mtype in (
        "ALLIANCE_ALL_FINAL_8",
        "ALLIANCE_ONE_FINAL_8",
        "ALLIANCE_ALL_FINAL_5",
        "ALLIANCE_ONE_FINAL_5",
    ):
        created += _add(mtype)
    return created


# ── HistoryPageView ───────────────────────────────────────────────────────────


class HistoryPageView(discord.ui.View):
    """
    Paginated embed view for district historical records.
    One district per page with a jump-to-district select menu.
    """

    def __init__(
        self,
        records: list[DistrictRecord],
        num_games: int,
        arena_str: str,
        national_kill_record: int | None = None,
        global_bb_avg: float | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.records = records
        self.num_games = num_games
        self.arena_str = arena_str
        self.national_kill_record = national_kill_record
        self.global_bb_avg = global_bb_avg
        self.page = 0
        self.total_pages = max(1, len(records))
        self.message: discord.Message | None = None

        self.btn_first = discord.ui.Button(
            emoji="⏮", style=discord.ButtonStyle.secondary, row=0, disabled=True
        )
        self.btn_prev = discord.ui.Button(
            emoji="◀", style=discord.ButtonStyle.secondary, row=0, disabled=True
        )
        self.btn_page_label = discord.ui.Button(
            label=f"District 1 / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=True,
        )
        self.btn_next = discord.ui.Button(
            emoji="▶",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=self.total_pages <= 1,
        )
        self.btn_last = discord.ui.Button(
            emoji="⏭",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=self.total_pages <= 1,
        )

        self.btn_first.callback = self._on_first
        self.btn_prev.callback = self._on_prev
        self.btn_next.callback = self._on_next
        self.btn_last.callback = self._on_last

        for btn in (
            self.btn_first,
            self.btn_prev,
            self.btn_page_label,
            self.btn_next,
            self.btn_last,
        ):
            self.add_item(btn)

        jump_options = [
            discord.SelectOption(label=f"District {r.district}", value=str(i))
            for i, r in enumerate(records)
        ]
        if jump_options:
            self.jump_select = discord.ui.Select(
                placeholder="Jump to district…",
                options=jump_options[:25],
                row=1,
            )
            self.jump_select.callback = self._on_jump
            self.add_item(self.jump_select)

    def _f(self, val: int | float | None, decimals: int = 0) -> str:
        if val is None:
            return "—"
        return f"{val:.{decimals}f}" if decimals else str(int(val))

    def _rate(self, num: int | None) -> str:
        if num is None or not self.num_games:
            return "—"
        return f"{num / self.num_games:.2f}"

    def _wr(self, wins: int | None) -> str:
        if wins is None or not self.num_games:
            return "—"
        return f"{wins / self.num_games * 100:.1f}%"

    def _fmt_factor(self, f: float) -> str:
        sign = "+" if f >= 1 else ""
        return f"×{round(f, 3)} ({sign}{round((f - 1) * 100, 1)}%)"

    def _sync_buttons(self) -> None:
        at_first = self.page == 0
        at_last = self.page >= self.total_pages - 1
        self.btn_first.disabled = at_first
        self.btn_prev.disabled = at_first
        self.btn_next.disabled = at_last
        self.btn_last.disabled = at_last
        r = self.records[self.page] if self.records else None
        d = r.district if r else self.page + 1
        self.btn_page_label.label = f"District {d} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        if not self.records:
            embed = discord.Embed(title="District Historical Records", color=0x8B4513)
            embed.description = "No district records found."
            return embed

        r = self.records[self.page]
        win_factor = _district_historical_factor(
            r, self.records, self.num_games, for_kills=False
        )
        kill_factor = _district_historical_factor(
            r, self.records, self.num_games, for_kills=True
        )
        rep_factor = reputation_factor(r.reputation)
        fund_factor = funding_factor(r.funding_level)

        embed = discord.Embed(
            title=f"District {r.district} — Historical Records",
            color=0x8B4513,
        )
        nkr_str = (
            self._f(self.national_kill_record)
            if self.national_kill_record is not None
            else "—"
        )
        _rep_labels = {1: "Highest", 2: "High", 3: "Neutral", 4: "Low", 5: "Lowest"}
        rep_str = (
            f"{r.reputation} ({_rep_labels.get(r.reputation, '—')}, {self._fmt_factor(rep_factor)})"
            if r.reputation is not None
            else "—"
        )
        fund_str = (
            f"{funding_label(r.funding_level)} ({self._fmt_factor(fund_factor)})"
            if r.funding_level is not None
            else "—"
        )
        global_bb_str = (
            f"{self.global_bb_avg:.2f}/game" if self.global_bb_avg is not None else "—"
        )
        embed.description = (
            f"Total past Games: **{self.num_games}** | {self.arena_str}\n"
            f"**National Kill Record:** {nkr_str} | "
            f"**Global Avg Bloodbath Deaths/Game:** {global_bb_str}\n"
            f"**Reputation:** {rep_str} | **Funding:** {fund_str}\n"
            f"**Win Factor:** {self._fmt_factor(win_factor)} | "
            f"**Kill Factor:** {self._fmt_factor(kill_factor)}"
        )

        # Victors
        embed.add_field(
            name="Victors",
            value=(
                f"**Total:** {self._f(r.wins)} ({self._wr(r.wins)})\n"
                f"♂ {self._f(r.victor_male_count)}  "
                f"♀ {self._f(r.victor_female_count)}"
            ),
            inline=True,
        )

        # Runner-up
        embed.add_field(
            name="Runner-Up (2nd)",
            value=(
                f"**Total:** {self._f(r.runner_up_finishes)}\n"
                f"♂ {self._f(r.runner_up_male)}  "
                f"♀ {self._f(r.runner_up_female)}"
            ),
            inline=True,
        )

        # Arena wins (natural = total wins − manmade wins)
        _natural_wins = (
            r.wins - r.manmade_arena_wins
            if r.wins is not None and r.manmade_arena_wins is not None
            else None
        )
        _has_wins = r.wins is not None and r.wins > 0
        _art_ratio = (
            f"{r.manmade_arena_wins / r.wins * 100:.1f}%"
            if _has_wins and r.manmade_arena_wins is not None
            else "N/A"
        )
        _nat_ratio = (
            f"{_natural_wins / r.wins * 100:.1f}%"
            if _has_wins and _natural_wins is not None
            else "N/A"
        )
        embed.add_field(
            name="Arena Wins",
            value=(
                f"Manmade: **{self._f(r.manmade_arena_wins)}** ({_art_ratio})\n"
                f"Natural: **{self._f(_natural_wins)}** ({_nat_ratio})"
            ),
            inline=True,
        )

        # Placements
        embed.add_field(
            name="Placements",
            value=(
                f"**Avg:** {self._f(r.avg_placement)}\n"
                f"**Last-5 Avg:** {self._f(r.avg_placement_last5)}\n"
                f"**Top-8:** {self._f(r.top8_finishes)}  |  **Top-5:** {self._f(r.top5_finishes)}"
            ),
            inline=False,
        )

        # Kills
        embed.add_field(
            name="Kills",
            value=(
                f"**Total:** {self._f(r.total_kills)} ({self._rate(r.total_kills)}/game)\n"
                f"♂ {self._f(r.male_kills)}  "
                f"♀ {self._f(r.female_kills)}"
            ),
            inline=True,
        )

        # Bloodbath & kill record
        embed.add_field(
            name="Bloodbath / Record",
            value=(
                f"**Bloodbath Kills:** {self._f(r.bloodbath_kills)}\n"
                f"**Bloodbath Deaths:** {self._f(r.bloodbath_deaths)} ({self._rate(r.bloodbath_deaths)}/game)\n"
                f"**Kill Record:** {self._f(r.kill_record)}"
            ),
            inline=True,
        )

        # Training scores
        embed.add_field(
            name="Avg Training Score",
            value=(
                f"**Overall:** {self._f(r.avg_training_score)}\n"
                f"♂ {self._f(r.avg_training_score_male)}  "
                f"♀ {self._f(r.avg_training_score_female)}"
            ),
            inline=False,
        )

        embed.set_footer(text=f"District {r.district} of {self.total_pages}")
        return embed

    async def _safe_edit(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.NotFound:
            pass

    async def _on_first(self, interaction: discord.Interaction) -> None:
        self.page = 0
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_last(self, interaction: discord.Interaction) -> None:
        self.page = self.total_pages - 1
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_jump(self, interaction: discord.Interaction) -> None:
        self.page = int(self.jump_select.values[0])
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.NotFound:
                pass


# ── AdminCog ──────────────────────────────────────────────────────────────────


_ADMIN_PERMS = discord.Permissions(administrator=True)



class AdminCog(commands.Cog):
    admin = app_commands.Group(
        name="admin",
        description="Panem Sportsbook admin commands",
        default_permissions=_ADMIN_PERMS,
    )
    tribute = app_commands.Group(
        name="tribute",
        description="Manage tributes",
        default_permissions=_ADMIN_PERMS,
    )
    # Top-level (not under /admin) to stay within Discord's 8000-char limit on
    # the admin command tree — same pattern as the tribute/restrict groups.
    market = app_commands.Group(
        name="market",
        description="Manage markets",
        default_permissions=_ADMIN_PERMS,
    )
    market_type = app_commands.Group(
        name="market_type",
        description="Manage custom market types",
        default_permissions=_ADMIN_PERMS,
    )
    resolve = app_commands.Group(
        name="resolve",
        description="Bulk-resolve markets by type (placements, props, etc.)",
        parent=admin,
    )
    parlay = app_commands.Group(
        name="parlay", description="Manage pre-built tailable parlays", parent=admin
    )
    stats = app_commands.Group(
        name="stats", description="Live running-game statistics", parent=admin
    )
    settings = app_commands.Group(
        name="settings",
        description="Bot settings",
        default_permissions=_ADMIN_PERMS,
    )
    alliance = app_commands.Group(
        name="alliance", description="Manage tribute alliances", parent=admin
    )
    history = app_commands.Group(
        name="history",
        description="District historical records",
        default_permissions=_ADMIN_PERMS,
    )
    restrict = app_commands.Group(
        name="restrict",
        description="Manage user betting and sportsbook access restrictions",
        default_permissions=_ADMIN_PERMS,
    )
    # Top-level (not under /admin) to stay within Discord's 8000-char limit on
    # the admin command tree — same pattern as the tribute/restrict groups.
    game = app_commands.Group(
        name="game",
        description="Game control",
        default_permissions=_ADMIN_PERMS,
    )
    phase = app_commands.Group(
        name="phase",
        description="Manage betting phases",
        default_permissions=_ADMIN_PERMS,
    )
    modifier = app_commands.Group(
        name="modifier",
        description="Manage odds modifiers",
        default_permissions=_ADMIN_PERMS,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            msg = "**Access denied.** You need the Admin role to use this command."
        elif isinstance(original, ValueError):
            log.warning(f"Admin command input error: {original}")
            msg = "Invalid selection. Please pick an option from the autocomplete list."
        else:
            log.error(f"Admin command error: {error}", exc_info=error)
            msg = "An error occurred. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass

    # ── TRIBUTE COMMANDS ──────────────────────────────────────────────────────

    @tribute.command(name="add", description="Add a new tribute to the Games")
    @app_commands.describe(
        name="Tribute's name",
        district="District number (1–12)",
        gender="Gender (determines odds)",
        age="Age (12–18); older tributes open at longer odds",
        face_claim="Face claim image URL",
        face_claim_file="Upload image as face claim",
        member="Server member (sets seniority bonus)",
        sade_participant="Mark as SADE participant",
        sade_champion="Mark as SADE Champion",
        non_binary="Display as NB (odds use selected gender)",
        times_played="Number of prior Games entered (0 = rookie)",
        highest_placement="Best prior finish (2–24; lower is better)",
    )
    @app_commands.choices(
        gender=[
            app_commands.Choice(name="Male", value="M"),
            app_commands.Choice(name="Female", value="F"),
        ]
    )
    @is_admin()
    async def tribute_add(
        self,
        interaction: discord.Interaction,
        name: str,
        district: app_commands.Range[int, 1, 12],
        gender: app_commands.Choice[str],
        age: app_commands.Range[int, 12, 18],
        face_claim: str | None = None,
        face_claim_file: discord.Attachment | None = None,
        member: discord.Member | None = None,
        sade_participant: bool = False,
        sade_champion: bool = False,
        non_binary: bool = False,
        times_played: app_commands.Range[int, 0, 20] = 0,
        highest_placement: app_commands.Range[int, 2, 24] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        resolved_face_claim = face_claim_file.url if face_claim_file else face_claim
        async with get_session() as session:
            if sade_champion:
                existing_champ = await session.execute(
                    select(Tribute).where(Tribute.sade_champion == True)  # noqa: E712
                )
                if existing_champ.scalars().first() is not None:
                    await interaction.followup.send(
                        "A SADE Champion already exists. Remove the title from the current champion first.",
                        ephemeral=True,
                    )
                    return

            roster = list((await session.execute(select(Tribute))).scalars().all())
            display_gender = "NB" if non_binary else gender.value
            slot_error = _district_slot_error(roster, district, display_gender)
            if slot_error:
                await interaction.followup.send(slot_error, ephemeral=True)
                return

            tribute = Tribute(
                name=name,
                district=district,
                gender=gender.value,
                age=age,
                face_claim=resolved_face_claim,
                discord_user_id=member.id if member else None,
                member_joined_at=member.joined_at if member else None,
                sade_participant=sade_participant,
                sade_champion=sade_champion,
                non_binary=non_binary,
                times_played=times_played,
                highest_placement=highest_placement,
            )
            session.add(tribute)
            await session.flush()

            result = await session.execute(select(Tribute))
            all_tributes = result.scalars().all()
            market_count = await _auto_create_tribute_markets(
                session, tribute, all_tributes
            )
            # Adding a tribute changes the field, so every field-relative market
            # (this tribute's brand-new ones plus all existing tributes') must be
            # re-priced against the now-larger roster.
            await _recalculate_markets(session)

            # If the game is already running, open any newly-created CLOSED markets
            # that belong to the current phase (e.g. EXACT_TRAINING_SCORE during Pre-Games).
            phase_raw = await session.get(GameSetting, "current_phase_id")
            current_phase_id = json.loads(phase_raw.value) if phase_raw else None
            if current_phase_id is not None:
                current_phase = await session.get(BettingPhase, current_phase_id)
                allowed = _open_types_for_phase(current_phase.name if current_phase else None)
                closed_result = await session.execute(
                    select(Market).where(Market.status == "CLOSED")
                )
                for m in closed_result.scalars().all():
                    if _market_should_open(m, current_phase_id, allowed):
                        m.status = "OPEN"

            tid = tribute.id
            _age_row = await session.get(GameSetting, "victor_avg_age")
            _age_neutral = float(json.loads(_age_row.value)) if _age_row else float(AGE_NEUTRAL)

        embed = discord.Embed(
            title="Tribute Added",
            description=f"**{name}** (District {district}) has entered the arena.",
            color=0x4CAF50,
        )
        gender_label = {"M": "Male", "F": "Female"}.get(gender.value, gender.value)
        if non_binary:
            gender_label += " (displays as NB)"
        embed.add_field(name="Gender", value=gender_label)
        embed.add_field(name="Age", value=f"{age}  (×{age_factor(age, _age_neutral)})")
        embed.add_field(name="ID", value=str(tid))
        embed.add_field(name="Markets Created", value=str(market_count), inline=False)
        if times_played > 0:
            pexp = prior_experience_factor(times_played, highest_placement)
            place_str = f", best placement: {highest_placement}" if highest_placement else ""
            embed.add_field(
                name="Prior Experience",
                value=f"{times_played}× played{place_str}  (×{pexp:.3f})",
                inline=False,
            )
        if sade_participant:
            champ_str = f" + Champion  (×{SADE_CHAMPION_FACTOR})" if sade_champion else ""
            embed.add_field(
                name="SADE",
                value=f"Participant{champ_str}",
                inline=False,
            )
        elif sade_champion:
            embed.add_field(
                name="SADE", value=f"Champion  (×{SADE_CHAMPION_FACTOR})", inline=False
            )
        if member:
            sf = _seniority_factor(member.joined_at)
            embed.add_field(
                name="Seniority Odds Modifier", value=f"×{sf}", inline=False
            )
        if resolved_face_claim:
            embed.set_thumbnail(url=resolved_face_claim)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(name="edit", description="Edit an existing tribute")
    @app_commands.describe(
        tribute_id="Tribute to edit",
        name="New name",
        district="New district (1–12)",
        age="New age (12–18)",
        score="New training score (1–12)",
        face_claim="New face claim URL",
        face_claim_file="Upload image as new face claim",
        member="Link/update server member for seniority",
        seniority_date="Seniority join date override (YYYY-MM-DD)",
        sade_participant="Set SADE participant status",
        sade_champion="Set SADE champion status",
        times_played="Number of prior Games entered (0 = rookie)",
        highest_placement="Best prior finish (2–24; lower is better)",
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_edit(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
        name: str | None = None,
        district: app_commands.Range[int, 1, 12] | None = None,
        age: app_commands.Range[int, 12, 18] | None = None,
        score: app_commands.Range[int, 1, 12] | None = None,
        face_claim: str | None = None,
        face_claim_file: discord.Attachment | None = None,
        member: discord.Member | None = None,
        seniority_date: str | None = None,
        sade_participant: bool | None = None,
        sade_champion: bool | None = None,
        times_played: app_commands.Range[int, 0, 20] | None = None,
        highest_placement: app_commands.Range[int, 2, 24] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        parsed_date: datetime | None = None
        if seniority_date is not None:
            try:
                parsed_date = datetime.strptime(seniority_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                await interaction.followup.send(
                    "Invalid date format. Use YYYY-MM-DD (e.g. `2021-06-15`).",
                    ephemeral=True,
                )
                return

        resolved_face_claim = face_claim_file.url if face_claim_file else face_claim
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if name:
                t.name = name
            if district and district != t.district:
                roster = list(
                    (await session.execute(select(Tribute))).scalars().all()
                )
                slot_error = _district_slot_error(
                    roster, district, t.display_gender, exclude_id=t.id
                )
                if slot_error:
                    await interaction.followup.send(slot_error, ephemeral=True)
                    return
                t.district = district
            if age is not None:
                t.age = age
            if score:
                t.training_score = score
            if resolved_face_claim is not None:
                t.face_claim = resolved_face_claim
            if member is not None:
                t.discord_user_id = member.id
                t.member_joined_at = member.joined_at
            if parsed_date is not None:
                t.member_joined_at = parsed_date
            if sade_participant is not None:
                t.sade_participant = sade_participant
            if sade_champion is not None:
                if sade_champion:
                    # Clear champion flag from any previous holder
                    prev_result = await session.execute(
                        select(Tribute).where(
                            Tribute.sade_champion == True
                        )  # noqa: E712
                    )
                    for prev in prev_result.scalars().all():
                        if prev.id != t.id:
                            prev.sade_champion = False
                t.sade_champion = sade_champion
            if times_played is not None:
                t.times_played = times_played
            if highest_placement is not None:
                t.highest_placement = highest_placement
            updated_name = t.name
            seniority_factor = _seniority_factor(t.member_joined_at)
            await _recalculate_markets(session)

        sf_str = f" (seniority: ×{seniority_factor})" if t.member_joined_at else ""
        await interaction.followup.send(
            f"Tribute **{updated_name}** updated{sf_str}.", ephemeral=True
        )

    @tribute.command(
        name="set_score",
        description="Set a tribute's training score and resolve score markets",
    )
    @app_commands.describe(
        tribute_id="Tribute to update",
        score="Training score (1–12)",
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_set_score(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
        score: app_commands.Range[int, 1, 12],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _score_notifs: list[dict] = []
        _score_token = _notif_buffer.set(_score_notifs)
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                _notif_buffer.reset(_score_token)
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return

            t.training_score = score
            await session.flush()

            all_result = await session.execute(select(Tribute))
            all_tributes = list(all_result.scalars().all())
            tribute_map = {tr.id: tr for tr in all_tributes}

            markets_resolved = 0
            chips_issued = 0

            # Resolve EXACT_TRAINING_SCORE and TRAINING_SCORE_OU for this tribute
            ts_result = await session.execute(
                select(Market).where(
                    Market.tribute_a_id == t.id,
                    Market.type.in_(["EXACT_TRAINING_SCORE", "TRAINING_SCORE_OU"]),
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in ts_result.scalars().all():
                if mkt.type == "EXACT_TRAINING_SCORE":
                    result = score == mkt.placement_num
                else:
                    line = mkt.ou_line if mkt.ou_line is not None else 6.5
                    result = score > line if mkt.ou_side == "OVER" else score <= line
                stats = await _resolve_market(session, mkt, result)
                markets_resolved += 1
                chips_issued += stats.get("credits", 0)

            # Resolve COMBINED_DISTRICT_SCORE markets where both tributes now have scores
            combined_result = await session.execute(
                select(Market).where(
                    or_(Market.tribute_a_id == t.id, Market.tribute_b_id == t.id),
                    Market.type == "COMBINED_DISTRICT_SCORE",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in combined_result.scalars().all():
                ta = tribute_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                tb = tribute_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                if (
                    ta is None
                    or tb is None
                    or ta.training_score is None
                    or tb.training_score is None
                ):
                    continue  # partner score not yet set — resolve later
                result = ta.training_score + tb.training_score == mkt.placement_num
                stats = await _resolve_market(session, mkt, result)
                markets_resolved += 1
                chips_issued += stats.get("credits", 0)

            # Resolve PARTNER_SCORE_* markets where both tributes now have scores
            pscore_result = await session.execute(
                select(Market).where(
                    or_(Market.tribute_a_id == t.id, Market.tribute_b_id == t.id),
                    Market.type.in_(["PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"]),
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in pscore_result.scalars().all():
                ta = tribute_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                tb = tribute_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                if (
                    ta is None
                    or tb is None
                    or ta.training_score is None
                    or tb.training_score is None
                ):
                    continue  # partner score not yet set — resolve later
                if mkt.type == "PARTNER_SCORE_HIGHER":
                    result = ta.training_score > tb.training_score
                else:
                    result = ta.training_score < tb.training_score
                stats = await _resolve_market(session, mkt, result)
                markets_resolved += 1
                chips_issued += stats.get("credits", 0)

            # Resolve HIGHEST/LOWEST training score + DISTRICT_HIGHEST_SCORE markets
            # once every tribute who can still be scored has a score (dead,
            # never-scored tributes count as null and don't block this).
            r, c = await _resolve_score_completion_markets(session, all_tributes, tribute_map)
            markets_resolved += r
            chips_issued += c

            await _recalculate_markets(session)

        _notif_buffer.reset(_score_token)
        await _send_resolution_dms(self.bot, _score_notifs)
        embed = discord.Embed(
            title="Training Score Set",
            description=f"**{t.name}** (D{t.district}) scored **{score}** in training.",
            color=0x4CAF50,
        )
        embed.add_field(name="Markets Resolved", value=str(markets_resolved))
        if chips_issued:
            embed.add_field(name="Chips Paid Out", value=fmt_chips(chips_issued))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(name="kill", description="Mark a tribute as dead")
    @app_commands.describe(
        tribute_id="Tribute who died",
        cause="Cause of death",
        killed_by_id="Tribute who killed them (optional)",
    )
    @app_commands.autocomplete(
        tribute_id=alive_tribute_autocomplete,
        killed_by_id=alive_tribute_autocomplete,
        cause=death_cause_autocomplete,
    )
    @is_admin()
    async def tribute_kill(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
        cause: str,
        killed_by_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        # Every tribute-inflicted kill must be attributed so it is credited and
        # tied to the killer. Environmental causes may have no killer.
        if cause == "Another Tribute" and not killed_by_id:
            await interaction.followup.send(
                "⚠️ Cause **Another Tribute** requires the killer — set `killed_by_id` "
                "so the kill is credited, or pick an environmental cause "
                "(Natural Causes / Mutt / Trap/Event).",
                ephemeral=True,
            )
            return
        current_phase_raw = await get_setting("current_phase_id")
        current_phase_id = json.loads(current_phase_raw) if current_phase_raw else None

        _kill_notifs: list[dict] = []
        _kill_token = _notif_buffer.set(_kill_notifs)
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if killed_by_id and int(killed_by_id) == t.id:
                await interaction.followup.send(
                    "A tribute cannot be their own killer.", ephemeral=True
                )
                return
            # Kills scored while Bloodbath is the active phase also bump the
            # tribute's bloodbath-scoped counter, so BLOODBATH_KILLS_OU /
            # ANY_BB_DOUBLE_KILL resolve on exactly the kills that happened
            # during the Bloodbath — regardless of when the admin later
            # triggers the transition into Arena.
            in_bloodbath = False
            if current_phase_id is not None:
                cur_phase = await session.get(BettingPhase, current_phase_id)
                in_bloodbath = bool(cur_phase and cur_phase.name == "Bloodbath")
            t.status = "DEAD"
            t.death_cause = cause
            # Died before ever receiving a training score (typically a Pre-Games
            # death) — every bet made on them is voided and refunded rather than
            # resolved as a loss, since they never got a fair shot at the Games.
            void_scoreless_death = t.training_score is None
            killer_name = None
            killer_district = None
            killer_gender = None
            kill_boost_factor = 1.0
            if killed_by_id:
                t.killed_by_id = int(killed_by_id)
                killer = await session.get(Tribute, int(killed_by_id))
                if killer:
                    # Additive kill boost: quality contribution with diminishing
                    # returns past half the national kill record.
                    kill_boost_factor = await _kill_quality_for_victim(session, t)
                    nkr = await session.scalar(
                        select(func.max(DistrictRecord.kill_record))
                    )
                    dr = kill_boost_dr_factor(killer.kills, nkr)
                    killer.kill_boost = (killer.kill_boost or 0.0) + max(0.0, kill_boost_factor - 1.0) * dr
                    killer.kills += 1
                    if in_bloodbath:
                        killer.bloodbath_kills += 1
                    killer_name = killer.name
                    killer_district = killer.district
                    killer_gender = killer.display_gender
                    # Resolve any KILLS_OU markets for the killer whose line was just crossed
                    kou_result = await session.execute(
                        select(Market).where(
                            Market.tribute_a_id == killer.id,
                            Market.type == "KILLS_OU",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    for kou in kou_result.scalars().all():
                        line = kou.ou_line if kou.ou_line is not None else 0.5
                        if killer.kills > line:
                            await _resolve_market(session, kou, kou.ou_side == "OVER")
            # Placement = one more than the number of OTHER tributes still alive
            # — every survivor outlasts this tribute. Sessions here run with
            # autoflush=False, so the pending status="DEAD" write above hasn't
            # hit the DB yet; excluding this tribute's id explicitly (rather than
            # relying on the flush) keeps the first kill landing on the correct
            # last-place number instead of one past it.
            alive_result = await session.execute(
                select(Tribute).where(Tribute.status == "ALIVE", Tribute.id != t.id)
            )
            alive_remaining = len(alive_result.scalars().all())
            t.placement = alive_remaining + 1
            tribute_name = t.name
            tribute_district = t.district
            tribute_gender = t.display_gender

            # Resolve all open markets for the dead tribute
            dead_id = t.id
            killer_id = int(killed_by_id) if killed_by_id else None

            # First blood: if a tribute killed this one, check whether first blood
            # hasn't been drawn yet. If not, resolve ALL FIRST_BLOOD markets now.
            if killer_id is not None:
                fb_result = await session.execute(
                    select(Market).where(
                        Market.type == "FIRST_BLOOD",
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                fb_markets = fb_result.scalars().all()
                if fb_markets:
                    for fb in fb_markets:
                        await _resolve_market(session, fb, fb.tribute_a_id == killer_id)

            # FIRST_TRIBUTE_TO_DIE: resolve if no other tribute has died yet
            prev_deaths = await session.scalar(
                select(func.count()).select_from(Tribute).where(
                    Tribute.placement.isnot(None), Tribute.id != dead_id
                )
            )
            if prev_deaths == 0:
                ftd_result = await session.execute(
                    select(Market).where(
                        Market.type == "FIRST_TRIBUTE_TO_DIE",
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in ftd_result.scalars().all():
                    await _resolve_market(session, mkt, mkt.tribute_a_id == dead_id)

            a_mkts = await session.execute(
                select(Market).where(
                    Market.tribute_a_id == dead_id,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in a_mkts.scalars().all():
                if void_scoreless_death:
                    # Never trained/scored before dying — void every bet on them
                    # instead of resolving it, win or lose.
                    await _resolve_market(session, mkt, None)
                elif mkt.type in ("MISSES_FINAL_8", "MISSES_FINAL_5"):
                    await _resolve_market(session, mkt, True)
                elif mkt.type == "TRIBUTE_KILLED_BLOODBATH":
                    # WIN only if this death happened while Bloodbath was the
                    # active phase — the catch-all below would otherwise always
                    # resolve this LOSE, since dying is the winning condition.
                    await _resolve_market(session, mkt, in_bloodbath)
                elif mkt.type == "DEATH_CAUSE":
                    # Exactly one cause wins; all others lose.
                    await _resolve_market(session, mkt, mkt.cause == cause)
                elif mkt.type == "KILLS_OU":
                    line = mkt.ou_line if mkt.ou_line is not None else 0.5
                    if mkt.ou_side == "OVER":
                        await _resolve_market(session, mkt, t.kills > line)
                    else:  # UNDER
                        await _resolve_market(session, mkt, t.kills <= line)
                elif mkt.type in _PLACEMENT_MARKET_TYPES:
                    # Placement is final the moment a tribute dies — settle precisely
                    # (e.g. dying 8th WINS a "Top 8" or "finishes 8th" bet).
                    await _resolve_market(
                        session, mkt, _placement_market_result(mkt, t.placement)
                    )
                elif mkt.type in ("PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"):
                    # Resolve now only if the partner's placement is already known.
                    partner = await session.get(Tribute, mkt.tribute_b_id) if mkt.tribute_b_id else None
                    if partner and partner.placement is not None:
                        if mkt.type == "PARTNER_PLACE_HIGHER":
                            await _resolve_market(session, mkt, t.placement < partner.placement)
                        else:
                            await _resolve_market(session, mkt, t.placement > partner.placement)
                    # else: leave open — partner not yet placed
                elif mkt.type == "TRIBUTE_KILLS":
                    # "Most kills" is undecided until the Games end — a dead tribute
                    # can still finish as the top killer. Leave OPEN for game end.
                    continue
                else:
                    await _resolve_market(session, mkt, False)

            b_mkts = await session.execute(
                select(Market).where(
                    Market.tribute_b_id == dead_id,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in b_mkts.scalars().all():
                if void_scoreless_death:
                    await _resolve_market(session, mkt, None)
                elif mkt.type in ("PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"):
                    # tribute_b (the partner) just died — resolve if tribute_a placement is known.
                    ta_obj = await session.get(Tribute, mkt.tribute_a_id) if mkt.tribute_a_id else None
                    if ta_obj and ta_obj.placement is not None:
                        if mkt.type == "PARTNER_PLACE_HIGHER":
                            await _resolve_market(session, mkt, ta_obj.placement < t.placement)
                        else:
                            await _resolve_market(session, mkt, ta_obj.placement > t.placement)
                    # else: leave open — tribute_a not yet placed
                else:
                    result = (mkt.tribute_a_id == killer_id) if killer_id else False
                    await _resolve_market(session, mkt, result)

            # ── Early-resolve district milestone markets ───────────────────────
            t_district = t.district
            dist_mbr_res = await session.execute(
                select(Tribute).where(Tribute.district == t_district)
            )
            dist_members = dist_mbr_res.scalars().all()
            # Count alive members excluding the tribute that just died
            dist_alive_after = sum(
                1 for ti in dist_members
                if ti.id != dead_id and ti.status == "ALIVE"
            )
            # DISTRICT_BOTH_*: impossible the moment any district member dies → FALSE
            dist_both_q = await session.execute(
                select(Market).where(
                    Market.type.in_(
                        ["DISTRICT_BOTH_FINAL_8", "DISTRICT_BOTH_FINAL_5"]
                    ),
                    Market.placement_num == t_district,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in dist_both_q.scalars().all():
                await _resolve_market(session, mkt, False)
            # DISTRICT_ONE_*: impossible only once every district member is dead → FALSE
            if dist_alive_after == 0:
                dist_one_q = await session.execute(
                    select(Market).where(
                        Market.type.in_(
                            ["DISTRICT_ONE_FINAL_8", "DISTRICT_ONE_FINAL_5"]
                        ),
                        Market.placement_num == t_district,
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in dist_one_q.scalars().all():
                    await _resolve_market(session, mkt, False)

                # FIRST_DISTRICT_WIPE: first district to be completely eliminated wins
                already_wiped = await session.scalar(
                    select(func.count()).select_from(Market).where(
                        Market.type == "FIRST_DISTRICT_WIPE",
                        Market.status == "RESOLVED",
                    )
                )
                if already_wiped == 0:
                    fdw_result = await session.execute(
                        select(Market).where(
                            Market.type == "FIRST_DISTRICT_WIPE",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    for mkt in fdw_result.scalars().all():
                        await _resolve_market(session, mkt, mkt.placement_num == t_district)

            # ── Early-resolve alliance milestone markets ───────────────────────
            t_alliance_id = t.alliance_id
            if t_alliance_id is not None:
                ally_mbr_res = await session.execute(
                    select(Tribute).where(Tribute.alliance_id == t_alliance_id)
                )
                ally_members = ally_mbr_res.scalars().all()
                ally_alive_after = sum(
                    1 for ti in ally_members
                    if ti.id != dead_id and ti.status == "ALIVE"
                )
                # ALLIANCE_ALL_*: impossible the moment any alliance member dies → FALSE
                ally_all_q = await session.execute(
                    select(Market).where(
                        Market.type.in_(
                            ["ALLIANCE_ALL_FINAL_8", "ALLIANCE_ALL_FINAL_5"]
                        ),
                        Market.placement_num == t_alliance_id,
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in ally_all_q.scalars().all():
                    await _resolve_market(session, mkt, False)
                # ALLIANCE_ONE_*: impossible only once every alliance member is dead → FALSE
                if ally_alive_after == 0:
                    ally_one_q = await session.execute(
                        select(Market).where(
                            Market.type.in_(
                                ["ALLIANCE_ONE_FINAL_8", "ALLIANCE_ONE_FINAL_5"]
                            ),
                            Market.placement_num == t_alliance_id,
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    for mkt in ally_one_q.scalars().all():
                        await _resolve_market(session, mkt, False)

                # FIRST_IN_ALLIANCE_DEATH: resolve if no other member of this alliance has died
                prev_alliance_deaths = await session.scalar(
                    select(func.count()).select_from(Tribute).where(
                        Tribute.alliance_id == t_alliance_id,
                        Tribute.placement.isnot(None),
                        Tribute.id != dead_id,
                    )
                )
                if prev_alliance_deaths == 0:
                    fiad_result = await session.execute(
                        select(Market).where(
                            Market.type == "FIRST_IN_ALLIANCE_DEATH",
                            Market.placement_num == t_alliance_id,
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    for mkt in fiad_result.scalars().all():
                        await _resolve_market(session, mkt, mkt.tribute_a_id == dead_id)

                # FIRST_ALLIANCE_WIPED: first alliance to reach ≤1 surviving members wins
                if ally_alive_after <= 1:
                    already_wiped = await session.scalar(
                        select(func.count()).select_from(Market).where(
                            Market.type == "FIRST_ALLIANCE_WIPED",
                            Market.status == "RESOLVED",
                        )
                    )
                    if already_wiped == 0:
                        faw_result = await session.execute(
                            select(Market).where(
                                Market.type == "FIRST_ALLIANCE_WIPED",
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        for mkt in faw_result.scalars().all():
                            await _resolve_market(
                                session, mkt, mkt.placement_num == t_alliance_id
                            )

            # This tribute is now excluded from the training-score completion
            # check (dead + never scored counts as null) — dying may be exactly
            # what was blocking HIGHEST/LOWEST_TRAINING_SCORE / DISTRICT_HIGHEST_SCORE
            # from resolving, so re-check now rather than waiting on a future
            # /admin tribute set_score call that will never come for this tribute.
            all_result = await session.execute(select(Tribute))
            all_tributes = list(all_result.scalars().all())
            tribute_map = {tr.id: tr for tr in all_tributes}
            await _resolve_score_completion_markets(session, all_tributes, tribute_map)

            await _recalculate_markets(session)

        _notif_buffer.reset(_kill_token)
        await _send_resolution_dms(self.bot, _kill_notifs)
        killer_str = (
            f" by D{killer_district}{killer_gender} {killer_name}"
            if killer_name
            else ""
        )
        await interaction.followup.send(
            f"💀 **{tribute_name}** (D{tribute_district}{tribute_gender}) has fallen{killer_str}. Cause: {cause}"
        )

    @tribute.command(
        name="unkill",
        description="Revert a tribute's death and restore all resolved markets",
    )
    @app_commands.describe(tribute_id="Dead tribute whose kill should be reverted")
    @app_commands.autocomplete(tribute_id=dead_tribute_autocomplete)
    @is_admin()
    async def tribute_unkill(
        self, interaction: discord.Interaction, tribute_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        current_phase_raw = await get_setting("current_phase_id")
        current_phase_id = json.loads(current_phase_raw) if current_phase_raw else None
        async with get_session() as session:
            in_bloodbath = False
            if current_phase_id is not None:
                cur_phase = await session.get(BettingPhase, current_phase_id)
                in_bloodbath = bool(cur_phase and cur_phase.name == "Bloodbath")
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if t.status != "DEAD":
                await interaction.followup.send(
                    f"**{t.name}** is not dead — nothing to revert.", ephemeral=True
                )
                return

            dead_id = t.id
            killer_id = t.killed_by_id
            tribute_name = t.name
            tribute_district = t.district
            tribute_gender = t.display_gender

            killer = await session.get(Tribute, killer_id) if killer_id else None

            # ── Collect markets to revert ─────────────────────────────────────
            markets_to_revert: list[Market] = []

            # All resolved markets involving the dead tribute
            a_res = await session.execute(
                select(Market).where(
                    Market.tribute_a_id == dead_id, Market.status == "RESOLVED"
                )
            )
            markets_to_revert.extend(a_res.scalars().all())

            b_res = await session.execute(
                select(Market).where(
                    Market.tribute_b_id == dead_id, Market.status == "RESOLVED"
                )
            )
            markets_to_revert.extend(b_res.scalars().all())

            # District milestone markets resolved FALSE due to this tribute's death
            t_district = t.district
            dist_mbr_res = await session.execute(
                select(Tribute).where(Tribute.district == t_district)
            )
            dist_others = [ti for ti in dist_mbr_res.scalars().all() if ti.id != dead_id]
            other_dist_dead = sum(1 for ti in dist_others if ti.status == "DEAD")
            other_dist_alive = sum(1 for ti in dist_others if ti.status == "ALIVE")
            # BOTH markets: only revert if T was the sole dead member (after unkill all alive)
            if other_dist_dead == 0:
                dist_both_rev = await session.execute(
                    select(Market).where(
                        Market.type.in_(
                            ["DISTRICT_BOTH_FINAL_8", "DISTRICT_BOTH_FINAL_5"]
                        ),
                        Market.placement_num == t_district,
                        Market.status == "RESOLVED",
                        Market.result == False,
                    )
                )
                markets_to_revert.extend(dist_both_rev.scalars().all())
            # ONE markets: only revert if all other district members are also dead
            # (meaning the FALSE resolution was caused by everyone dying)
            if other_dist_alive == 0:
                dist_one_rev = await session.execute(
                    select(Market).where(
                        Market.type.in_(
                            ["DISTRICT_ONE_FINAL_8", "DISTRICT_ONE_FINAL_5"]
                        ),
                        Market.placement_num == t_district,
                        Market.status == "RESOLVED",
                        Market.result == False,
                    )
                )
                markets_to_revert.extend(dist_one_rev.scalars().all())

            # Alliance milestone markets resolved FALSE due to this tribute's death
            t_alliance_id = t.alliance_id
            if t_alliance_id is not None:
                ally_mbr_res = await session.execute(
                    select(Tribute).where(Tribute.alliance_id == t_alliance_id)
                )
                ally_others = [ti for ti in ally_mbr_res.scalars().all() if ti.id != dead_id]
                other_ally_dead = sum(1 for ti in ally_others if ti.status == "DEAD")
                other_ally_alive = sum(1 for ti in ally_others if ti.status == "ALIVE")
                if other_ally_dead == 0:
                    ally_all_rev = await session.execute(
                        select(Market).where(
                            Market.type.in_(
                                ["ALLIANCE_ALL_FINAL_8", "ALLIANCE_ALL_FINAL_5"]
                            ),
                            Market.placement_num == t_alliance_id,
                            Market.status == "RESOLVED",
                            Market.result == False,
                        )
                    )
                    markets_to_revert.extend(ally_all_rev.scalars().all())
                if other_ally_alive == 0:
                    ally_one_rev = await session.execute(
                        select(Market).where(
                            Market.type.in_(
                                ["ALLIANCE_ONE_FINAL_8", "ALLIANCE_ONE_FINAL_5"]
                            ),
                            Market.placement_num == t_alliance_id,
                            Market.status == "RESOLVED",
                            Market.result == False,
                        )
                    )
                    markets_to_revert.extend(ally_one_rev.scalars().all())

            if killer is not None:
                # KILLS_OU markets for the killer where THIS kill crossed the line
                kou_res = await session.execute(
                    select(Market).where(
                        Market.tribute_a_id == killer.id,
                        Market.type == "KILLS_OU",
                        Market.status == "RESOLVED",
                    )
                )
                for kou in kou_res.scalars().all():
                    line = kou.ou_line if kou.ou_line is not None else 0.5
                    if (killer.kills - 1) <= line < killer.kills:
                        markets_to_revert.append(kou)

                # FIRST_BLOOD markets — only revert if no other tribute was killed by someone
                other_killed_res = await session.execute(
                    select(Tribute).where(
                        Tribute.status == "DEAD",
                        Tribute.id != dead_id,
                        Tribute.killed_by_id.is_not(None),
                    )
                )
                if not other_killed_res.scalars().all():
                    fb_res = await session.execute(
                        select(Market).where(
                            Market.type == "FIRST_BLOOD",
                            Market.status == "RESOLVED",
                        )
                    )
                    markets_to_revert.extend(fb_res.scalars().all())

            # Deduplicate (tribute_b markets might overlap with tribute_a on kills_ou etc.)
            seen_ids: set[int] = set()
            unique_markets: list[Market] = []
            for m in markets_to_revert:
                if m.id not in seen_ids:
                    seen_ids.add(m.id)
                    unique_markets.append(m)

            # ── Revert markets and bets ───────────────────────────────────────
            reverted_parlays: set[int] = set()
            total_unresolved = 0
            for mkt in unique_markets:
                result = await _unresolve_market(session, mkt, reverted_parlays)
                total_unresolved += result["unresolved"]

            # ── Revert the tribute ────────────────────────────────────────────
            t.status = "ALIVE"
            t.death_cause = None
            t.killed_by_id = None
            t.placement = None

            # ── Revert the killer's stats ─────────────────────────────────────
            killer_str = ""
            if killer is not None:
                kill_boost_factor = await _kill_quality_for_victim(session, t)
                nkr = await session.scalar(select(func.max(DistrictRecord.kill_record)))
                # kills_before the reverted kill was killer.kills - 1
                dr = kill_boost_dr_factor(killer.kills - 1, nkr)
                contribution = max(0.0, kill_boost_factor - 1.0) * dr
                killer.kills -= 1
                if in_bloodbath and killer.bloodbath_kills > 0:
                    killer.bloodbath_kills -= 1
                killer.kill_boost = max(0.0, (killer.kill_boost or 0.0) - contribution)
                killer_str = f" (credited kill removed from D{killer.district}{killer.display_gender} {killer.name})"

            await _recalculate_markets(session)

        await interaction.followup.send(
            f"↩️ **{tribute_name}** (D{tribute_district}{tribute_gender}) has been reinstated as ALIVE."
            f"{killer_str} {total_unresolved} bet(s) across {len(unique_markets)} market(s) reverted.",
            ephemeral=True,
        )

    @tribute.command(
        name="remove", description="Remove a tribute from the Games entirely"
    )
    @app_commands.describe(tribute_id="Tribute to remove")
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_remove(
        self, interaction: discord.Interaction, tribute_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _rem_notifs: list[dict] = []
        _rem_token = _notif_buffer.set(_rem_notifs)
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                _notif_buffer.reset(_rem_token)
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            name = t.name
            mkt_result = await session.execute(
                select(Market).where(
                    Market.tribute_a_id == t.id, Market.status == "OPEN"
                )
            )
            for mkt in mkt_result.scalars().all():
                await _resolve_market(session, mkt, None)
            await session.delete(t)

        _notif_buffer.reset(_rem_token)
        await _send_resolution_dms(self.bot, _rem_notifs)
        await interaction.followup.send(
            f"Tribute **{name}** removed and all related bets voided.", ephemeral=True
        )

    @tribute.command(
        name="debilitate",
        description="Set or clear a tribute's debilitation level, affecting their odds",
    )
    @app_commands.describe(
        tribute_id="Tribute to debilitate",
        level="Debilitation severity (or Clear to remove)",
    )
    @app_commands.choices(
        level=[
            app_commands.Choice(name="Debilitated (−25%)", value="DEBILITATED"),
            app_commands.Choice(
                name="Moderately Debilitated (−45%)", value="MODERATELY_DEBILITATED"
            ),
            app_commands.Choice(
                name="Severely Debilitated (−65%)", value="SEVERELY_DEBILITATED"
            ),
            app_commands.Choice(name="Clear (remove debilitation)", value="NONE"),
        ]
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_debilitate(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
        level: app_commands.Choice[str],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            name = t.name
            district_gender = f"D{t.district}{t.display_gender}"
            new_level = None if level.value == "NONE" else level.value
            old_level = t.debilitation_level
            t.debilitation_level = new_level
            await _recalculate_markets(session)

        if new_level is None:
            msg = (
                f"Debilitation cleared for **{name}** ({district_gender}). "
                "Open odds recalculated."
            )
        else:
            label = debilitation_label(new_level)
            pct = {
                "DEBILITATED": 25,
                "MODERATELY_DEBILITATED": 45,
                "SEVERELY_DEBILITATED": 65,
            }[new_level]
            prev = (
                f" (was: {debilitation_label(old_level)})"
                if old_level and old_level != new_level
                else ""
            )
            msg = (
                f"**{name}** ({district_gender}) set to **{label}** (−{pct}% odds){prev}. "
                "Open odds recalculated."
            )
        await interaction.followup.send(msg, ephemeral=True)

    @tribute.command(name="list", description="List all tributes")
    @is_admin()
    async def tribute_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(select(Tribute).order_by(Tribute.district))
            tributes = result.scalars().all()

            alliance_map: dict[int, str] = {}
            if any(t.alliance_id for t in tributes):
                a_result = await session.execute(select(Alliance))
                alliance_map = {a.id: a.name for a in a_result.scalars().all()}

        if not tributes:
            await interaction.followup.send("No tributes found.", ephemeral=True)
            return

        embed = discord.Embed(title="All Tributes", color=0xC9A227)
        for t in tributes:
            icon = {"ALIVE": "🟢", "DEAD": "💀", "VICTOR": "👑"}.get(t.status, "")
            alliance_str = (
                f" | {alliance_map[t.alliance_id]}"
                if t.alliance_id and t.alliance_id in alliance_map
                else ""
            )
            debil_str = (
                f" | ⚠ {debilitation_label(t.debilitation_level)}"
                if t.debilitation_level
                else ""
            )
            age_str = f" | Age: {t.age}" if t.age is not None else ""
            embed.add_field(
                name=f"D{t.district}{t.display_gender} {t.name} {icon}",
                value=f"Score: {t.training_score if t.training_score is not None else '?'} | {t.display_gender}{age_str} | Kills: {t.kills}{alliance_str}{debil_str}",
                inline=True,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(name="import_template", description="Download the JSON template for mass tribute import")
    @is_admin()
    async def tribute_import_template(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        import io, os
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "tributes_template.json",
        )
        try:
            with open(template_path, "rb") as f:
                buf = io.BytesIO(f.read())
        except FileNotFoundError:
            await interaction.followup.send("Template file not found on server.", ephemeral=True)
            return
        await interaction.followup.send(
            "Fill in this template and upload it with `/tribute mass_import`.",
            file=discord.File(buf, filename="tributes_template.json"),
            ephemeral=True,
        )

    @tribute.command(name="mass_import", description="Bulk-add tributes from a JSON file")
    @app_commands.describe(file="JSON file containing tribute data (get the template with /tribute import_template)")
    @is_admin()
    async def tribute_mass_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if not file.filename.endswith(".json"):
            await interaction.followup.send("File must be a `.json` file.", ephemeral=True)
            return
        if file.size > 512_000:
            await interaction.followup.send("File is too large (max 512 KB).", ephemeral=True)
            return

        try:
            raw = await file.read()
            data = json.loads(raw)
        except Exception as exc:
            await interaction.followup.send(f"Failed to parse JSON: {exc}", ephemeral=True)
            return

        if not isinstance(data, list):
            await interaction.followup.send(
                "JSON must be a list (`[...]`) of tribute objects.", ephemeral=True
            )
            return

        errors: list[str] = []
        valid_entries: list[dict] = []
        sade_champions_in_batch = 0

        for i, entry in enumerate(data):
            row = i + 1
            if not isinstance(entry, dict):
                errors.append(f"Row {row}: not an object")
                continue

            name = entry.get("name")
            district = entry.get("district")
            gender = entry.get("gender")

            if not name or not isinstance(name, str) or not name.strip():
                errors.append(f"Row {row}: 'name' is required and must be a non-empty string")
                continue
            name = name.strip()
            if not isinstance(district, int) or not (1 <= district <= 12):
                errors.append(f"Row {row} ({name!r}): 'district' must be an integer 1–12")
                continue
            if gender not in ("M", "F"):
                errors.append(f"Row {row} ({name!r}): 'gender' must be \"M\" or \"F\"")
                continue

            age = entry.get("age")
            if age is not None and (not isinstance(age, int) or not (12 <= age <= 18)):
                errors.append(
                    f"Row {row} ({name!r}): 'age' must be an integer 12–18 or null"
                )
                continue

            score = entry.get("training_score")
            if score is not None and (not isinstance(score, int) or not (1 <= score <= 12)):
                errors.append(
                    f"Row {row} ({name!r}): 'training_score' must be an integer 1–12 or null"
                )
                continue

            face_claim = entry.get("face_claim")
            if face_claim is not None and not isinstance(face_claim, str):
                errors.append(f"Row {row} ({name!r}): 'face_claim' must be a string URL or null")
                continue

            seniority_date = entry.get("seniority_date")
            if seniority_date is not None:
                if not isinstance(seniority_date, str):
                    errors.append(f"Row {row} ({name!r}): 'seniority_date' must be a string (YYYY-MM-DD) or null")
                    continue
                try:
                    datetime.strptime(seniority_date, "%Y-%m-%d")
                except ValueError:
                    errors.append(f"Row {row} ({name!r}): 'seniority_date' must be in YYYY-MM-DD format")
                    continue

            imp_times_played = entry.get("times_played", 0)
            if not isinstance(imp_times_played, int) or imp_times_played < 0:
                errors.append(f"Row {row} ({name!r}): 'times_played' must be a non-negative integer or omitted")
                continue

            imp_highest_placement = entry.get("highest_placement")
            if imp_highest_placement is not None and (
                not isinstance(imp_highest_placement, int)
                or not (2 <= imp_highest_placement <= 24)
            ):
                errors.append(f"Row {row} ({name!r}): 'highest_placement' must be an integer 2–24 or null")
                continue

            if entry.get("sade_champion"):
                sade_champions_in_batch += 1

            valid_entries.append(entry)

        if sade_champions_in_batch > 1:
            errors.append("Batch contains more than one sade_champion — at most one is allowed.")

        if errors:
            error_text = "\n".join(errors[:20])
            if len(errors) > 20:
                error_text += f"\n… and {len(errors) - 20} more"
            await interaction.followup.send(
                f"**Validation failed.** Fix these issues and re-upload:\n```\n{error_text}\n```",
                ephemeral=True,
            )
            return

        if not valid_entries:
            await interaction.followup.send("No tribute entries found in the file.", ephemeral=True)
            return

        added_labels: list[str] = []
        total_markets = 0

        async with get_session() as session:
            if sade_champions_in_batch:
                existing_champ = await session.execute(
                    select(Tribute).where(Tribute.sade_champion == True)  # noqa: E712
                )
                if existing_champ.scalars().first() is not None:
                    await interaction.followup.send(
                        "A SADE Champion already exists. Remove the title from the current champion first.",
                        ephemeral=True,
                    )
                    return

            # District-roster validation: at most two tributes per district and no
            # repeated displayed gender, counting tributes already in the DB plus
            # everything queued in this batch. Abort wholesale before any insert.
            existing_roster = list(
                (await session.execute(select(Tribute))).scalars().all()
            )
            district_genders: dict[int, list[str]] = {}
            for t in existing_roster:
                district_genders.setdefault(t.district, []).append(t.display_gender)
            roster_errors: list[str] = []
            for entry in valid_entries:
                district = entry["district"]
                dg = "NB" if entry.get("non_binary") else entry["gender"]
                ename = entry["name"].strip()
                taken = district_genders.setdefault(district, [])
                if len(taken) >= 2:
                    roster_errors.append(
                        f"{ename!r}: district {district} would exceed two tributes."
                    )
                elif dg in taken:
                    roster_errors.append(
                        f"{ename!r}: district {district} already has a "
                        f"{_GENDER_WORD.get(dg, dg)} tribute."
                    )
                else:
                    taken.append(dg)
            if roster_errors:
                error_text = "\n".join(roster_errors[:20])
                if len(roster_errors) > 20:
                    error_text += f"\n… and {len(roster_errors) - 20} more"
                await interaction.followup.send(
                    f"**District roster validation failed.** No tributes were added:\n"
                    f"```\n{error_text}\n```",
                    ephemeral=True,
                )
                return

            for entry in valid_entries:
                name = entry["name"].strip()
                district = entry["district"]
                gender = entry["gender"]
                age = entry.get("age")
                score = entry.get("training_score")
                face_claim = entry.get("face_claim") or None
                sade_participant = bool(entry.get("sade_participant", False))
                sade_champion = bool(entry.get("sade_champion", False))
                non_binary = bool(entry.get("non_binary", False))
                raw_date = entry.get("seniority_date")
                member_joined_at = (
                    datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if raw_date
                    else None
                )
                imp_times_played = int(entry.get("times_played", 0))
                imp_highest_placement = entry.get("highest_placement") or None

                tribute = Tribute(
                    name=name,
                    district=district,
                    gender=gender,
                    age=age,
                    training_score=score,
                    face_claim=face_claim,
                    member_joined_at=member_joined_at,
                    sade_participant=sade_participant,
                    sade_champion=sade_champion,
                    non_binary=non_binary,
                    times_played=imp_times_played,
                    highest_placement=imp_highest_placement,
                )
                session.add(tribute)
                await session.flush()

                all_result = await session.execute(select(Tribute))
                all_tributes = all_result.scalars().all()
                market_count = await _auto_create_tribute_markets(session, tribute, all_tributes)
                total_markets += market_count

                label = name
                if non_binary:
                    label += " (NB)"
                added_labels.append(f"D{district}{gender} {label}")

            await _recalculate_markets(session)

            # Open any newly-created CLOSED markets that belong to the current phase.
            phase_raw = await session.get(GameSetting, "current_phase_id")
            current_phase_id = json.loads(phase_raw.value) if phase_raw else None
            if current_phase_id is not None:
                current_phase = await session.get(BettingPhase, current_phase_id)
                allowed = _open_types_for_phase(current_phase.name if current_phase else None)
                closed_result = await session.execute(
                    select(Market).where(Market.status == "CLOSED")
                )
                for m in closed_result.scalars().all():
                    if _market_should_open(m, current_phase_id, allowed):
                        m.status = "OPEN"

        lines = [f"**{len(added_labels)}** tribute(s) added, **{total_markets}** markets created."]
        if added_labels:
            bullet_block = "\n".join(f"• {lbl}" for lbl in added_labels)
            lines.append(f"\n**Added:**\n{bullet_block}")

        embed = discord.Embed(
            title="Mass Import Complete",
            description="\n".join(lines),
            color=0x4CAF50,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(
        name="score_import_template",
        description="Download the current roster as a JSON template for mass score import",
    )
    @is_admin()
    async def tribute_score_import_template(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        import io
        async with get_read_session() as session:
            result = await session.execute(
                select(Tribute).order_by(Tribute.district, Tribute.gender)
            )
            tributes = result.scalars().all()
        if not tributes:
            await interaction.followup.send(
                "No tributes exist yet — add tributes before importing scores.", ephemeral=True
            )
            return
        template = [
            {
                "tribute_id": t.id,
                "name": t.name,
                "district": t.district,
                "gender": t.display_gender,
                "score": t.training_score,
            }
            for t in tributes
        ]
        buf = io.BytesIO(json.dumps(template, indent=2).encode("utf-8"))
        await interaction.followup.send(
            "Fill in the `score` field (1–12) for each tribute and upload it with "
            "`/tribute mass_import_scores`. Leave `score` as `null` to skip a tribute.",
            file=discord.File(buf, filename="tribute_scores_template.json"),
            ephemeral=True,
        )

    @tribute.command(
        name="mass_import_scores",
        description="Bulk-set training scores from a JSON file and resolve score markets",
    )
    @app_commands.describe(
        file="JSON file containing tribute scores (get the template with /tribute score_import_template)"
    )
    @is_admin()
    async def tribute_mass_import_scores(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if not file.filename.endswith(".json"):
            await interaction.followup.send("File must be a `.json` file.", ephemeral=True)
            return
        if file.size > 512_000:
            await interaction.followup.send("File is too large (max 512 KB).", ephemeral=True)
            return

        try:
            raw = await file.read()
            data = json.loads(raw)
        except Exception as exc:
            await interaction.followup.send(f"Failed to parse JSON: {exc}", ephemeral=True)
            return

        if not isinstance(data, list):
            await interaction.followup.send(
                "JSON must be a list (`[...]`) of tribute score objects.", ephemeral=True
            )
            return

        errors: list[str] = []
        updates: dict[int, int] = {}
        seen_ids: set[int] = set()

        for i, entry in enumerate(data):
            row = i + 1
            if not isinstance(entry, dict):
                errors.append(f"Row {row}: not an object")
                continue

            tribute_id = entry.get("tribute_id")
            if not isinstance(tribute_id, int) or isinstance(tribute_id, bool):
                errors.append(f"Row {row}: 'tribute_id' is required and must be an integer")
                continue

            score = entry.get("score")
            if score is None:
                continue  # unfilled row — skip silently, not an error

            if not isinstance(score, int) or isinstance(score, bool) or not (1 <= score <= 12):
                errors.append(
                    f"Row {row} (tribute #{tribute_id}): 'score' must be an integer 1–12 or null"
                )
                continue

            if tribute_id in seen_ids:
                errors.append(f"Row {row}: tribute #{tribute_id} appears more than once in this file")
                continue
            seen_ids.add(tribute_id)
            updates[tribute_id] = score

        if errors:
            error_text = "\n".join(errors[:20])
            if len(errors) > 20:
                error_text += f"\n… and {len(errors) - 20} more"
            await interaction.followup.send(
                f"**Validation failed.** Fix these issues and re-upload:\n```\n{error_text}\n```",
                ephemeral=True,
            )
            return

        if not updates:
            await interaction.followup.send(
                "No scores to import — every row was null or the file was empty.", ephemeral=True
            )
            return

        async with _collect_notifications() as notifs:
            async with get_session() as session:
                all_result = await session.execute(select(Tribute))
                all_tributes = list(all_result.scalars().all())
                tribute_map = {t.id: t for t in all_tributes}

                missing = [tid for tid in updates if tid not in tribute_map]
                if missing:
                    await interaction.followup.send(
                        f"Tribute ID(s) not found: {', '.join(str(m) for m in missing)}",
                        ephemeral=True,
                    )
                    return

                scored_names: list[str] = []
                for tid, score in updates.items():
                    t = tribute_map[tid]
                    t.training_score = score
                    scored_names.append(f"D{t.district}{t.gender} {t.name}: {score}")
                await session.flush()

                markets_resolved = 0
                chips_issued = 0
                updated_ids = set(updates.keys())

                # Resolve EXACT_TRAINING_SCORE and TRAINING_SCORE_OU for each updated tribute
                ts_result = await session.execute(
                    select(Market).where(
                        Market.tribute_a_id.in_(updated_ids),
                        Market.type.in_(["EXACT_TRAINING_SCORE", "TRAINING_SCORE_OU"]),
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in ts_result.scalars().all():
                    trib = tribute_map[mkt.tribute_a_id]
                    if mkt.type == "EXACT_TRAINING_SCORE":
                        result = trib.training_score == mkt.placement_num
                    else:
                        line = mkt.ou_line if mkt.ou_line is not None else 6.5
                        result = (
                            trib.training_score > line
                            if mkt.ou_side == "OVER"
                            else trib.training_score <= line
                        )
                    stats = await _resolve_market(session, mkt, result)
                    markets_resolved += 1
                    chips_issued += stats.get("credits", 0)

                # Resolve COMBINED_DISTRICT_SCORE markets where both tributes now have scores
                combined_result = await session.execute(
                    select(Market).where(
                        or_(
                            Market.tribute_a_id.in_(updated_ids),
                            Market.tribute_b_id.in_(updated_ids),
                        ),
                        Market.type == "COMBINED_DISTRICT_SCORE",
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in combined_result.scalars().all():
                    ta = tribute_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                    tb = tribute_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                    if (
                        ta is None
                        or tb is None
                        or ta.training_score is None
                        or tb.training_score is None
                    ):
                        continue  # partner score not yet set — resolve later
                    result = ta.training_score + tb.training_score == mkt.placement_num
                    stats = await _resolve_market(session, mkt, result)
                    markets_resolved += 1
                    chips_issued += stats.get("credits", 0)

                # Resolve PARTNER_SCORE_* markets where both tributes now have scores
                pscore_result = await session.execute(
                    select(Market).where(
                        or_(
                            Market.tribute_a_id.in_(updated_ids),
                            Market.tribute_b_id.in_(updated_ids),
                        ),
                        Market.type.in_(["PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"]),
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in pscore_result.scalars().all():
                    ta = tribute_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                    tb = tribute_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                    if (
                        ta is None
                        or tb is None
                        or ta.training_score is None
                        or tb.training_score is None
                    ):
                        continue  # partner score not yet set — resolve later
                    if mkt.type == "PARTNER_SCORE_HIGHER":
                        result = ta.training_score > tb.training_score
                    else:
                        result = ta.training_score < tb.training_score
                    stats = await _resolve_market(session, mkt, result)
                    markets_resolved += 1
                    chips_issued += stats.get("credits", 0)

                # Resolve HIGHEST/LOWEST training score + DISTRICT_HIGHEST_SCORE markets
                # once every tribute who can still be scored has a score (dead,
                # never-scored tributes count as null and don't block this).
                r, c = await _resolve_score_completion_markets(session, all_tributes, tribute_map)
                markets_resolved += r
                chips_issued += c

                await _recalculate_markets(session)

        await _send_resolution_dms(self.bot, notifs)

        lines = [
            f"**{len(scored_names)}** tribute(s) scored, **{markets_resolved}** market(s) resolved."
        ]
        if chips_issued:
            lines.append(f"**{fmt_chips(chips_issued)}** in chips paid out.")
        if scored_names:
            bullet_block = "\n".join(f"• {lbl}" for lbl in scored_names)
            lines.append(f"\n**Scores:**\n{bullet_block}")

        embed = discord.Embed(
            title="Mass Score Import Complete",
            description="\n".join(lines),
            color=0x4CAF50,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── MARKET COMMANDS ───────────────────────────────────────────────────────

    @market.command(name="add", description="Add a new betting market")
    @app_commands.describe(
        market_type="Market type",
        tribute_a_id="Primary tribute",
        district_num="District 1–12 (district markets)",
        alliance_id="Alliance (alliance markets)",
        tribute_b_id="Second tribute (kill events)",
        cause="Death cause / label",
        placement_num="Exact placement",
        top_n="Top-N count",
        ou_line="O/U line (e.g. 1.5)",
        ou_side="Over or Under",
        phase_id="Betting phase",
    )
    @app_commands.choices(
        ou_side=[
            app_commands.Choice(name="Over", value="OVER"),
            app_commands.Choice(name="Under", value="UNDER"),
        ],
    )
    @app_commands.autocomplete(
        market_type=market_type_autocomplete,
        tribute_a_id=tribute_autocomplete,
        tribute_b_id=tribute_autocomplete,
        alliance_id=alliance_autocomplete,
        phase_id=phase_autocomplete,
    )
    @is_admin()
    async def market_add(
        self,
        interaction: discord.Interaction,
        market_type: str,
        tribute_a_id: str | None = None,
        district_num: app_commands.Range[int, 1, 12] | None = None,
        alliance_id: str | None = None,
        tribute_b_id: str | None = None,
        cause: str | None = None,
        placement_num: app_commands.Range[int, 1, 24] | None = None,
        top_n: app_commands.Range[int, 2, 23] | None = None,
        ou_line: float | None = None,
        ou_side: app_commands.Choice[str] | None = None,
        phase_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if (
            not market_type.startswith("CUSTOM_")
            and market_type not in BUILT_IN_TYPE_VALUES
        ):
            await interaction.followup.send(
                "Unknown market type. Please select from the autocomplete suggestions.",
                ephemeral=True,
            )
            return

        is_district_market = market_type in _DISTRICT_MARKET_TYPES
        is_alliance_market = market_type in _ALLIANCE_MARKET_TYPES
        is_game_prop = market_type in _GAME_PROP_TYPES

        async with get_session() as session:
            pid = int(phase_id) if phase_id else None
            phase_name: str | None = None
            if pid:
                phase_obj = await session.get(BettingPhase, pid)
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return
                phase_name = phase_obj.name

            side_val = ou_side.value if ou_side else None

            if is_alliance_market:
                if alliance_id is None:
                    await interaction.followup.send(
                        "Alliance markets require an `alliance_id`.", ephemeral=True
                    )
                    return
                alliance_obj = await session.get(Alliance, int(alliance_id))
                if not alliance_obj:
                    await interaction.followup.send(
                        "Alliance not found.", ephemeral=True
                    )
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = list(all_t_result.scalars().all())
                members = [t for t in all_tributes if t.alliance_id == alliance_obj.id]
                odds = alliance_default_odds(
                    market_type,
                    members,
                    all_tributes,
                    ou_line=ou_line,
                    ou_side=side_val,
                    top_n=top_n,
                )
                label = _build_label(
                    market_type,
                    None,
                    None,
                    alliance_obj.name,
                    alliance_obj.id,
                    top_n,
                    ou_line,
                    side_val,
                )
                mkt = Market(
                    type=market_type,
                    label=label,
                    tribute_a_id=None,
                    tribute_b_id=None,
                    cause=alliance_obj.name,
                    placement_num=alliance_obj.id,
                    top_n=top_n,
                    ou_line=ou_line,
                    ou_side=side_val,
                    phase_id=pid,
                    odds=odds,
                )

            elif is_district_market:
                if district_num is None:
                    await interaction.followup.send(
                        "District markets require a `district_num` (1–12).",
                        ephemeral=True,
                    )
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = list(all_t_result.scalars().all())
                d_tributes = [t for t in all_tributes if t.district == district_num]
                odds = district_default_odds(
                    market_type,
                    d_tributes,
                    all_tributes,
                    ou_line=ou_line,
                    ou_side=side_val,
                )
                label = _build_label(
                    market_type, None, None, None, district_num, None, ou_line, side_val
                )
                mkt = Market(
                    type=market_type,
                    label=label,
                    tribute_a_id=None,
                    tribute_b_id=None,
                    placement_num=district_num,
                    ou_line=ou_line,
                    ou_side=side_val,
                    phase_id=pid,
                    odds=odds,
                )

            elif is_game_prop:
                all_t_result = await session.execute(select(Tribute))
                all_tributes = list(all_t_result.scalars().all())
                odds = game_default_odds(
                    market_type,
                    all_tributes,
                    placement_num=placement_num,
                    ou_line=ou_line,
                    ou_side=side_val,
                )
                label = _build_label(
                    market_type, None, None, None, placement_num, None, ou_line, side_val
                )
                mkt = Market(
                    type=market_type,
                    label=label,
                    tribute_a_id=None,
                    tribute_b_id=None,
                    placement_num=placement_num,
                    ou_line=ou_line,
                    ou_side=side_val,
                    phase_id=pid,
                    odds=odds,
                )

            elif market_type.startswith("CUSTOM_"):
                if tribute_a_id is None:
                    await interaction.followup.send(
                        "Custom markets require a primary tribute.", ephemeral=True
                    )
                    return
                trib_a = await session.get(Tribute, int(tribute_a_id))
                if not trib_a:
                    await interaction.followup.send(
                        "Primary tribute not found.", ephemeral=True
                    )
                    return
                trib_b = (
                    await session.get(Tribute, int(tribute_b_id))
                    if tribute_b_id
                    else None
                )
                try:
                    template_id = int(market_type[7:])
                except ValueError:
                    await interaction.followup.send(
                        "Invalid market type format.", ephemeral=True
                    )
                    return
                template = await session.get(MarketTemplate, template_id)
                if not template or not template.active:
                    await interaction.followup.send(
                        "Custom market type not found or inactive.", ephemeral=True
                    )
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = all_t_result.scalars().all()
                base = (
                    template.default_odds
                    if template.default_odds is not None
                    else DIFFICULTY_ODDS[template.difficulty]
                )
                odds = _compute_odds(
                    market_type,
                    trib_a,
                    all_tributes,
                    trib_b=trib_b,
                    base_odds_override=base,
                )
                tribute_str = f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}"
                if cause:
                    label = f"{tribute_str}: {cause}"
                elif template.label_template:
                    label = template.label_template.replace("{tribute}", tribute_str)
                else:
                    label = f"{tribute_str} — {template.name}"
                mkt = Market(
                    type=market_type,
                    label=label,
                    tribute_a_id=trib_a.id,
                    tribute_b_id=trib_b.id if trib_b else None,
                    cause=cause,
                    phase_id=pid,
                    odds=odds,
                )

            else:
                if tribute_a_id is None:
                    await interaction.followup.send(
                        "This market type requires a primary tribute.", ephemeral=True
                    )
                    return
                trib_a = await session.get(Tribute, int(tribute_a_id))
                if not trib_a:
                    await interaction.followup.send(
                        "Primary tribute not found.", ephemeral=True
                    )
                    return
                trib_b = (
                    await session.get(Tribute, int(tribute_b_id))
                    if tribute_b_id
                    else None
                )
                all_t_result = await session.execute(select(Tribute))
                all_tributes = all_t_result.scalars().all()

                # FIRST_IN_ALLIANCE_DEATH requires alliance context stored in cause/placement_num
                if market_type == "FIRST_IN_ALLIANCE_DEATH":
                    if alliance_id is None:
                        await interaction.followup.send(
                            "First in Alliance to Die requires an `alliance_id`.",
                            ephemeral=True,
                        )
                        return
                    alliance_obj = await session.get(Alliance, int(alliance_id))
                    if not alliance_obj:
                        await interaction.followup.send(
                            "Alliance not found.", ephemeral=True
                        )
                        return
                    placement_num = alliance_obj.id
                    cause = alliance_obj.name

                bt_result = await session.execute(
                    select(MarketTemplate).where(MarketTemplate.type_key == market_type)
                )
                bt = bt_result.scalars().first()

                if bt and bt.default_odds is not None:
                    odds = bt.default_odds
                else:
                    odds = _compute_odds(
                        market_type,
                        trib_a,
                        all_tributes,
                        trib_b=trib_b,
                        placement_num=placement_num,
                        top_n=top_n,
                        ou_line=ou_line,
                        ou_side=side_val,
                    )

                if bt and bt.label_template and not cause:
                    tribute_str = (
                        f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}"
                    )
                    label = bt.label_template.replace("{tribute}", tribute_str)
                else:
                    label = _build_label(
                        market_type,
                        trib_a,
                        trib_b,
                        cause,
                        placement_num,
                        top_n,
                        ou_line,
                        side_val,
                    )

                mkt = Market(
                    type=market_type,
                    label=label,
                    tribute_a_id=trib_a.id,
                    tribute_b_id=trib_b.id if trib_b else None,
                    cause=cause,
                    placement_num=placement_num,
                    top_n=top_n,
                    ou_line=ou_line,
                    ou_side=side_val,
                    phase_id=pid,
                    odds=odds,
                )

            session.add(mkt)
            await session.flush()
            mid = mkt.id

        embed = discord.Embed(title="Market Created", color=0xC9A227)
        embed.add_field(name="Label", value=label, inline=False)
        embed.add_field(name="Odds", value=fmt_odds(odds))
        embed.add_field(name="Market ID", value=str(mid))
        if phase_name:
            embed.add_field(name="Phase", value=phase_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market.command(
        name="set_phase", description="Assign (or clear) a betting phase for a market"
    )
    @app_commands.describe(
        market_id="Market to update",
        phase_id="Phase to assign (leave blank to clear — market becomes active all phases)",
    )
    @app_commands.autocomplete(
        market_id=open_market_autocomplete, phase_id=phase_autocomplete
    )
    @is_admin()
    async def market_set_phase(
        self,
        interaction: discord.Interaction,
        market_id: str,
        phase_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            if phase_id:
                phase_obj = await session.get(BettingPhase, int(phase_id))
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return
                mkt.phase_id = phase_obj.id
                phase_name = phase_obj.name
            else:
                mkt.phase_id = None
                phase_name = "All Phases"
            label = mkt.label

        await interaction.followup.send(
            f"Market **{label}** is now active during: **{phase_name}**.",
            ephemeral=True,
        )

    @market.command(name="odds", description="Override odds for a market")
    @app_commands.describe(
        market_id="Market to update", odds="American odds (e.g. -110 or +400)"
    )
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @is_admin()
    async def market_odds(
        self, interaction: discord.Interaction, market_id: str, odds: int
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            mkt.odds = odds
            mkt.odds_override = True
            label = mkt.label

        await interaction.followup.send(
            f"Odds for **{label}** set to **{fmt_odds(odds)}**.", ephemeral=True
        )

    @market.command(
        name="clear-override",
        description="Remove manual odds override(s) and let the model reprice. Omit market to clear all.",
    )
    @app_commands.describe(market_id="Market to clear (leave blank to clear all overrides)")
    @app_commands.autocomplete(market_id=overridden_market_autocomplete)
    @is_admin()
    async def market_clear_override(
        self, interaction: discord.Interaction, market_id: str | None = None
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            if market_id is not None:
                mkt = await session.get(Market, int(market_id))
                if not mkt:
                    await interaction.followup.send("Market not found.", ephemeral=True)
                    return
                if not mkt.odds_override:
                    await interaction.followup.send(
                        f"**{mkt.label}** has no manual override — nothing to clear.",
                        ephemeral=True,
                    )
                    return
                mkt.odds_override = False
                await _recalculate_markets(session)
                msg = f"Override cleared for **{mkt.label}**. Repriced to **{fmt_odds(mkt.odds)}**."
            else:
                result = await session.execute(
                    select(Market).where(
                        Market.status.in_(["OPEN", "CLOSED"]),
                        Market.odds_override == True,
                    )
                )
                overridden = result.scalars().all()
                if not overridden:
                    await interaction.followup.send(
                        "No markets have manual overrides.", ephemeral=True
                    )
                    return
                for mkt in overridden:
                    mkt.odds_override = False
                await _recalculate_markets(session)
                msg = f"Cleared overrides on **{len(overridden)}** market(s) and repriced."

        await interaction.followup.send(msg, ephemeral=True)

    @market.command(
        name="recalc", description="Re-price all markets using the current odds model"
    )
    @is_admin()
    async def market_recalc(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            count_row = await session.execute(
                select(func.count())
                .select_from(Market)
                .where(
                    Market.status.in_(["OPEN", "CLOSED"]), Market.odds_override == False
                )
            )
            count = count_row.scalar_one()
            await _recalculate_markets(session)

        await interaction.followup.send(
            f"Re-priced **{count}** market(s) against the current roster. "
            "Odds-overridden markets were left untouched.",
            ephemeral=True,
        )

    @market.command(name="close", description="Close a market to new bets")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @is_admin()
    async def market_close(
        self, interaction: discord.Interaction, market_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            mkt.status = "CLOSED"
            label = mkt.label

        await interaction.followup.send(f"Market **{label}** closed.", ephemeral=True)

    @market.command(name="reopen", description="Reopen a closed market")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @is_admin()
    async def market_reopen(
        self, interaction: discord.Interaction, market_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            mkt.status = "OPEN"
            label = mkt.label

        await interaction.followup.send(f"Market **{label}** reopened.", ephemeral=True)

    @market.command(name="resolve", description="Resolve a market (mark win/loss/void)")
    @app_commands.describe(
        market_id="Market to resolve", result="Outcome for bettors on this market"
    )
    @app_commands.choices(
        result=[
            app_commands.Choice(name="WIN  — bettors on this market WIN", value="WIN"),
            app_commands.Choice(
                name="LOSS — bettors on this market LOSE", value="LOSS"
            ),
            app_commands.Choice(name="VOID — refund all bets", value="VOID"),
        ]
    )
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @is_admin()
    async def market_resolve(
        self,
        interaction: discord.Interaction,
        market_id: str,
        result: app_commands.Choice[str],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        bool_result: bool | None = {"WIN": True, "LOSS": False, "VOID": None}[
            result.value
        ]
        async with _collect_notifications() as notifs:
            async with get_session() as session:
                mkt = await session.get(Market, int(market_id))
                if not mkt:
                    await interaction.followup.send("Market not found.", ephemeral=True)
                    return
                stats = await _resolve_market(session, mkt, bool_result)
                label = mkt.label

        await _send_resolution_dms(self.bot, notifs)
        color = (
            0x4CAF50
            if result.value == "WIN"
            else (0xCF4444 if result.value == "LOSS" else 0x888888)
        )
        embed = discord.Embed(title=f"Market Resolved: {result.value}", color=color)
        embed.add_field(name="Market", value=label, inline=False)
        embed.add_field(name="Bets Resolved", value=str(stats["resolved"]))
        if stats["credits"] > 0:
            embed.add_field(name="Chips Paid Out", value=fmt_chips(stats["credits"]))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market.command(
        name="bulk_close",
        description="Close open markets immediately, optionally filtered by type",
    )
    @app_commands.describe(
        market_type="Market type filter (blank = close all open markets)",
    )
    @app_commands.autocomplete(market_type=market_type_autocomplete)
    @is_admin()
    async def market_bulk_close(
        self, interaction: discord.Interaction, market_type: str | None = None
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            query = select(Market).where(Market.status == "OPEN")
            if market_type:
                query = query.where(Market.type == market_type)
            result = await session.execute(query)
            markets = result.scalars().all()
            count = len(markets)
            for m in markets:
                m.status = "CLOSED"

        if market_type:
            await interaction.followup.send(
                f"Closed {count} open {market_type} market(s).", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Closed {count} open market(s).", ephemeral=True
            )

    @market.command(
        name="bulk_open",
        description="Open closed markets, optionally filtered by phase and/or market type",
    )
    @app_commands.describe(
        phase_id="Phase filter (blank = any phase)",
        market_type="Market type filter (blank = all types). Bypasses phase-gating for that type.",
    )
    @app_commands.autocomplete(
        phase_id=phase_autocomplete, market_type=market_type_autocomplete
    )
    @is_admin()
    async def market_bulk_open(
        self,
        interaction: discord.Interaction,
        phase_id: str | None = None,
        market_type: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            phase_name: str | None = None
            filter_phase_id: int | None = None
            allowed: set[str] | None = None
            if phase_id:
                filter_phase_id = int(phase_id)
                phase_obj = await session.get(BettingPhase, filter_phase_id)
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return
                phase_name = phase_obj.name
                allowed = _open_types_for_phase(phase_name)
            query = select(Market).where(Market.status == "CLOSED")
            if market_type:
                query = query.where(Market.type == market_type)
            result = await session.execute(query)
            count = 0
            for m in result.scalars().all():
                if filter_phase_id is None or _market_should_open(m, filter_phase_id, allowed):
                    m.status = "OPEN"
                    count += 1

        scope_bits: list[str] = []
        if phase_name:
            scope_bits.append(f"in phase **{phase_name}**")
        if market_type:
            scope_bits.append(f"of type **{market_type}**")
        scope = " ".join(scope_bits) if scope_bits else "across all phases"
        await interaction.followup.send(
            f"Opened **{count}** market(s) {scope}.", ephemeral=True
        )

    @market.command(
        name="backfill",
        description="Create missing auto-generated markets for existing tributes and alliances",
    )
    @is_admin()
    async def market_backfill(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            counts = await _backfill_missing_markets(session)

        if not counts:
            await interaction.followup.send(
                "No missing markets found — all auto-generated markets are up to date.",
                ephemeral=True,
            )
            return

        total = sum(counts.values())
        lines = [f"• **{mtype}**: {count}" for mtype, count in sorted(counts.items())]
        embed = discord.Embed(
            title=f"Market Backfill — {total} Created",
            description="The following market types were missing and have been created (status: CLOSED).",
            color=0xC9A227,
        )
        embed.add_field(name="By Type", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market.command(name="list", description="Browse all markets with pagination")
    @app_commands.describe(status="Filter by market status (default: Open + Closed)")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Open", value="OPEN"),
            app_commands.Choice(name="Closed", value="CLOSED"),
            app_commands.Choice(name="Resolved", value="RESOLVED"),
            app_commands.Choice(name="All", value="ALL"),
        ]
    )
    @is_admin()
    async def market_list(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            query = select(Market)
            if status and status.value != "ALL":
                query = query.where(Market.status == status.value)
            elif not status:
                # Default: show OPEN and CLOSED (not resolved)
                query = query.where(Market.status.in_(["OPEN", "CLOSED"]))
            result = await session.execute(query)
            all_markets = result.scalars().all()

            trib_result = await session.execute(select(Tribute))
            tribute_map = {t.id: t for t in trib_result.scalars().all()}

            p_result = await session.execute(select(BettingPhase))
            phase_map = {p.id: p.name for p in p_result.scalars().all()}

            t_result = await session.execute(select(MarketTemplate))
            custom_type_labels = {
                f"CUSTOM_{t.id}": t.name for t in t_result.scalars().all()
            }

        if not all_markets:
            await interaction.followup.send("No markets found.", ephemeral=True)
            return

        status_label = status.name if status else "Open & Closed"
        sorted_mkts = sort_markets(all_markets, tribute_map)
        view = MarketPageView(
            sorted_mkts,
            tribute_map,
            phase_map=phase_map,
            is_admin=True,
            title=f"📊 MARKETS — {status_label.upper()}",
            extra_type_labels=custom_type_labels,
        )
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

    # ── MARKET TYPE COMMANDS ──────────────────────────────────────────────────

    @market_type.command(
        name="create",
        description="Create a new custom market type that persists across games",
    )
    @app_commands.describe(
        name="Display name for this market type",
        description="What this market type represents",
        difficulty="Sets default odds unless overridden",
        default_odds="American odds override (e.g. +500)",
        label_template="Label format — use {tribute} as placeholder",
    )
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    @is_admin()
    async def market_type_create(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        difficulty: app_commands.Choice[str],
        default_odds: int | None = None,
        label_template: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(MarketTemplate).where(MarketTemplate.name == name)
            )
            if existing.scalars().first():
                await interaction.followup.send(
                    f"A market type named **{name}** already exists.", ephemeral=True
                )
                return
            template = MarketTemplate(
                name=name,
                description=description,
                difficulty=difficulty.value,
                default_odds=default_odds,
                label_template=label_template,
                active=True,
            )
            session.add(template)
            await session.flush()
            tid = template.id

        effective_odds = (
            default_odds
            if default_odds is not None
            else DIFFICULTY_ODDS[difficulty.value]
        )
        embed = discord.Embed(title="Custom Market Type Created", color=0x4CAF50)
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Description", value=description, inline=False)
        embed.add_field(name="Difficulty", value=difficulty.name)
        embed.add_field(name="Default Odds", value=fmt_odds(effective_odds))
        embed.add_field(name="ID", value=str(tid))
        if label_template:
            embed.add_field(name="Label Template", value=label_template, inline=False)
        embed.set_footer(
            text="Use /admin market add and search for this type to create markets from it."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market_type.command(name="list", description="List all custom market types")
    @is_admin()
    async def market_type_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(
                select(MarketTemplate).order_by(MarketTemplate.id)
            )
            templates = result.scalars().all()

        if not templates:
            await interaction.followup.send(
                "No custom market types defined yet.", ephemeral=True
            )
            return

        view = MarketTypePageView(list(templates), DIFFICULTY_ODDS)
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

    @market_type.command(name="edit", description="Edit an existing custom market type")
    @app_commands.describe(
        template_id="Market type to edit",
        name="New display name",
        description="New description",
        difficulty="New difficulty level",
        default_odds="New explicit odds (set to 0 to revert to difficulty-based odds)",
        label_template="New label template (type 'none' to clear)",
        active="Enable or disable this market type",
    )
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    @app_commands.autocomplete(template_id=market_template_autocomplete)
    @is_admin()
    async def market_type_edit(
        self,
        interaction: discord.Interaction,
        template_id: str,
        name: str | None = None,
        description: str | None = None,
        difficulty: app_commands.Choice[str] | None = None,
        default_odds: int | None = None,
        label_template: str | None = None,
        active: bool | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            template = await session.get(MarketTemplate, int(template_id))
            if not template:
                await interaction.followup.send(
                    "Market type not found.", ephemeral=True
                )
                return
            if name:
                existing = await session.execute(
                    select(MarketTemplate).where(
                        MarketTemplate.name == name, MarketTemplate.id != template.id
                    )
                )
                if existing.scalars().first():
                    await interaction.followup.send(
                        f"A market type named **{name}** already exists.",
                        ephemeral=True,
                    )
                    return
                template.name = name
            if description:
                template.description = description
            if difficulty:
                template.difficulty = difficulty.value
            if default_odds is not None:
                template.default_odds = None if default_odds == 0 else default_odds
            if label_template is not None:
                template.label_template = (
                    None if label_template.lower() == "none" else label_template
                )
            if active is not None:
                template.active = active
            updated_name = template.name

        await interaction.followup.send(
            f"Market type **{updated_name}** updated.", ephemeral=True
        )

    @market_type.command(name="delete", description="Delete a custom market type")
    @app_commands.describe(template_id="Market type to delete")
    @app_commands.autocomplete(template_id=market_template_autocomplete)
    @is_admin()
    async def market_type_delete(
        self, interaction: discord.Interaction, template_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            template = await session.get(MarketTemplate, int(template_id))
            if not template:
                await interaction.followup.send(
                    "Market type not found.", ephemeral=True
                )
                return
            if template.is_builtin:
                await interaction.followup.send(
                    f"**{template.name}** is a built-in market type and cannot be deleted. "
                    "Use `/admin market_type edit` to deactivate it instead.",
                    ephemeral=True,
                )
                return
            type_key = f"CUSTOM_{template.id}"
            existing_markets = await session.execute(
                select(Market).where(Market.type == type_key)
            )
            if existing_markets.scalars().first():
                await interaction.followup.send(
                    f"Cannot delete **{template.name}** — markets of this type still exist. "
                    "Use `/admin market_type edit` to deactivate it instead.",
                    ephemeral=True,
                )
                return
            name = template.name
            await session.delete(template)

        await interaction.followup.send(
            f"Custom market type **{name}** deleted.", ephemeral=True
        )

    # ── GAME COMMANDS ─────────────────────────────────────────────────────────

    @game.command(
        name="start", description="Start the Games — always opens Pre-Games markets"
    )
    @is_admin()
    async def game_start(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        result = await _start_game()
        if result.get("error") == "already_active":
            await interaction.followup.send(
                "The Games are already running. Use `/game end` to conclude them first.",
                ephemeral=True,
            )
            return

        phase_str = f" ({result['phase_name']} phase)" if result["phase_name"] else ""
        embed = discord.Embed(
            title="⚡ THE HUNGER GAMES HAVE BEGUN",
            description=f"Opened **{result['opened']}** market(s){phase_str}. May the odds be ever in your favor.",
            color=0xC9A227,
        )
        if result["auto_parlays"]:
            embed.add_field(
                name="Featured Parlays",
                value=f"{result['auto_parlays']} auto-parlay(s) posted to the tailing board.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(
        name="arena",
        description="Set the arena type for the current game and adjust death-cause odds",
    )
    @app_commands.describe(
        arena_type="Arena environment — affects death-cause market odds"
    )
    @app_commands.choices(
        arena_type=[
            app_commands.Choice(
                name="Artificial — traps, tech hazards, constructed environment",
                value="ARTIFICIAL",
            ),
            app_commands.Choice(
                name="Natural   — wilderness, wildlife, environmental hazards",
                value="NATURAL",
            ),
            app_commands.Choice(
                name="Neutral   — no arena-type adjustment", value="NONE"
            ),
        ]
    )
    @is_admin()
    async def game_arena(
        self,
        interaction: discord.Interaction,
        arena_type: app_commands.Choice[str],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        val: str | None = None if arena_type.value == "NONE" else arena_type.value
        await set_setting("arena_type", val)
        _arena_notifs: list[dict] = []
        _arena_token = _notif_buffer.set(_arena_notifs)
        resolved_count = 0
        try:
            async with get_session() as session:
                await _recalculate_markets(session)
                if val is not None:
                    arena_mkt_result = await session.execute(
                        select(Market).where(
                            Market.type.in_(["ARENA_TYPE", "ARENA_IS_NATURAL", "ARENA_IS_ARTIFICIAL"]),
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    for mkt in arena_mkt_result.scalars().all():
                        if mkt.type == "ARENA_TYPE":
                            outcome = mkt.cause == val
                        elif mkt.type == "ARENA_IS_NATURAL":
                            outcome = val == "NATURAL"
                        else:  # ARENA_IS_ARTIFICIAL
                            outcome = val == "ARTIFICIAL"
                        stats = await _resolve_market(session, mkt, outcome)
                        resolved_count += stats["resolved"]
        finally:
            _notif_buffer.reset(_arena_token)
        await _send_resolution_dms(self.bot, _arena_notifs)
        label = arena_type.name.split("—")[0].strip()
        extra = f" Resolved arena-type markets ({resolved_count} bet(s) settled)." if resolved_count else ""
        await interaction.followup.send(
            f"Arena type set to **{label}**. Death-cause market odds recalculated.{extra}",
            ephemeral=True,
        )

    @game.command(name="set_phase", description="Transition to a new betting phase")
    @app_commands.describe(
        phase_id="Phase to activate",
        confirm="Type 'yes' to proceed off Pre-Games even though some tributes have no training score",
    )
    @app_commands.autocomplete(phase_id=phase_autocomplete)
    @is_admin()
    async def game_set_phase(
        self, interaction: discord.Interaction, phase_id: str, confirm: str | None = None
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        new_phase_id = int(phase_id)

        current_phase_raw = await get_setting("current_phase_id")
        old_phase_id = json.loads(current_phase_raw) if current_phase_raw else None

        game_active_raw = await get_setting("game_active")
        game_active = json.loads(game_active_raw) if game_active_raw else False

        _phase_notifs: list[dict] = []
        _phase_token = _notif_buffer.set(_phase_notifs)
        async with get_session() as session:
            new_phase = await session.get(BettingPhase, new_phase_id)
            if not new_phase:
                _notif_buffer.reset(_phase_token)
                await interaction.followup.send("Phase not found.", ephemeral=True)
                return

            old_phase: BettingPhase | None = None
            if old_phase_id:
                old_phase = await session.get(BettingPhase, old_phase_id)

            # Leaving Pre-Games with tributes still missing a training score is
            # usually a mistake (scouting/training-score markets resolve the
            # instant Pre-Games ends) — require an explicit confirm to proceed.
            if (
                old_phase
                and old_phase.name == "Pre-Games"
                and new_phase_id != old_phase_id
                and (confirm or "").lower() != "yes"
            ):
                missing_result = await session.execute(
                    select(Tribute).where(Tribute.training_score.is_(None))
                )
                missing = list(missing_result.scalars().all())
                if missing:
                    _notif_buffer.reset(_phase_token)
                    names = ", ".join(_tribute_label(t) for t in missing[:20])
                    more = f" (+{len(missing) - 20} more)" if len(missing) > 20 else ""
                    await interaction.followup.send(
                        f"⚠️ **{len(missing)}** tribute(s) have no training score set yet: "
                        f"{names}{more}.\nRe-run with `confirm: yes` to leave Pre-Games anyway.",
                        ephemeral=True,
                    )
                    return

            closed_count = 0
            opened_count = 0
            auto_resolved: list[str] = []

            if game_active:
                # ── Auto-resolutions ───────────────────────────────────────────
                # Which markets are OPEN is decided by the type-gating pass at the
                # end; here we only settle markets whose outcome is now known.
                trib_result = await session.execute(select(Tribute))
                all_tributes = list(trib_result.scalars().all())
                alive_ids = {t.id for t in all_tributes if t.status == "ALIVE"}
                trib_map = {t.id: t for t in all_tributes}

                # Bloodbath markets resolve when the Arena opens — the bloodbath
                # is over and its outcomes are final. (Bloodbath markets stay open
                # through Bloodbath and Sponsors Open until this point.)
                if new_phase.name == "Arena":
                    # Resolve BLOODBATH_SURVIVOR: alive = WIN, dead = LOSE
                    bb_result = await session.execute(
                        select(Market).where(
                            Market.type == "BLOODBATH_SURVIVOR",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    bb_count = 0
                    for mkt in bb_result.scalars().all():
                        await _resolve_market(
                            session, mkt, mkt.tribute_a_id in alive_ids
                        )
                        bb_count += 1
                    if bb_count:
                        auto_resolved.append(f"{bb_count} Bloodbath Survivor")

                    # Resolve DISTRICT_BOTH_BLOODBATH: WIN if both district tributes alive
                    dbb_result = await session.execute(
                        select(Market).where(
                            Market.type == "DISTRICT_BOTH_BLOODBATH",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    dbb_count = 0
                    for mkt in dbb_result.scalars().all():
                        d = mkt.placement_num
                        if d is None:
                            await _resolve_market(session, mkt, None)
                        else:
                            d_alive = [
                                t
                                for t in all_tributes
                                if t.district == d and t.id in alive_ids
                            ]
                            d_total = [t for t in all_tributes if t.district == d]
                            await _resolve_market(
                                session,
                                mkt,
                                len(d_alive) == len(d_total) and len(d_total) > 0,
                            )
                        dbb_count += 1
                    if dbb_count:
                        auto_resolved.append(
                            f"{dbb_count} District Both Survive Bloodbath"
                        )

                    # Resolve TRIBUTE_KILLED_BLOODBATH: WIN if tribute is dead
                    tkb_result = await session.execute(
                        select(Market).where(
                            Market.type == "TRIBUTE_KILLED_BLOODBATH",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    tkb_count = 0
                    for mkt in tkb_result.scalars().all():
                        await _resolve_market(
                            session, mkt, mkt.tribute_a_id not in alive_ids
                        )
                        tkb_count += 1
                    if tkb_count:
                        auto_resolved.append(f"{tkb_count} Killed in Bloodbath")

                    # Resolve DISTRICT_WIPED_BLOODBATH: WIN if all district tributes dead
                    dwb_result = await session.execute(
                        select(Market).where(
                            Market.type == "DISTRICT_WIPED_BLOODBATH",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    dwb_count = 0
                    for mkt in dwb_result.scalars().all():
                        d = mkt.placement_num
                        if d is None:
                            await _resolve_market(session, mkt, None)
                        else:
                            d_alive = [
                                t
                                for t in all_tributes
                                if t.district == d and t.id in alive_ids
                            ]
                            d_total = [t for t in all_tributes if t.district == d]
                            await _resolve_market(
                                session,
                                mkt,
                                len(d_alive) == 0 and len(d_total) > 0,
                            )
                        dwb_count += 1
                    if dwb_count:
                        auto_resolved.append(
                            f"{dwb_count} District Wiped in Bloodbath"
                        )

                    # Resolve BLOODBATH_KILLS_OU: sum kills scored during Bloodbath
                    bb_kills_total = sum(t.bloodbath_kills for t in all_tributes)
                    bk_result = await session.execute(
                        select(Market).where(
                            Market.type == "BLOODBATH_KILLS_OU",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    bk_mkts = bk_result.scalars().all()
                    for mkt in bk_mkts:
                        line = mkt.ou_line if mkt.ou_line is not None else 0.5
                        result = (
                            bb_kills_total > line
                            if mkt.ou_side == "OVER"
                            else bb_kills_total <= line
                        )
                        await _resolve_market(session, mkt, result)
                    if bk_mkts:
                        auto_resolved.append(
                            f"{len(bk_mkts)} Bloodbath Kills Over/Under"
                        )

                    # Void any FIRST_BLOOD markets that never resolved
                    fb_result = await session.execute(
                        select(Market).where(
                            Market.type == "FIRST_BLOOD",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    fb_mkts = fb_result.scalars().all()
                    fb_count = len(fb_mkts)
                    for mkt in fb_mkts:
                        await _resolve_market(session, mkt, None)  # void
                    if fb_count:
                        auto_resolved.append(
                            f"{fb_count} First Blood (voided — no first kill)"
                        )

                    # Bloodbath death-count props: deaths = tributes who died during bloodbath
                    bb_dead_count = len(all_tributes) - len(alive_ids)
                    for bb_type, mkts_sql in [
                        ("BLOODBATH_NO_DEATHS", select(Market).where(
                            Market.type == "BLOODBATH_NO_DEATHS",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )),
                        ("BLOODBATH_DEATHS_OU", select(Market).where(
                            Market.type == "BLOODBATH_DEATHS_OU",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )),
                        ("EXACT_BLOODBATH_DEATHS", select(Market).where(
                            Market.type == "EXACT_BLOODBATH_DEATHS",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )),
                    ]:
                        bbd_result = await session.execute(mkts_sql)
                        bbd_mkts = bbd_result.scalars().all()
                        for mkt in bbd_mkts:
                            if bb_type == "BLOODBATH_NO_DEATHS":
                                result = bb_dead_count == 0
                            elif bb_type == "BLOODBATH_DEATHS_OU":
                                line = mkt.ou_line if mkt.ou_line is not None else 0.5
                                result = (
                                    bb_dead_count > line
                                    if mkt.ou_side == "OVER"
                                    else bb_dead_count <= line
                                )
                            else:  # EXACT_BLOODBATH_DEATHS
                                result = bb_dead_count == (mkt.placement_num or -1)
                            await _resolve_market(session, mkt, result)
                        if bbd_mkts:
                            auto_resolved.append(f"{len(bbd_mkts)} {bb_type.replace('_', ' ').title()}")

                    # ANY_BB_DOUBLE_KILL: did any tribute rack up 2+ kills during
                    # the Bloodbath specifically.
                    bb_double = any(t.bloodbath_kills >= 2 for t in all_tributes)
                    abdk_result = await session.execute(
                        select(Market).where(
                            Market.type == "ANY_BB_DOUBLE_KILL",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    abdk_mkts = abdk_result.scalars().all()
                    for mkt in abdk_mkts:
                        await _resolve_market(session, mkt, bb_double)
                    if abdk_mkts:
                        auto_resolved.append(
                            f"{len(abdk_mkts)} Any Bloodbath Double-Kill"
                        )

                # Pre-Games props (training scores, arena type, etc.) resolve when
                # the Bloodbath begins — i.e. when Pre-Games ends.
                if old_phase and old_phase.name == "Pre-Games" and old_phase.id != new_phase_id:
                    arena_type_row = await session.get(GameSetting, "arena_type")
                    arena_type_val = (
                        json.loads(arena_type_row.value) if arena_type_row else None
                    )

                    prop_result = await session.execute(
                        select(Market).where(
                            Market.type.in_(_PREGAMES_PROP_TYPES),
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    prop_count = 0
                    for mkt in prop_result.scalars().all():
                        if mkt.type == "ARENA_TYPE":
                            if arena_type_val is None:
                                result = None  # void — arena type never set
                            else:
                                result = mkt.cause == arena_type_val

                        elif mkt.type == "EXACT_TRAINING_SCORE":
                            trib = (
                                trib_map.get(mkt.tribute_a_id)
                                if mkt.tribute_a_id
                                else None
                            )
                            if trib is None or trib.training_score is None:
                                result = None  # void — score never set
                            else:
                                result = trib.training_score == mkt.placement_num

                        elif mkt.type == "COMBINED_DISTRICT_SCORE":
                            ta = (
                                trib_map.get(mkt.tribute_a_id)
                                if mkt.tribute_a_id
                                else None
                            )
                            tb = (
                                trib_map.get(mkt.tribute_b_id)
                                if mkt.tribute_b_id
                                else None
                            )
                            if (
                                ta is None
                                or tb is None
                                or ta.training_score is None
                                or tb.training_score is None
                            ):
                                result = None  # void — one or both scores never set
                            else:
                                result = (
                                    ta.training_score + tb.training_score
                                    == mkt.placement_num
                                )

                        elif mkt.type == "TRAINING_SCORE_OU":
                            trib = (
                                trib_map.get(mkt.tribute_a_id)
                                if mkt.tribute_a_id
                                else None
                            )
                            if trib is None or trib.training_score is None:
                                result = None  # void — score never set
                            else:
                                line = mkt.ou_line if mkt.ou_line is not None else 6.5
                                if mkt.ou_side == "OVER":
                                    result = trib.training_score > line
                                else:
                                    result = trib.training_score <= line
                        elif mkt.type == "ARENA_IS_NATURAL":
                            result = (
                                arena_type_val == "NATURAL" if arena_type_val else None
                            )
                        elif mkt.type == "ARENA_IS_ARTIFICIAL":
                            result = (
                                arena_type_val == "ARTIFICIAL" if arena_type_val else None
                            )
                        elif mkt.type == "NUM_TENS_OU":
                            high_count = sum(
                                1 for t in all_tributes
                                if t.training_score is not None and t.training_score >= 10
                            )
                            line = mkt.ou_line if mkt.ou_line is not None else 0.5
                            result = (
                                high_count > line
                                if mkt.ou_side == "OVER"
                                else high_count <= line
                            )
                        elif mkt.type == "SOLO_TRIBUTES_OU":
                            solo_count = sum(
                                1 for t in all_tributes
                                if t.status == "ALIVE" and t.alliance_id is None
                            )
                            line = mkt.ou_line if mkt.ou_line is not None else 0.5
                            result = (
                                solo_count > line
                                if mkt.ou_side == "OVER"
                                else solo_count <= line
                            )
                        elif mkt.type in ("PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"):
                            ta = trib_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                            tb = trib_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                            if ta is None or tb is None or ta.training_score is None or tb.training_score is None:
                                result = None  # void — one or both scores never set
                            elif mkt.type == "PARTNER_SCORE_HIGHER":
                                result = ta.training_score > tb.training_score
                            else:
                                result = ta.training_score < tb.training_score
                        else:
                            result = None

                        await _resolve_market(session, mkt, result)
                        prop_count += 1
                    if prop_count:
                        auto_resolved.append(f"{prop_count} Pre-Games props")

                    # HIGHEST_TRAINING_SCORE / LOWEST_TRAINING_SCORE
                    scored = [t for t in all_tributes if t.training_score is not None]
                    if scored:
                        max_score = max(t.training_score for t in scored)
                        min_score = min(t.training_score for t in scored)
                        for mtype, target in (
                            ("HIGHEST_TRAINING_SCORE", max_score),
                            ("LOWEST_TRAINING_SCORE", min_score),
                        ):
                            ts_q = await session.execute(
                                select(Market).where(
                                    Market.type == mtype,
                                    Market.status.in_(["OPEN", "CLOSED"]),
                                )
                            )
                            ts_markets = ts_q.scalars().all()
                            for mkt in ts_markets:
                                trib = trib_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                                result = bool(trib and trib.training_score == target)
                                await _resolve_market(session, mkt, result)
                            if ts_markets:
                                label = "Highest" if mtype == "HIGHEST_TRAINING_SCORE" else "Lowest"
                                auto_resolved.append(f"{len(ts_markets)} {label} Training Score")

                    # DISTRICT_HIGHEST_SCORE
                    dist_scores: dict[int, int] = {}
                    for t in all_tributes:
                        if t.training_score is not None:
                            dist_scores[t.district] = (
                                dist_scores.get(t.district, 0) + t.training_score
                            )
                    if dist_scores:
                        max_dist_score = max(dist_scores.values())
                        dhs_q = await session.execute(
                            select(Market).where(
                                Market.type == "DISTRICT_HIGHEST_SCORE",
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        dhs_count = 0
                        for mkt in dhs_q.scalars().all():
                            d = mkt.placement_num
                            result = (
                                dist_scores.get(d, 0) == max_dist_score
                                if d is not None
                                else None
                            )
                            await _resolve_market(session, mkt, result)
                            dhs_count += 1
                        if dhs_count:
                            auto_resolved.append(f"{dhs_count} District Highest Score")

                # ── Auto-resolutions when entering new phase ───────────────────
                if new_phase.name in _PHASE_ENTRY_MARKETS:
                    makes_type, misses_type = _PHASE_ENTRY_MARKETS[new_phase.name]
                    milestone_count = 0
                    for mtype, wins_if_alive in (
                        (makes_type, True),
                        (misses_type, False),
                    ):
                        m_result = await session.execute(
                            select(Market).where(
                                Market.type == mtype,
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        for mkt in m_result.scalars().all():
                            is_alive = mkt.tribute_a_id in alive_ids
                            await _resolve_market(
                                session,
                                mkt,
                                is_alive if wins_if_alive else not is_alive,
                            )
                            milestone_count += 1
                    if milestone_count:
                        auto_resolved.append(
                            f"{milestone_count} {new_phase.name} milestone markets"
                        )

                if new_phase.name in _DISTRICT_PHASE_ENTRY:
                    dist_milestone_count = 0
                    for mtype in _DISTRICT_PHASE_ENTRY[new_phase.name]:
                        dm_result = await session.execute(
                            select(Market).where(
                                Market.type == mtype,
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        for mkt in dm_result.scalars().all():
                            d = mkt.placement_num
                            if d is None:
                                await _resolve_market(session, mkt, None)
                            else:
                                d_tributes = [
                                    t for t in all_tributes if t.district == d
                                ]
                                alive_d_count = sum(
                                    1 for t in d_tributes if t.id in alive_ids
                                )
                                if mtype.startswith("DISTRICT_BOTH"):
                                    result = (
                                        alive_d_count == len(d_tributes)
                                        and len(d_tributes) > 0
                                    )
                                else:
                                    result = alive_d_count >= 1
                                await _resolve_market(session, mkt, result)
                            dist_milestone_count += 1
                    if dist_milestone_count:
                        auto_resolved.append(
                            f"{dist_milestone_count} {new_phase.name} district milestones"
                        )

                if new_phase.name in _ALLIANCE_PHASE_ENTRY:
                    alliance_milestone_count = 0
                    for mtype in _ALLIANCE_PHASE_ENTRY[new_phase.name]:
                        am_result = await session.execute(
                            select(Market).where(
                                Market.type == mtype,
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        for mkt in am_result.scalars().all():
                            aid = mkt.placement_num
                            if aid is None:
                                await _resolve_market(session, mkt, None)
                            else:
                                a_members = [
                                    t for t in all_tributes if t.alliance_id == aid
                                ]
                                alive_a_count = sum(
                                    1 for t in a_members if t.id in alive_ids
                                )
                                if mtype.startswith("ALLIANCE_ALL"):
                                    result = (
                                        alive_a_count == len(a_members)
                                        and len(a_members) > 0
                                    )
                                else:
                                    result = alive_a_count >= 1
                                await _resolve_market(session, mkt, result)
                            alliance_milestone_count += 1
                    if alliance_milestone_count:
                        auto_resolved.append(
                            f"{alliance_milestone_count} {new_phase.name} alliance milestones"
                        )

                # ── Phase-gated open/close pass ───────────────────────────────
                # After resolutions, re-gate every still-live market: markets
                # whose type is available in this phase open, the rest close.
                # Resolved markets are skipped (no longer OPEN/CLOSED) — but
                # sessions here run with autoflush=False, so the RESOLVED writes
                # from the auto-resolutions above (bloodbath survivor/killed,
                # milestones, etc.) aren't visible to a fresh query until
                # flushed. Without this, the SELECT below still sees their old
                # OPEN/CLOSED status at the SQL level and hands back the same
                # (already-mutated-to-RESOLVED) ORM objects, which then get
                # flipped straight back to OPEN by the loop below — the market
                # pays out correctly but ends up looking open again.
                await session.flush()
                allowed_types = _open_types_for_phase(new_phase.name)
                gate_result = await session.execute(
                    select(Market).where(Market.status.in_(["OPEN", "CLOSED"]))
                )
                for m in gate_result.scalars().all():
                    if _market_should_open(m, new_phase_id, allowed_types):
                        if m.status != "OPEN":
                            m.status = "OPEN"
                            opened_count += 1
                    elif m.status != "CLOSED":
                        m.status = "CLOSED"
                        closed_count += 1

                # ── Sponsor window → funding-modifier strength ────────────────
                # Sponsors Open amplifies the funding edge; Sponsors Closing cuts
                # it to a residual. Re-price every market against the new state.
                if new_phase.name == "Sponsors Open":
                    await _set_sponsor_state(session, "open")
                    await _recalculate_markets(session)
                    auto_resolved.append("Funding modifiers boosted (sponsors open)")
                elif new_phase.name == "Sponsors Closing":
                    await _set_sponsor_state(session, "closed")
                    await _recalculate_markets(session)
                    auto_resolved.append("Funding modifiers reduced (sponsors closed)")

                # Refresh the three featured parlays from this phase's open markets.
                # Both generators are gated by _AUTO_PARLAY_GENERATION_ENABLED, so
                # this is a no-op while that kill switch is off.
                auto_made = (
                    await _try_generate_ai_parlays(session, new_phase_id)
                    or await _generate_auto_parlays(session, new_phase_id)
                )
                if auto_made:
                    auto_resolved.append(f"{auto_made} featured parlay(s) posted for tailing")

        _notif_buffer.reset(_phase_token)
        await set_setting("current_phase_id", new_phase_id)
        await _send_resolution_dms(self.bot, _phase_notifs)

        embed = discord.Embed(
            title=f"Phase: {new_phase.name}",
            description=new_phase.description or "",
            color=0xC9A227,
        )
        if game_active:
            embed.add_field(name="Markets Closed", value=str(closed_count))
            embed.add_field(name="Markets Opened", value=str(opened_count))
            if auto_resolved:
                embed.add_field(
                    name="Auto-Resolved",
                    value="\n".join(f"• {s}" for s in auto_resolved),
                    inline=False,
                )
        else:
            embed.set_footer(text="Phase set. Markets will open when the game starts.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(name="end", description="End the Games — declare a victor")
    @app_commands.describe(victor_id="The winning tribute")
    @app_commands.autocomplete(victor_id=alive_tribute_autocomplete)
    @is_admin()
    async def game_end(
        self,
        interaction: discord.Interaction,
        victor_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        game_active_raw = await get_setting("game_active")
        if not json.loads(game_active_raw or "false"):
            await interaction.followup.send(
                "No game is currently running. Use `/game start` first.",
                ephemeral=True,
            )
            return
        _end_notifs: list[dict] = []
        _end_token = _notif_buffer.set(_end_notifs)
        async with get_session() as session:
            victor = await session.get(Tribute, int(victor_id))
            if not victor:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            victor.status = "VICTOR"
            victor.placement = 1
            victor_name = victor.name
            victor_district = victor.district

            mkt_result = await session.execute(
                select(Market).where(
                    Market.type == "TRIBUTE_WINS",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in mkt_result.scalars().all():
                await _resolve_market(session, mkt, mkt.tribute_a_id == victor.id)

            # Resolve DISTRICT_VICTOR: WIN if victor came from the bet district
            dv_result = await session.execute(
                select(Market).where(
                    Market.type == "DISTRICT_VICTOR",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in dv_result.scalars().all():
                await _resolve_market(
                    session, mkt, mkt.placement_num == victor_district
                )

            # Load all tributes once for kill-total resolutions
            all_t_result = await session.execute(select(Tribute))
            all_t_final = list(all_t_result.scalars().all())

            # Resolve DISTRICT_KILLS_OU based on combined district kill totals
            dko_result = await session.execute(
                select(Market).where(
                    Market.type == "DISTRICT_KILLS_OU",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in dko_result.scalars().all():
                d = mkt.placement_num
                if d is None:
                    await _resolve_market(session, mkt, None)
                else:
                    total_kills = sum(t.kills for t in all_t_final if t.district == d)
                    line = mkt.ou_line if mkt.ou_line is not None else 1.5
                    result = (
                        total_kills > line
                        if mkt.ou_side == "OVER"
                        else total_kills <= line
                    )
                    await _resolve_market(session, mkt, result)

            # Resolve ALLIANCE_VICTOR: WIN if victor was a member of the alliance
            av_result = await session.execute(
                select(Market).where(
                    Market.type == "ALLIANCE_VICTOR",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in av_result.scalars().all():
                await _resolve_market(
                    session, mkt, mkt.placement_num == victor.alliance_id
                )

            # Resolve ALLIANCE_KILLS_OU based on combined alliance kill totals
            ako_result = await session.execute(
                select(Market).where(
                    Market.type == "ALLIANCE_KILLS_OU",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in ako_result.scalars().all():
                aid = mkt.placement_num
                if aid is None:
                    await _resolve_market(session, mkt, None)
                else:
                    total_kills = sum(
                        t.kills for t in all_t_final if t.alliance_id == aid
                    )
                    line = mkt.ou_line if mkt.ou_line is not None else 1.5
                    result = (
                        total_kills > line
                        if mkt.ou_side == "OVER"
                        else total_kills <= line
                    )
                    await _resolve_market(session, mkt, result)

            # Resolve TRIBUTE_RUNNER_UP: tribute who finished 2nd
            ru_result = await session.execute(
                select(Market).where(
                    Market.type == "TRIBUTE_RUNNER_UP",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in ru_result.scalars().all():
                trib = next(
                    (t for t in all_t_final if t.id == mkt.tribute_a_id), None
                )
                await _resolve_market(
                    session, mkt, bool(trib and trib.placement == 2)
                )

            # Resolve ALLIANCE_RUNNER_UP: any member of alliance finished 2nd
            aru_result = await session.execute(
                select(Market).where(
                    Market.type == "ALLIANCE_RUNNER_UP",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in aru_result.scalars().all():
                aid = mkt.placement_num
                if aid is None:
                    await _resolve_market(session, mkt, None)
                else:
                    has_runner_up = any(
                        t.placement == 2 for t in all_t_final if t.alliance_id == aid
                    )
                    await _resolve_market(session, mkt, has_runner_up)

            # Resolve ALLIANCE_MOST_KILLS: alliance with the highest kill total wins
            # Build kill totals per alliance, then resolve
            alliance_kills: dict[int, int] = {}
            for t in all_t_final:
                if t.alliance_id is not None:
                    alliance_kills[t.alliance_id] = (
                        alliance_kills.get(t.alliance_id, 0) + t.kills
                    )
            if alliance_kills:
                max_alliance_kills = max(alliance_kills.values())
                amk_result = await session.execute(
                    select(Market).where(
                        Market.type == "ALLIANCE_MOST_KILLS",
                        Market.status.in_(["OPEN", "CLOSED"]),
                    )
                )
                for mkt in amk_result.scalars().all():
                    aid = mkt.placement_num
                    result = (
                        alliance_kills.get(aid, 0) == max_alliance_kills
                        if aid is not None
                        else None
                    )
                    await _resolve_market(session, mkt, result)

            # Resolve EXACT_ALLIANCE_KILLS: alliance kill count == top_n
            eak_result = await session.execute(
                select(Market).where(
                    Market.type == "EXACT_ALLIANCE_KILLS",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in eak_result.scalars().all():
                aid = mkt.placement_num
                guessed = mkt.top_n
                if aid is None or guessed is None:
                    await _resolve_market(session, mkt, None)
                else:
                    actual = sum(t.kills for t in all_t_final if t.alliance_id == aid)
                    await _resolve_market(session, mkt, actual == guessed)

            # Resolve DISTRICT_PARTNER_KILL: any tribute killed their district partner
            any_partner_kill = any(
                t.killed_by_id is not None and any(
                    k.district == t.district and k.id != t.id
                    for k in all_t_final
                    if k.id == t.killed_by_id
                )
                for t in all_t_final
                if t.killed_by_id is not None
            )
            dpk_result = await session.execute(
                select(Market).where(
                    Market.type == "DISTRICT_PARTNER_KILL",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in dpk_result.scalars().all():
                await _resolve_market(session, mkt, any_partner_kill)

            # Settle the victor's death-contingent markets — they survive, so these
            # can never resolve on a death event and would otherwise be orphaned.
            victor_mkts = await session.execute(
                select(Market).where(
                    or_(
                        Market.tribute_a_id == victor.id,
                        Market.tribute_b_id == victor.id,
                    ),
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in victor_mkts.scalars().all():
                if mkt.type == "DEATH_CAUSE":
                    await _resolve_market(session, mkt, False)  # victor never died
                elif mkt.type == "KILLS_OU":
                    line = mkt.ou_line if mkt.ou_line is not None else 0.5
                    result = (
                        victor.kills > line
                        if mkt.ou_side == "OVER"
                        else victor.kills <= line
                    )
                    await _resolve_market(session, mkt, result)
                elif mkt.type == "KILL_EVENT" and mkt.tribute_b_id == victor.id:
                    await _resolve_market(session, mkt, False)  # victor was never killed

            # Settle every placement-driven market (the victor placed 1st; the
            # rest carry the placement recorded at death) and the "most kills"
            # markets from final kill totals.
            place_stats = await _resolve_placement_markets(session)
            partner_place_stats = await _resolve_partner_place_markets(session)
            killer_stats = await _resolve_top_killer_markets(session)

            open_result = await session.execute(
                select(Market).where(Market.status == "OPEN")
            )
            for mkt in open_result.scalars().all():
                mkt.status = "CLOSED"

        _notif_buffer.reset(_end_token)
        await set_setting("game_active", False)
        await set_setting("sponsor_state", None)
        await _send_resolution_dms(self.bot, _end_notifs)
        embed = discord.Embed(
            title=f"👑 VICTOR: {victor_name.upper()} OF DISTRICT {victor_district}",
            description="The Games have concluded. The Capitol thanks you for your patronage.",
            color=0xFFD700,
        )
        settled = place_stats["resolved"] + partner_place_stats["resolved"] + killer_stats["resolved"]
        if settled:
            embed.add_field(
                name="Final Markets Settled",
                value=(
                    f"{place_stats['resolved']} placement • "
                    f"{partner_place_stats['resolved']} partner-place • "
                    f"{killer_stats['resolved']} most-kills"
                ),
                inline=False,
            )
        embed.set_footer(
            text="Settle game props (duration, feast, betrayal, arena deaths) with "
            "/admin resolve."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Manual type-level market resolution ────────────────────────────────────

    @resolve.command(
        name="placements",
        description="Settle all placement markets (exact place, top-N, O/U, runner-up)",
    )
    @is_admin()
    async def resolve_placements(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _pl_notifs: list[dict] = []
        _pl_token = _notif_buffer.set(_pl_notifs)
        async with get_session() as session:
            stats = await _resolve_placement_markets(session)
            pp_stats = await _resolve_partner_place_markets(session)
        _notif_buffer.reset(_pl_token)
        await _send_resolution_dms(self.bot, _pl_notifs)
        msg = (
            f"✅ Settled **{stats['resolved']}** placement market(s) and "
            f"**{pp_stats['resolved']}** partner-place market(s) from recorded placements."
        )
        skipped = stats["skipped"] + pp_stats["skipped"]
        if skipped:
            msg += (
                f"\n⏳ Left **{skipped}** open — those tributes are still "
                f"alive (no final placement yet)."
            )
        await interaction.followup.send(msg, ephemeral=True)

    @resolve.command(
        name="top_killer",
        description="Settle 'Most Kills' markets — the tribute(s) with the most kills win",
    )
    @is_admin()
    async def resolve_top_killer(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _tk_notifs: list[dict] = []
        _tk_token = _notif_buffer.set(_tk_notifs)
        async with get_session() as session:
            stats = await _resolve_top_killer_markets(session)
        _notif_buffer.reset(_tk_token)
        await _send_resolution_dms(self.bot, _tk_notifs)
        if stats["max_kills"] == 0:
            await interaction.followup.send(
                f"No kills recorded yet — voided **{stats['resolved']}** Most-Kills "
                f"market(s).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Settled **{stats['resolved']}** Most-Kills market(s). "
            f"Winning total: **{stats['max_kills']}** kill(s).",
            ephemeral=True,
        )

    @resolve.command(
        name="duration",
        description="Settle 'Games Duration' markets with the actual number of days",
    )
    @app_commands.describe(actual_days="How many days the Games actually lasted")
    @is_admin()
    async def resolve_duration(
        self, interaction: discord.Interaction, actual_days: int
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _dur_notifs: list[dict] = []
        _dur_token = _notif_buffer.set(_dur_notifs)
        async with get_session() as session:
            res = await session.execute(
                select(Market).where(
                    Market.type == "GAMES_DURATION",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            markets = res.scalars().all()
            for m in markets:
                await _resolve_market(session, m, m.placement_num == actual_days)
        _notif_buffer.reset(_dur_token)
        await _send_resolution_dms(self.bot, _dur_notifs)
        await interaction.followup.send(
            f"✅ Settled **{len(markets)}** Games-Duration market(s) — winners bet on "
            f"**{actual_days}** day(s).",
            ephemeral=True,
        )

    @resolve.command(
        name="feast",
        description="Settle 'Games Features a Feast' markets",
    )
    @app_commands.describe(happened="Did a Feast occur during the Games?")
    @is_admin()
    async def resolve_feast(
        self, interaction: discord.Interaction, happened: bool
    ) -> None:
        await self._resolve_binary_prop(
            interaction, "GAMES_FEAST", happened, "Feast"
        )

    @resolve.command(
        name="betrayal",
        description="Settle 'Games Features a Betrayal' markets",
    )
    @app_commands.describe(happened="Did a betrayal occur during the Games?")
    @is_admin()
    async def resolve_betrayal(
        self, interaction: discord.Interaction, happened: bool
    ) -> None:
        await self._resolve_binary_prop(
            interaction, "GAMES_BETRAYAL", happened, "Betrayal"
        )

    @resolve.command(
        name="bb_double_kill",
        description="Settle 'Any Bloodbath Double-Kill' markets (manual override)",
    )
    @app_commands.describe(
        happened="Did any tribute score 2+ kills in the Bloodbath?"
    )
    @is_admin()
    async def resolve_bb_double_kill(
        self, interaction: discord.Interaction, happened: bool
    ) -> None:
        await self._resolve_binary_prop(
            interaction, "ANY_BB_DOUBLE_KILL", happened, "Bloodbath double-kill"
        )

    async def _resolve_binary_prop(
        self,
        interaction: discord.Interaction,
        market_type: str,
        happened: bool,
        label: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _bp_notifs: list[dict] = []
        _bp_token = _notif_buffer.set(_bp_notifs)
        async with get_session() as session:
            stats = await _resolve_markets_of_type(session, market_type, happened)
        _notif_buffer.reset(_bp_token)
        await _send_resolution_dms(self.bot, _bp_notifs)
        outcome = "occurred" if happened else "did NOT occur"
        await interaction.followup.send(
            f"✅ Settled **{stats['markets']}** {label} market(s) — {label} {outcome}. "
            f"{stats['bets']} bet(s) graded"
            + (f", {fmt_chips(stats['credits'])} paid out." if stats["credits"] else "."),
            ephemeral=True,
        )

    @resolve.command(
        name="trap_deaths",
        description="Settle 'Arena Trap Deaths O/U' markets with the actual count",
    )
    @app_commands.describe(
        actual_count="Number of tributes killed by Gamemaker traps/events"
    )
    @is_admin()
    async def resolve_trap_deaths(
        self, interaction: discord.Interaction, actual_count: int
    ) -> None:
        await self._resolve_count_ou(
            interaction, "ARENA_TRAP_DEATHS_OU", actual_count, "trap death", 1.5
        )

    @resolve.command(
        name="env_deaths",
        description="Settle 'Arena Environmental Deaths O/U' markets with the actual count",
    )
    @app_commands.describe(
        actual_count="Number of tributes killed by the environment (nature/mutts)"
    )
    @is_admin()
    async def resolve_env_deaths(
        self, interaction: discord.Interaction, actual_count: int
    ) -> None:
        await self._resolve_count_ou(
            interaction, "ARENA_ENV_DEATHS_OU", actual_count, "environmental death", 1.5
        )

    async def _resolve_count_ou(
        self,
        interaction: discord.Interaction,
        market_type: str,
        actual_count: int,
        label: str,
        default_line: float,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        _ou_notifs: list[dict] = []
        _ou_token = _notif_buffer.set(_ou_notifs)
        async with get_session() as session:
            res = await session.execute(
                select(Market).where(
                    Market.type == market_type,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            markets = res.scalars().all()
            for m in markets:
                line = m.ou_line if m.ou_line is not None else default_line
                result = (
                    actual_count > line
                    if m.ou_side == "OVER"
                    else actual_count <= line
                )
                await _resolve_market(session, m, result)
        _notif_buffer.reset(_ou_token)
        await _send_resolution_dms(self.bot, _ou_notifs)
        await interaction.followup.send(
            f"✅ Settled **{len(markets)}** {label} O/U market(s) against an actual "
            f"count of **{actual_count}**.",
            ephemeral=True,
        )

    @resolve.command(
        name="type",
        description="Resolve EVERY open/closed market of one type to the same outcome",
    )
    @app_commands.describe(
        market_type="Market type to resolve in bulk",
        result="Outcome applied to all markets of this type",
    )
    @app_commands.choices(
        result=[
            app_commands.Choice(name="WIN  — all bettors WIN", value="WIN"),
            app_commands.Choice(name="LOSS — all bettors LOSE", value="LOSS"),
            app_commands.Choice(name="VOID — refund all bets", value="VOID"),
        ]
    )
    @app_commands.autocomplete(market_type=market_type_autocomplete)
    @is_admin()
    async def resolve_type(
        self,
        interaction: discord.Interaction,
        market_type: str,
        result: app_commands.Choice[str],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        bool_result: bool | None = {"WIN": True, "LOSS": False, "VOID": None}[
            result.value
        ]
        _type_notifs: list[dict] = []
        _type_token = _notif_buffer.set(_type_notifs)
        async with get_session() as session:
            stats = await _resolve_markets_of_type(session, market_type, bool_result)
        _notif_buffer.reset(_type_token)
        await _send_resolution_dms(self.bot, _type_notifs)
        if stats["markets"] == 0:
            await interaction.followup.send(
                f"No open/closed markets of type `{market_type}` to resolve.",
                ephemeral=True,
            )
            return
        color = (
            0x4CAF50
            if result.value == "WIN"
            else (0xCF4444 if result.value == "LOSS" else 0x888888)
        )
        embed = discord.Embed(
            title=f"Bulk Resolve: {result.value}", color=color
        )
        embed.add_field(name="Market Type", value=f"`{market_type}`", inline=False)
        embed.add_field(name="Markets Resolved", value=str(stats["markets"]))
        embed.add_field(name="Bets Graded", value=str(stats["bets"]))
        if stats["credits"] > 0:
            embed.add_field(name="Chips Paid Out", value=fmt_chips(stats["credits"]))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Live running-game statistics ───────────────────────────────────────────

    async def _load_stats_context(self):
        """Return (all tributes, current phase name, game_active flag)."""
        async with get_read_session() as session:
            tributes = list((await session.execute(select(Tribute))).scalars().all())
            phase_raw = await get_setting("current_phase_id")
            active_raw = await get_setting("game_active")
            phase_name = "—"
            if phase_raw:
                pid = json.loads(phase_raw)
                if pid:
                    p = await session.get(BettingPhase, pid)
                    if p:
                        phase_name = p.name
        active = json.loads(active_raw) if active_raw else False
        return tributes, phase_name, active

    @stats.command(
        name="overview",
        description="Snapshot of the running Games — survivors, eliminations, kills",
    )
    @is_admin()
    async def stats_overview(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tributes, phase_name, active = await self._load_stats_context()
        if not tributes:
            await interaction.followup.send("No tributes in play.", ephemeral=True)
            return

        alive = [t for t in tributes if t.status == "ALIVE"]
        victors = [t for t in tributes if t.status == "VICTOR"]
        dead = [t for t in tributes if t.status == "DEAD"]
        total_kills = sum(t.kills for t in tributes)

        embed = discord.Embed(
            title="🏟️ Games — Live Overview",
            description=(
                f"**Phase:** {phase_name}  •  "
                f"**Status:** {'🟢 Running' if active else '⚪ Idle'}"
            ),
            color=0xC9A227,
        )
        embed.add_field(name="Tributes", value=str(len(tributes)))
        embed.add_field(name="Alive", value=str(len(alive)))
        embed.add_field(
            name="Fallen", value=str(len(dead) + len(victors))
        )
        embed.add_field(name="Total Kills", value=str(total_kills))

        if victors:
            embed.add_field(
                name="👑 Victor",
                value=", ".join(_tribute_label(v) for v in victors),
                inline=False,
            )

        # Top killers
        killers = sorted(
            [t for t in tributes if t.kills > 0], key=lambda t: -t.kills
        )
        if killers:
            top = killers[: min(8, len(killers))]
            embed.add_field(
                name="🔪 Top Killers",
                value="\n".join(
                    f"`{t.kills:>2}` {_tribute_label(t)}" for t in top
                ),
                inline=False,
            )

        # Eliminations, most recent first (smallest placement among the dead died last)
        eliminated = sorted(dead, key=lambda t: (t.placement or 9999))
        if eliminated:
            lines = []
            for t in eliminated[:12]:
                place = f"{t.placement}." if t.placement else "—"
                killer = next(
                    (k for k in tributes if k.id == t.killed_by_id), None
                )
                by = f" ← {_tribute_label(killer)}" if killer else ""
                cause = f" ({t.death_cause})" if t.death_cause else ""
                lines.append(f"`{place:>3}` {_tribute_label(t)}{cause}{by}")
            _chunk_lines_into_fields(embed, "🪦 Recent Eliminations", lines)

        # Death-cause breakdown
        causes: dict[str, int] = {}
        for t in dead:
            if t.death_cause:
                causes[t.death_cause] = causes.get(t.death_cause, 0) + 1
        if causes:
            embed.add_field(
                name="Death Causes",
                value="  •  ".join(
                    f"{c}: {n}" for c, n in sorted(causes.items(), key=lambda x: -x[1])
                ),
                inline=False,
            )
        embed.set_footer(
            text="Detail: /admin stats kills • /admin stats districts • /admin stats alliances"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(
        name="kills",
        description="Kill leaderboard and full kill feed (who killed whom, how)",
    )
    @is_admin()
    async def stats_kills(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tributes, _, _ = await self._load_stats_context()
        if not tributes:
            await interaction.followup.send("No tributes in play.", ephemeral=True)
            return
        by_id = {t.id: t for t in tributes}

        embed = discord.Embed(
            title="🔪 Kill Tracker",
            description=f"Total kills recorded: **{sum(t.kills for t in tributes)}**",
            color=0xCF4444,
        )

        # Leaderboard
        killers = sorted(
            [t for t in tributes if t.kills > 0], key=lambda t: -t.kills
        )
        if killers:
            lines = []
            for t in killers:
                victims = [
                    v for v in tributes if v.killed_by_id == t.id
                ]
                vstr = (
                    " — " + ", ".join(_tribute_label(v) for v in victims)
                    if victims
                    else ""
                )
                lines.append(f"`{t.kills:>2}` {_tribute_label(t)}{vstr}")
            _chunk_lines_into_fields(embed, "Leaderboard", lines)
        else:
            embed.add_field(name="Leaderboard", value="No kills yet.", inline=False)

        # Full kill feed (chronological-ish: earliest deaths have highest placement)
        fallen = [t for t in tributes if t.status in ("DEAD", "VICTOR")]
        feed_lines = []
        for t in sorted(fallen, key=lambda t: -(t.placement or 0)):
            killer = by_id.get(t.killed_by_id) if t.killed_by_id else None
            cause = t.death_cause or "Unknown"
            if killer:
                feed_lines.append(
                    f"💀 {_tribute_label(t)} — killed by {_tribute_label(killer)} ({cause})"
                )
            else:
                feed_lines.append(f"💀 {_tribute_label(t)} — {cause} (no killer)")
        _chunk_lines_into_fields(
            embed, "Kill Feed", feed_lines, empty="No deaths yet."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(
        name="districts",
        description="Per-district breakdown — survivors, kills, training scores",
    )
    @is_admin()
    async def stats_districts(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tributes, _, _ = await self._load_stats_context()
        if not tributes:
            await interaction.followup.send("No tributes in play.", ephemeral=True)
            return

        embed = discord.Embed(title="🏙️ District Stats", color=0x4C7BAF)
        lines = []
        for d in sorted({t.district for t in tributes}):
            members = [t for t in tributes if t.district == d]
            alive = sum(1 for t in members if t.status == "ALIVE")
            kills = sum(t.kills for t in members)
            scores = [t.training_score for t in members if t.training_score is not None]
            score_str = f" • score {sum(scores)}" if scores else ""
            lines.append(
                f"`D{d:>2}` {alive}/{len(members)} alive • {kills} kill(s){score_str}"
            )
        _chunk_lines_into_fields(embed, "Districts", lines)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(
        name="alliances",
        description="Per-alliance breakdown — members, survivors, kills",
    )
    @is_admin()
    async def stats_alliances(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            tributes = list((await session.execute(select(Tribute))).scalars().all())
            alliances = list(
                (await session.execute(select(Alliance).order_by(Alliance.name)))
                .scalars()
                .all()
            )
        if not alliances:
            await interaction.followup.send(
                "No alliances have been formed.", ephemeral=True
            )
            return

        embed = discord.Embed(title="🤝 Alliance Stats", color=0x7B4CAF)
        for a in alliances:
            members = [t for t in tributes if t.alliance_id == a.id]
            if not members:
                continue
            alive = sum(1 for t in members if t.status == "ALIVE")
            kills = sum(t.kills for t in members)
            roster = ", ".join(
                f"{_tribute_label(t)}{'' if t.status == 'ALIVE' else ' †'}"
                for t in members
            )
            embed.add_field(
                name=f"{a.name} — {alive}/{len(members)} alive • {kills} kill(s)",
                value=roster[:1024],
                inline=False,
            )
        # Solo (unallied) tributes summary
        solo = [t for t in tributes if t.alliance_id is None]
        if solo:
            alive_solo = sum(1 for t in solo if t.status == "ALIVE")
            embed.add_field(
                name=f"Solo / Unallied — {alive_solo}/{len(solo)} alive",
                value=", ".join(_tribute_label(t) for t in solo)[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(
        name="reset_confirm",
        description="DANGER: Delete all tributes, bets, and markets",
    )
    @app_commands.describe(confirm="Type 'yes' to confirm the full reset")
    @is_admin()
    async def game_reset_confirm(
        self, interaction: discord.Interaction, confirm: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if confirm.lower() != "yes":
            await interaction.followup.send("Reset cancelled.", ephemeral=True)
            return
        async with get_session() as session:
            # Null out self-referential FK before deleting tributes
            trib_result = await session.execute(select(Tribute))
            for t in trib_result.scalars().all():
                t.killed_by_id = None
            await session.flush()

            for model in [
                PendingParlayLeg,
                Bet,
                Parlay,
                Market,
                ModifierAssignment,
                Tribute,
            ]:
                result = await session.execute(select(model))
                for row in result.scalars().all():
                    await session.delete(row)

        await set_setting("game_active", False)
        await set_setting("current_phase_id", None)
        await set_setting("arena_type", None)
        await set_setting("sponsor_state", None)
        await interaction.followup.send(
            "All tributes, bets, parlays, and markets have been reset.", ephemeral=True
        )

    # ── PRE-BUILT PARLAY COMMANDS ─────────────────────────────────────────────

    async def _template_markets(self, session, template_id: int):
        """Return ``(template, [(leg, market), ...])`` ordered by sort_order, or
        ``(None, [])`` if the template doesn't exist."""
        tpl = await session.get(ParlayTemplate, template_id)
        if tpl is None:
            return None, []
        leg_result = await session.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == template_id)
            .order_by(ParlayTemplateLeg.sort_order)
        )
        pairs = []
        for leg in leg_result.scalars().all():
            mkt = await session.get(Market, leg.market_id)
            pairs.append((leg, mkt))
        return tpl, pairs

    @parlay.command(name="create", description="Create an empty pre-built parlay template")
    @app_commands.describe(name="Display name", description="Short description (optional)")
    @is_admin()
    async def parlay_create(
        self, interaction: discord.Interaction, name: str, description: str | None = None
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            tpl = ParlayTemplate(name=name[:100], description=description, source="ADMIN")
            session.add(tpl)
            await session.flush()
            tpl_id = tpl.id
        await interaction.followup.send(
            f"Created parlay template **#{tpl_id} — {name}**.\n"
            f"Add legs with `/admin parlay addleg template_id:{tpl_id} market:…`.",
            ephemeral=True,
        )

    @parlay.command(
        name="save_slip",
        description="Save your current /parlay slip as a pre-built tailable parlay",
    )
    @app_commands.describe(name="Display name", description="Short description (optional)")
    @is_admin()
    async def parlay_save_slip(
        self, interaction: discord.Interaction, name: str, description: str | None = None
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(PendingParlayLeg.user_id == interaction.user.id)
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()
            if len(legs) < 2:
                await interaction.followup.send(
                    "Your `/parlay` slip needs at least 2 legs to save. "
                    "Build it with `/parlay add` first.",
                    ephemeral=True,
                )
                return
            tpl = ParlayTemplate(
                name=name[:100], description=description, source="ADMIN", active=True
            )
            session.add(tpl)
            await session.flush()
            for i, leg in enumerate(legs):
                session.add(ParlayTemplateLeg(
                    template_id=tpl.id, market_id=leg.market_id, sort_order=i
                ))
            tpl_id = tpl.id
            count = len(legs)
        await interaction.followup.send(
            f"Saved template **#{tpl_id} — {name}** with **{count}** legs from your slip. "
            "It's now live on the tailing board.",
            ephemeral=True,
        )

    @parlay.command(name="addleg", description="Add a market leg to a parlay template")
    @app_commands.describe(template_id="Template to edit", market="Market to add as a leg")
    @app_commands.autocomplete(
        template_id=parlay_template_autocomplete, market=open_market_autocomplete
    )
    @is_admin()
    async def parlay_addleg(
        self, interaction: discord.Interaction, template_id: str, market: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not template_id.isdigit() or not market.isdigit():
            await interaction.followup.send("Pick both from the autocomplete lists.", ephemeral=True)
            return
        async with get_session() as session:
            tpl, pairs = await self._template_markets(session, int(template_id))
            if tpl is None:
                await interaction.followup.send("Template not found.", ephemeral=True)
                return
            new_mkt = await session.get(Market, int(market))
            if new_mkt is None:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            if any(leg.market_id == new_mkt.id for leg, _ in pairs):
                await interaction.followup.send("That market is already a leg.", ephemeral=True)
                return
            existing_mkts = [m for _, m in pairs if m is not None]
            tribute_by_id = await tribute_lookup_for_markets(session, existing_mkts + [new_mkt])
            conflict = _parlay_conflict(existing_mkts, new_mkt, tribute_by_id)
            if conflict:
                await interaction.followup.send(conflict, ephemeral=True)
                return
            session.add(ParlayTemplateLeg(
                template_id=tpl.id, market_id=new_mkt.id, sort_order=len(pairs)
            ))
            label = new_mkt.label
            name = tpl.name
        await interaction.followup.send(
            f"Added **{label}** to template **#{template_id} — {name}** "
            f"(now {len(pairs) + 1} legs).",
            ephemeral=True,
        )

    @parlay.command(name="removeleg", description="Remove a leg from a parlay template by position")
    @app_commands.describe(template_id="Template to edit", leg_number="Leg number (see /admin parlay list)")
    @app_commands.autocomplete(template_id=parlay_template_autocomplete)
    @is_admin()
    async def parlay_removeleg(
        self,
        interaction: discord.Interaction,
        template_id: str,
        leg_number: app_commands.Range[int, 1, 10],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not template_id.isdigit():
            await interaction.followup.send("Pick a template from the list.", ephemeral=True)
            return
        async with get_session() as session:
            tpl, pairs = await self._template_markets(session, int(template_id))
            if tpl is None:
                await interaction.followup.send("Template not found.", ephemeral=True)
                return
            if leg_number > len(pairs):
                await interaction.followup.send(
                    f"That template only has {len(pairs)} leg(s).", ephemeral=True
                )
                return
            leg, mkt = pairs[leg_number - 1]
            label = mkt.label if mkt else f"market {leg.market_id}"
            await session.delete(leg)
        await interaction.followup.send(
            f"Removed leg {leg_number} (**{label}**) from template #{template_id}.",
            ephemeral=True,
        )

    @parlay.command(name="list", description="List all pre-built parlay templates")
    @is_admin()
    async def parlay_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(
                select(ParlayTemplate).order_by(ParlayTemplate.source, ParlayTemplate.id)
            )
            templates = result.scalars().all()
            rendered: list[tuple[ParlayTemplate, list[tuple], int | None, bool]] = []
            for tpl in templates:
                _, pairs = await self._template_markets(session, tpl.id)
                odds_list = [m.odds for _, m in pairs if m is not None]
                all_open = bool(pairs) and all(
                    m is not None and m.status == "OPEN" for _, m in pairs
                )
                combined = combined_american(odds_list) if len(odds_list) >= 2 else None
                rendered.append((tpl, pairs, combined, all_open))

        if not rendered:
            await interaction.followup.send(
                "No parlay templates yet. Create one with `/admin parlay create` "
                "or `/admin parlay save_slip`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🎟️ Pre-Built Parlay Templates", color=0xC9A227)
        for tpl, pairs, combined, all_open in rendered[:25]:
            status = "live" if (tpl.active and all_open) else (
                "hidden" if not tpl.active else "not all legs open"
            )
            head = f"#{tpl.id} · {tpl.source}"
            if combined is not None:
                head += f" · {fmt_odds(combined)}"
            head += f" · {status}"
            leg_lines = [
                f"`{i}.` {(m.label if m else 'missing')} ({fmt_odds(m.odds) if m else '—'})"
                for i, (_, m) in enumerate(pairs, 1)
            ] or ["_(no legs yet)_"]
            value = f"{head}\n" + "\n".join(leg_lines)
            embed.add_field(name=tpl.name[:256], value=value[:1024], inline=False)
        embed.set_footer(text="Templates show on the board only while every leg is OPEN.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @parlay.command(name="toggle", description="Show/hide a parlay template on the tailing board")
    @app_commands.describe(template_id="Template to toggle")
    @app_commands.autocomplete(template_id=parlay_template_autocomplete)
    @is_admin()
    async def parlay_toggle(self, interaction: discord.Interaction, template_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not template_id.isdigit():
            await interaction.followup.send("Pick a template from the list.", ephemeral=True)
            return
        async with get_session() as session:
            tpl = await session.get(ParlayTemplate, int(template_id))
            if tpl is None:
                await interaction.followup.send("Template not found.", ephemeral=True)
                return
            tpl.active = not tpl.active
            state = "visible" if tpl.active else "hidden"
            name = tpl.name
        await interaction.followup.send(
            f"Template **#{template_id} — {name}** is now **{state}**.", ephemeral=True
        )

    @parlay.command(
        name="set-description",
        description="Edit the description shown for a parlay (works on auto-generated ones too)",
    )
    @app_commands.describe(
        template_id="Template to edit",
        description="New description text (use 'none' to clear it)",
    )
    @app_commands.autocomplete(template_id=parlay_template_autocomplete)
    @is_admin()
    async def parlay_set_description(
        self, interaction: discord.Interaction, template_id: str, description: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not template_id.isdigit():
            await interaction.followup.send("Pick a template from the list.", ephemeral=True)
            return
        from bot.lemonade import _fit_description

        async with get_session() as session:
            tpl = await session.get(ParlayTemplate, int(template_id))
            if tpl is None:
                await interaction.followup.send("Template not found.", ephemeral=True)
                return
            if description.strip().lower() == "none":
                tpl.description = ""
            else:
                # Same whitespace-collapse + 2-line fit the board applies to AI
                # blurbs, so a manual description can't overflow the card.
                tpl.description = _fit_description(description)
            name = tpl.name
            new_desc = tpl.description
        await interaction.followup.send(
            f"Updated description for **#{template_id} — {name}**:\n> {new_desc or '_(cleared)_'}",
            ephemeral=True,
        )

    @parlay.command(name="delete", description="Delete a parlay template")
    @app_commands.describe(template_id="Template to delete")
    @app_commands.autocomplete(template_id=parlay_template_autocomplete)
    @is_admin()
    async def parlay_delete(self, interaction: discord.Interaction, template_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not template_id.isdigit():
            await interaction.followup.send("Pick a template from the list.", ephemeral=True)
            return
        async with get_session() as session:
            tpl = await session.get(ParlayTemplate, int(template_id))
            if tpl is None:
                await interaction.followup.send("Template not found.", ephemeral=True)
                return
            name = tpl.name
            await session.delete(tpl)
        await interaction.followup.send(
            f"Deleted template **#{template_id} — {name}**.", ephemeral=True
        )

    @parlay.command(
        name="generate",
        description="Regenerate auto featured parlays from open markets now",
    )
    @app_commands.describe(count="How many themed parlays to generate (default 3, max 10)")
    @is_admin()
    async def parlay_generate(
        self,
        interaction: discord.Interaction,
        count: int = 3,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not _AUTO_PARLAY_GENERATION_ENABLED:
            await interaction.followup.send(
                "Parlay auto-generation is temporarily disabled.", ephemeral=True
            )
            return
        count = max(1, min(count, 10))
        phase_raw = await get_setting("current_phase_id")
        phase_id = json.loads(phase_raw) if phase_raw else None
        async with get_session() as session:
            made = (
                await _try_generate_ai_parlays(session, phase_id, count)
                or await _generate_auto_parlays(session, phase_id, count=count)
            )
        if made:
            await interaction.followup.send(
                f"Generated **{made}** featured parlay(s) from the open markets.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Not enough open markets to build themed parlays right now.", ephemeral=True
            )

    @parlay.command(
        name="ai-lore",
        description="Upload a PDF of district lore so the AI can generate smarter parlays",
    )
    @app_commands.describe(pdf="PDF file with district identity / background descriptions")
    @is_admin()
    async def parlay_ai_lore(
        self,
        interaction: discord.Interaction,
        pdf: discord.Attachment,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not pdf.filename.lower().endswith(".pdf"):
            await interaction.followup.send("Please attach a PDF file.", ephemeral=True)
            return

        import io
        import pypdf

        async with __import__("httpx").AsyncClient(timeout=30.0) as http:
            resp = await http.get(pdf.url)
            resp.raise_for_status()
            pdf_bytes = resp.content

        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            lore_text = "\n\n".join(p.strip() for p in pages if p.strip())
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to read the PDF: {exc}", ephemeral=True
            )
            return

        if not lore_text:
            await interaction.followup.send(
                "The PDF appears to have no extractable text (it may be image-only).",
                ephemeral=True,
            )
            return

        await set_setting("lemonade_district_lore", lore_text)
        await interaction.followup.send(
            f"District lore saved ({len(reader.pages)} page(s), "
            f"{len(lore_text):,} characters). "
            "Run **/admin parlay ai-generate** to use it.",
            ephemeral=True,
        )

    @parlay.command(
        name="ai-generate",
        description="Use local AI (Lemonade) to regenerate featured parlays from district lore and stats",
    )
    @app_commands.describe(
        count="How many parlays to generate (default 3, max 10)",
        clear_existing="Delete the previous auto-generated parlays first (default True). "
        "Set False to add these alongside the current ones.",
    )
    @is_admin()
    async def parlay_ai_generate(
        self,
        interaction: discord.Interaction,
        count: int = 3,
        clear_existing: bool = True,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not _AUTO_PARLAY_GENERATION_ENABLED:
            await interaction.followup.send(
                "Parlay auto-generation is temporarily disabled.", ephemeral=True
            )
            return
        count = max(1, min(count, 10))

        from bot import config as _cfg

        if not await get_setting("lemonade_district_lore"):
            await interaction.followup.send(
                "No district lore loaded yet — upload a PDF first with **/admin parlay ai-lore**.",
                ephemeral=True,
            )
            return

        phase_raw = await get_setting("current_phase_id")
        phase_id = json.loads(phase_raw) if phase_raw else None

        async with get_session() as session:
            made = await _try_generate_ai_parlays(
                session, phase_id, count, replace_existing=clear_existing
            )

        if made:
            kept_note = "" if clear_existing else " (kept the existing ones)"
            await interaction.followup.send(
                f"AI generated **{made}** featured parlay(s){kept_note} using Lemonade "
                f"(`{_cfg.LEMONADE_MODEL}`) with district lore and historical stats.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Lemonade didn't produce any parlays — check that the server is running "
                f"at `{_cfg.LEMONADE_BASE_URL}` and that a model is loaded. "
                "Check bot logs for details.",
                ephemeral=True,
            )

    # ── PHASE COMMANDS ────────────────────────────────────────────────────────

    @phase.command(name="add", description="Add a new betting phase")
    @app_commands.describe(
        name="Phase name (e.g. 'Pre-Games')",
        description="Short description of this phase",
        sort_order="Display order (lower = earlier)",
    )
    @is_admin()
    async def phase_add(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str | None = None,
        sort_order: int = 99,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingPhase).where(BettingPhase.name == name)
            )
            if existing.scalars().first():
                await interaction.followup.send(
                    f"A phase named **{name}** already exists.", ephemeral=True
                )
                return
            p = BettingPhase(name=name, description=description, sort_order=sort_order)
            session.add(p)
            await session.flush()
            pid = p.id

        await interaction.followup.send(
            f"Phase **{name}** created (ID: {pid}).", ephemeral=True
        )

    @phase.command(name="list", description="List all betting phases")
    @is_admin()
    async def phase_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        current_phase_raw = await get_setting("current_phase_id")
        current_phase_id = json.loads(current_phase_raw) if current_phase_raw else None

        async with get_session() as session:
            result = await session.execute(
                select(BettingPhase).order_by(BettingPhase.sort_order)
            )
            phases = result.scalars().all()

        if not phases:
            await interaction.followup.send("No phases found.", ephemeral=True)
            return

        embed = discord.Embed(title="Betting Phases", color=0xC9A227)
        for p in phases:
            active_marker = " ◀ ACTIVE" if p.id == current_phase_id else ""
            embed.add_field(
                name=f"#{p.id} {p.name}{active_marker}",
                value=p.description or "—",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @phase.command(name="delete", description="Delete a betting phase")
    @app_commands.describe(phase_id="Phase to delete")
    @app_commands.autocomplete(phase_id=phase_autocomplete)
    @is_admin()
    async def phase_delete(
        self, interaction: discord.Interaction, phase_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            p = await session.get(BettingPhase, int(phase_id))
            if not p:
                await interaction.followup.send("Phase not found.", ephemeral=True)
                return
            assigned = await session.execute(
                select(Market).where(Market.phase_id == p.id)
            )
            if assigned.scalars().first():
                await interaction.followup.send(
                    f"Cannot delete **{p.name}** — markets are still assigned to it. "
                    "Use `/admin market set_phase` to reassign them first.",
                    ephemeral=True,
                )
                return
            name = p.name
            await session.delete(p)

        await interaction.followup.send(f"Phase **{name}** deleted.", ephemeral=True)

    # ── ALLIANCE COMMANDS ─────────────────────────────────────────────────────

    @alliance.command(name="create", description="Create a new tribute alliance")
    @app_commands.describe(name="Alliance name")
    @is_admin()
    async def alliance_create(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            a = Alliance(name=name)
            session.add(a)
            await session.flush()
            aid = a.id

        await interaction.followup.send(
            f"Alliance **{name}** created (ID: {aid}).", ephemeral=True
        )

    @alliance.command(name="add_tribute", description="Add a tribute to an alliance")
    @app_commands.describe(alliance_id="Alliance to join", tribute_id="Tribute to add")
    @app_commands.autocomplete(
        alliance_id=alliance_autocomplete, tribute_id=unallied_tribute_autocomplete
    )
    @is_admin()
    async def alliance_add_tribute(
        self,
        interaction: discord.Interaction,
        alliance_id: str,
        tribute_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            a = await session.get(Alliance, int(alliance_id))
            t = await session.get(Tribute, int(tribute_id))
            if not a:
                await interaction.followup.send("Alliance not found.", ephemeral=True)
                return
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if t.alliance_id is not None:
                if t.alliance_id == a.id:
                    await interaction.followup.send(
                        f"**{t.name}** is already in **{a.name}**.", ephemeral=True
                    )
                else:
                    current = await session.get(Alliance, t.alliance_id)
                    current_name = current.name if current else "another alliance"
                    await interaction.followup.send(
                        f"**{t.name}** is already in **{current_name}**. Remove them "
                        f"first with `/admin alliance remove_tribute`.",
                        ephemeral=True,
                    )
                return
            t.alliance_id = a.id
            await session.flush()

            all_t_result = await session.execute(select(Tribute))
            all_tributes = list(all_t_result.scalars().all())
            members = [m for m in all_tributes if m.alliance_id == a.id]
            markets_created = 0
            if len(members) == 2:
                markets_created = await _auto_create_alliance_markets(
                    session, a, all_tributes
                )

            await _recalculate_markets(session)
            tname, aname = t.name, a.name

        msg = (
            f"**{tname}** added to alliance **{aname}**. Open market odds recalculated."
        )
        if markets_created:
            msg += f"\n{markets_created} alliance markets created (status: CLOSED)."
        await interaction.followup.send(msg, ephemeral=True)

    @alliance.command(
        name="remove_tribute", description="Remove a tribute from their alliance"
    )
    @app_commands.describe(tribute_id="Tribute to remove from their alliance")
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def alliance_remove_tribute(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if t.alliance_id is None:
                await interaction.followup.send(
                    f"**{t.name}** is not in an alliance.", ephemeral=True
                )
                return
            t.alliance_id = None
            name = t.name
            await _recalculate_markets(session)

        await interaction.followup.send(
            f"**{name}** removed from their alliance. Open market odds recalculated.",
            ephemeral=True,
        )

    @alliance.command(
        name="delete", description="Delete an alliance (removes all members from it)"
    )
    @app_commands.describe(alliance_id="Alliance to delete")
    @app_commands.autocomplete(alliance_id=alliance_autocomplete)
    @is_admin()
    async def alliance_delete(
        self, interaction: discord.Interaction, alliance_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            a = await session.get(Alliance, int(alliance_id))
            if not a:
                await interaction.followup.send("Alliance not found.", ephemeral=True)
                return
            t_result = await session.execute(
                select(Tribute).where(Tribute.alliance_id == a.id)
            )
            for t in t_result.scalars().all():
                t.alliance_id = None
            name = a.name
            aid = a.id

            # Void all open/closed alliance markets for this alliance
            am_result = await session.execute(
                select(Market).where(
                    Market.type.in_(_ALLIANCE_MARKET_TYPES),
                    Market.placement_num == aid,
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            voided = 0
            for mkt in am_result.scalars().all():
                await _resolve_market(session, mkt, None)
                voided += 1

            await session.delete(a)
            await _recalculate_markets(session)

        msg = f"Alliance **{name}** deleted and all members unassigned."
        if voided:
            msg += f"\n{voided} alliance market(s) voided."
        await interaction.followup.send(msg, ephemeral=True)

    # ── HISTORY COMMANDS ──────────────────────────────────────────────────────

    @history.command(name="games", description="Set the total number of past Games")
    @app_commands.describe(count="Total number of Games played in server history")
    @is_admin()
    async def history_games(self, interaction: discord.Interaction, count: int) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            row = await session.get(GameSetting, "num_games")
            if row:
                row.value = json.dumps(count)
            else:
                session.add(GameSetting(key="num_games", value=json.dumps(count)))
            await _recalculate_markets(session)
        await interaction.followup.send(
            f"Total past Games set to **{count}**. Odds recalculated.", ephemeral=True
        )

    @history.command(name="arena", description="Set historical arena type counts")
    @app_commands.describe(
        artificial="Past Games in artificial arenas",
        natural="Past Games in natural arenas",
    )
    @is_admin()
    async def history_arena(
        self,
        interaction: discord.Interaction,
        artificial: int | None = None,
        natural: int | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if artificial is None and natural is None:
            await interaction.followup.send(
                "Provide at least one arena count to update.", ephemeral=True
            )
            return
        async with get_session() as session:
            if artificial is not None:
                row = await session.get(GameSetting, "arena_artificial_count")
                if row:
                    row.value = json.dumps(artificial)
                else:
                    session.add(
                        GameSetting(
                            key="arena_artificial_count", value=json.dumps(artificial)
                        )
                    )
            if natural is not None:
                row = await session.get(GameSetting, "arena_natural_count")
                if row:
                    row.value = json.dumps(natural)
                else:
                    session.add(
                        GameSetting(
                            key="arena_natural_count", value=json.dumps(natural)
                        )
                    )

        art = artificial if artificial is not None else "unchanged"
        nat = natural if natural is not None else "unchanged"
        await interaction.followup.send(
            f"Arena history updated — Artificial: **{art}** | Natural: **{nat}**.",
            ephemeral=True,
        )

    @history.command(
        name="set", description="Update aggregate historical stats for a district"
    )
    @app_commands.describe(
        district="District number (1–12)",
        wins="Games this district has won",
        victor_male_count="Male victor count",
        victor_female_count="Female victor count",
        runner_up_finishes="Runner-up finishes total",
        runner_up_male="Runner-up finishes (male)",
        runner_up_female="Runner-up finishes (female)",
        avg_placement="All-time avg placement",
        avg_placement_last5="Avg placement (last 5 games)",
        top8_finishes="Top-8 finishes total",
        top5_finishes="Top-5 finishes total",
        male_kills="Male kills (total auto-computed)",
        female_kills="Female kills (total auto-computed)",
        bloodbath_kills="Bloodbath kills total",
        bloodbath_deaths="Total district tribute deaths in the bloodbath (all past games)",
        kill_record="Single-game kill record",
        manmade_arena_wins="Wins in artificial arenas",
        avg_training_score_male="Avg score (male)",
        avg_training_score_female="Avg score (female)",
        reputation="1=best odds, 5=worst, 3=neutral",
        funding_level="District funding level (affects win/kill odds)",
    )
    @app_commands.choices(
        funding_level=[
            app_commands.Choice(name="Rich (2.5×)", value="rich"),
            app_commands.Choice(name="Well-Funded (2.25×)", value="well_funded"),
            app_commands.Choice(name="Under-Funded (0.75×)", value="under_funded"),
            app_commands.Choice(name="Poor (0.5×)", value="poor"),
            app_commands.Choice(name="None (clear)", value="none"),
        ]
    )
    @is_admin()
    async def history_set(
        self,
        interaction: discord.Interaction,
        district: app_commands.Range[int, 1, 12],
        wins: int | None = None,
        victor_male_count: int | None = None,
        victor_female_count: int | None = None,
        runner_up_finishes: int | None = None,
        runner_up_male: int | None = None,
        runner_up_female: int | None = None,
        avg_placement: int | None = None,
        avg_placement_last5: int | None = None,
        top8_finishes: int | None = None,
        top5_finishes: int | None = None,
        male_kills: int | None = None,
        female_kills: int | None = None,
        bloodbath_kills: int | None = None,
        bloodbath_deaths: int | None = None,
        kill_record: int | None = None,
        manmade_arena_wins: int | None = None,
        avg_training_score_male: int | None = None,
        avg_training_score_female: int | None = None,
        reputation: app_commands.Range[int, 1, 5] | None = None,
        funding_level: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        all_vals = (
            wins,
            victor_male_count,
            victor_female_count,
            runner_up_finishes,
            runner_up_male,
            runner_up_female,
            avg_placement,
            avg_placement_last5,
            top8_finishes,
            top5_finishes,
            male_kills,
            female_kills,
            bloodbath_kills,
            bloodbath_deaths,
            kill_record,
            manmade_arena_wins,
            avg_training_score_male,
            avg_training_score_female,
            reputation,
            funding_level,
        )
        if all(v is None for v in all_vals):
            await interaction.followup.send(
                "Provide at least one stat to update.", ephemeral=True
            )
            return
        async with get_session() as session:
            record = await session.get(DistrictRecord, district)
            if record is None:
                record = DistrictRecord(district=district)
                session.add(record)
            if wins is not None:
                record.wins = wins
            if victor_male_count is not None:
                record.victor_male_count = victor_male_count
            if victor_female_count is not None:
                record.victor_female_count = victor_female_count
            if runner_up_finishes is not None:
                record.runner_up_finishes = runner_up_finishes
            if runner_up_male is not None:
                record.runner_up_male = runner_up_male
            if runner_up_female is not None:
                record.runner_up_female = runner_up_female
            if avg_placement is not None:
                record.avg_placement = avg_placement
            if avg_placement_last5 is not None:
                record.avg_placement_last5 = avg_placement_last5
            if top8_finishes is not None:
                record.top8_finishes = top8_finishes
            if top5_finishes is not None:
                record.top5_finishes = top5_finishes
            if male_kills is not None:
                record.male_kills = male_kills
            if female_kills is not None:
                record.female_kills = female_kills
            if bloodbath_kills is not None:
                record.bloodbath_kills = bloodbath_kills
            if bloodbath_deaths is not None:
                record.bloodbath_deaths = bloodbath_deaths
            if kill_record is not None:
                record.kill_record = kill_record
            if manmade_arena_wins is not None:
                record.manmade_arena_wins = manmade_arena_wins
            if avg_training_score_male is not None:
                record.avg_training_score_male = avg_training_score_male
            if avg_training_score_female is not None:
                record.avg_training_score_female = avg_training_score_female
            if reputation is not None:
                record.reputation = reputation
            if funding_level is not None:
                record.funding_level = (
                    None if funding_level == "none" else funding_level
                )
            # Auto-compute total_kills from gender-specific columns
            if any(v is not None for v in (record.male_kills, record.female_kills)):
                record.total_kills = (record.male_kills or 0) + (
                    record.female_kills or 0
                )
            # Auto-compute avg_training_score from gender-specific scores
            _ts_scores = [
                v
                for v in (
                    record.avg_training_score_male,
                    record.avg_training_score_female,
                )
                if v is not None
            ]
            if _ts_scores:
                record.avg_training_score = round(sum(_ts_scores) / len(_ts_scores))
            await _recalculate_markets(session)
        await interaction.followup.send(
            f"District {district} history updated. Odds recalculated.", ephemeral=True
        )

    @history.command(
        name="list", description="View aggregate historical stats for all districts"
    )
    @is_admin()
    async def history_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            result = await session.execute(
                select(DistrictRecord).order_by(DistrictRecord.district)
            )
            records = list(result.scalars().all())
            num_games_row = await session.get(GameSetting, "num_games")
            num_games = int(json.loads(num_games_row.value)) if num_games_row else 0
            art_row = await session.get(GameSetting, "arena_artificial_count")
            nat_row = await session.get(GameSetting, "arena_natural_count")
            art_count = int(json.loads(art_row.value)) if art_row else 0
            nat_count = int(json.loads(nat_row.value)) if nat_row else 0

        total_arena = art_count + nat_count
        if total_arena > 0:
            arena_str = (
                f"Arena ratio — Artificial: **{art_count}** ({art_count / total_arena * 100:.1f}%) | "
                f"Natural: **{nat_count}** ({nat_count / total_arena * 100:.1f}%)"
            )
        else:
            arena_str = "Arena ratio — Artificial: **—** | Natural: **—**"

        national_kill_record = max(
            (r.kill_record for r in records if r.kill_record is not None),
            default=None,
        )
        bb_death_records = [r for r in records if r.bloodbath_deaths is not None]
        global_bb_avg = (
            sum(r.bloodbath_deaths / num_games for r in bb_death_records)
            / len(bb_death_records)
            if bb_death_records and num_games > 0
            else None
        )
        view = HistoryPageView(
            records, num_games, arena_str, national_kill_record, global_bb_avg
        )
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

    @history.command(name="reset", description="Clear historical stats for a district")
    @app_commands.describe(district="District to reset")
    @is_admin()
    async def history_reset(
        self,
        interaction: discord.Interaction,
        district: app_commands.Range[int, 1, 12],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            record = await session.get(DistrictRecord, district)
            if record is None:
                record = DistrictRecord(district=district)
                session.add(record)
            else:
                record.wins = None
                record.victor_male_count = None
                record.victor_female_count = None
                record.runner_up_finishes = None
                record.runner_up_male = None
                record.runner_up_female = None
                record.avg_placement = None
                record.avg_placement_last5 = None
                record.top8_finishes = None
                record.top5_finishes = None
                record.total_kills = None
                record.male_kills = None
                record.female_kills = None
                record.bloodbath_kills = None
                record.bloodbath_deaths = None
                record.kill_record = None
                record.manmade_arena_wins = None
                record.avg_training_score = None
                record.avg_training_score_male = None
                record.avg_training_score_female = None
                record.reputation = None
                record.funding_level = None
            await _recalculate_markets(session)
        await interaction.followup.send(
            f"District {district} history cleared. Odds recalculated.", ephemeral=True
        )

    # ── MODIFIER COMMANDS ─────────────────────────────────────────────────────

    @modifier.command(name="create", description="Create a reusable odds modifier")
    @app_commands.describe(
        label="Modifier name (e.g. 'Career Training')",
        weight="Prob. multiplier (1.5=+50%, 0.75=-25%)",
    )
    @is_admin()
    async def modifier_create(
        self,
        interaction: discord.Interaction,
        label: str,
        weight: float,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if weight <= 0:
            await interaction.followup.send(
                "Weight must be greater than 0.", ephemeral=True
            )
            return
        async with get_session() as session:
            mod = Modifier(label=label, weight=weight)
            session.add(mod)
            await session.flush()
            mid = mod.id

        direction = (
            f"+{round((weight - 1) * 100)}%"
            if weight >= 1
            else f"{round((weight - 1) * 100)}%"
        )
        await interaction.followup.send(
            f"Modifier **{label}** created (ID: {mid}) — ×{weight} ({direction}). "
            f"Use `/modifier assign` to apply it to a tribute, district, or alliance.",
            ephemeral=True,
        )

    @modifier.command(
        name="delete", description="Delete a modifier and all its assignments"
    )
    @app_commands.describe(modifier_id="Modifier to delete")
    @app_commands.autocomplete(modifier_id=modifier_autocomplete)
    @is_admin()
    async def modifier_delete(
        self, interaction: discord.Interaction, modifier_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mod = await session.get(Modifier, int(modifier_id))
            if not mod:
                await interaction.followup.send("Modifier not found.", ephemeral=True)
                return
            label = mod.label
            await session.delete(mod)
            await _recalculate_markets(session)

        await interaction.followup.send(
            f"Modifier **{label}** and all its assignments deleted. Open odds recalculated.",
            ephemeral=True,
        )

    @modifier.command(
        name="assign", description="Apply a modifier to a tribute, district, or alliance"
    )
    @app_commands.describe(
        modifier_id="Modifier to assign",
        tribute_id="Tribute (blank = district- or alliance-wide)",
        district="District number (blank = tribute- or alliance-specific)",
        alliance_id="Alliance (blank = tribute- or district-specific)",
    )
    @app_commands.autocomplete(
        modifier_id=modifier_autocomplete,
        tribute_id=tribute_autocomplete,
        alliance_id=alliance_autocomplete,
    )
    @is_admin()
    async def modifier_assign(
        self,
        interaction: discord.Interaction,
        modifier_id: str,
        tribute_id: str | None = None,
        district: app_commands.Range[int, 1, 12] | None = None,
        alliance_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        provided = sum(x is not None for x in (tribute_id, district, alliance_id))
        if provided == 0:
            await interaction.followup.send(
                "Provide exactly one of: tribute, district, or alliance.",
                ephemeral=True,
            )
            return
        if provided > 1:
            await interaction.followup.send(
                "Provide only one of tribute, district, or alliance — not multiple.",
                ephemeral=True,
            )
            return
        async with get_session() as session:
            mod = await session.get(Modifier, int(modifier_id))
            if not mod:
                await interaction.followup.send("Modifier not found.", ephemeral=True)
                return
            tid = int(tribute_id) if tribute_id else None
            aid_int = int(alliance_id) if alliance_id else None
            if tid is not None:
                t = await session.get(Tribute, tid)
                if not t:
                    await interaction.followup.send(
                        "Tribute not found.", ephemeral=True
                    )
                    return
                scope_str = f"tribute **{t.name}** (D{t.district}{t.display_gender})"
            elif aid_int is not None:
                a = await session.get(Alliance, aid_int)
                if not a:
                    await interaction.followup.send(
                        "Alliance not found.", ephemeral=True
                    )
                    return
                scope_str = f"alliance **{a.name}**"
            else:
                scope_str = f"District {district}"

            assignment = ModifierAssignment(
                modifier_id=mod.id,
                tribute_id=tid,
                district=district,
                alliance_id=aid_int,
            )
            session.add(assignment)
            await session.flush()
            assign_id = assignment.id
            mod_label = mod.label
            weight = mod.weight
            await _recalculate_markets(session)

        direction = (
            f"+{round((weight - 1) * 100)}%"
            if weight >= 1
            else f"{round((weight - 1) * 100)}%"
        )
        await interaction.followup.send(
            f"Modifier **{mod_label}** (×{weight}, {direction}) assigned to {scope_str} "
            f"(assignment ID: {assign_id}). Open odds recalculated.",
            ephemeral=True,
        )

    @modifier.command(
        name="bulk_assign",
        description="Apply a modifier to multiple targets at once",
    )
    @app_commands.describe(
        modifier_id="Modifier to assign",
        targets=(
            "Comma-separated targets: districts (D1, D2), tributes (D1F, D2M, D3NB),"
            " or alliance names (Salvos Grads, The Legion)"
        ),
    )
    @app_commands.autocomplete(modifier_id=modifier_autocomplete)
    @is_admin()
    async def modifier_bulk_assign(
        self,
        interaction: discord.Interaction,
        modifier_id: str,
        targets: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        tokens = [t.strip() for t in targets.split(",") if t.strip()]
        if not tokens:
            await interaction.followup.send("No targets provided.", ephemeral=True)
            return

        async with get_session() as session:
            mod = await session.get(Modifier, int(modifier_id))
            if not mod:
                await interaction.followup.send("Modifier not found.", ephemeral=True)
                return

            all_alliances_result = await session.execute(select(Alliance))
            alliance_map: dict[str, Alliance] = {
                a.name.lower(): a for a in all_alliances_result.scalars().all()
            }
            all_tributes_result = await session.execute(select(Tribute))
            tributes: list[Tribute] = all_tributes_result.scalars().all()

            assigned: list[str] = []
            skipped: list[str] = []

            for token in tokens:
                dm = re.fullmatch(r"[Dd](\d+)", token)
                if dm:
                    district_num = int(dm.group(1))
                    session.add(
                        ModifierAssignment(
                            modifier_id=mod.id,
                            district=district_num,
                        )
                    )
                    assigned.append(f"District {district_num}")
                    continue

                # Tribute by district+gender: D1F, D2M, D3NB
                tm = re.fullmatch(r"[Dd](\d+)(NB|nb|F|f|M|m)", token)
                if tm:
                    district_num = int(tm.group(1))
                    gender_code = tm.group(2).upper()
                    match = next(
                        (
                            t for t in tributes
                            if t.district == district_num
                            and (
                                (gender_code == "NB" and t.non_binary)
                                or (gender_code == "F" and t.gender == "F" and not t.non_binary)
                                or (gender_code == "M" and t.gender == "M" and not t.non_binary)
                            )
                        ),
                        None,
                    )
                    if match is None:
                        skipped.append(f"{token} (tribute not found)")
                        continue
                    session.add(
                        ModifierAssignment(
                            modifier_id=mod.id,
                            tribute_id=match.id,
                        )
                    )
                    assigned.append(f"{match.name} (D{district_num}{match.display_gender})")
                    continue

                # Alliance by name
                alliance = alliance_map.get(token.lower())
                if alliance:
                    session.add(
                        ModifierAssignment(
                            modifier_id=mod.id,
                            alliance_id=alliance.id,
                        )
                    )
                    assigned.append(f"alliance **{alliance.name}**")
                    continue

                skipped.append(f"{token} (not recognised)")

            if not assigned:
                await interaction.followup.send(
                    f"No valid targets found. Skipped: {', '.join(skipped)}",
                    ephemeral=True,
                )
                return

            await session.flush()
            await _recalculate_markets(session)

        direction = (
            f"+{round((mod.weight - 1) * 100)}%"
            if mod.weight >= 1
            else f"{round((mod.weight - 1) * 100)}%"
        )
        lines = [
            f"Modifier **{mod.label}** (×{mod.weight}, {direction}) assigned to "
            f"{len(assigned)} target(s). Open odds recalculated.",
            "",
            "**Assigned:**",
            *[f"• {a}" for a in assigned],
        ]
        if skipped:
            lines += ["", "**Skipped:**", *[f"• {s}" for s in skipped]]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @modifier.command(
        name="unassign",
        description="Remove a modifier assignment from a tribute or district",
    )
    @app_commands.describe(assignment_id="Assignment to remove")
    @app_commands.autocomplete(assignment_id=modifier_assignment_autocomplete)
    @is_admin()
    async def modifier_unassign(
        self, interaction: discord.Interaction, assignment_id: str
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            assignment = await session.get(ModifierAssignment, int(assignment_id))
            if not assignment:
                await interaction.followup.send("Assignment not found.", ephemeral=True)
                return
            mod = await session.get(Modifier, assignment.modifier_id)
            mod_label = mod.label if mod else "Unknown"
            await session.delete(assignment)
            await _recalculate_markets(session)

        await interaction.followup.send(
            f"Assignment for **{mod_label}** removed. Open odds recalculated.",
            ephemeral=True,
        )

    @modifier.command(
        name="list", description="List all modifiers and their current assignments"
    )
    @is_admin()
    async def modifier_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            mod_result = await session.execute(select(Modifier).order_by(Modifier.id))
            mods = mod_result.scalars().all()
            assign_result = await session.execute(
                select(ModifierAssignment).order_by(ModifierAssignment.modifier_id)
            )
            assignments = assign_result.scalars().all()
            trib_result = await session.execute(select(Tribute))
            tributes = {t.id: t for t in trib_result.scalars().all()}
            alliance_result = await session.execute(select(Alliance))
            alliance_map_list = {a.id: a for a in alliance_result.scalars().all()}

        if not mods:
            await interaction.followup.send("No modifiers defined.", ephemeral=True)
            return

        assign_map: dict[int, list[ModifierAssignment]] = {}
        for a in assignments:
            assign_map.setdefault(a.modifier_id, []).append(a)

        embed = discord.Embed(title="Odds Modifiers", color=0x7B68EE)
        for mod in mods:
            direction = (
                f"+{round((mod.weight - 1) * 100)}%"
                if mod.weight >= 1
                else f"{round((mod.weight - 1) * 100)}%"
            )
            header = f"×{mod.weight} ({direction})"
            assigned = assign_map.get(mod.id, [])
            if assigned:
                lines = []
                for a in assigned:
                    if a.tribute_id is not None:
                        t = tributes.get(a.tribute_id)
                        scope = (
                            f"D{t.district}{t.display_gender} {t.name}"
                            if t
                            else f"Tribute #{a.tribute_id}"
                        )
                    elif a.alliance_id is not None:
                        al = alliance_map_list.get(a.alliance_id)
                        scope = (
                            f"{al.name} (alliance)"
                            if al
                            else f"Alliance #{a.alliance_id}"
                        )
                    else:
                        scope = f"District {a.district} (all)"
                    lines.append(f"• {scope} (assignment ID: {a.id})")
                value = f"{header}\n" + "\n".join(lines)
            else:
                value = f"{header}\n*(unassigned — no effect)*"
            embed.add_field(
                name=f"[ID {mod.id}] {mod.label}", value=value, inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── SETTINGS COMMANDS ─────────────────────────────────────────────────────

    @settings.command(name="cashout", description="Configure global cashout settings")
    @app_commands.describe(
        allowed="Allow early cashout globally",
        rate="Cashout rate 0.0–1.0 (0.65 = 65% profit)",
    )
    @app_commands.choices(
        allowed=[
            app_commands.Choice(name="Allow cashout", value="yes"),
            app_commands.Choice(name="Disallow cashout", value="no"),
        ]
    )
    @is_admin()
    async def settings_cashout(
        self,
        interaction: discord.Interaction,
        allowed: app_commands.Choice[str],
        rate: app_commands.Range[float, 0.0, 1.0] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await set_setting("cashout_allowed", allowed.value == "yes")
        if rate is not None:
            await set_setting("cashout_rate", rate)

        rate_str = f" at **{rate * 100:.0f}%** rate" if rate is not None else ""
        status_str = "**allowed**" if allowed.value == "yes" else "**disabled**"
        await interaction.followup.send(
            f"Early cashout is now {status_str}{rate_str}.", ephemeral=True
        )

    @settings.command(
        name="market_cashout",
        description="Override cashout settings for a specific market",
    )
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @app_commands.choices(
        allowed=[
            app_commands.Choice(name="Allow", value="yes"),
            app_commands.Choice(name="Disallow", value="no"),
        ]
    )
    @is_admin()
    async def settings_market_cashout(
        self,
        interaction: discord.Interaction,
        market_id: str,
        allowed: app_commands.Choice[str],
        rate: app_commands.Range[float, 0.0, 1.0] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            mkt.cashout_allowed = allowed.value == "yes"
            if rate is not None:
                mkt.cashout_rate = rate
            label = mkt.label

        await interaction.followup.send(
            f"Cashout for **{label}** set to {'allowed' if allowed.value == 'yes' else 'disabled'}.",
            ephemeral=True,
        )

    @settings.command(
        name="market_type_cashout",
        description="Override cashout settings for every market of one type",
    )
    @app_commands.describe(
        market_type="Market type to override",
        allowed="Allow early cashout for this market type",
        rate="Cashout rate 0.0–1.0 (0.65 = 65% profit)",
    )
    @app_commands.choices(
        allowed=[
            app_commands.Choice(name="Allow", value="yes"),
            app_commands.Choice(name="Disallow", value="no"),
        ]
    )
    @app_commands.autocomplete(market_type=market_type_autocomplete)
    @is_admin()
    async def settings_market_type_cashout(
        self,
        interaction: discord.Interaction,
        market_type: str,
        allowed: app_commands.Choice[str],
        rate: app_commands.Range[float, 0.0, 1.0] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        raw = await get_setting("cashout_by_type")
        by_type: dict = json.loads(raw) if raw else {}
        entry = {"allowed": allowed.value == "yes"}
        if rate is not None:
            entry["rate"] = rate
        elif market_type in by_type and "rate" in by_type[market_type]:
            entry["rate"] = by_type[market_type]["rate"]
        by_type[market_type] = entry
        await set_setting("cashout_by_type", by_type)

        rate_str = f" at **{rate * 100:.0f}%** rate" if rate is not None else ""
        await interaction.followup.send(
            f"Cashout for market type `{market_type}` set to "
            f"{'allowed' if allowed.value == 'yes' else 'disabled'}{rate_str}.",
            ephemeral=True,
        )

    @settings.command(
        name="parlay_cashout",
        description="Override cashout settings for one specific publicly-tailable parlay",
    )
    @app_commands.autocomplete(parlay_id=public_parlay_autocomplete)
    @app_commands.choices(
        allowed=[
            app_commands.Choice(name="Allow", value="yes"),
            app_commands.Choice(name="Disallow", value="no"),
        ]
    )
    @is_admin()
    async def settings_parlay_cashout(
        self,
        interaction: discord.Interaction,
        parlay_id: str,
        allowed: app_commands.Choice[str],
        rate: app_commands.Range[float, 0.0, 1.0] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            p = await session.get(Parlay, int(parlay_id))
            if not p:
                await interaction.followup.send("Parlay not found.", ephemeral=True)
                return
            p.cashout_allowed = allowed.value == "yes"
            if rate is not None:
                p.cashout_rate = rate

        rate_str = f" at **{rate * 100:.0f}%** rate" if rate is not None else ""
        await interaction.followup.send(
            f"Cashout for **Parlay #{parlay_id}** set to "
            f"{'allowed' if allowed.value == 'yes' else 'disabled'}{rate_str}.",
            ephemeral=True,
        )

    @settings.command(name="chips_give", description="Give chips to a user")
    @app_commands.describe(user="User to give chips to", amount="Amount of chips")
    @is_admin()
    async def chips_give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user, current_guild_id())
            u.chips += amount
            new_bal = u.chips

        await interaction.followup.send(
            f"Gave **{fmt_chips(amount)}** to {user.mention}. New balance: **{fmt_chips(new_bal)}**.",
            ephemeral=True,
        )

    @settings.command(
        name="chips_give_all",
        description="Give chips to all registered users (optionally filtered by role)",
    )
    @app_commands.describe(
        amount="Amount of chips to give each user",
        role="Only award users who have this role (best-effort, uses member cache)",
    )
    @is_admin()
    async def chips_give_all(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1_000_000],
        role: discord.Role | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        guild = interaction.guild
        awarded = 0
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.guild_id == (current_guild_id()))
            )
            for u in result.scalars().all():
                if role is not None:
                    member = guild.get_member(u.discord_id) if guild else None
                    if member is None or role not in member.roles:
                        continue
                u.chips += amount
                awarded += 1

        target = f" with {role.mention}" if role is not None else ""
        await interaction.followup.send(
            f"Gave **{fmt_chips(amount)}** to **{awarded}** user(s){target}.",
            ephemeral=True,
        )

    @settings.command(name="chips_take", description="Take chips from a user")
    @app_commands.describe(user="User to take chips from", amount="Amount to take")
    @is_admin()
    async def chips_take(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user, current_guild_id())
            u.chips = max(0, u.chips - amount)
            new_bal = u.chips

        await interaction.followup.send(
            f"Took **{fmt_chips(amount)}** from {user.mention}. New balance: **{fmt_chips(new_bal)}**.",
            ephemeral=True,
        )

    @settings.command(name="chips_set", description="Set a user's chip balance")
    @app_commands.describe(user="User to update", amount="New chip balance")
    @is_admin()
    async def chips_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 0, 10_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user, current_guild_id())
            u.chips = amount

        await interaction.followup.send(
            f"Set {user.mention}'s balance to **{fmt_chips(amount)}**.", ephemeral=True
        )

    @settings.command(
        name="chips_reset", description="Reset ALL users to the default chip balance"
    )
    @is_admin()
    async def chips_reset(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        default = json.loads(await get_setting("default_chips") or "1000")
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.guild_id == (current_guild_id()))
            )
            for u in result.scalars().all():
                u.chips = default

        await interaction.followup.send(
            f"All user balances reset to {fmt_chips(default)}.", ephemeral=True
        )

    @settings.command(
        name="default_chips",
        description="Set the default starting chip balance for new users",
    )
    @is_admin()
    async def default_chips(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 100, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await set_setting("default_chips", amount)
        await interaction.followup.send(
            f"Default starting balance set to {fmt_chips(amount)}.", ephemeral=True
        )

    @settings.command(
        name="announce",
        description="Post a Capitol announcement to the announcement channel",
    )
    @app_commands.describe(message="The announcement message")
    @is_admin()
    async def announce(self, interaction: discord.Interaction, message: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot import config as cfg
        from bot.database.engine import get_guild_setting

        embed = discord.Embed(
            title="📢 CAPITOL ANNOUNCEMENT",
            description=message,
            color=0xC9A227,
        )
        embed.set_footer(
            text="Panem Sportsbook — May the odds be ever in your favor."
        )

        channel_id = None
        if interaction.guild_id:
            raw = await get_guild_setting(current_guild_id(), "announcement_channel_id")
            if raw:
                channel_id = json.loads(raw)
        if channel_id is None:
            channel_id = cfg.ANNOUNCEMENT_CHANNEL_ID

        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                await channel.send(embed=embed)
                await interaction.followup.send("Announcement posted.", ephemeral=True)
                return

        await interaction.followup.send(embed=embed, ephemeral=True)

    @settings.command(
        name="announce_channel",
        description="Set the channel where /admin settings announce posts go",
    )
    @app_commands.describe(
        channel="Channel for Capitol announcements (leave empty to clear)",
    )
    @is_admin()
    async def announce_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot.database.engine import set_guild_setting
        if channel is None:
            await set_guild_setting(current_guild_id(), "announcement_channel_id", None)
            await interaction.followup.send(
                "Cleared the announcement channel. Announcements will now send as "
                "an ephemeral reply unless ANNOUNCEMENT_CHANNEL_ID is set in the env.",
                ephemeral=True,
            )
            return
        await set_guild_setting(current_guild_id(), "announcement_channel_id", channel.id)
        await interaction.followup.send(
            f"Capitol announcements will now be posted in {channel.mention}.",
            ephemeral=True,
        )

    @settings.command(
        name="admin_role",
        description="Set the role that can use admin commands on this server",
    )
    @app_commands.describe(
        role="Role to grant admin access (leave empty to clear and rely on server Administrator permission)",
    )
    @is_admin()
    async def admin_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot.database.engine import set_guild_setting
        if role is None:
            await set_guild_setting(current_guild_id(), "admin_role_id", None)
            await interaction.followup.send(
                "Cleared the admin role. Only users with the server Administrator "
                "permission can use admin commands (or ADMIN_ROLE_ID env var if set).",
                ephemeral=True,
            )
            return
        await set_guild_setting(current_guild_id(), "admin_role_id", role.id)
        await interaction.followup.send(
            f"Admin commands are now accessible to members with the {role.mention} role.",
            ephemeral=True,
        )

    @settings.command(
        name="withdraw_channel",
        description="Set the channel where /withdraw and /deposit requests are posted",
    )
    @app_commands.describe(
        channel="Channel to post payout requests in (leave empty to clear)",
    )
    @is_admin()
    async def withdraw_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot.database.engine import set_guild_setting
        if channel is None:
            await set_guild_setting(current_guild_id(), "withdraw_channel_id", None)
            await interaction.followup.send(
                "Cleared the withdraw/deposit channel. Requests now fall back to "
                "the WITHDRAW_CHANNEL_ID env var (if set).",
                ephemeral=True,
            )
            return
        await set_guild_setting(current_guild_id(), "withdraw_channel_id", channel.id)
        await interaction.followup.send(
            f"Withdraw/deposit requests will now be posted in {channel.mention}.",
            ephemeral=True,
        )

    @settings.command(
        name="log_channel",
        description="Set the channel where admin action audit logs are posted",
    )
    @app_commands.describe(
        channel="Channel for admin audit logs (leave empty to clear)",
    )
    @is_admin()
    async def log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot.database.engine import set_guild_setting
        if channel is None:
            await set_guild_setting(current_guild_id(), "log_channel_id", None)
            await interaction.followup.send(
                "Cleared the audit log channel. Admin actions will no longer be logged.",
                ephemeral=True,
            )
            return
        await set_guild_setting(current_guild_id(), "log_channel_id", channel.id)
        await interaction.followup.send(
            f"Admin actions will now be logged in {channel.mention}.",
            ephemeral=True,
        )

    @settings.command(
        name="theme", description="Set the visual theme for sportsbook images"
    )
    @app_commands.describe(name="Theme name (e.g. dark, forest)")
    @is_admin()
    async def settings_theme(self, interaction: discord.Interaction, name: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        theme = get_theme_by_name(name)
        if theme is None:
            available = ", ".join(f"**{n}**" for n in list_theme_names())
            await interaction.followup.send(
                f"Unknown theme **{name}**. Available themes: {available}.",
                ephemeral=True,
            )
            return
        set_active_theme(theme)
        await set_setting("image_theme", name.lower())
        await interaction.followup.send(
            f"Image theme set to **{theme.name}**. All new renders will use this theme.",
            ephemeral=True,
        )

    @settings_theme.autocomplete("name")
    async def _theme_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=n, value=n)
            for n in list_theme_names()
            if current.lower() in n.lower()
        ]

    @settings.command(
        name="victor_avg_age",
        description="Set the global average victor age used to anchor the age odds factor",
    )
    @app_commands.describe(
        age="Average age of victors across all past games (12–18). Leave empty to reset to default.",
    )
    @is_admin()
    async def settings_victor_avg_age(
        self,
        interaction: discord.Interaction,
        age: app_commands.Range[int, 12, 18] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            row = await session.get(GameSetting, "victor_avg_age")
            if age is None:
                if row:
                    await session.delete(row)
            else:
                if row:
                    row.value = json.dumps(age)
                else:
                    session.add(GameSetting(key="victor_avg_age", value=json.dumps(age)))
            await _recalculate_markets(session)
        if age is None:
            await interaction.followup.send(
                f"Victor average age reset to default ({AGE_NEUTRAL}). Odds recalculated.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Global victor average age set to **{age}**. Odds recalculated.",
                ephemeral=True,
            )

    @settings.command(
        name="announcement",
        description="Set or clear the Capitol announcement shown in /odds image footers",
    )
    @app_commands.describe(text="Announcement text (leave blank to reset to default)")
    @is_admin()
    async def settings_announcement(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot.imaging.hot_odds import DEFAULT_ANNOUNCEMENT
        if text is None:
            await set_setting("capitol_announcement", None)
            await interaction.followup.send(
                f"Capitol announcement reset to default:\n> {DEFAULT_ANNOUNCEMENT}",
                ephemeral=True,
            )
        else:
            await set_setting("capitol_announcement", text)
            await interaction.followup.send(
                f"Capitol announcement updated:\n> {text}",
                ephemeral=True,
            )

    # ── RESTRICT COMMANDS ─────────────────────────────────────────────────────

    @restrict.command(name="ban", description="Block a user from placing any bets")
    @app_commands.describe(member="User to ban from betting")
    @is_admin()
    async def restrict_ban(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "ALL",
                )
            )
            if existing.scalar_one_or_none():
                await interaction.followup.send(
                    f"**{member.display_name}** is already banned from betting.",
                    ephemeral=True,
                )
                return
            session.add(BettingRestriction(
                guild_id=current_guild_id(),
                discord_user_id=member.id,
                restriction_type="ALL",
            ))
        await interaction.followup.send(
            f"**{member.display_name}** has been banned from placing any bets.",
            ephemeral=True,
        )

    @restrict.command(name="unban", description="Lift a user's full betting ban")
    @app_commands.describe(member="User to unban")
    @is_admin()
    async def restrict_unban(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "ALL",
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                await interaction.followup.send(
                    f"**{member.display_name}** does not have a full betting ban.",
                    ephemeral=True,
                )
                return
            await session.delete(row)
        await interaction.followup.send(
            f"Full betting ban lifted for **{member.display_name}**.", ephemeral=True
        )

    @restrict.command(
        name="block_district",
        description="Block a user from betting on district markets",
    )
    @app_commands.describe(member="User to restrict", district="District number (1–12)")
    @is_admin()
    async def restrict_block_district(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        district: app_commands.Range[int, 1, 12],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "DISTRICT",
                    BettingRestriction.district == district,
                )
            )
            if existing.scalar_one_or_none():
                await interaction.followup.send(
                    f"**{member.display_name}** is already blocked from betting on District {district}.",
                    ephemeral=True,
                )
                return
            session.add(BettingRestriction(
                guild_id=current_guild_id(),
                discord_user_id=member.id,
                restriction_type="DISTRICT",
                district=district,
            ))
        await interaction.followup.send(
            f"**{member.display_name}** is now blocked from betting on District {district} markets.",
            ephemeral=True,
        )

    @restrict.command(
        name="unblock_district", description="Remove a user's district restriction"
    )
    @app_commands.describe(member="User to update", district="District number (1–12)")
    @is_admin()
    async def restrict_unblock_district(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        district: app_commands.Range[int, 1, 12],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "DISTRICT",
                    BettingRestriction.district == district,
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                await interaction.followup.send(
                    f"**{member.display_name}** has no District {district} restriction.",
                    ephemeral=True,
                )
                return
            await session.delete(row)
        await interaction.followup.send(
            f"District {district} restriction removed for **{member.display_name}**.",
            ephemeral=True,
        )

    @restrict.command(
        name="block_tribute", description="Block a user from betting on tribute markets"
    )
    @app_commands.describe(
        member="User to restrict", tribute_id="Tribute to block betting on"
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def restrict_block_tribute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tribute_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tid = _parse_tribute_id(tribute_id)
        if tid is None:
            await interaction.followup.send(
                "Invalid tribute. Please pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        async with get_session() as session:
            tribute = await session.get(Tribute, tid)
            if not tribute:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "TRIBUTE",
                    BettingRestriction.tribute_id == tid,
                )
            )
            if existing.scalar_one_or_none():
                await interaction.followup.send(
                    f"**{member.display_name}** is already blocked from betting on **{tribute.name}**.",
                    ephemeral=True,
                )
                return
            session.add(BettingRestriction(
                guild_id=current_guild_id(),
                discord_user_id=member.id,
                restriction_type="TRIBUTE",
                tribute_id=tid,
            ))
            tribute_name = tribute.name
        await interaction.followup.send(
            f"**{member.display_name}** is now blocked from betting on markets involving **{tribute_name}**.",
            ephemeral=True,
        )

    @restrict.command(
        name="unblock_tribute", description="Remove a user's tribute restriction"
    )
    @app_commands.describe(member="User to update", tribute_id="Tribute to unblock")
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def restrict_unblock_tribute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tribute_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tid = _parse_tribute_id(tribute_id)
        if tid is None:
            await interaction.followup.send(
                "Invalid tribute. Please pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.guild_id == (current_guild_id()),
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "TRIBUTE",
                    BettingRestriction.tribute_id == tid,
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                tribute = await session.get(Tribute, tid)
                name = tribute.name if tribute else str(tid)
                await interaction.followup.send(
                    f"**{member.display_name}** has no restriction on **{name}**.",
                    ephemeral=True,
                )
                return
            tribute = await session.get(Tribute, tid)
            tribute_name = tribute.name if tribute else str(tid)
            await session.delete(row)
        await interaction.followup.send(
            f"Tribute restriction on **{tribute_name}** removed for **{member.display_name}**.",
            ephemeral=True,
        )

    @restrict.command(
        name="view", description="View all betting restrictions for a user"
    )
    @app_commands.describe(member="User to inspect")
    @is_admin()
    async def restrict_view(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            result = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.discord_user_id == member.id
                )
            )
            restrictions = result.scalars().all()

        if not restrictions:
            await interaction.followup.send(
                f"**{member.display_name}** has no betting restrictions.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Betting Restrictions — {member.display_name}",
            color=0xE74C3C,
        )
        lines = []
        for r in restrictions:
            if r.restriction_type == "ALL":
                lines.append("• **Full ban** — cannot place any bets")
            elif r.restriction_type == "DISTRICT":
                lines.append(
                    f"• **District {r.district}** — blocked from all District {r.district} markets"
                )
            elif r.restriction_type == "TRIBUTE":
                lines.append(
                    f"• **Tribute ID {r.tribute_id}** — blocked from markets involving this tribute"
                )
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @restrict.command(
        name="lock_tribute",
        description="Mark a user as playing a Tribute, blocking all sportsbook access (bot, Activity, website)",
    )
    @app_commands.describe(
        member="The Discord user playing as this Tribute",
        tribute_id="Which Tribute they're playing as",
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def restrict_lock_tribute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tribute_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        tid = _parse_tribute_id(tribute_id)
        if tid is None:
            await interaction.followup.send(
                "Invalid tribute. Please pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        async with get_session() as session:
            tribute = await session.get(Tribute, tid)
            if not tribute:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            existing = await session.execute(
                select(TributeLock).where(
                    TributeLock.guild_id == current_guild_id(),
                    TributeLock.discord_user_id == member.id,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.tribute_id = tid
            else:
                session.add(TributeLock(
                    guild_id=current_guild_id(),
                    discord_user_id=member.id,
                    tribute_id=tid,
                ))
            tribute_name = tribute.name
        await interaction.followup.send(
            f"**{member.display_name}** is now locked out of the sportsbook as **{tribute_name}** — "
            "blocked from bot commands, the Activity, and the website.",
            ephemeral=True,
        )

    @restrict.command(
        name="unlock_tribute",
        description="Restore sportsbook access for a user previously locked out as a Tribute",
    )
    @app_commands.describe(member="User to unlock")
    @is_admin()
    async def restrict_unlock_tribute(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(TributeLock).where(
                    TributeLock.guild_id == current_guild_id(),
                    TributeLock.discord_user_id == member.id,
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                await interaction.followup.send(
                    f"**{member.display_name}** is not locked out as a Tribute.",
                    ephemeral=True,
                )
                return
            await session.delete(row)
        await interaction.followup.send(
            f"**{member.display_name}**'s sportsbook access has been restored.", ephemeral=True
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_or_create_user(session, member: discord.Member, guild_id: int) -> User:
    result = await session.execute(
        select(User).where(User.guild_id == guild_id, User.discord_id == member.id)
    )
    u = result.scalar_one_or_none()
    if u is None:
        default_raw = await get_setting("default_chips")
        default_chips = json.loads(default_raw) if default_raw else 1000
        u = User(
            guild_id=guild_id, discord_id=member.id,
            username=member.display_name, chips=default_chips,
        )
        session.add(u)
        await session.flush()
    else:
        u.username = member.display_name
    return u


def _build_label(
    market_type: str,
    trib_a: Tribute | None,
    trib_b: Tribute | None,
    cause: str | None,
    placement_num: int | None,
    top_n: int | None,
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> str:
    a = (
        f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}"
        if trib_a
        else "Capitol"
    )
    b = f"D{trib_b.district}{trib_b.display_gender} {trib_b.name}" if trib_b else ""
    d = (
        str(trib_a.district)
        if trib_a
        else (str(placement_num) if placement_num is not None else "?")
    )
    side = "Over" if ou_side == "OVER" else ("Under" if ou_side == "UNDER" else "")
    line_str = f"{ou_line:g}" if ou_line is not None else ""
    arena_side = (
        "Artificial"
        if cause == "ARTIFICIAL"
        else "Natural" if cause == "NATURAL" else cause or "?"
    )
    return {
        "TRIBUTE_WINS": f"{a} Wins the Games",
        "TRIBUTE_PLACEMENT": f"{a} Finishes {_ordinal(placement_num or 2)}",
        "TRIBUTE_TOP_N": f"{a} Top {top_n or 3} Finish",
        "TRIBUTE_RUNNER_UP": f"{a} Finishes Runner-Up (2nd)",
        "FIRST_TRIBUTE_TO_DIE": f"{a} is First Tribute to Die",
        "HIGHEST_TRAINING_SCORE": f"{a} Receives Highest Training Score",
        "LOWEST_TRAINING_SCORE": f"{a} Receives Lowest Training Score",
        "TRIBUTE_KILLS": f"{a} Gets Most Kills",
        "KILL_EVENT": f"{a} Kills {b}",
        "DEATH_CAUSE": f"{a} Dies by {cause or 'Unknown Cause'}",
        "FIRST_BLOOD": f"{a} Gets First Kill",
        "BLOODBATH_SURVIVOR": f"{a} Survives the Bloodbath",
        "TRIBUTE_KILLED_BLOODBATH": f"{a} Killed in the Bloodbath",
        "FIRST_IN_ALLIANCE_DEATH": f"{a} Dies First in {cause or 'Alliance'}",
        "KILLS_OU": f"{a} Kills — {side} {line_str}",
        "PLACEMENT_OU": f"{a} Placement — {side} {line_str}",
        "MAKES_FINAL_8": f"{a} Makes Final 8",
        "MISSES_FINAL_8": f"{a} Eliminated Before Final 8",
        "MAKES_FINAL_5": f"{a} Makes Final 5",
        "MISSES_FINAL_5": f"{a} Eliminated Before Final 5",
        "ARENA_TYPE": f"Arena Type — {arena_side}",
        "EXACT_TRAINING_SCORE": f"{a} Training Score = {placement_num or '?'}",
        "COMBINED_DISTRICT_SCORE": f"D{d} Combined Score = {placement_num or '?'}",
        "PARTNER_SCORE_HIGHER": f"{a} Scores Higher Than {b or 'Partner'}",
        "PARTNER_SCORE_LOWER": f"{a} Scores Lower Than {b or 'Partner'}",
        "PARTNER_PLACE_HIGHER": f"{a} Places Higher Than {b or 'Partner'}",
        "PARTNER_PLACE_LOWER": f"{a} Places Lower Than {b or 'Partner'}",
        "TRAINING_SCORE_OU": f"{a} Training Score — {side} {line_str}",
        # Game-level prop markets
        "BLOODBATH_KILLS_OU": f"Bloodbath Kills — {side} {line_str}",
        "BLOODBATH_DEATHS_OU": f"Bloodbath Deaths — {side} {line_str}",
        "EXACT_BLOODBATH_DEATHS": f"Bloodbath Deaths = {placement_num or '?'}",
        "BLOODBATH_NO_DEATHS": "Bloodbath Contains No Deaths",
        "ANY_BB_DOUBLE_KILL": "Any Tribute Records 2+ Bloodbath Kills",
        "ARENA_TRAP_DEATHS_OU": f"Arena Trap Deaths — {side} {line_str}",
        "ARENA_ENV_DEATHS_OU": f"Arena Environmental Deaths — {side} {line_str}",
        "ARENA_IS_NATURAL": "Arena is a Natural Arena",
        "ARENA_IS_ARTIFICIAL": "Arena is an Artificial Arena",
        "NUM_TENS_OU": f"Number of 10+ Scores Awarded — {side} {line_str}",
        "SOLO_TRIBUTES_OU": f"Solo (Unallied) Tributes — {side} {line_str}",
        "GAMES_DURATION": f"Games Last {placement_num or '?'} Days",
        "GAMES_FEAST": "Games Features a Feast",
        "GAMES_BETRAYAL": "Games Features a Betrayal",
        "DISTRICT_PARTNER_KILL": "Games Features a District Partner Kill",
        # District-level markets
        "DISTRICT_VICTOR": f"D{d} Victor",
        "DISTRICT_HIGHEST_SCORE": f"D{d} Has Highest Combined Training Score",
        "FIRST_DISTRICT_WIPE": f"D{d} is First District Fully Eliminated",
        "DISTRICT_KILLS_OU": f"D{d} Total Kills — {side} {line_str}",
        "DISTRICT_BOTH_BLOODBATH": f"D{d} Both Survive Bloodbath",
        "DISTRICT_WIPED_BLOODBATH": f"D{d} Both Killed in Bloodbath",
        "DISTRICT_BOTH_FINAL_8": f"D{d} Both Make Final 8",
        "DISTRICT_ONE_FINAL_8": f"Someone from D{d} Makes Final 8",
        "DISTRICT_BOTH_FINAL_5": f"D{d} Both Make Final 5",
        "DISTRICT_ONE_FINAL_5": f"Someone from D{d} Makes Final 5",
        # Alliance-level markets (cause = alliance name)
        "ALLIANCE_VICTOR": f"{cause or 'Alliance'} Victor",
        "ALLIANCE_KILLS_OU": f"{cause or 'Alliance'} Total Kills — {side} {line_str}",
        "ALLIANCE_ALL_FINAL_8": f"{cause or 'Alliance'} All Make Final 8",
        "ALLIANCE_ONE_FINAL_8": f"Someone from {cause or 'Alliance'} Makes Final 8",
        "ALLIANCE_ALL_FINAL_5": f"{cause or 'Alliance'} All Make Final 5",
        "ALLIANCE_ONE_FINAL_5": f"Someone from {cause or 'Alliance'} Makes Final 5",
        "FIRST_ALLIANCE_WIPED": f"{cause or 'Alliance'} is First Alliance Wiped",
        "ALLIANCE_MOST_KILLS": f"{cause or 'Alliance'} Gets Most Kills",
        "EXACT_ALLIANCE_KILLS": f"{cause or 'Alliance'} Kills = {top_n or '?'}",
        "ALLIANCE_RUNNER_UP": f"{cause or 'Alliance'} Produces the Runner-Up",
    }.get(market_type, f"{a} — {market_type}")


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n if n <= 3 else 0, f"{n}th")


def _parse_tribute_id(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

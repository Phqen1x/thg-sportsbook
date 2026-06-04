from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, or_, select

from bot.database.engine import get_session, get_read_session, set_setting, get_setting
from bot.database.models import (
    Alliance, Bet, BettingPhase, BettingRestriction, DistrictRecord, GameSetting, Market,
    MarketTemplate, Modifier, ModifierAssignment, Parlay, PendingParlayLeg,
    Tribute, User,
)
from bot.odds.calculator import implied_probability, straight_payout, parlay_payout
from bot.odds.defaults import (
    ALLIANCE_FACTOR_ALPHA, DEFAULT_FALLBACK_ODDS, HIST_ALPHA, HIST_KILL_ALPHA,
    alliance_default_odds, apply_group_influence, death_cause_share,
    default_odds, district_default_odds,
    district_pair_kill_factor, kill_quality_multiplier, reputation_factor, strength_hurts,
)
from bot.imaging.base import get_active_theme, get_theme_by_name, list_theme_names, set_active_theme
from bot.utils.checks import is_admin
from bot.utils.formatters import fmt_chips, fmt_odds, safe_defer
from bot.utils.market_view import MarketPageView, MarketTypePageView, sort_markets

log = logging.getLogger("capitol.admin")

MARKET_TYPES = [
    app_commands.Choice(name="Tribute Wins (Victor)",          value="TRIBUTE_WINS"),
    app_commands.Choice(name="Tribute Placement (Exact)",      value="TRIBUTE_PLACEMENT"),
    app_commands.Choice(name="Tribute Top-N Finish",           value="TRIBUTE_TOP_N"),
    app_commands.Choice(name="Top Killer",                     value="TRIBUTE_KILLS"),
    app_commands.Choice(name="Kill Event (A kills B)",         value="KILL_EVENT"),
    app_commands.Choice(name="Death Cause",                    value="DEATH_CAUSE"),
    app_commands.Choice(name="First Blood",                    value="FIRST_BLOOD"),
    app_commands.Choice(name="Bloodbath Survivor",             value="BLOODBATH_SURVIVOR"),
    app_commands.Choice(name="Sponsor Event (Custom)",         value="SPONSOR_EVENT"),
    app_commands.Choice(name="Kills Over/Under",               value="KILLS_OU"),
    app_commands.Choice(name="Placement Over/Under",           value="PLACEMENT_OU"),
    app_commands.Choice(name="Makes Final 8",                  value="MAKES_FINAL_8"),
    app_commands.Choice(name="Eliminated Before Final 8",      value="MISSES_FINAL_8"),
    app_commands.Choice(name="Makes Final 5",                  value="MAKES_FINAL_5"),
    app_commands.Choice(name="Eliminated Before Final 5",      value="MISSES_FINAL_5"),
    app_commands.Choice(name="Makes the Finale",               value="MAKES_FINALE"),
    app_commands.Choice(name="Eliminated Before Finale",       value="MISSES_FINALE"),
    app_commands.Choice(name="Arena Type (Pre-Games)",         value="ARENA_TYPE"),
    app_commands.Choice(name="Exact Training Score",           value="EXACT_TRAINING_SCORE"),
    app_commands.Choice(name="Combined District Score",        value="COMBINED_DISTRICT_SCORE"),
    app_commands.Choice(name="Training Score Over/Under",      value="TRAINING_SCORE_OU"),
    # ── District-level markets ─────────────────────────────────────────────────
    app_commands.Choice(name="District Victor",                        value="DISTRICT_VICTOR"),
    app_commands.Choice(name="District Total Kills Over/Under",        value="DISTRICT_KILLS_OU"),
    app_commands.Choice(name="District Both Survive Bloodbath",        value="DISTRICT_BOTH_BLOODBATH"),
    app_commands.Choice(name="District Both Make Final 8",             value="DISTRICT_BOTH_FINAL_8"),
    app_commands.Choice(name="District At Least One Makes Final 8",    value="DISTRICT_ONE_FINAL_8"),
    app_commands.Choice(name="District Both Make Final 5",             value="DISTRICT_BOTH_FINAL_5"),
    app_commands.Choice(name="District At Least One Makes Final 5",    value="DISTRICT_ONE_FINAL_5"),
    app_commands.Choice(name="District Both Make the Finale",          value="DISTRICT_BOTH_FINALE"),
    app_commands.Choice(name="District At Least One Makes the Finale", value="DISTRICT_ONE_FINALE"),
    # ── Alliance-level markets ─────────────────────────────────────────────────
    app_commands.Choice(name="Alliance Victor",                        value="ALLIANCE_VICTOR"),
    app_commands.Choice(name="Alliance Total Kills Over/Under",        value="ALLIANCE_KILLS_OU"),
    app_commands.Choice(name="Alliance All Survive Bloodbath",         value="ALLIANCE_ALL_BLOODBATH"),
    app_commands.Choice(name="Alliance All Make Final 8",              value="ALLIANCE_ALL_FINAL_8"),
    app_commands.Choice(name="Alliance At Least One Makes Final 8",    value="ALLIANCE_ONE_FINAL_8"),
    app_commands.Choice(name="Alliance All Make Final 5",              value="ALLIANCE_ALL_FINAL_5"),
    app_commands.Choice(name="Alliance At Least One Makes Final 5",    value="ALLIANCE_ONE_FINAL_5"),
    app_commands.Choice(name="Alliance All Make the Finale",           value="ALLIANCE_ALL_FINALE"),
    app_commands.Choice(name="Alliance At Least One Makes the Finale", value="ALLIANCE_ONE_FINALE"),
]

# Milestone market constants used for parlay validation
_MAKES_MILESTONES = {"MAKES_FINAL_8", "MAKES_FINAL_5", "MAKES_FINALE"}
_ALL_MILESTONES = {
    "MAKES_FINAL_8", "MISSES_FINAL_8",
    "MAKES_FINAL_5", "MISSES_FINAL_5",
    "MAKES_FINALE",  "MISSES_FINALE",
}
_MILESTONE_GROUP = {
    "MAKES_FINAL_8": "FINAL_8", "MISSES_FINAL_8": "FINAL_8",
    "MAKES_FINAL_5": "FINAL_5", "MISSES_FINAL_5": "FINAL_5",
    "MAKES_FINALE":  "FINALE",  "MISSES_FINALE":  "FINALE",
}

# Market types that resolve when their respective phase is entered
_PHASE_ENTRY_MARKETS: dict[str, tuple[str, str]] = {
    "Final 8": ("MAKES_FINAL_8", "MISSES_FINAL_8"),
    "Final 5": ("MAKES_FINAL_5", "MISSES_FINAL_5"),
    "Finale":  ("MAKES_FINALE",  "MISSES_FINALE"),
}

# Pre-Games prop markets — resolve when Pre-Games ends
_PREGAMES_PROP_TYPES = {"ARENA_TYPE", "EXACT_TRAINING_SCORE", "COMBINED_DISTRICT_SCORE", "TRAINING_SCORE_OU"}
# Bloodbath markets — resolve/void when Bloodbath ends
_BLOODBATH_RESOLVE_TYPES = {"BLOODBATH_SURVIVOR"}
_BLOODBATH_VOID_TYPES = {"FIRST_BLOOD"}

# District-level market types
_DISTRICT_MARKET_TYPES = {
    "DISTRICT_VICTOR",
    "DISTRICT_KILLS_OU",
    "DISTRICT_BOTH_BLOODBATH",
    "DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8",
    "DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5",
    "DISTRICT_BOTH_FINALE",  "DISTRICT_ONE_FINALE",
}
# District milestone markets that resolve at phase entry
_DISTRICT_PHASE_ENTRY: dict[str, tuple[str, ...]] = {
    "Final 8": ("DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8"),
    "Final 5": ("DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5"),
    "Finale":  ("DISTRICT_BOTH_FINALE",  "DISTRICT_ONE_FINALE"),
}

# Alliance-level market types (placement_num = alliance_id, cause = alliance name)
_ALLIANCE_MARKET_TYPES = {
    "ALLIANCE_VICTOR",
    "ALLIANCE_KILLS_OU",
    "ALLIANCE_ALL_BLOODBATH",
    "ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8",
    "ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5",
    "ALLIANCE_ALL_FINALE",  "ALLIANCE_ONE_FINALE",
}
_ALLIANCE_PHASE_ENTRY: dict[str, tuple[str, ...]] = {
    "Final 8": ("ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8"),
    "Final 5": ("ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5"),
    "Finale":  ("ALLIANCE_ALL_FINALE",  "ALLIANCE_ONE_FINALE"),
}

BUILT_IN_TYPE_VALUES = {c.value for c in MARKET_TYPES}

DIFFICULTY_ODDS: dict[str, int] = {
    "EASY":      -200,
    "MODERATE":  +100,
    "HARD":      +300,
    "VERY_HARD": +700,
    "LONGSHOT":  +1500,
}

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy      (≈ -200 odds)", value="EASY"),
    app_commands.Choice(name="Moderate  (≈ +100 odds)", value="MODERATE"),
    app_commands.Choice(name="Hard      (≈ +300 odds)", value="HARD"),
    app_commands.Choice(name="Very Hard (≈ +700 odds)", value="VERY_HARD"),
    app_commands.Choice(name="Longshot  (≈ +1500 odds)", value="LONGSHOT"),
]

_DEATH_CAUSES = ["Natural Causes", "Mutt", "Another Tribute", "Gamemakers"]

_DEATH_CAUSE_SUGGESTIONS = [
    "Killed by another tribute",
    "Killed by Capitol Muttation",
    "Killed by Arena",
    "Killed by Gamemakers",
]


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).order_by(Tribute.district, Tribute.non_binary, Tribute.gender)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def alive_tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(
                Tribute.district, Tribute.non_binary, Tribute.gender
            )
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def dead_tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).where(Tribute.status == "DEAD").order_by(
                Tribute.district, Tribute.non_binary, Tribute.gender
            )
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} (DEAD)"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def open_market_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Market).where(Market.status.in_(["OPEN", "CLOSED"])).order_by(Market.id)
        )
        markets = result.scalars().all()
    choices = []
    for m in markets:
        label = f"[{m.status}] {m.label}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
    return choices[:25]


async def death_cause_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    for cause in _DEATH_CAUSE_SUGGESTIONS:
        if current.lower() in cause.lower():
            choices.append(app_commands.Choice(name=cause, value=cause))
    # If the user typed something that doesn't match any suggestion, surface it as a custom entry
    if current and not any(current.lower() == c.value.lower() for c in choices):
        choices.append(app_commands.Choice(name=f'Custom: "{current}"', value=current))
    return choices[:25]


async def phase_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(BettingPhase).order_by(BettingPhase.sort_order))
        phases = result.scalars().all()
    choices = []
    for p in phases:
        if current.lower() in p.name.lower():
            choices.append(app_commands.Choice(name=p.name, value=str(p.id)))
    return choices[:25]


async def alliance_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(Alliance).order_by(Alliance.name))
        alliances = result.scalars().all()
    choices = []
    for a in alliances:
        if current.lower() in a.name.lower():
            choices.append(app_commands.Choice(name=a.name, value=str(a.id)))
    return choices[:25]


async def market_type_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
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


async def market_template_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(MarketTemplate).order_by(MarketTemplate.id))
        templates = result.scalars().all()
    choices = []
    for t in templates:
        label = f"{'[INACTIVE] ' if not t.active else ''}{t.name}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(t.id)))
    return choices[:25]




async def modifier_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(Modifier).order_by(Modifier.id))
        mods = result.scalars().all()
    choices = []
    for m in mods:
        label = f"{m.label} (×{m.weight})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
    return choices[:25]


async def modifier_assignment_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        assign_result = await session.execute(
            select(ModifierAssignment, Modifier.label, Modifier.weight)
            .join(Modifier, ModifierAssignment.modifier_id == Modifier.id)
            .order_by(ModifierAssignment.id)
        )
        rows = assign_result.all()
        trib_result = await session.execute(select(Tribute))
        tributes = {t.id: t for t in trib_result.scalars().all()}
    choices = []
    for assignment, mod_label, weight in rows:
        if assignment.tribute_id is not None:
            t = tributes.get(assignment.tribute_id)
            scope = f"D{t.district}{t.display_gender} {t.name}" if t else f"Tribute #{assignment.tribute_id}"
        else:
            scope = f"District {assignment.district}"
        label = f"{mod_label} → {scope} (×{weight})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(assignment.id)))
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

    def _component(d_val: float | None, g_vals: list[float], invert: bool = False) -> None:
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
        kills_g_vals = [r.male_kills / num_games for r in all_records if r.male_kills is not None]
    elif gender == "F" and record.female_kills is not None:
        kills_val = record.female_kills / num_games
        kills_g_vals = [r.female_kills / num_games for r in all_records if r.female_kills is not None]
    else:
        kills_set = [r for r in all_records if r.total_kills is not None]
        kills_val = record.total_kills / num_games if record.total_kills is not None else None
        kills_g_vals = [r.total_kills / num_games for r in kills_set]

    if for_kills:
        # Kill markets: high historical kills → positive factor.
        # Use gender-specific kill counts when available so male/female tributes
        # are priced against their gender's own historical kill rates.
        _component(kills_val, kills_g_vals)
        _component(
            record.bloodbath_kills / num_games if record.bloodbath_kills is not None else None,
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
            record.bloodbath_kills / num_games if record.bloodbath_kills is not None else None,
            [r.bloodbath_kills / num_games for r in bb_set],
            invert=True,
        )
        # High bloodbath death rate (tributes from this district dying in the bloodbath)
        # is a survival penalty — compare each district against the field average.
        _component(
            record.bloodbath_deaths / num_games if record.bloodbath_deaths is not None else None,
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
            wins_g_vals = [float(r.victor_male_count) for r in all_records if r.victor_male_count is not None]
        elif gender == "F" and record.victor_female_count is not None:
            wins_val = float(record.victor_female_count)
            wins_g_vals = [float(r.victor_female_count) for r in all_records if r.victor_female_count is not None]
        else:
            wins_val = float(record.wins) if record.wins is not None else None
            wins_g_vals = [float(r.wins) for r in all_records if r.wins is not None]
        _component(wins_val, wins_g_vals)
        _component(
            float(record.top8_finishes) if record.top8_finishes is not None else None,
            [float(r.top8_finishes) for r in all_records if r.top8_finishes is not None],
        )
        _component(
            float(record.top5_finishes) if record.top5_finishes is not None else None,
            [float(r.top5_finishes) for r in all_records if r.top5_finishes is not None],
        )
        _component(
            float(record.kill_record) if record.kill_record is not None else None,
            [float(r.kill_record) for r in all_records if r.kill_record is not None],
        )
        # Use gender-specific runner-up counts when available.
        if gender == "M" and record.runner_up_male is not None:
            ru_val: float | None = record.runner_up_male / num_games
            ru_g_vals = [r.runner_up_male / num_games for r in all_records if r.runner_up_male is not None]
        elif gender == "F" and record.runner_up_female is not None:
            ru_val = record.runner_up_female / num_games
            ru_g_vals = [r.runner_up_female / num_games for r in all_records if r.runner_up_female is not None]
        else:
            runner_up_set = [r for r in all_records if r.runner_up_finishes is not None]
            ru_val = record.runner_up_finishes / num_games if record.runner_up_finishes is not None else None
            ru_g_vals = [r.runner_up_finishes / num_games for r in runner_up_set]
        _component(ru_val, ru_g_vals)
        _component(
            record.avg_placement_last5,
            [r.avg_placement_last5 for r in all_records if r.avg_placement_last5 is not None],
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


def _arena_preference_factor(record: DistrictRecord | None, arena_type: str | None) -> float:
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
    Call after the victim's status has been set to DEAD so the alive count is the
    surviving field; the victim is added back in for the field-average baseline.
    """
    alive_count = await session.scalar(
        select(func.count()).select_from(Tribute).where(Tribute.status == "ALIVE")
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
            market_type, trib_a, all_tributes,
            tribute_b=trib_b, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side,
            hist_avg_score=hist_avg_score,
        )
    if trib_a is None:
        return base
    district_mates = [t for t in all_tributes if t.district == trib_a.district and t.id != trib_a.id]
    alliance_mates = [
        t for t in all_tributes
        if trib_a.alliance_id and t.alliance_id == trib_a.alliance_id and t.id != trib_a.id
    ]
    if market_type == "DEATH_CAUSE":
        # Death cause is anchored to the tribute's win probability: a tribute
        # that is P% to win is (1-P)% to die, and that death budget is split
        # across the causes so they sum back to it — not four flat longshots.
        # Reuse the win market's own pricing pipeline to get P(win).
        win_base = default_odds("TRIBUTE_WINS", trib_a, all_tributes)
        win_odds = apply_group_influence(
            win_base, "TRIBUTE_WINS", trib_a, district_mates, alliance_mates, all_tributes
        )
        p_win = 1.0 / american_to_decimal(win_odds)
        if modifier_factor != 1.0:
            p_win = max(0.01, min(0.99, p_win * modifier_factor))
        share = death_cause_share(
            cause, 1.0 - p_win, arena_type,
            district_record, all_district_records, num_games,
        )
        return prob_to_american(max(0.005, min(0.99, share)))
    odds = apply_group_influence(base, market_type, trib_a, district_mates, alliance_mates, all_tributes, ou_side=ou_side)
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

    # Build raw per-tribute modifier factors from direct/district assignments
    assign_result = await session.execute(
        select(ModifierAssignment, Modifier.weight)
        .join(Modifier, ModifierAssignment.modifier_id == Modifier.id)
    )
    raw_factors: dict[int, float] = {}
    for assignment, weight in assign_result.all():
        if assignment.tribute_id is not None:
            raw_factors[assignment.tribute_id] = raw_factors.get(assignment.tribute_id, 1.0) * weight
        elif assignment.district is not None:
            for t in all_tributes:
                if t.district == assignment.district:
                    raw_factors[t.id] = raw_factors.get(t.id, 1.0) * weight

    # Build a base-odds map for custom market types (difficulty baseline used as starting odds)
    tmpl_result = await session.execute(select(MarketTemplate))
    custom_bases: dict[str, int] = {
        f"CUSTOM_{t.id}": (t.default_odds if t.default_odds is not None else DIFFICULTY_ODDS[t.difficulty])
        for t in tmpl_result.scalars().all()
    }

    # Load district aggregate records, global game count, and arena type for historical/death factors
    hist_result = await session.execute(select(DistrictRecord))
    all_dr = list(hist_result.scalars().all())
    num_games_row = await session.get(GameSetting, "num_games")
    num_games = int(json.loads(num_games_row.value)) if num_games_row else 0
    arena_type_row = await session.get(GameSetting, "arena_type")
    arena_type = json.loads(arena_type_row.value) if arena_type_row else None
    dr_map = {r.district: r for r in all_dr}
    # Manually-set district reputation factor (applies to both win and kill markets)
    dist_rep_factor: dict[int, float] = {
        d: reputation_factor(dr_map[d].reputation) if d in dr_map else 1.0
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
        # Accumulated kill-quality boost (killing strong tributes lifts a
        # tribute's win and kill odds; killing long-shots barely moves them).
        kboost = t.kill_boost or 1.0
        hist_win = _district_historical_factor(dr_map.get(t.district), all_dr, num_games, for_kills=False, gender=t.gender)
        hist_kill = _district_historical_factor(dr_map.get(t.district), all_dr, num_games, for_kills=True, gender=t.gender)
        arena_pref = _arena_preference_factor(dr_map.get(t.district), arena_type)
        solo_win_factors[t.id] = own * seniority * hist_win * arena_pref * rep * kboost
        solo_kill_factors[t.id] = own * seniority * hist_kill * rep * kboost

    # The field-average circumstantial strength is the neutral benchmark each ally
    # is judged against. An ally exactly at the average neither helps nor hurts.
    avg_win = sum(solo_win_factors.values()) / len(solo_win_factors) if solo_win_factors else 1.0
    avg_kill = sum(solo_kill_factors.values()) / len(solo_kill_factors) if solo_kill_factors else 1.0

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
            allies = [m for m in alive if m.alliance_id == t.alliance_id and m.id != t.id]
            if allies:
                win_nudge = ALLIANCE_FACTOR_ALPHA * sum(solo_win_factors[m.id] - avg_win for m in allies)
                kill_nudge = ALLIANCE_FACTOR_ALPHA * sum(solo_kill_factors[m.id] - avg_kill for m in allies)
                win *= max(0.4, min(2.5, 1.0 + win_nudge))
                kill *= max(0.4, min(2.5, 1.0 + kill_nudge))
        tribute_win_factors[t.id] = win
        tribute_kill_factors[t.id] = kill

    _KILL_MARKET_TYPES = {"TRIBUTE_KILLS", "KILLS_OU", "FIRST_BLOOD", "KILL_EVENT"}

    for market in markets:
        if market.tribute_a_id is None:
            continue  # global markets (e.g. ARENA_TYPE) have no tribute-driven odds
        trib_a = next((t for t in all_tributes if t.id == market.tribute_a_id), None)
        trib_b = next((t for t in all_tributes if t.id == market.tribute_b_id), None) if market.tribute_b_id else None
        if trib_a:
            mfactor = (
                tribute_kill_factors.get(trib_a.id, 1.0)
                if market.type in _KILL_MARKET_TYPES
                else tribute_win_factors.get(trib_a.id, 1.0)
            )
            market.odds = _compute_odds(
                market.type, trib_a, all_tributes,
                trib_b=trib_b,
                placement_num=market.placement_num,
                top_n=market.top_n,
                ou_line=market.ou_line,
                ou_side=market.ou_side,
                modifier_factor=mfactor,
                cause=market.cause,
                arena_type=arena_type,
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
            market.type, d_tributes, all_tributes,
            ou_line=market.ou_line,
            ou_side=market.ou_side,
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
            market.type, members, all_tributes,
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
    for bet in bets:
        if result is None:
            bet.status = "VOIDED"
            user = await session.get(User, bet.user_id)
            if user:
                user.chips += bet.wager
        elif result is True and bet.parlay_id is None:
            bet.status = "WON"
            user = await session.get(User, bet.user_id)
            if user:
                user.chips += bet.payout_if_win
                user.total_won += bet.payout_if_win
            credits_issued += bet.payout_if_win
        elif result is False and bet.parlay_id is None:
            bet.status = "LOST"
        elif bet.parlay_id is not None:
            bet.status = "WON" if result is True else ("VOIDED" if result is None else "LOST")
            await _check_parlay(session, bet.parlay_id)
        resolved_count += 1
    return {"resolved": resolved_count, "credits": credits_issued}


async def _check_parlay(session, parlay_id: int) -> None:
    parlay = await session.get(Parlay, parlay_id)
    if parlay is None or parlay.status != "PENDING":
        return
    leg_result = await session.execute(select(Bet).where(Bet.parlay_id == parlay_id))
    legs = leg_result.scalars().all()
    statuses = [leg.status for leg in legs]
    if "LOST" in statuses:
        parlay.status = "LOST"
        return
    if any(s == "PENDING" for s in statuses):
        active_legs = [l for l in legs if l.status != "VOIDED"]
        if len(active_legs) < len(legs):
            parlay.total_payout = parlay_payout(parlay.total_wager, [l.odds_at_placement for l in active_legs])
        return
    active_legs = [l for l in legs if l.status != "VOIDED"]
    if all(l.status == "WON" for l in active_legs):
        parlay.status = "WON"
        user = await session.get(User, parlay.user_id)
        if user:
            user.chips += parlay.total_payout
            user.total_won += parlay.total_payout
    elif all(l.status == "VOIDED" for l in legs):
        parlay.status = "WON"
        user = await session.get(User, parlay.user_id)
        if user:
            user.chips += parlay.total_wager


async def _unresolve_market(session, market: Market, reverted_parlays: set[int]) -> dict:
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
                    p_user = await session.get(User, parlay.user_id)
                    if parlay.status == "WON":
                        leg_res = await session.execute(select(Bet).where(Bet.parlay_id == bet.parlay_id))
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
            user = await session.get(User, bet.user_id)
            if user:
                user.chips -= bet.payout_if_win
                user.total_won -= bet.payout_if_win
            chips_reclaimed += bet.payout_if_win
            bet.status = "PENDING"
        elif bet.status == "LOST":
            bet.status = "PENDING"
        elif bet.status == "VOIDED":
            user = await session.get(User, bet.user_id)
            if user:
                user.chips -= bet.wager
            chips_reclaimed += bet.wager
            bet.status = "PENDING"
        unresolved_count += 1

    market.status = "OPEN"
    market.result = None
    return {"unresolved": unresolved_count, "chips_reclaimed": chips_reclaimed}


async def _reply(interaction: discord.Interaction, *args, **kwargs) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)
    except discord.NotFound:
        log.warning("Interaction expired before response could be sent.")


# ── Auto-market creation ──────────────────────────────────────────────────────

async def _auto_create_tribute_markets(session, new_tribute: Tribute, all_tributes: list[Tribute]) -> int:
    """Creates all standard markets when a tribute is added. Returns count created."""
    n = len([t for t in all_tributes if t.status == "ALIVE"])

    # Look up Pre-Games phase for phase-restricted markets
    pre_result = await session.execute(
        select(BettingPhase).where(BettingPhase.name == "Pre-Games").limit(1)
    )
    pre_phase = pre_result.scalars().first()
    pre_games_id = pre_phase.id if pre_phase else None

    # Historical training score average for this tribute's district/gender
    dr_result = await session.execute(
        select(DistrictRecord).where(DistrictRecord.district == new_tribute.district).limit(1)
    )
    dr = dr_result.scalars().first()
    if dr:
        hist_avg = (
            dr.avg_training_score_male   if new_tribute.gender == "M"
            else dr.avg_training_score_female if new_tribute.gender == "F"
            else dr.avg_training_score
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
            type_, trib_a, all_tributes,
            trib_b=trib_b, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side, hist_avg_score=h_avg,
        )
        if type_ == "DEATH_CAUSE":
            odds = DEFAULT_FALLBACK_ODDS
        label = _build_label(type_, trib_a, trib_b, cause, placement_num, top_n, ou_line, ou_side)
        m = Market(
            type=type_, label=label,
            tribute_a_id=trib_a.id if trib_a else None,
            tribute_b_id=trib_b.id if trib_b else None,
            cause=cause, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side,
            phase_id=phase_id,
            odds=odds,
            status="CLOSED",
        )
        session.add(m)
        return 1

    created = 0

    # ── All-phase single-tribute markets ──────────────────────────────────────
    created += _add("TRIBUTE_WINS",       new_tribute)
    created += _add("TRIBUTE_KILLS",      new_tribute)
    created += _add("FIRST_BLOOD",        new_tribute)
    created += _add("BLOODBATH_SURVIVOR", new_tribute)

    for cause in _DEATH_CAUSES:
        created += _add("DEATH_CAUSE", new_tribute, cause=cause)

    for line in [0.5, 1.5]:
        for side in ["OVER", "UNDER"]:
            created += _add("KILLS_OU", new_tribute, ou_line=line, ou_side=side)

    mid = round(n / 2.0 + 0.5, 1) if n > 1 else 1.5
    for side in ["OVER", "UNDER"]:
        created += _add("PLACEMENT_OU", new_tribute, ou_line=mid, ou_side=side)

    # ── Milestone markets (auto-resolve when respective phase begins) ──────────
    for mtype in (
        "MAKES_FINAL_8", "MISSES_FINAL_8",
        "MAKES_FINAL_5", "MISSES_FINAL_5",
        "MAKES_FINALE",  "MISSES_FINALE",
    ):
        created += _add(mtype, new_tribute)

    # ── Pre-Games only: training score prop markets ───────────────────────────
    for guessed_score in range(1, 13):
        created += _add(
            "EXACT_TRAINING_SCORE", new_tribute,
            placement_num=guessed_score,
            phase_id=pre_games_id,
            h_avg=hist_avg,
        )

    for side in ["OVER", "UNDER"]:
        created += _add(
            "TRAINING_SCORE_OU", new_tribute,
            ou_line=6.5, ou_side=side,
            phase_id=pre_games_id,
            h_avg=hist_avg,
        )

    # ── Kill-event pairs ──────────────────────────────────────────────────────
    others = [t for t in all_tributes if t.id != new_tribute.id]
    for other in others:
        created += _add("KILL_EVENT", new_tribute, trib_b=other)
        created += _add("KILL_EVENT", other, trib_b=new_tribute)

    # ── Combined district score + district markets (when second tribute is added)
    district_partners = [
        t for t in all_tributes
        if t.district == new_tribute.district and t.id != new_tribute.id
    ]
    if len(district_partners) == 1:
        partner = district_partners[0]
        for guessed_sum in range(2, 25):
            created += _add(
                "COMBINED_DISTRICT_SCORE", new_tribute,
                trib_b=partner, placement_num=guessed_sum,
                phase_id=pre_games_id,
            )

        d = new_tribute.district

        def _add_district(
            type_: str,
            ou_line_: float | None = None,
            ou_side_: str | None = None,
            phase_id_: int | None = None,
        ) -> int:
            d_tributes = [t for t in all_tributes if t.district == d]
            odds = district_default_odds(type_, d_tributes, all_tributes, ou_line=ou_line_, ou_side=ou_side_)
            label = _build_label(type_, None, None, None, d, None, ou_line_, ou_side_)
            m = Market(
                type=type_, label=label,
                tribute_a_id=None, tribute_b_id=None,
                placement_num=d,
                ou_line=ou_line_, ou_side=ou_side_,
                phase_id=phase_id_,
                odds=odds,
                status="CLOSED",
            )
            session.add(m)
            return 1

        created += _add_district("DISTRICT_VICTOR")
        for line in [0.5, 1.5, 2.5]:
            for side in ["OVER", "UNDER"]:
                created += _add_district("DISTRICT_KILLS_OU", ou_line_=line, ou_side_=side)
        created += _add_district("DISTRICT_BOTH_BLOODBATH")
        for mtype in (
            "DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8",
            "DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5",
            "DISTRICT_BOTH_FINALE",  "DISTRICT_ONE_FINALE",
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
    because the midpoint shifts with field size. If any OVER/UNDER market of
    that type already exists for the tribute, we leave it alone.
    COMBINED_DISTRICT_SCORE is keyed by (type, min_id, max_id, guessed_sum)
    so a/b ordering in the DB doesn't matter.
    District markets are keyed by (type, district_num, ou_line, ou_side).
    """
    all_t_result = await session.execute(select(Tribute))
    all_tributes = list(all_t_result.scalars().all())

    all_m_result = await session.execute(select(Market))
    all_markets = list(all_m_result.scalars().all())

    pre_result = await session.execute(
        select(BettingPhase).where(BettingPhase.name == "Pre-Games").limit(1)
    )
    pre_phase = pre_result.scalars().first()
    pre_games_id = pre_phase.id if pre_phase else None

    dr_result = await session.execute(select(DistrictRecord))
    dr_map = {r.district: r for r in dr_result.scalars().all()}

    # Current field size for PLACEMENT_OU midpoint
    n_alive = len([t for t in all_tributes if t.status == "ALIVE"])
    mid = round(n_alive / 2.0 + 0.5, 1) if n_alive > 1 else 1.5

    # ── Build existing-market key sets ─────────────────────────────────────────
    # simple: (type, tribute_a_id) — single-tribute, no extra params
    existing_simple: set[tuple[str, int | None]] = set()
    # keyed: (type, ta_id [, tb_id] [, extra...]) — extra params distinguish variants
    existing_keyed: set[tuple] = set()
    # district: (type, district_num, ou_line, ou_side)
    existing_district: set[tuple] = set()

    _SIMPLE_TYPES = {
        "TRIBUTE_WINS", "TRIBUTE_KILLS", "FIRST_BLOOD", "BLOODBATH_SURVIVOR",
        "MAKES_FINAL_8", "MISSES_FINAL_8",
        "MAKES_FINAL_5", "MISSES_FINAL_5",
        "MAKES_FINALE",  "MISSES_FINALE",
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
                existing_keyed.add(("COMBINED_DISTRICT_SCORE", min(ta, tb), max(ta, tb), m.placement_num))
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
            type_, trib_a, all_tributes,
            trib_b=trib_b, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side, hist_avg_score=h_avg,
        )
        if type_ == "DEATH_CAUSE":
            odds = DEFAULT_FALLBACK_ODDS
        label = _build_label(type_, trib_a, trib_b, cause, placement_num, top_n, ou_line, ou_side)
        session.add(Market(
            type=type_, label=label,
            tribute_a_id=trib_a.id if trib_a else None,
            tribute_b_id=trib_b.id if trib_b else None,
            cause=cause, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side,
            phase_id=phase_id, odds=odds, status="CLOSED",
        ))
        created_counts[type_] = created_counts.get(type_, 0) + 1

    def _make_district(
        type_: str,
        district: int,
        ou_line: float | None = None,
        ou_side: str | None = None,
    ) -> None:
        d_tributes = [t for t in all_tributes if t.district == district]
        odds = district_default_odds(type_, d_tributes, all_tributes, ou_line=ou_line, ou_side=ou_side)
        label = _build_label(type_, None, None, None, district, None, ou_line, ou_side)
        session.add(Market(
            type=type_, label=label,
            tribute_a_id=None, tribute_b_id=None,
            placement_num=district,
            ou_line=ou_line, ou_side=ou_side,
            phase_id=None, odds=odds, status="CLOSED",
        ))
        created_counts[type_] = created_counts.get(type_, 0) + 1

    def _make_alliance(
        alliance_id: int,
        alliance_name: str,
        type_: str,
        ou_line: float | None = None,
        ou_side: str | None = None,
    ) -> None:
        members = [t for t in all_tributes if t.alliance_id == alliance_id]
        odds = alliance_default_odds(type_, members, all_tributes, ou_line=ou_line, ou_side=ou_side)
        label = _build_label(type_, None, None, alliance_name, alliance_id, None, ou_line, ou_side)
        session.add(Market(
            type=type_, label=label,
            tribute_a_id=None, tribute_b_id=None,
            cause=alliance_name, placement_num=alliance_id,
            ou_line=ou_line, ou_side=ou_side,
            phase_id=None, odds=odds, status="CLOSED",
        ))
        created_counts[type_] = created_counts.get(type_, 0) + 1

    # ── Per-tribute markets ────────────────────────────────────────────────────
    for tribute in all_tributes:
        h_avg = _hist_avg(tribute)
        tid = tribute.id

        for mtype in _SIMPLE_TYPES:
            if (mtype, tid) not in existing_simple:
                _make(mtype, tribute)
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
                _make("PLACEMENT_OU", tribute, ou_line=mid, ou_side=side)
                existing_keyed.add(key)

        for score in range(1, 13):
            key = ("EXACT_TRAINING_SCORE", tid, score)
            if key not in existing_keyed:
                _make("EXACT_TRAINING_SCORE", tribute, placement_num=score,
                      phase_id=pre_games_id, h_avg=h_avg)
                existing_keyed.add(key)

        for side in ["OVER", "UNDER"]:
            key = ("TRAINING_SCORE_OU", tid, 6.5, side)
            if key not in existing_keyed:
                _make("TRAINING_SCORE_OU", tribute, ou_line=6.5, ou_side=side,
                      phase_id=pre_games_id, h_avg=h_avg)
                existing_keyed.add(key)

    # ── Kill-event pairs ───────────────────────────────────────────────────────
    for i, ta in enumerate(all_tributes):
        for tb in all_tributes[i + 1:]:
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
                _make("COMBINED_DISTRICT_SCORE", ta, trib_b=tb, placement_num=guessed_sum,
                      phase_id=pre_games_id)
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

        for mtype in (
            "DISTRICT_BOTH_FINAL_8", "DISTRICT_ONE_FINAL_8",
            "DISTRICT_BOTH_FINAL_5", "DISTRICT_ONE_FINAL_5",
            "DISTRICT_BOTH_FINALE",  "DISTRICT_ONE_FINALE",
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

        for line in [0.5, 1.5, 2.5]:
            for side in ["OVER", "UNDER"]:
                key = ("ALLIANCE_KILLS_OU", aid, line, side)
                if key not in existing_alliance:
                    _make_alliance(aid, aname, "ALLIANCE_KILLS_OU", ou_line=line, ou_side=side)
                    existing_alliance.add(key)

        if ("ALLIANCE_ALL_BLOODBATH", aid, None, None) not in existing_alliance:
            _make_alliance(aid, aname, "ALLIANCE_ALL_BLOODBATH")
            existing_alliance.add(("ALLIANCE_ALL_BLOODBATH", aid, None, None))

        for mtype in (
            "ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8",
            "ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5",
            "ALLIANCE_ALL_FINALE",  "ALLIANCE_ONE_FINALE",
        ):
            key = (mtype, aid, None, None)
            if key not in existing_alliance:
                _make_alliance(aid, aname, mtype)
                existing_alliance.add(key)

    return created_counts


async def _auto_create_alliance_markets(session, alliance: Alliance, all_tributes: list[Tribute]) -> int:
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
        odds = alliance_default_odds(type_, members_here, all_tributes, ou_line=ou_line, ou_side=ou_side)
        label = _build_label(type_, None, None, aname, aid, None, ou_line, ou_side)
        session.add(Market(
            type=type_, label=label,
            tribute_a_id=None, tribute_b_id=None,
            cause=aname, placement_num=aid,
            ou_line=ou_line, ou_side=ou_side,
            odds=odds, status="CLOSED",
        ))
        return 1

    created = 0
    created += _add("ALLIANCE_VICTOR")
    for line in [0.5, 1.5, 2.5]:
        for side in ["OVER", "UNDER"]:
            created += _add("ALLIANCE_KILLS_OU", ou_line=line, ou_side=side)
    created += _add("ALLIANCE_ALL_BLOODBATH")
    for mtype in (
        "ALLIANCE_ALL_FINAL_8", "ALLIANCE_ONE_FINAL_8",
        "ALLIANCE_ALL_FINAL_5", "ALLIANCE_ONE_FINAL_5",
        "ALLIANCE_ALL_FINALE",  "ALLIANCE_ONE_FINALE",
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

        self.btn_first = discord.ui.Button(emoji="⏮", style=discord.ButtonStyle.secondary, row=0, disabled=True)
        self.btn_prev  = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0, disabled=True)
        self.btn_page_label = discord.ui.Button(
            label=f"District 1 / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=True,
        )
        self.btn_next = discord.ui.Button(
            emoji="▶", style=discord.ButtonStyle.secondary, row=0,
            disabled=self.total_pages <= 1,
        )
        self.btn_last = discord.ui.Button(
            emoji="⏭", style=discord.ButtonStyle.secondary, row=0,
            disabled=self.total_pages <= 1,
        )

        self.btn_first.callback = self._on_first
        self.btn_prev.callback  = self._on_prev
        self.btn_next.callback  = self._on_next
        self.btn_last.callback  = self._on_last

        for btn in (self.btn_first, self.btn_prev, self.btn_page_label, self.btn_next, self.btn_last):
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
        at_last  = self.page >= self.total_pages - 1
        self.btn_first.disabled = at_first
        self.btn_prev.disabled  = at_first
        self.btn_next.disabled  = at_last
        self.btn_last.disabled  = at_last
        r = self.records[self.page] if self.records else None
        d = r.district if r else self.page + 1
        self.btn_page_label.label = f"District {d} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        if not self.records:
            embed = discord.Embed(title="District Historical Records", color=0x8B4513)
            embed.description = "No district records found."
            return embed

        r = self.records[self.page]
        win_factor  = _district_historical_factor(r, self.records, self.num_games, for_kills=False)
        kill_factor = _district_historical_factor(r, self.records, self.num_games, for_kills=True)
        rep_factor  = reputation_factor(r.reputation)

        embed = discord.Embed(
            title=f"District {r.district} — Historical Records",
            color=0x8B4513,
        )
        nkr_str = self._f(self.national_kill_record) if self.national_kill_record is not None else "—"
        _rep_labels = {1: "Highest", 2: "High", 3: "Neutral", 4: "Low", 5: "Lowest"}
        rep_str = (
            f"{r.reputation} ({_rep_labels.get(r.reputation, '—')}, {self._fmt_factor(rep_factor)})"
            if r.reputation is not None else "—"
        )
        global_bb_str = (
            f"{self.global_bb_avg:.2f}/game" if self.global_bb_avg is not None else "—"
        )
        embed.description = (
            f"Total past Games: **{self.num_games}** | {self.arena_str}\n"
            f"**National Kill Record:** {nkr_str} | "
            f"**Global Avg Bloodbath Deaths/Game:** {global_bb_str}\n"
            f"**Reputation:** {rep_str}\n"
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

class AdminCog(commands.Cog):
    admin       = app_commands.Group(name="admin",       description="Capitol Sportsbook admin commands")
    tribute     = app_commands.Group(name="tribute",     description="Manage tributes",             parent=admin)
    market      = app_commands.Group(name="market",      description="Manage markets",              parent=admin)
    market_type = app_commands.Group(name="market_type", description="Manage custom market types",  parent=admin)
    game        = app_commands.Group(name="game",        description="Game control",                parent=admin)
    settings    = app_commands.Group(name="settings",    description="Bot settings",                parent=admin)
    phase       = app_commands.Group(name="phase",       description="Manage betting phases",       parent=admin)
    alliance    = app_commands.Group(name="alliance",    description="Manage tribute alliances",    parent=admin)
    modifier    = app_commands.Group(name="modifier",    description="Manage odds modifiers",       parent=admin)
    history     = app_commands.Group(name="history",     description="District historical records",  parent=admin)
    restrict    = app_commands.Group(name="restrict",    description="Manage user betting restrictions")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
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
        score="Training score (1–12), optional",
        face_claim="Face claim image URL",
        face_claim_file="Upload image as face claim",
        member="Server member (sets seniority bonus)",
        sade_participant="Mark as SADE participant",
        sade_champion="Mark as SADE Champion",
        non_binary="Display as NB (odds use selected gender)",
    )
    @app_commands.choices(gender=[
        app_commands.Choice(name="Male",   value="M"),
        app_commands.Choice(name="Female", value="F"),
    ])
    @is_admin()
    async def tribute_add(
        self,
        interaction: discord.Interaction,
        name: str,
        district: app_commands.Range[int, 1, 12],
        gender: app_commands.Choice[str],
        score: app_commands.Range[int, 1, 12] | None = None,
        face_claim: str | None = None,
        face_claim_file: discord.Attachment | None = None,
        member: discord.Member | None = None,
        sade_participant: bool = False,
        sade_champion: bool = False,
        non_binary: bool = False,
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
            tribute = Tribute(
                name=name, district=district, gender=gender.value,
                training_score=score, face_claim=resolved_face_claim,
                discord_user_id=member.id if member else None,
                member_joined_at=member.joined_at if member else None,
                sade_participant=sade_participant,
                sade_champion=sade_champion,
                non_binary=non_binary,
            )
            session.add(tribute)
            await session.flush()

            result = await session.execute(select(Tribute))
            all_tributes = result.scalars().all()
            market_count = await _auto_create_tribute_markets(session, tribute, all_tributes)
            # Adding a tribute changes the field, so every field-relative market
            # (this tribute's brand-new ones plus all existing tributes') must be
            # re-priced against the now-larger roster.
            await _recalculate_markets(session)
            tid = tribute.id

        embed = discord.Embed(
            title="Tribute Added",
            description=f"**{name}** (District {district}) has entered the arena.",
            color=0x4CAF50,
        )
        gender_label = {"M": "Male", "F": "Female"}.get(gender.value, gender.value)
        if non_binary:
            gender_label += " (displays as NB)"
        embed.add_field(name="Gender", value=gender_label)
        embed.add_field(name="Training Score", value=str(score) if score is not None else "Not set")
        embed.add_field(name="ID", value=str(tid))
        embed.add_field(name="Markets Created", value=str(market_count), inline=False)
        if sade_participant:
            embed.add_field(name="SADE", value="Participant" + (" + Champion" if sade_champion else ""), inline=False)
        elif sade_champion:
            embed.add_field(name="SADE", value="Champion", inline=False)
        if member:
            sf = _seniority_factor(member.joined_at)
            embed.add_field(name="Seniority Odds Modifier", value=f"×{sf}", inline=False)
        if resolved_face_claim:
            embed.set_thumbnail(url=resolved_face_claim)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(name="edit", description="Edit an existing tribute")
    @app_commands.describe(
        tribute_id="Tribute to edit",
        name="New name",
        district="New district (1–12)",
        score="New training score (1–12)",
        face_claim="New face claim URL",
        face_claim_file="Upload image as new face claim",
        member="Link/update server member for seniority",
        seniority_date="Seniority join date override (YYYY-MM-DD)",
        sade_participant="Set SADE participant status",
        sade_champion="Set SADE champion status",
    )
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_edit(
        self,
        interaction: discord.Interaction,
        tribute_id: str,
        name: str | None = None,
        district: app_commands.Range[int, 1, 12] | None = None,
        score: app_commands.Range[int, 1, 12] | None = None,
        face_claim: str | None = None,
        face_claim_file: discord.Attachment | None = None,
        member: discord.Member | None = None,
        seniority_date: str | None = None,
        sade_participant: bool | None = None,
        sade_champion: bool | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        parsed_date: datetime | None = None
        if seniority_date is not None:
            try:
                parsed_date = datetime.strptime(seniority_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                await interaction.followup.send(
                    "Invalid date format. Use YYYY-MM-DD (e.g. `2021-06-15`).", ephemeral=True
                )
                return

        resolved_face_claim = face_claim_file.url if face_claim_file else face_claim
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if name:       t.name = name
            if district:   t.district = district
            if score:      t.training_score = score
            if resolved_face_claim is not None: t.face_claim = resolved_face_claim
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
                        select(Tribute).where(Tribute.sade_champion == True)  # noqa: E712
                    )
                    for prev in prev_result.scalars().all():
                        if prev.id != t.id:
                            prev.sade_champion = False
                t.sade_champion = sade_champion
            updated_name = t.name
            seniority_factor = _seniority_factor(t.member_joined_at)
            await _recalculate_markets(session)

        sf_str = f" (seniority: ×{seniority_factor})" if t.member_joined_at else ""
        await interaction.followup.send(
            f"Tribute **{updated_name}** updated{sf_str}.", ephemeral=True
        )

    @tribute.command(name="set_score", description="Set a tribute's training score and resolve score markets")
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
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
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
                    result = (score == mkt.placement_num)
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
                if ta is None or tb is None or ta.training_score is None or tb.training_score is None:
                    continue  # partner score not yet set — resolve later
                result = (ta.training_score + tb.training_score == mkt.placement_num)
                stats = await _resolve_market(session, mkt, result)
                markets_resolved += 1
                chips_issued += stats.get("credits", 0)

            await _recalculate_markets(session)

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
    @app_commands.autocomplete(tribute_id=alive_tribute_autocomplete, killed_by_id=alive_tribute_autocomplete, cause=death_cause_autocomplete)
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
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            t.status = "DEAD"
            t.death_cause = cause
            killer_name = None
            killer_district = None
            killer_gender = None
            kill_boost_factor = 1.0
            if killed_by_id:
                t.killed_by_id = int(killed_by_id)
                killer = await session.get(Tribute, int(killed_by_id))
                if killer:
                    killer.kills += 1
                    # Boost the killer's odds by how strong the victim was: a kill
                    # against a favourite is worth more than finishing a long-shot.
                    kill_boost_factor = await _kill_quality_for_victim(session, t)
                    killer.kill_boost = (killer.kill_boost or 1.0) * kill_boost_factor
                    killer_name = killer.name
                    killer_district = killer.district
                    killer_gender = killer.display_gender
                    # Resolve any KILLS_OU markets for the killer whose line was just crossed
                    kou_result = await session.execute(
                        select(Market).where(
                            Market.tribute_a_id == killer.id,
                            Market.type == "KILLS_OU",
                            Market.status == "OPEN",
                        )
                    )
                    for kou in kou_result.scalars().all():
                        line = kou.ou_line if kou.ou_line is not None else 0.5
                        if killer.kills > line:
                            await _resolve_market(session, kou, kou.ou_side == "OVER")
            alive_result = await session.execute(
                select(Tribute).where(Tribute.status == "ALIVE")
            )
            alive_count = len(alive_result.scalars().all()) + 1
            t.placement = alive_count + 1
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

            a_mkts = await session.execute(
                select(Market).where(Market.tribute_a_id == dead_id, Market.status == "OPEN")
            )
            for mkt in a_mkts.scalars().all():
                if mkt.type in ("MISSES_FINAL_8", "MISSES_FINAL_5", "MISSES_FINALE"):
                    await _resolve_market(session, mkt, True)
                elif mkt.type == "KILLS_OU":
                    line = mkt.ou_line if mkt.ou_line is not None else 0.5
                    if mkt.ou_side == "OVER":
                        await _resolve_market(session, mkt, t.kills > line)
                    else:  # UNDER
                        await _resolve_market(session, mkt, t.kills <= line)
                else:
                    await _resolve_market(session, mkt, False)

            b_mkts = await session.execute(
                select(Market).where(Market.tribute_b_id == dead_id, Market.status == "OPEN")
            )
            for mkt in b_mkts.scalars().all():
                result = (mkt.tribute_a_id == killer_id) if killer_id else False
                await _resolve_market(session, mkt, result)

            await _recalculate_markets(session)

        killer_str = f" by D{killer_district}{killer_gender} {killer_name}" if killer_name else ""
        await interaction.followup.send(
            f"💀 **{tribute_name}** (D{tribute_district}{tribute_gender}) has fallen{killer_str}. Cause: {cause}"
        )

    @tribute.command(name="unkill", description="Revert a tribute's death and restore all resolved markets")
    @app_commands.describe(tribute_id="Dead tribute whose kill should be reverted")
    @app_commands.autocomplete(tribute_id=dead_tribute_autocomplete)
    @is_admin()
    async def tribute_unkill(self, interaction: discord.Interaction, tribute_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
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
                killer.kills -= 1
                if killer.kill_boost and kill_boost_factor > 0:
                    killer.kill_boost = max(1.0, killer.kill_boost / kill_boost_factor)
                killer_str = f" (credited kill removed from D{killer.district}{killer.display_gender} {killer.name})"

            await _recalculate_markets(session)

        await interaction.followup.send(
            f"↩️ **{tribute_name}** (D{tribute_district}{tribute_gender}) has been reinstated as ALIVE."
            f"{killer_str} {total_unresolved} bet(s) across {len(unique_markets)} market(s) reverted.",
            ephemeral=True,
        )

    @tribute.command(name="remove", description="Remove a tribute from the Games entirely")
    @app_commands.describe(tribute_id="Tribute to remove")
    @app_commands.autocomplete(tribute_id=tribute_autocomplete)
    @is_admin()
    async def tribute_remove(self, interaction: discord.Interaction, tribute_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            name = t.name
            mkt_result = await session.execute(
                select(Market).where(Market.tribute_a_id == t.id, Market.status == "OPEN")
            )
            for mkt in mkt_result.scalars().all():
                await _resolve_market(session, mkt, None)
            await session.delete(t)

        await interaction.followup.send(
            f"Tribute **{name}** removed and all related bets voided.", ephemeral=True
        )

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
            alliance_str = f" | {alliance_map[t.alliance_id]}" if t.alliance_id and t.alliance_id in alliance_map else ""
            embed.add_field(
                name=f"D{t.district}{t.display_gender} {t.name} {icon}",
                value=f"Score: {t.training_score if t.training_score is not None else '?'} | {t.display_gender} | Kills: {t.kills}{alliance_str}",
                inline=True,
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
            app_commands.Choice(name="Over",  value="OVER"),
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

        if not market_type.startswith("CUSTOM_") and market_type not in BUILT_IN_TYPE_VALUES:
            await interaction.followup.send(
                "Unknown market type. Please select from the autocomplete suggestions.", ephemeral=True
            )
            return

        is_district_market = market_type in _DISTRICT_MARKET_TYPES
        is_alliance_market = market_type in _ALLIANCE_MARKET_TYPES

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
                    await interaction.followup.send("Alliance not found.", ephemeral=True)
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = list(all_t_result.scalars().all())
                members = [t for t in all_tributes if t.alliance_id == alliance_obj.id]
                odds = alliance_default_odds(
                    market_type, members, all_tributes,
                    ou_line=ou_line, ou_side=side_val,
                )
                label = _build_label(market_type, None, None, alliance_obj.name, alliance_obj.id, None, ou_line, side_val)
                mkt = Market(
                    type=market_type, label=label,
                    tribute_a_id=None, tribute_b_id=None,
                    cause=alliance_obj.name, placement_num=alliance_obj.id,
                    ou_line=ou_line, ou_side=side_val,
                    phase_id=pid, odds=odds,
                )

            elif is_district_market:
                if district_num is None:
                    await interaction.followup.send(
                        "District markets require a `district_num` (1–12).", ephemeral=True
                    )
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = list(all_t_result.scalars().all())
                d_tributes = [t for t in all_tributes if t.district == district_num]
                odds = district_default_odds(
                    market_type, d_tributes, all_tributes,
                    ou_line=ou_line, ou_side=side_val,
                )
                label = _build_label(market_type, None, None, None, district_num, None, ou_line, side_val)
                mkt = Market(
                    type=market_type, label=label,
                    tribute_a_id=None, tribute_b_id=None,
                    placement_num=district_num,
                    ou_line=ou_line, ou_side=side_val,
                    phase_id=pid, odds=odds,
                )

            elif market_type.startswith("CUSTOM_"):
                if tribute_a_id is None:
                    await interaction.followup.send("Custom markets require a primary tribute.", ephemeral=True)
                    return
                trib_a = await session.get(Tribute, int(tribute_a_id))
                if not trib_a:
                    await interaction.followup.send("Primary tribute not found.", ephemeral=True)
                    return
                trib_b = await session.get(Tribute, int(tribute_b_id)) if tribute_b_id else None
                try:
                    template_id = int(market_type[7:])
                except ValueError:
                    await interaction.followup.send("Invalid market type format.", ephemeral=True)
                    return
                template = await session.get(MarketTemplate, template_id)
                if not template or not template.active:
                    await interaction.followup.send("Custom market type not found or inactive.", ephemeral=True)
                    return
                all_t_result = await session.execute(select(Tribute))
                all_tributes = all_t_result.scalars().all()
                base = template.default_odds if template.default_odds is not None else DIFFICULTY_ODDS[template.difficulty]
                odds = _compute_odds(market_type, trib_a, all_tributes, trib_b=trib_b, base_odds_override=base)
                tribute_str = f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}"
                if cause:
                    label = f"{tribute_str}: {cause}"
                elif template.label_template:
                    label = template.label_template.replace("{tribute}", tribute_str)
                else:
                    label = f"{tribute_str} — {template.name}"
                mkt = Market(
                    type=market_type, label=label,
                    tribute_a_id=trib_a.id,
                    tribute_b_id=trib_b.id if trib_b else None,
                    cause=cause, phase_id=pid, odds=odds,
                )

            else:
                if tribute_a_id is None:
                    await interaction.followup.send("This market type requires a primary tribute.", ephemeral=True)
                    return
                trib_a = await session.get(Tribute, int(tribute_a_id))
                if not trib_a:
                    await interaction.followup.send("Primary tribute not found.", ephemeral=True)
                    return
                trib_b = await session.get(Tribute, int(tribute_b_id)) if tribute_b_id else None
                all_t_result = await session.execute(select(Tribute))
                all_tributes = all_t_result.scalars().all()

                bt_result = await session.execute(
                    select(MarketTemplate).where(MarketTemplate.type_key == market_type)
                )
                bt = bt_result.scalars().first()

                if bt and bt.default_odds is not None:
                    odds = bt.default_odds
                else:
                    odds = _compute_odds(
                        market_type, trib_a, all_tributes,
                        trib_b=trib_b, placement_num=placement_num, top_n=top_n,
                        ou_line=ou_line, ou_side=side_val,
                    )

                if bt and bt.label_template and not cause:
                    tribute_str = f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}"
                    label = bt.label_template.replace("{tribute}", tribute_str)
                else:
                    label = _build_label(market_type, trib_a, trib_b, cause, placement_num, top_n, ou_line, side_val)

                mkt = Market(
                    type=market_type, label=label,
                    tribute_a_id=trib_a.id,
                    tribute_b_id=trib_b.id if trib_b else None,
                    cause=cause, placement_num=placement_num, top_n=top_n,
                    ou_line=ou_line, ou_side=side_val, phase_id=pid, odds=odds,
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

    @market.command(name="set_phase", description="Assign (or clear) a betting phase for a market")
    @app_commands.describe(
        market_id="Market to update",
        phase_id="Phase to assign (leave blank to clear — market becomes active all phases)",
    )
    @app_commands.autocomplete(market_id=open_market_autocomplete, phase_id=phase_autocomplete)
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
            f"Market **{label}** is now active during: **{phase_name}**.", ephemeral=True
        )

    @market.command(name="odds", description="Override odds for a market")
    @app_commands.describe(market_id="Market to update", odds="American odds (e.g. -110 or +400)")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @is_admin()
    async def market_odds(self, interaction: discord.Interaction, market_id: str, odds: int) -> None:
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

    @market.command(name="recalc", description="Re-price all markets using the current odds model")
    @is_admin()
    async def market_recalc(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            count_row = await session.execute(
                select(func.count()).select_from(Market).where(
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
    async def market_close(self, interaction: discord.Interaction, market_id: str) -> None:
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
    async def market_reopen(self, interaction: discord.Interaction, market_id: str) -> None:
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
    @app_commands.describe(market_id="Market to resolve", result="Outcome for bettors on this market")
    @app_commands.choices(result=[
        app_commands.Choice(name="WIN  — bettors on this market WIN",  value="WIN"),
        app_commands.Choice(name="LOSS — bettors on this market LOSE", value="LOSS"),
        app_commands.Choice(name="VOID — refund all bets",             value="VOID"),
    ])
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
        bool_result: bool | None = {"WIN": True, "LOSS": False, "VOID": None}[result.value]
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt:
                await interaction.followup.send("Market not found.", ephemeral=True)
                return
            stats = await _resolve_market(session, mkt, bool_result)
            label = mkt.label

        color = 0x4CAF50 if result.value == "WIN" else (0xCF4444 if result.value == "LOSS" else 0x888888)
        embed = discord.Embed(title=f"Market Resolved: {result.value}", color=color)
        embed.add_field(name="Market", value=label, inline=False)
        embed.add_field(name="Bets Resolved", value=str(stats["resolved"]))
        if stats["credits"] > 0:
            embed.add_field(name="Chips Paid Out", value=fmt_chips(stats["credits"]))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market.command(name="bulk_close", description="Close ALL open markets immediately")
    @is_admin()
    async def market_bulk_close(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(select(Market).where(Market.status == "OPEN"))
            markets = result.scalars().all()
            count = len(markets)
            for m in markets:
                m.status = "CLOSED"

        await interaction.followup.send(f"Closed {count} open market(s).", ephemeral=True)

    @market.command(name="bulk_open", description="Open all closed markets (optionally phase-filtered)")
    @app_commands.describe(
        phase_id="Phase filter (blank = open all closed markets)",
    )
    @app_commands.autocomplete(phase_id=phase_autocomplete)
    @is_admin()
    async def market_bulk_open(
        self,
        interaction: discord.Interaction,
        phase_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            query = select(Market).where(Market.status == "CLOSED")
            phase_name: str | None = None
            if phase_id:
                pid = int(phase_id)
                phase_obj = await session.get(BettingPhase, pid)
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return
                query = query.where(Market.phase_id == pid)
                phase_name = phase_obj.name
            result = await session.execute(query)
            markets = result.scalars().all()
            count = len(markets)
            for m in markets:
                m.status = "OPEN"

        scope = f"in phase **{phase_name}**" if phase_name else "across all phases"
        await interaction.followup.send(f"Opened **{count}** market(s) {scope}.", ephemeral=True)

    @market.command(name="backfill", description="Create missing auto-generated markets for existing tributes and alliances")
    @is_admin()
    async def market_backfill(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            counts = await _backfill_missing_markets(session)

        if not counts:
            await interaction.followup.send(
                "No missing markets found — all auto-generated markets are up to date.", ephemeral=True
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
    @app_commands.choices(status=[
        app_commands.Choice(name="Open",     value="OPEN"),
        app_commands.Choice(name="Closed",   value="CLOSED"),
        app_commands.Choice(name="Resolved", value="RESOLVED"),
        app_commands.Choice(name="All",      value="ALL"),
    ])
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
            custom_type_labels = {f"CUSTOM_{t.id}": t.name for t in t_result.scalars().all()}

        if not all_markets:
            await interaction.followup.send("No markets found.", ephemeral=True)
            return

        status_label = status.name if status else "Open & Closed"
        sorted_mkts = sort_markets(all_markets, tribute_map)
        view = MarketPageView(
            sorted_mkts, tribute_map,
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

    @market_type.command(name="create", description="Create a new custom market type that persists across games")
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

        effective_odds = default_odds if default_odds is not None else DIFFICULTY_ODDS[difficulty.value]
        embed = discord.Embed(title="Custom Market Type Created", color=0x4CAF50)
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Description", value=description, inline=False)
        embed.add_field(name="Difficulty", value=difficulty.name)
        embed.add_field(name="Default Odds", value=fmt_odds(effective_odds))
        embed.add_field(name="ID", value=str(tid))
        if label_template:
            embed.add_field(name="Label Template", value=label_template, inline=False)
        embed.set_footer(text="Use /admin market add and search for this type to create markets from it.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @market_type.command(name="list", description="List all custom market types")
    @is_admin()
    async def market_type_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(select(MarketTemplate).order_by(MarketTemplate.id))
            templates = result.scalars().all()

        if not templates:
            await interaction.followup.send("No custom market types defined yet.", ephemeral=True)
            return

        view = MarketTypePageView(list(templates), DIFFICULTY_ODDS)
        msg = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
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
                await interaction.followup.send("Market type not found.", ephemeral=True)
                return
            if name:
                existing = await session.execute(
                    select(MarketTemplate).where(MarketTemplate.name == name, MarketTemplate.id != template.id)
                )
                if existing.scalars().first():
                    await interaction.followup.send(
                        f"A market type named **{name}** already exists.", ephemeral=True
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
                template.label_template = None if label_template.lower() == "none" else label_template
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
    async def market_type_delete(self, interaction: discord.Interaction, template_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            template = await session.get(MarketTemplate, int(template_id))
            if not template:
                await interaction.followup.send("Market type not found.", ephemeral=True)
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

    @game.command(name="start", description="Start the Games — always opens Pre-Games markets")
    @is_admin()
    async def game_start(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        game_active_raw = await get_setting("game_active")
        if json.loads(game_active_raw or "false"):
            await interaction.followup.send(
                "The Games are already running. Use `/admin game end` to conclude them first.",
                ephemeral=True,
            )
            return

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
                    session.add(Market(
                        type="ARENA_TYPE",
                        label=arena_label,
                        tribute_a_id=None,
                        cause=arena_cause,
                        phase_id=pre_phase_id,
                        odds=arena_odds,
                        status="CLOSED",
                    ))

            mkt_result = await session.execute(select(Market).where(Market.status == "CLOSED"))
            opened = 0
            for m in mkt_result.scalars().all():
                if m.phase_id is None or m.phase_id == pre_phase_id:
                    m.status = "OPEN"
                    opened += 1

        await set_setting("game_active", True)
        if pre_phase_id is not None:
            await set_setting("current_phase_id", pre_phase_id)

        phase_str = f" ({pre_phase_name} phase)" if pre_phase_name else ""
        embed = discord.Embed(
            title="⚡ THE HUNGER GAMES HAVE BEGUN",
            description=f"Opened **{opened}** market(s){phase_str}. May the odds be ever in your favor.",
            color=0xC9A227,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(name="arena", description="Set the arena type for the current game and adjust death-cause odds")
    @app_commands.describe(arena_type="Arena environment — affects death-cause market odds")
    @app_commands.choices(arena_type=[
        app_commands.Choice(name="Artificial — traps, tech hazards, constructed environment", value="ARTIFICIAL"),
        app_commands.Choice(name="Natural   — wilderness, wildlife, environmental hazards",   value="NATURAL"),
        app_commands.Choice(name="Neutral   — no arena-type adjustment",                      value="NONE"),
    ])
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
        async with get_session() as session:
            await _recalculate_markets(session)
        label = arena_type.name.split("—")[0].strip()
        await interaction.followup.send(
            f"Arena type set to **{label}**. Death-cause market odds recalculated.",
            ephemeral=True,
        )

    @game.command(name="set_phase", description="Transition to a new betting phase")
    @app_commands.describe(phase_id="Phase to activate")
    @app_commands.autocomplete(phase_id=phase_autocomplete)
    @is_admin()
    async def game_set_phase(self, interaction: discord.Interaction, phase_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        new_phase_id = int(phase_id)

        current_phase_raw = await get_setting("current_phase_id")
        old_phase_id = json.loads(current_phase_raw) if current_phase_raw else None

        game_active_raw = await get_setting("game_active")
        game_active = json.loads(game_active_raw) if game_active_raw else False

        async with get_session() as session:
            new_phase = await session.get(BettingPhase, new_phase_id)
            if not new_phase:
                await interaction.followup.send("Phase not found.", ephemeral=True)
                return

            old_phase: BettingPhase | None = None
            if old_phase_id:
                old_phase = await session.get(BettingPhase, old_phase_id)

            closed_count = 0
            opened_count = 0
            auto_resolved: list[str] = []

            if game_active:
                # Close markets that belong exclusively to the old phase
                all_open = await session.execute(select(Market).where(Market.status == "OPEN"))
                for m in all_open.scalars().all():
                    if m.phase_id == old_phase_id and old_phase_id is not None:
                        m.status = "CLOSED"
                        closed_count += 1

                # ── Auto-resolutions when leaving old phase ────────────────────
                trib_result = await session.execute(select(Tribute))
                all_tributes = list(trib_result.scalars().all())
                alive_ids = {t.id for t in all_tributes if t.status == "ALIVE"}
                trib_map = {t.id: t for t in all_tributes}

                if old_phase and old_phase.name == "Bloodbath":
                    # Resolve BLOODBATH_SURVIVOR: alive = WIN, dead = LOSE
                    bb_result = await session.execute(
                        select(Market).where(
                            Market.type == "BLOODBATH_SURVIVOR",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    bb_count = 0
                    for mkt in bb_result.scalars().all():
                        await _resolve_market(session, mkt, mkt.tribute_a_id in alive_ids)
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
                            d_alive = [t for t in all_tributes if t.district == d and t.id in alive_ids]
                            d_total = [t for t in all_tributes if t.district == d]
                            await _resolve_market(session, mkt, len(d_alive) == len(d_total) and len(d_total) > 0)
                        dbb_count += 1
                    if dbb_count:
                        auto_resolved.append(f"{dbb_count} District Both Survive Bloodbath")

                    # Resolve ALLIANCE_ALL_BLOODBATH: WIN if all alliance members alive
                    abb_result = await session.execute(
                        select(Market).where(
                            Market.type == "ALLIANCE_ALL_BLOODBATH",
                            Market.status.in_(["OPEN", "CLOSED"]),
                        )
                    )
                    abb_count = 0
                    for mkt in abb_result.scalars().all():
                        aid = mkt.placement_num
                        if aid is None:
                            await _resolve_market(session, mkt, None)
                        else:
                            a_members = [t for t in all_tributes if t.alliance_id == aid]
                            all_alive = len(a_members) > 0 and all(t.id in alive_ids for t in a_members)
                            await _resolve_market(session, mkt, all_alive)
                        abb_count += 1
                    if abb_count:
                        auto_resolved.append(f"{abb_count} Alliance All Survive Bloodbath")

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
                        auto_resolved.append(f"{fb_count} First Blood (voided — no first kill)")

                elif old_phase and old_phase.name == "Pre-Games":
                    arena_type_row = await session.get(GameSetting, "arena_type")
                    arena_type_val = json.loads(arena_type_row.value) if arena_type_row else None

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
                                result = (mkt.cause == arena_type_val)

                        elif mkt.type == "EXACT_TRAINING_SCORE":
                            trib = trib_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                            if trib is None or trib.training_score is None:
                                result = None  # void — score never set
                            else:
                                result = (trib.training_score == mkt.placement_num)

                        elif mkt.type == "COMBINED_DISTRICT_SCORE":
                            ta = trib_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                            tb = trib_map.get(mkt.tribute_b_id) if mkt.tribute_b_id else None
                            if ta is None or tb is None or ta.training_score is None or tb.training_score is None:
                                result = None  # void — one or both scores never set
                            else:
                                result = (ta.training_score + tb.training_score == mkt.placement_num)

                        elif mkt.type == "TRAINING_SCORE_OU":
                            trib = trib_map.get(mkt.tribute_a_id) if mkt.tribute_a_id else None
                            if trib is None or trib.training_score is None:
                                result = None  # void — score never set
                            else:
                                line = mkt.ou_line if mkt.ou_line is not None else 6.5
                                if mkt.ou_side == "OVER":
                                    result = trib.training_score > line
                                else:
                                    result = trib.training_score <= line
                        else:
                            result = None

                        await _resolve_market(session, mkt, result)
                        prop_count += 1
                    if prop_count:
                        auto_resolved.append(f"{prop_count} Pre-Games props")

                # ── Auto-resolutions when entering new phase ───────────────────
                if new_phase.name in _PHASE_ENTRY_MARKETS:
                    makes_type, misses_type = _PHASE_ENTRY_MARKETS[new_phase.name]
                    milestone_count = 0
                    for mtype, wins_if_alive in ((makes_type, True), (misses_type, False)):
                        m_result = await session.execute(
                            select(Market).where(
                                Market.type == mtype,
                                Market.status.in_(["OPEN", "CLOSED"]),
                            )
                        )
                        for mkt in m_result.scalars().all():
                            is_alive = mkt.tribute_a_id in alive_ids
                            await _resolve_market(session, mkt, is_alive if wins_if_alive else not is_alive)
                            milestone_count += 1
                    if milestone_count:
                        auto_resolved.append(f"{milestone_count} {new_phase.name} milestone markets")

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
                                d_tributes = [t for t in all_tributes if t.district == d]
                                alive_d_count = sum(1 for t in d_tributes if t.id in alive_ids)
                                if mtype.startswith("DISTRICT_BOTH"):
                                    result = alive_d_count == len(d_tributes) and len(d_tributes) > 0
                                else:
                                    result = alive_d_count >= 1
                                await _resolve_market(session, mkt, result)
                            dist_milestone_count += 1
                    if dist_milestone_count:
                        auto_resolved.append(f"{dist_milestone_count} {new_phase.name} district milestones")

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
                                a_members = [t for t in all_tributes if t.alliance_id == aid]
                                alive_a_count = sum(1 for t in a_members if t.id in alive_ids)
                                if mtype.startswith("ALLIANCE_ALL"):
                                    result = alive_a_count == len(a_members) and len(a_members) > 0
                                else:
                                    result = alive_a_count >= 1
                                await _resolve_market(session, mkt, result)
                            alliance_milestone_count += 1
                    if alliance_milestone_count:
                        auto_resolved.append(f"{alliance_milestone_count} {new_phase.name} alliance milestones")

                # Open markets for the new phase (includes phase_id=None)
                all_closed = await session.execute(select(Market).where(Market.status == "CLOSED"))
                for m in all_closed.scalars().all():
                    if m.phase_id == new_phase_id or m.phase_id is None:
                        m.status = "OPEN"
                        opened_count += 1

        await set_setting("current_phase_id", new_phase_id)

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
                "No game is currently running. Use `/admin game start` first.",
                ephemeral=True,
            )
            return
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
                await _resolve_market(session, mkt, mkt.placement_num == victor_district)

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
                    result = total_kills > line if mkt.ou_side == "OVER" else total_kills <= line
                    await _resolve_market(session, mkt, result)

            # Resolve ALLIANCE_VICTOR: WIN if victor was a member of the alliance
            av_result = await session.execute(
                select(Market).where(
                    Market.type == "ALLIANCE_VICTOR",
                    Market.status.in_(["OPEN", "CLOSED"]),
                )
            )
            for mkt in av_result.scalars().all():
                await _resolve_market(session, mkt, mkt.placement_num == victor.alliance_id)

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
                    total_kills = sum(t.kills for t in all_t_final if t.alliance_id == aid)
                    line = mkt.ou_line if mkt.ou_line is not None else 1.5
                    result = total_kills > line if mkt.ou_side == "OVER" else total_kills <= line
                    await _resolve_market(session, mkt, result)

            open_result = await session.execute(select(Market).where(Market.status == "OPEN"))
            for mkt in open_result.scalars().all():
                mkt.status = "CLOSED"

        await set_setting("game_active", False)
        embed = discord.Embed(
            title=f"👑 VICTOR: {victor_name.upper()} OF DISTRICT {victor_district}",
            description="The Games have concluded. The Capitol thanks you for your patronage.",
            color=0xFFD700,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(name="reset_confirm", description="DANGER: Delete all tributes, bets, and markets")
    @app_commands.describe(confirm="Type 'yes' to confirm the full reset")
    @is_admin()
    async def game_reset_confirm(self, interaction: discord.Interaction, confirm: str) -> None:
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

            for model in [PendingParlayLeg, Bet, Parlay, Market, ModifierAssignment, Tribute]:
                result = await session.execute(select(model))
                for row in result.scalars().all():
                    await session.delete(row)

        await set_setting("game_active", False)
        await set_setting("current_phase_id", None)
        await set_setting("arena_type", None)
        await interaction.followup.send(
            "All tributes, bets, parlays, and markets have been reset.", ephemeral=True
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
                await interaction.followup.send(f"A phase named **{name}** already exists.", ephemeral=True)
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
            result = await session.execute(select(BettingPhase).order_by(BettingPhase.sort_order))
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
    async def phase_delete(self, interaction: discord.Interaction, phase_id: str) -> None:
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
                    "Use `/admin market set_phase` to reassign them first.", ephemeral=True
                )
                return
            name = p.name
            await session.delete(p)

        await interaction.followup.send(f"Phase **{name}** deleted.", ephemeral=True)

    # ── ALLIANCE COMMANDS ─────────────────────────────────────────────────────

    @alliance.command(name="create", description="Create a new tribute alliance")
    @app_commands.describe(name="Alliance name")
    @is_admin()
    async def alliance_create(self, interaction: discord.Interaction, name: str) -> None:
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
    @app_commands.autocomplete(alliance_id=alliance_autocomplete, tribute_id=tribute_autocomplete)
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
            t.alliance_id = a.id
            await session.flush()

            all_t_result = await session.execute(select(Tribute))
            all_tributes = list(all_t_result.scalars().all())
            members = [m for m in all_tributes if m.alliance_id == a.id]
            markets_created = 0
            if len(members) == 2:
                markets_created = await _auto_create_alliance_markets(session, a, all_tributes)

            await _recalculate_markets(session)
            tname, aname = t.name, a.name

        msg = f"**{tname}** added to alliance **{aname}**. Open market odds recalculated."
        if markets_created:
            msg += f"\n{markets_created} alliance markets created (status: CLOSED)."
        await interaction.followup.send(msg, ephemeral=True)

    @alliance.command(name="remove_tribute", description="Remove a tribute from their alliance")
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
                await interaction.followup.send(f"**{t.name}** is not in an alliance.", ephemeral=True)
                return
            t.alliance_id = None
            name = t.name
            await _recalculate_markets(session)

        await interaction.followup.send(
            f"**{name}** removed from their alliance. Open market odds recalculated.", ephemeral=True
        )

    @alliance.command(name="list", description="List all alliances and their members")
    @is_admin()
    async def alliance_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            a_result = await session.execute(select(Alliance).order_by(Alliance.name))
            alliances = a_result.scalars().all()
            t_result = await session.execute(select(Tribute).order_by(Tribute.district))
            all_tributes = t_result.scalars().all()

        if not alliances:
            await interaction.followup.send("No alliances found.", ephemeral=True)
            return

        embed = discord.Embed(title="Alliances", color=0xC9A227)
        for a in alliances:
            members = [t for t in all_tributes if t.alliance_id == a.id]
            if members:
                member_str = ", ".join(f"D{t.district}{t.display_gender} {t.name}" for t in members)
            else:
                member_str = "*(no members)*"
            embed.add_field(name=f"{a.name} (ID: {a.id})", value=member_str, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @alliance.command(name="delete", description="Delete an alliance (removes all members from it)")
    @app_commands.describe(alliance_id="Alliance to delete")
    @app_commands.autocomplete(alliance_id=alliance_autocomplete)
    @is_admin()
    async def alliance_delete(self, interaction: discord.Interaction, alliance_id: str) -> None:
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
            await interaction.followup.send("Provide at least one arena count to update.", ephemeral=True)
            return
        async with get_session() as session:
            if artificial is not None:
                row = await session.get(GameSetting, "arena_artificial_count")
                if row:
                    row.value = json.dumps(artificial)
                else:
                    session.add(GameSetting(key="arena_artificial_count", value=json.dumps(artificial)))
            if natural is not None:
                row = await session.get(GameSetting, "arena_natural_count")
                if row:
                    row.value = json.dumps(natural)
                else:
                    session.add(GameSetting(key="arena_natural_count", value=json.dumps(natural)))

        art = artificial if artificial is not None else "unchanged"
        nat = natural if natural is not None else "unchanged"
        await interaction.followup.send(
            f"Arena history updated — Artificial: **{art}** | Natural: **{nat}**.", ephemeral=True
        )

    @history.command(name="set", description="Update aggregate historical stats for a district")
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
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        all_vals = (
            wins, victor_male_count, victor_female_count,
            runner_up_finishes, runner_up_male, runner_up_female,
            avg_placement, avg_placement_last5, top8_finishes, top5_finishes,
            male_kills, female_kills, bloodbath_kills, bloodbath_deaths, kill_record,
            manmade_arena_wins, avg_training_score_male, avg_training_score_female,
            reputation,
        )
        if all(v is None for v in all_vals):
            await interaction.followup.send("Provide at least one stat to update.", ephemeral=True)
            return
        async with get_session() as session:
            record = await session.get(DistrictRecord, district)
            if record is None:
                record = DistrictRecord(district=district)
                session.add(record)
            if wins is not None:                      record.wins = wins
            if victor_male_count is not None:         record.victor_male_count = victor_male_count
            if victor_female_count is not None:       record.victor_female_count = victor_female_count
            if runner_up_finishes is not None:        record.runner_up_finishes = runner_up_finishes
            if runner_up_male is not None:            record.runner_up_male = runner_up_male
            if runner_up_female is not None:          record.runner_up_female = runner_up_female
            if avg_placement is not None:             record.avg_placement = avg_placement
            if avg_placement_last5 is not None:       record.avg_placement_last5 = avg_placement_last5
            if top8_finishes is not None:             record.top8_finishes = top8_finishes
            if top5_finishes is not None:             record.top5_finishes = top5_finishes
            if male_kills is not None:                record.male_kills = male_kills
            if female_kills is not None:              record.female_kills = female_kills
            if bloodbath_kills is not None:           record.bloodbath_kills = bloodbath_kills
            if bloodbath_deaths is not None:          record.bloodbath_deaths = bloodbath_deaths
            if kill_record is not None:               record.kill_record = kill_record
            if manmade_arena_wins is not None:        record.manmade_arena_wins = manmade_arena_wins
            if avg_training_score_male is not None:   record.avg_training_score_male = avg_training_score_male
            if avg_training_score_female is not None: record.avg_training_score_female = avg_training_score_female
            if reputation is not None:                record.reputation = reputation
            # Auto-compute total_kills from gender-specific columns
            if any(v is not None for v in (record.male_kills, record.female_kills)):
                record.total_kills = (record.male_kills or 0) + (record.female_kills or 0)
            # Auto-compute avg_training_score from gender-specific scores
            _ts_scores = [v for v in (record.avg_training_score_male, record.avg_training_score_female) if v is not None]
            if _ts_scores:
                record.avg_training_score = round(sum(_ts_scores) / len(_ts_scores))
            await _recalculate_markets(session)
        await interaction.followup.send(
            f"District {district} history updated. Odds recalculated.", ephemeral=True
        )

    @history.command(name="list", description="View aggregate historical stats for all districts")
    @is_admin()
    async def history_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            result = await session.execute(select(DistrictRecord).order_by(DistrictRecord.district))
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
            sum(r.bloodbath_deaths / num_games for r in bb_death_records) / len(bb_death_records)
            if bb_death_records and num_games > 0
            else None
        )
        view = HistoryPageView(records, num_games, arena_str, national_kill_record, global_bb_avg)
        msg = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
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
            await interaction.followup.send("Weight must be greater than 0.", ephemeral=True)
            return
        async with get_session() as session:
            mod = Modifier(label=label, weight=weight)
            session.add(mod)
            await session.flush()
            mid = mod.id

        direction = f"+{round((weight - 1) * 100)}%" if weight >= 1 else f"{round((weight - 1) * 100)}%"
        await interaction.followup.send(
            f"Modifier **{label}** created (ID: {mid}) — ×{weight} ({direction}). "
            f"Use `/admin modifier assign` to apply it to a tribute or district.",
            ephemeral=True,
        )

    @modifier.command(name="delete", description="Delete a modifier and all its assignments")
    @app_commands.describe(modifier_id="Modifier to delete")
    @app_commands.autocomplete(modifier_id=modifier_autocomplete)
    @is_admin()
    async def modifier_delete(self, interaction: discord.Interaction, modifier_id: str) -> None:
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

    @modifier.command(name="assign", description="Apply a modifier to a tribute or district")
    @app_commands.describe(
        modifier_id="Modifier to assign",
        tribute_id="Tribute (blank = district-wide)",
        district="District (blank = tribute-specific)",
    )
    @app_commands.autocomplete(modifier_id=modifier_autocomplete, tribute_id=tribute_autocomplete)
    @is_admin()
    async def modifier_assign(
        self,
        interaction: discord.Interaction,
        modifier_id: str,
        tribute_id: str | None = None,
        district: app_commands.Range[int, 1, 12] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if tribute_id is None and district is None:
            await interaction.followup.send(
                "Provide either a tribute or a district to assign the modifier to.", ephemeral=True
            )
            return
        if tribute_id is not None and district is not None:
            await interaction.followup.send(
                "Provide only one of tribute or district, not both.", ephemeral=True
            )
            return
        async with get_session() as session:
            mod = await session.get(Modifier, int(modifier_id))
            if not mod:
                await interaction.followup.send("Modifier not found.", ephemeral=True)
                return
            tid = int(tribute_id) if tribute_id else None
            if tid is not None:
                t = await session.get(Tribute, tid)
                if not t:
                    await interaction.followup.send("Tribute not found.", ephemeral=True)
                    return
                scope_str = f"tribute **{t.name}** (D{t.district}{t.display_gender})"
            else:
                scope_str = f"District {district}"

            assignment = ModifierAssignment(modifier_id=mod.id, tribute_id=tid, district=district)
            session.add(assignment)
            await session.flush()
            aid = assignment.id
            mod_label = mod.label
            weight = mod.weight
            await _recalculate_markets(session)

        direction = f"+{round((weight - 1) * 100)}%" if weight >= 1 else f"{round((weight - 1) * 100)}%"
        await interaction.followup.send(
            f"Modifier **{mod_label}** (×{weight}, {direction}) assigned to {scope_str} "
            f"(assignment ID: {aid}). Open odds recalculated.",
            ephemeral=True,
        )

    @modifier.command(name="unassign", description="Remove a modifier assignment from a tribute or district")
    @app_commands.describe(assignment_id="Assignment to remove")
    @app_commands.autocomplete(assignment_id=modifier_assignment_autocomplete)
    @is_admin()
    async def modifier_unassign(self, interaction: discord.Interaction, assignment_id: str) -> None:
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
            f"Assignment for **{mod_label}** removed. Open odds recalculated.", ephemeral=True
        )

    @modifier.command(name="list", description="List all modifiers and their current assignments")
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

        if not mods:
            await interaction.followup.send("No modifiers defined.", ephemeral=True)
            return

        assign_map: dict[int, list[ModifierAssignment]] = {}
        for a in assignments:
            assign_map.setdefault(a.modifier_id, []).append(a)

        embed = discord.Embed(title="Odds Modifiers", color=0x7B68EE)
        for mod in mods:
            direction = f"+{round((mod.weight - 1) * 100)}%" if mod.weight >= 1 else f"{round((mod.weight - 1) * 100)}%"
            header = f"×{mod.weight} ({direction})"
            assigned = assign_map.get(mod.id, [])
            if assigned:
                lines = []
                for a in assigned:
                    if a.tribute_id is not None:
                        t = tributes.get(a.tribute_id)
                        scope = f"D{t.district}{t.display_gender} {t.name}" if t else f"Tribute #{a.tribute_id}"
                    else:
                        scope = f"District {a.district} (all)"
                    lines.append(f"• {scope} (assignment ID: {a.id})")
                value = f"{header}\n" + "\n".join(lines)
            else:
                value = f"{header}\n*(unassigned — no effect)*"
            embed.add_field(name=f"[ID {mod.id}] {mod.label}", value=value, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── SETTINGS COMMANDS ─────────────────────────────────────────────────────

    @settings.command(name="cashout", description="Configure global cashout settings")
    @app_commands.describe(
        allowed="Allow early cashout globally",
        rate="Cashout rate 0.0–1.0 (0.65 = 65% profit)",
    )
    @app_commands.choices(allowed=[
        app_commands.Choice(name="Allow cashout",    value="yes"),
        app_commands.Choice(name="Disallow cashout", value="no"),
    ])
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

    @settings.command(name="market_cashout", description="Override cashout settings for a specific market")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    @app_commands.choices(allowed=[
        app_commands.Choice(name="Allow",    value="yes"),
        app_commands.Choice(name="Disallow", value="no"),
    ])
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

    @settings.command(name="chips_give", description="Give chips to a user")
    @app_commands.describe(user="User to give chips to", amount="Amount of chips")
    @is_admin()
    async def chips_give(
        self, interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user)
            u.chips += amount
            new_bal = u.chips

        await interaction.followup.send(
            f"Gave **{fmt_chips(amount)}** to {user.mention}. New balance: **{fmt_chips(new_bal)}**.",
            ephemeral=True,
        )

    @settings.command(name="chips_take", description="Take chips from a user")
    @app_commands.describe(user="User to take chips from", amount="Amount to take")
    @is_admin()
    async def chips_take(
        self, interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user)
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
        self, interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 0, 10_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            u = await _get_or_create_user(session, user)
            u.chips = amount

        await interaction.followup.send(
            f"Set {user.mention}'s balance to **{fmt_chips(amount)}**.", ephemeral=True
        )

    @settings.command(name="chips_reset", description="Reset ALL users to the default chip balance")
    @is_admin()
    async def chips_reset(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        default = json.loads(await get_setting("default_chips") or "1000")
        async with get_session() as session:
            result = await session.execute(select(User))
            for u in result.scalars().all():
                u.chips = default

        await interaction.followup.send(
            f"All user balances reset to {fmt_chips(default)}.", ephemeral=True
        )

    @settings.command(name="default_chips", description="Set the default starting chip balance for new users")
    @is_admin()
    async def default_chips(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 100, 1_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await set_setting("default_chips", amount)
        await interaction.followup.send(
            f"Default starting balance set to {fmt_chips(amount)}.", ephemeral=True
        )

    @settings.command(name="announce", description="Post a Capitol announcement to the announcement channel")
    @app_commands.describe(message="The announcement message")
    @is_admin()
    async def announce(self, interaction: discord.Interaction, message: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        from bot import config as cfg
        embed = discord.Embed(
            title="📢 CAPITOL ANNOUNCEMENT",
            description=message,
            color=0xC9A227,
        )
        embed.set_footer(text="Capitol Sportsbook — May the odds be ever in your favor.")

        if cfg.ANNOUNCEMENT_CHANNEL_ID:
            channel = interaction.guild.get_channel(cfg.ANNOUNCEMENT_CHANNEL_ID)
            if channel:
                await channel.send(embed=embed)
                await interaction.followup.send("Announcement posted.", ephemeral=True)
                return

        await interaction.followup.send(embed=embed, ephemeral=True)

    @settings.command(name="theme", description="Set the visual theme for sportsbook images")
    @app_commands.describe(name="Theme name (e.g. dark, forest)")
    @is_admin()
    async def settings_theme(self, interaction: discord.Interaction, name: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        theme = get_theme_by_name(name)
        if theme is None:
            available = ", ".join(f"**{n}**" for n in list_theme_names())
            await interaction.followup.send(
                f"Unknown theme **{name}**. Available themes: {available}.", ephemeral=True
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

    # ── RESTRICT COMMANDS ─────────────────────────────────────────────────────

    @restrict.command(name="ban", description="Block a user from placing any bets")
    @app_commands.describe(member="User to ban from betting")
    @is_admin()
    async def restrict_ban(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "ALL",
                )
            )
            if existing.scalar_one_or_none():
                await interaction.followup.send(
                    f"**{member.display_name}** is already banned from betting.", ephemeral=True
                )
                return
            session.add(BettingRestriction(discord_user_id=member.id, restriction_type="ALL"))
        await interaction.followup.send(
            f"**{member.display_name}** has been banned from placing any bets.", ephemeral=True
        )

    @restrict.command(name="unban", description="Lift a user's full betting ban")
    @app_commands.describe(member="User to unban")
    @is_admin()
    async def restrict_unban(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "ALL",
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                await interaction.followup.send(
                    f"**{member.display_name}** does not have a full betting ban.", ephemeral=True
                )
                return
            await session.delete(row)
        await interaction.followup.send(
            f"Full betting ban lifted for **{member.display_name}**.", ephemeral=True
        )

    @restrict.command(name="block_district", description="Block a user from betting on district markets")
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
                discord_user_id=member.id,
                restriction_type="DISTRICT",
                district=district,
            ))
        await interaction.followup.send(
            f"**{member.display_name}** is now blocked from betting on District {district} markets.",
            ephemeral=True,
        )

    @restrict.command(name="unblock_district", description="Remove a user's district restriction")
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
                    BettingRestriction.discord_user_id == member.id,
                    BettingRestriction.restriction_type == "DISTRICT",
                    BettingRestriction.district == district,
                )
            )
            row = existing.scalar_one_or_none()
            if not row:
                await interaction.followup.send(
                    f"**{member.display_name}** has no District {district} restriction.", ephemeral=True
                )
                return
            await session.delete(row)
        await interaction.followup.send(
            f"District {district} restriction removed for **{member.display_name}**.", ephemeral=True
        )

    @restrict.command(name="block_tribute", description="Block a user from betting on tribute markets")
    @app_commands.describe(member="User to restrict", tribute_id="Tribute to block betting on")
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
                "Invalid tribute. Please pick one from the autocomplete list.", ephemeral=True
            )
            return
        async with get_session() as session:
            tribute = await session.get(Tribute, tid)
            if not tribute:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            existing = await session.execute(
                select(BettingRestriction).where(
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
                discord_user_id=member.id,
                restriction_type="TRIBUTE",
                tribute_id=tid,
            ))
            tribute_name = tribute.name
        await interaction.followup.send(
            f"**{member.display_name}** is now blocked from betting on markets involving **{tribute_name}**.",
            ephemeral=True,
        )

    @restrict.command(name="unblock_tribute", description="Remove a user's tribute restriction")
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
                "Invalid tribute. Please pick one from the autocomplete list.", ephemeral=True
            )
            return
        async with get_session() as session:
            existing = await session.execute(
                select(BettingRestriction).where(
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
                    f"**{member.display_name}** has no restriction on **{name}**.", ephemeral=True
                )
                return
            tribute = await session.get(Tribute, tid)
            tribute_name = tribute.name if tribute else str(tid)
            await session.delete(row)
        await interaction.followup.send(
            f"Tribute restriction on **{tribute_name}** removed for **{member.display_name}**.",
            ephemeral=True,
        )

    @restrict.command(name="view", description="View all betting restrictions for a user")
    @app_commands.describe(member="User to inspect")
    @is_admin()
    async def restrict_view(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            result = await session.execute(
                select(BettingRestriction).where(BettingRestriction.discord_user_id == member.id)
            )
            restrictions = result.scalars().all()

        if not restrictions:
            await interaction.followup.send(
                f"**{member.display_name}** has no betting restrictions.", ephemeral=True
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
                lines.append(f"• **District {r.district}** — blocked from all District {r.district} markets")
            elif r.restriction_type == "TRIBUTE":
                lines.append(f"• **Tribute ID {r.tribute_id}** — blocked from markets involving this tribute")
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_create_user(session, member: discord.Member) -> User:
    u = await session.get(User, member.id)
    if u is None:
        default_raw = await get_setting("default_chips")
        default_chips = json.loads(default_raw) if default_raw else 1000
        u = User(discord_id=member.id, username=member.display_name, chips=default_chips)
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
    a = f"D{trib_a.district}{trib_a.display_gender} {trib_a.name}" if trib_a else "Capitol"
    b = f"D{trib_b.district}{trib_b.display_gender} {trib_b.name}" if trib_b else ""
    d = str(trib_a.district) if trib_a else (str(placement_num) if placement_num is not None else "?")
    side = "Over" if ou_side == "OVER" else ("Under" if ou_side == "UNDER" else "")
    line_str = f"{ou_line:g}" if ou_line is not None else ""
    arena_side = (
        "Artificial" if cause == "ARTIFICIAL"
        else "Natural" if cause == "NATURAL"
        else cause or "?"
    )
    return {
        "TRIBUTE_WINS":            f"{a} Wins the Games",
        "TRIBUTE_PLACEMENT":       f"{a} Finishes {_ordinal(placement_num or 2)}",
        "TRIBUTE_TOP_N":           f"{a} Top {top_n or 3} Finish",
        "TRIBUTE_KILLS":           f"{a} Gets Most Kills",
        "KILL_EVENT":              f"{a} Kills {b}",
        "DEATH_CAUSE":             f"{a} Dies by {cause or 'Unknown Cause'}",
        "FIRST_BLOOD":             f"{a} Gets First Kill",
        "BLOODBATH_SURVIVOR":      f"{a} Survives the Bloodbath",
        "SPONSOR_EVENT":           f"{a}: {cause or 'Sponsor Event'}",
        "KILLS_OU":                f"{a} Kills — {side} {line_str}",
        "PLACEMENT_OU":            f"{a} Placement — {side} {line_str}",
        "MAKES_FINAL_8":           f"{a} Makes Final 8",
        "MISSES_FINAL_8":          f"{a} Eliminated Before Final 8",
        "MAKES_FINAL_5":           f"{a} Makes Final 5",
        "MISSES_FINAL_5":          f"{a} Eliminated Before Final 5",
        "MAKES_FINALE":            f"{a} Makes the Finale",
        "MISSES_FINALE":           f"{a} Eliminated Before Finale",
        "ARENA_TYPE":              f"Arena Type — {arena_side}",
        "EXACT_TRAINING_SCORE":    f"{a} Training Score = {placement_num or '?'}",
        "COMBINED_DISTRICT_SCORE": f"D{d} Combined Score = {placement_num or '?'}",
        "TRAINING_SCORE_OU":       f"{a} Training Score — {side} {line_str}",
        # District-level markets
        "DISTRICT_VICTOR":         f"D{d} Victor",
        "DISTRICT_KILLS_OU":       f"D{d} Total Kills — {side} {line_str}",
        "DISTRICT_BOTH_BLOODBATH": f"D{d} Both Survive Bloodbath",
        "DISTRICT_BOTH_FINAL_8":   f"D{d} Both Make Final 8",
        "DISTRICT_ONE_FINAL_8":    f"Someone from D{d} Makes Final 8",
        "DISTRICT_BOTH_FINAL_5":   f"D{d} Both Make Final 5",
        "DISTRICT_ONE_FINAL_5":    f"Someone from D{d} Makes Final 5",
        "DISTRICT_BOTH_FINALE":    f"D{d} Both Make the Finale",
        "DISTRICT_ONE_FINALE":     f"Someone from D{d} Makes the Finale",
        # Alliance-level markets (cause = alliance name)
        "ALLIANCE_VICTOR":         f"{cause or 'Alliance'} Victor",
        "ALLIANCE_KILLS_OU":       f"{cause or 'Alliance'} Total Kills — {side} {line_str}",
        "ALLIANCE_ALL_BLOODBATH":  f"{cause or 'Alliance'} All Survive Bloodbath",
        "ALLIANCE_ALL_FINAL_8":    f"{cause or 'Alliance'} All Make Final 8",
        "ALLIANCE_ONE_FINAL_8":    f"Someone from {cause or 'Alliance'} Makes Final 8",
        "ALLIANCE_ALL_FINAL_5":    f"{cause or 'Alliance'} All Make Final 5",
        "ALLIANCE_ONE_FINAL_5":    f"Someone from {cause or 'Alliance'} Makes Final 5",
        "ALLIANCE_ALL_FINALE":     f"{cause or 'Alliance'} All Make the Finale",
        "ALLIANCE_ONE_FINALE":     f"Someone from {cause or 'Alliance'} Makes the Finale",
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

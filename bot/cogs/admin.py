from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.database.engine import get_session, get_read_session, set_setting, get_setting
from bot.database.models import (
    Alliance, Bet, BettingPhase, DistrictRecord, GameSetting, Market,
    MarketTemplate, Modifier, ModifierAssignment, Parlay, PendingParlayLeg,
    Tribute, User,
)
from bot.odds.calculator import straight_payout, parlay_payout
from bot.odds.defaults import (
    DEFAULT_FALLBACK_ODDS, HIST_ALPHA, MODIFIER_ALLIANCE_ALPHA,
    apply_group_influence, default_odds,
)
from bot.utils.checks import is_admin
from bot.utils.formatters import fmt_chips, fmt_odds, safe_defer
from bot.utils.market_view import MarketPageView, sort_markets

log = logging.getLogger("capitol.admin")

MARKET_TYPES = [
    app_commands.Choice(name="Tribute Wins (Victor)",       value="TRIBUTE_WINS"),
    app_commands.Choice(name="Tribute Placement (Exact)",   value="TRIBUTE_PLACEMENT"),
    app_commands.Choice(name="Tribute Top-N Finish",        value="TRIBUTE_TOP_N"),
    app_commands.Choice(name="Top Killer",                  value="TRIBUTE_KILLS"),
    app_commands.Choice(name="Kill Event (A kills B)",      value="KILL_EVENT"),
    app_commands.Choice(name="Death Cause",                 value="DEATH_CAUSE"),
    app_commands.Choice(name="First Blood",                 value="FIRST_BLOOD"),
    app_commands.Choice(name="Bloodbath Survivor",          value="BLOODBATH_SURVIVOR"),
    app_commands.Choice(name="Sponsor Event (Custom)",      value="SPONSOR_EVENT"),
    app_commands.Choice(name="Kills Over/Under",            value="KILLS_OU"),
    app_commands.Choice(name="Placement Over/Under",        value="PLACEMENT_OU"),
]

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


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(Tribute).order_by(Tribute.district))
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def alive_tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(Tribute.district)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district} {t.name}"
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


async def game_label_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(DistrictRecord.game_label).distinct().order_by(DistrictRecord.game_label)
        )
        labels = [r for (r,) in result.all() if r]
    return [
        app_commands.Choice(name=lbl, value=lbl)
        for lbl in labels if current.lower() in lbl.lower()
    ][:25]


async def district_record_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(select(DistrictRecord).order_by(DistrictRecord.id))
        records = result.scalars().all()
    choices = []
    for r in records:
        label = f"[{r.game_label or '—'}] D{r.district} {r.tribute_name}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(r.id)))
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
            scope = f"D{t.district} {t.name}" if t else f"Tribute #{assignment.tribute_id}"
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


def _district_historical_factor(district: int, records: list[DistrictRecord]) -> float:
    """
    Multiplicative probability factor derived from a district's historical game performance.
    Compares district averages (win rate, placement, kills) against the global average
    across all records, then dampens by HIST_ALPHA so history is one input, not the whole story.
    """
    if not records:
        return 1.0
    d_recs = [r for r in records if r.district == district]
    if not d_recs:
        return 1.0

    d_win_rate = sum(r.won for r in d_recs) / len(d_recs)
    d_avg_kills = sum(r.kills for r in d_recs) / len(d_recs)
    d_placements = [r.placement for r in d_recs if r.placement is not None]
    d_avg_place = sum(d_placements) / len(d_placements) if d_placements else None

    g_win_rate = sum(r.won for r in records) / len(records)
    g_avg_kills = sum(r.kills for r in records) / len(records)
    g_placements = [r.placement for r in records if r.placement is not None]
    g_avg_place = sum(g_placements) / len(g_placements) if g_placements else None

    components: list[float] = []

    if g_win_rate > 0:
        components.append(d_win_rate / g_win_rate)
    elif d_win_rate == 0:
        components.append(1.0)

    if g_avg_kills > 0:
        components.append(d_avg_kills / g_avg_kills)
    elif d_avg_kills == 0:
        components.append(1.0)

    if d_avg_place is not None and g_avg_place is not None and d_avg_place > 0:
        components.append(g_avg_place / d_avg_place)  # lower placement = better → invert

    if not components:
        return 1.0

    raw_factor = sum(components) / len(components)
    return 1.0 + (raw_factor - 1.0) * HIST_ALPHA


# ── Odds helpers ──────────────────────────────────────────────────────────────

def _compute_odds(
    market_type: str,
    trib_a: Tribute,
    all_tributes: list[Tribute],
    trib_b: Tribute | None = None,
    placement_num: int | None = None,
    top_n: int | None = None,
    ou_line: float | None = None,
    ou_side: str | None = None,
    modifier_factor: float = 1.0,
) -> int:
    from bot.odds.calculator import american_to_decimal, prob_to_american
    base = default_odds(
        market_type, trib_a, all_tributes,
        tribute_b=trib_b, placement_num=placement_num, top_n=top_n,
        ou_line=ou_line, ou_side=ou_side,
    )
    district_mates = [t for t in all_tributes if t.district == trib_a.district and t.id != trib_a.id]
    alliance_mates = [
        t for t in all_tributes
        if trib_a.alliance_id and t.alliance_id == trib_a.alliance_id and t.id != trib_a.id
    ]
    odds = apply_group_influence(base, market_type, trib_a, district_mates, alliance_mates, all_tributes)
    if modifier_factor != 1.0:
        dec = american_to_decimal(odds)
        adj_prob = max(0.01, min(0.99, (1.0 / dec) * modifier_factor))
        odds = prob_to_american(adj_prob)
    return odds


async def _recalculate_open_markets(session) -> None:
    result = await session.execute(
        select(Market).where(Market.status == "OPEN", Market.odds_override == False)
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

    # Load district historical records and pre-compute per-district factor
    hist_result = await session.execute(select(DistrictRecord))
    all_records = hist_result.scalars().all()
    dist_hist_factor: dict[int, float] = {
        d: _district_historical_factor(d, list(all_records))
        for d in range(1, 13)
    }

    # Blend each tribute's raw factor with their alive alliance members' average,
    # then multiply in seniority and district history
    alive = [t for t in all_tributes if t.status == "ALIVE"]
    tribute_factors: dict[int, float] = {}
    for t in alive:
        own = raw_factors.get(t.id, 1.0)
        if t.alliance_id is not None:
            allies = [m for m in alive if m.alliance_id == t.alliance_id and m.id != t.id]
            if allies:
                ally_avg = sum(raw_factors.get(m.id, 1.0) for m in allies) / len(allies)
                own = own + MODIFIER_ALLIANCE_ALPHA * (ally_avg - own)
        own *= _seniority_factor(t.member_joined_at)
        own *= dist_hist_factor.get(t.district, 1.0)
        tribute_factors[t.id] = own

    for market in markets:
        trib_a = next((t for t in all_tributes if t.id == market.tribute_a_id), None)
        trib_b = next((t for t in all_tributes if t.id == market.tribute_b_id), None) if market.tribute_b_id else None
        if trib_a:
            market.odds = _compute_odds(
                market.type, trib_a, all_tributes,
                trib_b=trib_b,
                placement_num=market.placement_num,
                top_n=market.top_n,
                ou_line=market.ou_line,
                ou_side=market.ou_side,
                modifier_factor=tribute_factors.get(trib_a.id, 1.0),
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

    def _add(type_: str, trib_a: Tribute, trib_b: Tribute | None = None,
              cause: str | None = None, placement_num: int | None = None,
              top_n: int | None = None, ou_line: float | None = None,
              ou_side: str | None = None) -> int:
        odds = _compute_odds(type_, trib_a, all_tributes, trib_b=trib_b,
                             placement_num=placement_num, top_n=top_n,
                             ou_line=ou_line, ou_side=ou_side)
        # Death cause uses fallback odds (no formula)
        if type_ == "DEATH_CAUSE":
            odds = DEFAULT_FALLBACK_ODDS
        label = _build_label(type_, trib_a, trib_b, cause, placement_num, top_n, ou_line, ou_side)
        m = Market(
            type=type_, label=label,
            tribute_a_id=trib_a.id,
            tribute_b_id=trib_b.id if trib_b else None,
            cause=cause, placement_num=placement_num, top_n=top_n,
            ou_line=ou_line, ou_side=ou_side,
            odds=odds,
            status="CLOSED",
        )
        session.add(m)
        return 1

    created = 0

    # Single-tribute markets for the new tribute
    created += _add("TRIBUTE_WINS",       new_tribute)
    created += _add("TRIBUTE_KILLS",      new_tribute)
    created += _add("FIRST_BLOOD",        new_tribute)
    created += _add("BLOODBATH_SURVIVOR", new_tribute)

    for cause in _DEATH_CAUSES:
        created += _add("DEATH_CAUSE", new_tribute, cause=cause)

    # Kills over/under at 0.5 and 1.5
    for line in [0.5, 1.5]:
        for side in ["OVER", "UNDER"]:
            created += _add("KILLS_OU", new_tribute, ou_line=line, ou_side=side)

    # Placement over/under at midpoint of current tribute count
    mid = round(n / 2.0 + 0.5, 1) if n > 1 else 1.5
    for side in ["OVER", "UNDER"]:
        created += _add("PLACEMENT_OU", new_tribute, ou_line=mid, ou_side=side)

    # Kill-event pairs with every other existing tribute
    others = [t for t in all_tributes if t.id != new_tribute.id]
    for other in others:
        created += _add("KILL_EVENT", new_tribute, trib_b=other)
        created += _add("KILL_EVENT", other, trib_b=new_tribute)

    return created


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

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "**Access denied.** You need the Admin role to use this command."
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
        gender="Gender",
        score="Training score (1–12)",
        face_claim="URL to the tribute's face claim image",
        face_claim_file="Upload an image file as the face claim",
        member="Server member this tribute represents (sets seniority odds bonus)",
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
        score: app_commands.Range[int, 1, 12],
        face_claim: str | None = None,
        face_claim_file: discord.Attachment | None = None,
        member: discord.Member | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        resolved_face_claim = face_claim_file.url if face_claim_file else face_claim
        async with get_session() as session:
            tribute = Tribute(
                name=name, district=district, gender=gender.value,
                training_score=score, face_claim=resolved_face_claim,
                discord_user_id=member.id if member else None,
                member_joined_at=member.joined_at if member else None,
            )
            session.add(tribute)
            await session.flush()

            result = await session.execute(select(Tribute))
            all_tributes = result.scalars().all()
            market_count = await _auto_create_tribute_markets(session, tribute, all_tributes)
            tid = tribute.id

        embed = discord.Embed(
            title="Tribute Added",
            description=f"**{name}** (District {district}) has entered the arena.",
            color=0x4CAF50,
        )
        embed.add_field(name="Gender", value="Male" if gender.value == "M" else "Female")
        embed.add_field(name="Training Score", value=str(score))
        embed.add_field(name="ID", value=str(tid))
        embed.add_field(name="Markets Created", value=str(market_count), inline=False)
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
        face_claim_file="Upload an image file as the new face claim",
        member="Link or update the server member for seniority odds",
        seniority_date="Override join date for seniority (YYYY-MM-DD) — use when member rejoined after leaving",
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
            updated_name = t.name
            seniority_factor = _seniority_factor(t.member_joined_at)
            await _recalculate_open_markets(session)

        sf_str = f" (seniority: ×{seniority_factor})" if t.member_joined_at else ""
        await interaction.followup.send(
            f"Tribute **{updated_name}** updated{sf_str}.", ephemeral=True
        )

    @tribute.command(name="kill", description="Mark a tribute as dead")
    @app_commands.describe(
        tribute_id="Tribute who died",
        cause="Cause of death",
        killed_by_id="Tribute who killed them (optional)",
    )
    @app_commands.autocomplete(tribute_id=alive_tribute_autocomplete, killed_by_id=alive_tribute_autocomplete)
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
            if killed_by_id:
                t.killed_by_id = int(killed_by_id)
                killer = await session.get(Tribute, int(killed_by_id))
                if killer:
                    killer.kills += 1
                    killer_name = killer.name
                    killer_district = killer.district
            alive_result = await session.execute(
                select(Tribute).where(Tribute.status == "ALIVE")
            )
            alive_count = len(alive_result.scalars().all()) + 1
            t.placement = alive_count + 1
            tribute_name = t.name
            tribute_district = t.district

            # Resolve all open markets for the dead tribute
            dead_id = t.id
            killer_id = int(killed_by_id) if killed_by_id else None

            a_mkts = await session.execute(
                select(Market).where(Market.tribute_a_id == dead_id, Market.status == "OPEN")
            )
            for mkt in a_mkts.scalars().all():
                await _resolve_market(session, mkt, False)

            b_mkts = await session.execute(
                select(Market).where(Market.tribute_b_id == dead_id, Market.status == "OPEN")
            )
            for mkt in b_mkts.scalars().all():
                result = (mkt.tribute_a_id == killer_id) if killer_id else False
                await _resolve_market(session, mkt, result)

            await _recalculate_open_markets(session)

        killer_str = f" by D{killer_district} {killer_name}" if killer_name else ""
        await interaction.followup.send(
            f"💀 **{tribute_name}** (D{tribute_district}) has fallen{killer_str}. Cause: {cause}"
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
                name=f"D{t.district} {t.name} {icon}",
                value=f"Score: {t.training_score} | {t.gender} | Kills: {t.kills}{alliance_str}",
                inline=True,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── MARKET COMMANDS ───────────────────────────────────────────────────────

    @market.command(name="add", description="Add a new betting market")
    @app_commands.describe(
        market_type="Type of market — start typing to search built-in or custom types",
        tribute_a_id="Primary tribute",
        tribute_b_id="Second tribute (for Kill Event markets)",
        cause="Death cause or custom label override",
        placement_num="Exact placement number (for Placement markets)",
        top_n="Top-N value (for Top-N markets)",
        ou_line="Over/Under line value (e.g. 1.5 for kills or 12.5 for placement)",
        ou_side="Over or Under side",
        phase_id="Betting phase this market is active during (omit = all phases)",
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
        phase_id=phase_autocomplete,
    )
    @is_admin()
    async def market_add(
        self,
        interaction: discord.Interaction,
        market_type: str,
        tribute_a_id: str,
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

        async with get_session() as session:
            trib_a = await session.get(Tribute, int(tribute_a_id))
            if not trib_a:
                await interaction.followup.send("Primary tribute not found.", ephemeral=True)
                return
            trib_b = await session.get(Tribute, int(tribute_b_id)) if tribute_b_id else None

            pid = int(phase_id) if phase_id else None
            phase_name: str | None = None
            if pid:
                phase_obj = await session.get(BettingPhase, pid)
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return
                phase_name = phase_obj.name

            side_val = ou_side.value if ou_side else None

            if market_type.startswith("CUSTOM_"):
                try:
                    template_id = int(market_type[7:])
                except ValueError:
                    await interaction.followup.send("Invalid market type format.", ephemeral=True)
                    return
                template = await session.get(MarketTemplate, template_id)
                if not template or not template.active:
                    await interaction.followup.send("Custom market type not found or inactive.", ephemeral=True)
                    return
                odds = template.default_odds if template.default_odds is not None else DIFFICULTY_ODDS[template.difficulty]
                tribute_str = f"D{trib_a.district} {trib_a.name}"
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
                    cause=cause, phase_id=pid, odds=odds, odds_override=True,
                )
            else:
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
                    tribute_str = f"D{trib_a.district} {trib_a.name}"
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

    @market.command(name="list", description="Browse all markets with pagination, sorted by type and district")
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
        name="Display name for this market type (e.g. 'Victor Betrayal')",
        description="What this market type represents — shown in the type list",
        difficulty="How likely this event is to happen; sets default odds unless overridden",
        default_odds="Exact American odds override (e.g. +500); leave blank to use difficulty",
        label_template="Market label format — use {tribute} as a placeholder (e.g. '{tribute} Survives Final 4')",
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

        embed = discord.Embed(title="Market Types", color=0xC9A227)
        for t in templates:
            kind = "Built-in" if t.is_builtin else "Custom"
            status = "ACTIVE" if t.active else "INACTIVE"
            if t.default_odds is not None:
                odds_str = fmt_odds(t.default_odds)
            elif t.is_builtin:
                odds_str = "Computed"
            else:
                odds_str = fmt_odds(DIFFICULTY_ODDS[t.difficulty])
            label_str = f"\nLabel: `{t.label_template}`" if t.label_template else ""
            embed.add_field(
                name=f"#{t.id} {t.name} [{kind} · {status}]",
                value=(
                    f"{t.description}\n"
                    f"Difficulty: **{t.difficulty}** | Odds: **{odds_str}**"
                    f"{label_str}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

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

    @market_type.command(name="delete", description="Delete a custom market type (blocked if markets of this type exist)")
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

    @game.command(name="start", description="Start the Games — open markets for the current phase")
    @is_admin()
    async def game_start(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        current_phase_raw = await get_setting("current_phase_id")
        current_phase_id = json.loads(current_phase_raw) if current_phase_raw else None

        async with get_session() as session:
            query = select(Market).where(Market.status == "CLOSED")
            result = await session.execute(query)
            markets = result.scalars().all()
            opened = 0
            for m in markets:
                # Open if no phase restriction OR phase matches current phase
                if m.phase_id is None or m.phase_id == current_phase_id:
                    m.status = "OPEN"
                    opened += 1

            phase_name = None
            if current_phase_id:
                p = await session.get(BettingPhase, current_phase_id)
                phase_name = p.name if p else None

        await set_setting("game_active", True)
        phase_str = f" ({phase_name} phase)" if phase_name else ""
        embed = discord.Embed(
            title="⚡ THE HUNGER GAMES HAVE BEGUN",
            description=f"Opened **{opened}** market(s){phase_str}. May the odds be ever in your favor.",
            color=0xC9A227,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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

            closed_count = 0
            opened_count = 0

            if game_active:
                all_open = await session.execute(select(Market).where(Market.status == "OPEN"))
                for m in all_open.scalars().all():
                    if m.phase_id == old_phase_id and old_phase_id is not None:
                        m.status = "CLOSED"
                        closed_count += 1

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
        else:
            embed.set_footer(text="Phase set. Markets will open when the game starts.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(name="end", description="End the Games — declare a victor")
    @app_commands.describe(
        victor_id="The winning tribute",
        game_label="Label for this game in the historical record (e.g. 'Game 74') — auto-generated if omitted",
    )
    @app_commands.autocomplete(victor_id=alive_tribute_autocomplete)
    @is_admin()
    async def game_end(
        self,
        interaction: discord.Interaction,
        victor_id: str,
        game_label: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
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

            open_result = await session.execute(select(Market).where(Market.status == "OPEN"))
            for mkt in open_result.scalars().all():
                mkt.status = "CLOSED"

            # Auto-archive all tributes still in the DB to district history
            if game_label is None:
                distinct = await session.execute(
                    select(DistrictRecord.game_label).distinct()
                )
                existing_labels = {r for (r,) in distinct.all() if r}
                n = len(existing_labels) + 1
                game_label = f"Game {n}"

            all_tributes = (await session.execute(select(Tribute))).scalars().all()
            archived = 0
            for t in all_tributes:
                session.add(DistrictRecord(
                    district=t.district,
                    game_label=game_label,
                    tribute_name=t.name,
                    placement=t.placement,
                    kills=t.kills,
                    won=(t.id == victor.id),
                ))
                archived += 1

        await set_setting("game_active", False)
        embed = discord.Embed(
            title=f"👑 VICTOR: {victor_name.upper()} OF DISTRICT {victor_district}",
            description=(
                f"The Games have concluded. The Capitol thanks you for your patronage.\n"
                f"**{archived}** tribute(s) archived to district history as **{game_label}**."
            ),
            color=0xFFD700,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @game.command(name="reset_confirm", description="DANGER: Delete all bets, parlays, and markets. Type 'yes' to confirm.")
    @app_commands.describe(confirm="Type 'yes' to confirm the full reset")
    @is_admin()
    async def game_reset_confirm(self, interaction: discord.Interaction, confirm: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        if confirm.lower() != "yes":
            await interaction.followup.send("Reset cancelled.", ephemeral=True)
            return
        async with get_session() as session:
            for model in [PendingParlayLeg, Bet, Parlay, Market]:
                result = await session.execute(select(model))
                for row in result.scalars().all():
                    await session.delete(row)

        await interaction.followup.send("All bets, parlays, and markets have been reset.", ephemeral=True)

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

    @phase.command(name="delete", description="Delete a betting phase (only if no markets are assigned to it)")
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
            await _recalculate_open_markets(session)

        await interaction.followup.send(
            f"**{t.name}** added to alliance **{a.name}**. Open market odds recalculated.", ephemeral=True
        )

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
            await _recalculate_open_markets(session)

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
                member_str = ", ".join(f"D{t.district} {t.name}" for t in members)
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
            await session.delete(a)
            await _recalculate_open_markets(session)

        await interaction.followup.send(
            f"Alliance **{name}** deleted and all members unassigned.", ephemeral=True
        )

    # ── HISTORY COMMANDS ──────────────────────────────────────────────────────

    @history.command(name="add", description="Manually add a historical performance record for a district")
    @app_commands.describe(
        district="District number (1–12)",
        tribute_name="Name of the tribute this record is for",
        placement="Final placement (1 = winner, leave blank if unknown)",
        kills="Number of kills",
        won="Did this tribute win the Games?",
        game_label="Label to group this record with a specific game (e.g. 'Game 74')",
    )
    @is_admin()
    async def history_add(
        self,
        interaction: discord.Interaction,
        district: app_commands.Range[int, 1, 12],
        tribute_name: str,
        kills: int = 0,
        won: bool = False,
        placement: int | None = None,
        game_label: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            record = DistrictRecord(
                district=district,
                game_label=game_label,
                tribute_name=tribute_name,
                placement=placement,
                kills=kills,
                won=won,
            )
            session.add(record)
            await session.flush()
            rid = record.id
            await _recalculate_open_markets(session)

        place_str = f", placement #{placement}" if placement else ""
        game_str = f" [{game_label}]" if game_label else ""
        await interaction.followup.send(
            f"Historical record added (ID: {rid}): D{district} **{tribute_name}**{game_str} — "
            f"{kills} kill(s){place_str}{', **winner**' if won else ''}. Odds recalculated.",
            ephemeral=True,
        )

    @history.command(name="list", description="View district historical performance records")
    @app_commands.describe(
        district="Filter by district (leave blank for all)",
        game_label="Filter by game label (leave blank for all)",
    )
    @app_commands.autocomplete(game_label=game_label_autocomplete)
    @is_admin()
    async def history_list(
        self,
        interaction: discord.Interaction,
        district: app_commands.Range[int, 1, 12] | None = None,
        game_label: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_read_session() as session:
            query = select(DistrictRecord).order_by(DistrictRecord.game_label, DistrictRecord.district)
            if district is not None:
                query = query.where(DistrictRecord.district == district)
            if game_label is not None:
                query = query.where(DistrictRecord.game_label == game_label)
            result = await session.execute(query)
            records = result.scalars().all()
            all_records = (await session.execute(select(DistrictRecord))).scalars().all()

        if not records:
            await interaction.followup.send("No historical records found.", ephemeral=True)
            return

        # Group by game_label for display
        games: dict[str, list[DistrictRecord]] = {}
        for r in records:
            key = r.game_label or "Unlabeled"
            games.setdefault(key, []).append(r)

        embed = discord.Embed(title="District Historical Records", color=0x8B4513)
        for game, recs in list(games.items())[:6]:  # cap at 6 games per embed to avoid overflow
            lines = []
            for r in sorted(recs, key=lambda x: x.district):
                place = f"#{r.placement}" if r.placement else "—"
                crown = " 👑" if r.won else ""
                lines.append(f"D{r.district} **{r.tribute_name}** — place {place}, {r.kills}K{crown}")
            embed.add_field(name=game, value="\n".join(lines) or "—", inline=False)

        if len(games) > 6:
            embed.set_footer(text=f"Showing 6 of {len(games)} games. Filter by game_label to see more.")

        # Summary: per-district factor from all records
        if district is not None:
            factor = _district_historical_factor(district, list(all_records))
            direction = f"+{round((factor - 1) * 100, 1)}%" if factor >= 1 else f"{round((factor - 1) * 100, 1)}%"
            embed.description = f"District {district} historical odds factor: **×{round(factor, 3)}** ({direction})"

        await interaction.followup.send(embed=embed, ephemeral=True)

    @history.command(name="delete", description="Delete a specific historical record")
    @app_commands.describe(record_id="Record to delete")
    @app_commands.autocomplete(record_id=district_record_autocomplete)
    @is_admin()
    async def history_delete(self, interaction: discord.Interaction, record_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            record = await session.get(DistrictRecord, int(record_id))
            if not record:
                await interaction.followup.send("Record not found.", ephemeral=True)
                return
            desc = f"D{record.district} {record.tribute_name} [{record.game_label or '—'}]"
            await session.delete(record)
            await _recalculate_open_markets(session)

        await interaction.followup.send(
            f"Record **{desc}** deleted. Odds recalculated.", ephemeral=True
        )

    @history.command(name="clear_game", description="Delete all records for a specific game")
    @app_commands.describe(game_label="Game whose records should be cleared")
    @app_commands.autocomplete(game_label=game_label_autocomplete)
    @is_admin()
    async def history_clear_game(self, interaction: discord.Interaction, game_label: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(
                select(DistrictRecord).where(DistrictRecord.game_label == game_label)
            )
            records = result.scalars().all()
            if not records:
                await interaction.followup.send(
                    f"No records found for **{game_label}**.", ephemeral=True
                )
                return
            count = len(records)
            for r in records:
                await session.delete(r)
            await _recalculate_open_markets(session)

        await interaction.followup.send(
            f"Deleted {count} record(s) for **{game_label}**. Odds recalculated.", ephemeral=True
        )

    # ── MODIFIER COMMANDS ─────────────────────────────────────────────────────

    @modifier.command(name="create", description="Create a reusable odds modifier")
    @app_commands.describe(
        label="Name for this modifier (e.g. 'Career Training', 'Injured')",
        weight="Probability multiplier — e.g. 1.5 = +50%, 0.75 = -25%",
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
            await _recalculate_open_markets(session)

        await interaction.followup.send(
            f"Modifier **{label}** and all its assignments deleted. Open odds recalculated.",
            ephemeral=True,
        )

    @modifier.command(name="assign", description="Apply a modifier to a tribute or district")
    @app_commands.describe(
        modifier_id="Modifier to assign",
        tribute_id="Tribute to apply it to (leave blank for district-wide)",
        district="District to apply it to (leave blank for tribute-specific)",
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
                scope_str = f"tribute **{t.name}** (D{t.district})"
            else:
                scope_str = f"District {district}"

            assignment = ModifierAssignment(modifier_id=mod.id, tribute_id=tid, district=district)
            session.add(assignment)
            await session.flush()
            aid = assignment.id
            mod_label = mod.label
            weight = mod.weight
            await _recalculate_open_markets(session)

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
            await _recalculate_open_markets(session)

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
                        scope = f"D{t.district} {t.name}" if t else f"Tribute #{a.tribute_id}"
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
        rate="Cashout rate 0.0–1.0 (e.g. 0.65 = 65% of expected profit returned)",
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
    trib_a: Tribute,
    trib_b: Tribute | None,
    cause: str | None,
    placement_num: int | None,
    top_n: int | None,
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> str:
    a = f"D{trib_a.district} {trib_a.name}"
    b = f"D{trib_b.district} {trib_b.name}" if trib_b else ""
    side = "Over" if ou_side == "OVER" else ("Under" if ou_side == "UNDER" else "")
    line_str = f"{ou_line:g}" if ou_line is not None else ""
    return {
        "TRIBUTE_WINS":       f"{a} Wins the Games",
        "TRIBUTE_PLACEMENT":  f"{a} Finishes {_ordinal(placement_num or 2)}",
        "TRIBUTE_TOP_N":      f"{a} Top {top_n or 3} Finish",
        "TRIBUTE_KILLS":      f"{a} Gets Most Kills",
        "KILL_EVENT":         f"{a} Kills {b}",
        "DEATH_CAUSE":        f"{a} Dies by {cause or 'Unknown Cause'}",
        "FIRST_BLOOD":        f"{a} Gets First Kill",
        "BLOODBATH_SURVIVOR": f"{a} Survives the Bloodbath",
        "SPONSOR_EVENT":      f"{a}: {cause or 'Sponsor Event'}",
        "KILLS_OU":           f"{a} Kills — {side} {line_str}",
        "PLACEMENT_OU":       f"{a} Placement — {side} {line_str}",
    }.get(market_type, f"{a} — {market_type}")


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n if n <= 3 else 0, f"{n}th")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

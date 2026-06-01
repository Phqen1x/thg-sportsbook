from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.database.engine import get_session, set_setting, get_setting
from bot.database.models import (
    Alliance, Bet, BettingPhase, GameSetting, Market, Parlay,
    PendingParlayLeg, Tribute, User,
)
from bot.odds.calculator import straight_payout, parlay_payout
from bot.odds.defaults import (
    DEFAULT_FALLBACK_ODDS, apply_group_influence, default_odds,
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

_DEATH_CAUSES = ["Natural Causes", "Mutt", "Another Tribute", "Gamemakers"]


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_session() as session:
        result = await session.execute(select(Tribute).order_by(Tribute.district))
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def alive_tribute_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_session() as session:
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
    async with get_session() as session:
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
    async with get_session() as session:
        result = await session.execute(select(BettingPhase).order_by(BettingPhase.sort_order))
        phases = result.scalars().all()
    choices = []
    for p in phases:
        if current.lower() in p.name.lower():
            choices.append(app_commands.Choice(name=p.name, value=str(p.id)))
    return choices[:25]


async def alliance_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with get_session() as session:
        result = await session.execute(select(Alliance).order_by(Alliance.name))
        alliances = result.scalars().all()
    choices = []
    for a in alliances:
        if current.lower() in a.name.lower():
            choices.append(app_commands.Choice(name=a.name, value=str(a.id)))
    return choices[:25]


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
) -> int:
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
    return apply_group_influence(base, market_type, trib_a, district_mates, alliance_mates, all_tributes)


async def _recalculate_open_markets(session) -> None:
    result = await session.execute(
        select(Market).where(Market.status == "OPEN", Market.odds_override == False)
    )
    markets = result.scalars().all()
    trib_result = await session.execute(select(Tribute))
    all_tributes = trib_result.scalars().all()
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
    admin    = app_commands.Group(name="admin",    description="Capitol Sportsbook admin commands")
    tribute  = app_commands.Group(name="tribute",  description="Manage tributes",          parent=admin)
    market   = app_commands.Group(name="market",   description="Manage markets",           parent=admin)
    game     = app_commands.Group(name="game",     description="Game control",             parent=admin)
    settings = app_commands.Group(name="settings", description="Bot settings",             parent=admin)
    phase    = app_commands.Group(name="phase",    description="Manage betting phases",    parent=admin)
    alliance = app_commands.Group(name="alliance", description="Manage tribute alliances", parent=admin)

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
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            tribute = Tribute(
                name=name, district=district, gender=gender.value,
                training_score=score, face_claim=face_claim,
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
        if face_claim:
            embed.set_thumbnail(url=face_claim)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tribute.command(name="edit", description="Edit an existing tribute")
    @app_commands.describe(
        tribute_id="Tribute to edit",
        name="New name",
        district="New district (1–12)",
        score="New training score (1–12)",
        face_claim="New face claim URL",
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
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            t = await session.get(Tribute, int(tribute_id))
            if not t:
                await interaction.followup.send("Tribute not found.", ephemeral=True)
                return
            if name:       t.name = name
            if district:   t.district = district
            if score:      t.training_score = score
            if face_claim is not None: t.face_claim = face_claim
            updated_name = t.name

        await interaction.followup.send(f"Tribute **{updated_name}** updated.", ephemeral=True)

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
        market_type="Type of market",
        tribute_a_id="Primary tribute",
        tribute_b_id="Second tribute (for Kill Event markets)",
        cause="Death cause or custom label",
        placement_num="Exact placement number (for Placement markets)",
        top_n="Top-N value (for Top-N markets)",
        ou_line="Over/Under line value (e.g. 1.5 for kills or 12.5 for placement)",
        ou_side="Over or Under side",
        phase_id="Betting phase this market is active during (omit = all phases)",
    )
    @app_commands.choices(
        market_type=MARKET_TYPES,
        ou_side=[
            app_commands.Choice(name="Over",  value="OVER"),
            app_commands.Choice(name="Under", value="UNDER"),
        ],
    )
    @app_commands.autocomplete(
        tribute_a_id=tribute_autocomplete,
        tribute_b_id=tribute_autocomplete,
        phase_id=phase_autocomplete,
    )
    @is_admin()
    async def market_add(
        self,
        interaction: discord.Interaction,
        market_type: app_commands.Choice[str],
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
        async with get_session() as session:
            trib_a = await session.get(Tribute, int(tribute_a_id))
            if not trib_a:
                await interaction.followup.send("Primary tribute not found.", ephemeral=True)
                return
            trib_b = await session.get(Tribute, int(tribute_b_id)) if tribute_b_id else None
            all_t_result = await session.execute(select(Tribute))
            all_tributes = all_t_result.scalars().all()

            side_val = ou_side.value if ou_side else None
            odds = _compute_odds(
                market_type.value, trib_a, all_tributes,
                trib_b=trib_b, placement_num=placement_num, top_n=top_n,
                ou_line=ou_line, ou_side=side_val,
            )
            label = _build_label(market_type.value, trib_a, trib_b, cause, placement_num, top_n, ou_line, side_val)

            pid = int(phase_id) if phase_id else None
            if pid:
                phase_obj = await session.get(BettingPhase, pid)
                if not phase_obj:
                    await interaction.followup.send("Phase not found.", ephemeral=True)
                    return

            mkt = Market(
                type=market_type.value, label=label,
                tribute_a_id=trib_a.id,
                tribute_b_id=trib_b.id if trib_b else None,
                cause=cause, placement_num=placement_num, top_n=top_n,
                ou_line=ou_line, ou_side=side_val,
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
        if pid:
            embed.add_field(name="Phase", value=phase_obj.name)
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
        )
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

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
    @app_commands.describe(victor_id="The winning tribute")
    @app_commands.autocomplete(victor_id=alive_tribute_autocomplete)
    @is_admin()
    async def game_end(self, interaction: discord.Interaction, victor_id: str) -> None:
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

        await set_setting("game_active", False)
        embed = discord.Embed(
            title=f"👑 VICTOR: {victor_name.upper()} OF DISTRICT {victor_district}",
            description="The Games have concluded. The Capitol thanks you for your patronage.",
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

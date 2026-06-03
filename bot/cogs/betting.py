from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.database.engine import get_session, get_setting
from bot.database.models import Bet, Market, Parlay, PendingParlayLeg, User
from bot.imaging.bet_slip import ParlayLegData, render_parlay_slip
from bot.imaging.my_bets import BetRowData, ParlayData, render_my_bets
from bot.imaging.base import render_async, buf_to_discord_file
from bot.odds.calculator import (
    straight_payout, parlay_payout, combined_american, cashout_value
)
from bot.utils.formatters import fmt_chips, fmt_odds, safe_defer

log = logging.getLogger("capitol.betting")

MAX_PARLAY_LEGS = 10

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


# Every market type that constrains where a tribute finishes. A victor bet is
# just an exact-placement bet on 1st, so it counts as a placement market too.
_PLACEMENT_TYPES = {"TRIBUTE_WINS", "TRIBUTE_PLACEMENT", "TRIBUTE_TOP_N", "PLACEMENT_OU"}


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _exact_placement(m: Market) -> int | None:
    """The single finishing position a market pins down, or None if it covers a
    range (top-N / over-under) rather than one exact spot."""
    if m.type == "TRIBUTE_WINS":
        return 1
    if m.type == "TRIBUTE_PLACEMENT":
        return m.placement_num
    return None


def _placement_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Return an error string if adding new_mkt would violate placement parlay rules.

    A tribute can only finish in one position, so two placement bets on the SAME
    tribute conflict — the lone exception being an opposite over/under pair (e.g.
    over 3rd AND under 12th, which together describe a finishing window). Across
    DIFFERENT tributes placement bets are fine, except two bets that pin the same
    exact position (two victors, or two tributes both finishing exactly Nth),
    since only one tribute can occupy a given spot.
    """
    if new_mkt.type not in _PLACEMENT_TYPES:
        return None
    for m in existing_markets:
        if m.type not in _PLACEMENT_TYPES:
            continue
        same_tribute = (
            m.tribute_a_id is not None and m.tribute_a_id == new_mkt.tribute_a_id
        )
        if same_tribute:
            # Opposite-side placement over/unders together describe a window and
            # are the only allowed pairing on a single tribute.
            if (
                m.type == "PLACEMENT_OU"
                and new_mkt.type == "PLACEMENT_OU"
                and m.ou_side and new_mkt.ou_side
                and m.ou_side != new_mkt.ou_side
            ):
                continue
            return (
                "You can't parlay two placement bets on the same tribute — a "
                "tribute only finishes in one position, so victor, exact "
                "placement, top-N, and placement over/under bets all conflict "
                "with each other. (The only exception is an opposite over/under "
                "pair, e.g. over 3rd **and** under 12th.)"
            )
        ea, eb = _exact_placement(m), _exact_placement(new_mkt)
        if ea is not None and ea == eb:
            if ea == 1:
                return "You can't parlay two victor bets — only one tribute can win the Games."
            return (
                f"You can't parlay two bets on a tribute finishing exactly "
                f"{_ordinal(ea)} — only one tribute can take that position."
            )
    return None


def _milestone_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Return an error string if adding new_mkt would violate milestone parlay rules."""
    if new_mkt.type not in _ALL_MILESTONES:
        return None
    same = [
        m for m in existing_markets
        if m.tribute_a_id == new_mkt.tribute_a_id and m.type in _ALL_MILESTONES
    ]
    new_group = _MILESTONE_GROUP[new_mkt.type]
    for m in same:
        if _MILESTONE_GROUP[m.type] == new_group:
            return (
                f"Cannot combine two milestone markets for the same phase on one tribute "
                f"(both target {new_group.replace('_', ' ').title()})."
            )
    if new_mkt.type in _MAKES_MILESTONES:
        for m in same:
            if m.type in _MAKES_MILESTONES:
                return "Cannot include two 'makes milestone' bets for the same tribute in one parlay."
    return None


async def _get_or_create_user(session, member: discord.Member) -> User:
    u = await session.get(User, member.id)
    if u is None:
        default_raw = await get_setting("default_chips")
        default = json.loads(default_raw) if default_raw else 1000
        u = User(discord_id=member.id, username=member.display_name, chips=default)
        session.add(u)
        await session.flush()
    else:
        u.username = member.display_name
    return u


async def open_market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_session() as session:
        result = await session.execute(
            select(Market).where(Market.status == "OPEN").order_by(Market.id)
        )
        markets = result.scalars().all()
    choices = []
    for m in markets:
        if current.lower() in m.label.lower():
            choices.append(app_commands.Choice(name=m.label[:100], value=str(m.id)))
    return choices[:25]


async def user_bet_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    async with get_session() as session:
        result = await session.execute(
            select(Bet).where(Bet.user_id == uid, Bet.status == "PENDING", Bet.parlay_id == None)
        )
        bets = result.scalars().all()
    choices = []
    for b in bets:
        async with get_session() as session:
            mkt = await session.get(Market, b.market_id)
        label = f"#{b.id} {mkt.label if mkt else '?'} ({fmt_odds(b.odds_at_placement)})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(b.id)))
    return choices[:25]


async def user_parlay_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    async with get_session() as session:
        result = await session.execute(
            select(Parlay).where(Parlay.user_id == uid, Parlay.status == "PENDING")
        )
        parlays = result.scalars().all()
    choices = []
    for p in parlays:
        label = f"Parlay #{p.id} — {fmt_chips(p.total_wager)} wager"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(p.id)))
    return choices[:25]


class BettingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error(f"Betting command error: {error}", exc_info=error)
        msg = "An error occurred. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass

    # ── /bet ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="bet", description="Place a straight single bet on an open market")
    @app_commands.describe(market_id="Market to bet on", amount="Amount of chips to wager")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    async def bet(
        self,
        interaction: discord.Interaction,
        market_id: str,
        amount: app_commands.Range[int, 1, 500000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt or mkt.status != "OPEN":
                await interaction.followup.send("That market is not open for betting.", ephemeral=True)
                return

            user = await _get_or_create_user(session, interaction.user)
            if user.chips < amount:
                await interaction.followup.send(
                    f"Insufficient chips. You have **{fmt_chips(user.chips)}** but need **{fmt_chips(amount)}**.",
                    ephemeral=True,
                )
                return

            payout = straight_payout(amount, mkt.odds)
            user.chips -= amount
            user.total_wagered += amount

            b = Bet(
                user_id=user.discord_id,
                market_id=mkt.id,
                wager=amount,
                odds_at_placement=mkt.odds,
                payout_if_win=payout,
            )
            session.add(b)
            await session.flush()
            bet_id = b.id
            label = mkt.label
            odds = mkt.odds
            new_balance = user.chips

        embed = discord.Embed(title="Bet Placed!", color=0x4CAF50)
        embed.add_field(name="Market", value=label, inline=False)
        embed.add_field(name="Wager", value=fmt_chips(amount))
        embed.add_field(name="Odds", value=fmt_odds(odds))
        embed.add_field(name="Potential Payout", value=fmt_chips(payout))
        embed.add_field(name="Bet ID", value=f"#{bet_id}")
        embed.set_footer(text=f"Remaining balance: {fmt_chips(new_balance)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /parlay ───────────────────────────────────────────────────────────────

    parlay_group = app_commands.Group(name="parlay", description="Build and submit a parlay bet")

    @parlay_group.command(name="add", description="Add a market to your pending parlay slip")
    @app_commands.describe(market_id="Market to add to your parlay slip")
    @app_commands.autocomplete(market_id=open_market_autocomplete)
    async def parlay_add(self, interaction: discord.Interaction, market_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt = await session.get(Market, int(market_id))
            if not mkt or mkt.status != "OPEN":
                await interaction.followup.send("That market is not open.", ephemeral=True)
                return

            user = await _get_or_create_user(session, interaction.user)

            dup = await session.execute(
                select(PendingParlayLeg).where(
                    PendingParlayLeg.user_id == user.discord_id,
                    PendingParlayLeg.market_id == mkt.id,
                )
            )
            if dup.scalar_one_or_none():
                await interaction.followup.send("That market is already on your slip.", ephemeral=True)
                return

            existing_legs_result = await session.execute(
                select(PendingParlayLeg).where(PendingParlayLeg.user_id == user.discord_id)
            )
            existing_legs = existing_legs_result.scalars().all()
            if len(existing_legs) >= MAX_PARLAY_LEGS:
                await interaction.followup.send(
                    f"Maximum {MAX_PARLAY_LEGS} legs per parlay.", ephemeral=True
                )
                return

            # Milestone + placement validation against the legs already on the slip
            if mkt.type in _ALL_MILESTONES or mkt.type in _PLACEMENT_TYPES:
                existing_mkts: list[Market] = []
                for leg in existing_legs:
                    leg_mkt = await session.get(Market, leg.market_id)
                    if leg_mkt:
                        existing_mkts.append(leg_mkt)
                conflict = (
                    _milestone_conflict(existing_mkts, mkt)
                    or _placement_conflict(existing_mkts, mkt)
                )
                if conflict:
                    await interaction.followup.send(conflict, ephemeral=True)
                    return

            session.add(PendingParlayLeg(user_id=user.discord_id, market_id=mkt.id))
            mkt_label = mkt.label
            mkt_odds = mkt.odds

        await interaction.followup.send(
            f"Added **{mkt_label}** ({fmt_odds(mkt_odds)}) to your parlay slip.\n"
            "Use `/parlay view` to preview or `/parlay submit` to lock in.",
            ephemeral=True,
        )

    @parlay_group.command(name="view", description="Preview your current parlay slip")
    async def parlay_view(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(PendingParlayLeg.user_id == user.discord_id)
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if not legs:
                await interaction.followup.send(
                    "Your parlay slip is empty. Use `/parlay add` to add markets.", ephemeral=True
                )
                return

            leg_data: list[ParlayLegData] = []
            for i, leg in enumerate(legs, 1):
                mkt = await session.get(Market, leg.market_id)
                if mkt:
                    leg_data.append(ParlayLegData(
                        leg_num=i,
                        market_label=mkt.label,
                        odds=mkt.odds,
                    ))

        if not leg_data:
            await interaction.followup.send("Could not load parlay data.", ephemeral=True)
            return

        preview_wager = 100
        preview_payout = parlay_payout(preview_wager, [l.odds for l in leg_data])
        buf = await render_async(render_parlay_slip, leg_data, preview_wager, preview_payout, False)
        f = buf_to_discord_file(buf, "parlay_slip.png")
        await interaction.followup.send(
            f"Your parlay slip ({len(leg_data)} legs). Use `/parlay submit` with your wager to lock in.",
            file=f,
            ephemeral=True,
        )

    @parlay_group.command(name="submit", description="Submit your parlay with a wager amount")
    @app_commands.describe(wager="Amount of chips to wager on this parlay")
    async def parlay_submit(
        self,
        interaction: discord.Interaction,
        wager: app_commands.Range[int, 1, 500000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            if user.chips < wager:
                await interaction.followup.send(
                    f"Insufficient chips. You have **{fmt_chips(user.chips)}**.", ephemeral=True
                )
                return

            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(PendingParlayLeg.user_id == user.discord_id)
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if len(legs) < 2:
                await interaction.followup.send(
                    "A parlay requires at least 2 legs. Use `/parlay add` to add more.", ephemeral=True
                )
                return

            markets: list[Market] = []
            for leg in legs:
                mkt = await session.get(Market, leg.market_id)
                if not mkt or mkt.status != "OPEN":
                    await interaction.followup.send(
                        f"Market '{mkt.label if mkt else leg.market_id}' is no longer open. Remove it and resubmit.",
                        ephemeral=True,
                    )
                    return
                markets.append(mkt)

            # Final milestone + placement validation pass before committing
            for i, mkt in enumerate(markets):
                conflict = (
                    _milestone_conflict(markets[:i], mkt)
                    or _placement_conflict(markets[:i], mkt)
                )
                if conflict:
                    await interaction.followup.send(conflict, ephemeral=True)
                    return

            all_odds = [m.odds for m in markets]
            total_payout = parlay_payout(wager, all_odds)

            user.chips -= wager
            user.total_wagered += wager

            parlay = Parlay(
                user_id=user.discord_id,
                total_wager=wager,
                total_payout=total_payout,
            )
            session.add(parlay)
            await session.flush()

            leg_data: list[ParlayLegData] = []
            for i, (leg, mkt) in enumerate(zip(legs, markets), 1):
                b = Bet(
                    user_id=user.discord_id,
                    parlay_id=parlay.id,
                    market_id=mkt.id,
                    wager=wager,
                    odds_at_placement=mkt.odds,
                    payout_if_win=total_payout,
                )
                session.add(b)
                leg_data.append(ParlayLegData(leg_num=i, market_label=mkt.label, odds=mkt.odds))
                await session.delete(leg)

            parlay_id = parlay.id
            new_balance = user.chips

        buf = await render_async(render_parlay_slip, leg_data, wager, total_payout, True)
        f = buf_to_discord_file(buf, f"parlay_{parlay_id}.png")
        await interaction.followup.send(
            f"**Parlay #{parlay_id} submitted!** Wagered **{fmt_chips(wager)}** for a potential **{fmt_chips(total_payout)}**.\n"
            f"Remaining balance: {fmt_chips(new_balance)}",
            file=f,
            ephemeral=True,
        )

    @parlay_group.command(name="remove", description="Remove a leg from your pending parlay slip by position")
    @app_commands.describe(leg_number="Leg number to remove (see /parlay view)")
    async def parlay_remove(
        self,
        interaction: discord.Interaction,
        leg_number: app_commands.Range[int, 1, 10],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(PendingParlayLeg.user_id == user.discord_id)
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if leg_number > len(legs):
                await interaction.followup.send(
                    f"You only have {len(legs)} leg(s) on your slip.", ephemeral=True
                )
                return

            leg_to_remove = legs[leg_number - 1]
            mkt = await session.get(Market, leg_to_remove.market_id)
            label = mkt.label if mkt else "Unknown market"
            await session.delete(leg_to_remove)

        await interaction.followup.send(
            f"Removed leg {leg_number} (**{label}**) from your slip.", ephemeral=True
        )

    @parlay_group.command(name="clear", description="Clear your entire pending parlay slip")
    async def parlay_clear(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            legs_result = await session.execute(
                select(PendingParlayLeg).where(PendingParlayLeg.user_id == user.discord_id)
            )
            for leg in legs_result.scalars().all():
                await session.delete(leg)

        await interaction.followup.send("Parlay slip cleared.", ephemeral=True)

    # ── /cashout ──────────────────────────────────────────────────────────────

    @app_commands.command(name="cashout", description="Cash out a pending bet or parlay early")
    @app_commands.describe(
        cashout_type="Cash out a single bet or an entire parlay",
        cashout_id="ID of the bet or parlay to cash out",
    )
    @app_commands.choices(cashout_type=[
        app_commands.Choice(name="Single Bet",   value="BET"),
        app_commands.Choice(name="Parlay",        value="PARLAY"),
    ])
    async def cashout(
        self,
        interaction: discord.Interaction,
        cashout_type: app_commands.Choice[str],
        cashout_id: int,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        global_allowed_raw = await get_setting("cashout_allowed")
        global_allowed = json.loads(global_allowed_raw) if global_allowed_raw else False
        global_rate_raw = await get_setting("cashout_rate")
        global_rate = json.loads(global_rate_raw) if global_rate_raw else 0.65

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)

            if cashout_type.value == "BET":
                b = await session.get(Bet, cashout_id)
                if not b or b.user_id != user.discord_id:
                    await interaction.followup.send("Bet not found.", ephemeral=True)
                    return
                if b.status != "PENDING" or b.parlay_id is not None:
                    await interaction.followup.send(
                        "You can only cash out pending straight bets.", ephemeral=True
                    )
                    return

                mkt = await session.get(Market, b.market_id)
                allowed = mkt.cashout_allowed if mkt and mkt.cashout_allowed is not None else global_allowed
                rate = mkt.cashout_rate if mkt and mkt.cashout_rate is not None else global_rate

                if not allowed:
                    await interaction.followup.send("Early cashout is not available for this bet.", ephemeral=True)
                    return

                amount = cashout_value(b.wager, b.payout_if_win, rate)
                b.status = "CASHED_OUT"
                b.cashout_amount = amount
                user.chips += amount
                label = mkt.label if mkt else "Unknown"

            else:  # PARLAY
                p = await session.get(Parlay, cashout_id)
                if not p or p.user_id != user.discord_id:
                    await interaction.followup.send("Parlay not found.", ephemeral=True)
                    return
                if p.status != "PENDING":
                    await interaction.followup.send("That parlay is no longer pending.", ephemeral=True)
                    return

                if not global_allowed:
                    await interaction.followup.send("Early cashout is not available.", ephemeral=True)
                    return

                amount = cashout_value(p.total_wager, p.total_payout, global_rate)
                p.status = "CASHED_OUT"
                p.cashout_amount = amount
                user.chips += amount

                legs_result = await session.execute(
                    select(Bet).where(Bet.parlay_id == p.id, Bet.status == "PENDING")
                )
                for leg in legs_result.scalars().all():
                    leg.status = "CASHED_OUT"
                    leg.cashout_amount = amount

                label = f"Parlay #{cashout_id}"

        embed = discord.Embed(title="Cashed Out!", color=0x5B9BD5)
        embed.add_field(name="Market/Parlay", value=label, inline=False)
        embed.add_field(name="Cashout Amount", value=fmt_chips(amount))
        embed.add_field(name="New Balance", value=fmt_chips(user.chips))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /mybets ───────────────────────────────────────────────────────────────

    @app_commands.command(name="mybets", description="View your bets as a styled card")
    @app_commands.choices(filter_by=[
        app_commands.Choice(name="All",     value="ALL"),
        app_commands.Choice(name="Pending", value="PENDING"),
        app_commands.Choice(name="Won",     value="WON"),
        app_commands.Choice(name="Lost",    value="LOST"),
    ])
    async def mybets(
        self,
        interaction: discord.Interaction,
        filter_by: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        filter_val = filter_by.value if filter_by else "ALL"

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)

            straight_q = select(Bet).where(Bet.user_id == user.discord_id, Bet.parlay_id == None)
            parlay_q = select(Parlay).where(Parlay.user_id == user.discord_id)
            if filter_val != "ALL":
                straight_q = straight_q.where(Bet.status == filter_val)
                parlay_q = parlay_q.where(Parlay.status == filter_val)

            straight_result = await session.execute(straight_q.order_by(Bet.placed_at.desc()).limit(20))
            straight_bets_orm = straight_result.scalars().all()

            parlay_result = await session.execute(parlay_q.order_by(Parlay.placed_at.desc()).limit(10))
            parlays_orm = parlay_result.scalars().all()

            straight_rows: list[BetRowData] = []
            for b in straight_bets_orm:
                mkt = await session.get(Market, b.market_id)
                straight_rows.append(BetRowData(
                    bet_id=b.id,
                    market_label=mkt.label if mkt else f"Market #{b.market_id}",
                    wager=b.wager,
                    odds=b.odds_at_placement,
                    payout=b.payout_if_win,
                    status=b.status,
                ))

            parlay_data: list[ParlayData] = []
            for p in parlays_orm:
                legs_result = await session.execute(
                    select(Bet).where(Bet.parlay_id == p.id).order_by(Bet.placed_at)
                )
                leg_rows: list[BetRowData] = []
                for b in legs_result.scalars().all():
                    mkt = await session.get(Market, b.market_id)
                    leg_rows.append(BetRowData(
                        bet_id=b.id,
                        market_label=mkt.label if mkt else f"Market #{b.market_id}",
                        wager=b.wager,
                        odds=b.odds_at_placement,
                        payout=b.payout_if_win,
                        status=b.status,
                    ))
                combo = combined_american([l.odds for l in leg_rows]) if leg_rows else 0
                parlay_data.append(ParlayData(
                    parlay_id=p.id,
                    total_wager=p.total_wager,
                    total_payout=p.total_payout,
                    combined_odds=combo,
                    status=p.status,
                    legs=leg_rows,
                ))

            chips = user.chips
            username = user.username

        buf = await render_async(render_my_bets, username, chips, straight_rows, parlay_data, filter_val)
        f = buf_to_discord_file(buf, "my_bets.png")
        await interaction.followup.send(file=f, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BettingCog(bot))

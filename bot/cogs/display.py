from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func, or_

from bot.database.engine import get_session, get_read_session, get_setting
from bot.database.models import Bet, Market, MarketTemplate, Tribute, User
from bot.imaging.hot_odds import (
    TributeCardData, FeaturedMarket, TributeDetailData,
    render_hot_odds, render_tribute_detail,
)
from bot.imaging.base import render_async, fetch_image_bytes, buf_to_discord_file
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_pct, market_type_label, safe_defer
from bot.utils.market_view import MarketPageView, sort_markets
from bot.odds.calculator import implied_probability

log = logging.getLogger("capitol.display")


def _balance(m: Market) -> int:
    """How competitive a line is: |american odds|, where even money (±100) is
    smallest and lopsided rails (±9900) are largest. Lower = more interesting."""
    return abs(m.odds)


def _balance_polarities(ranked: list[Market], n: int = 8) -> list[Market]:
    """From a pre-ranked list pick n markets with a balanced mix of +/- odds,
    preserving rank order within each sign. Falls back to top-n if only one sign exists."""
    pos = [m for m in ranked if m.odds > 0]
    neg = [m for m in ranked if m.odds < 0]

    if not pos or not neg:
        result = ranked[:n]
    else:
        target_neg = min(len(neg), n // 2)
        target_pos = min(len(pos), n - target_neg)
        target_neg = n - target_pos
        result = pos[:target_pos] + neg[:target_neg]

    result.sort(key=lambda m: m.odds)
    return result


def _select_variety_markets(markets: list[Market]) -> list[Market]:
    """Pick up to 8 markets spanning different types with a mix of positive and
    negative odds, favouring the most competitive (near even-money) lines."""
    if len(markets) <= 8:
        return sorted(markets, key=lambda m: m.odds)

    by_type: dict[str, list[Market]] = {}
    for m in markets:
        by_type.setdefault(m.type, []).append(m)
    for lst in by_type.values():
        lst.sort(key=_balance)

    candidates: list[Market] = []
    seen_ids: set[int] = set()

    # Pass 1: most competitive line of each type for variety across market types.
    for lst in sorted(by_type.values(), key=lambda l: _balance(l[0])):
        m = lst[0]
        candidates.append(m)
        seen_ids.add(m.id)

    # Pass 2: remaining slots filled with next most competitive lines overall.
    for m in sorted(markets, key=_balance):
        if m.id not in seen_ids:
            candidates.append(m)
            seen_ids.add(m.id)

    return _balance_polarities(candidates)


async def _tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
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


class DisplayCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error(f"Display command error: {error}", exc_info=error)
        msg = "An error occurred. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass

    # ── /odds ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="odds", description="View the live Hot Odds board")
    @app_commands.describe(tribute="Show detailed odds for a specific tribute")
    @app_commands.autocomplete(tribute=_tribute_autocomplete)
    async def odds(self, interaction: discord.Interaction, tribute: str | None = None) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        # ── Single-tribute detail view ────────────────────────────────────────
        if tribute is not None:
            tribute_id = int(tribute)
            async with get_session() as session:
                user = await _get_or_create_user(session, interaction.user)
                user_chips = user.chips

                t = await session.get(Tribute, tribute_id)
                if t is None:
                    await interaction.followup.send("Tribute not found.", ephemeral=True)
                    return

                mkt_result = await session.execute(
                    select(Market)
                    .where(Market.status == "OPEN")
                    .where(or_(
                        Market.tribute_a_id == tribute_id,
                        Market.tribute_b_id == tribute_id,
                    ))
                    .order_by(Market.id)
                )
                tribute_markets = mkt_result.scalars().all()

            win_odds = next(
                (m.odds for m in tribute_markets if m.type == "TRIBUTE_WINS"), None
            )
            non_win = [m for m in tribute_markets if m.type != "TRIBUTE_WINS"]
            featured_mkts = _balance_polarities(sorted(non_win, key=lambda m: abs(m.odds)), n=16)

            face_bytes: bytes | None = None
            if t.face_claim:
                result = await fetch_image_bytes(t.face_claim)
                face_bytes = result if isinstance(result, bytes) else None

            detail = TributeDetailData(
                name=t.name,
                district=t.district,
                gender=t.display_gender,
                training_score=t.training_score,
                status=t.status,
                kills=t.kills,
                placement=t.placement,
                death_cause=t.death_cause,
                win_odds=win_odds,
                face_bytes=face_bytes,
                markets=[
                    FeaturedMarket(label=m.label, odds=m.odds, market_type=m.type)
                    for m in featured_mkts
                ],
            )
            buf = await render_async(render_tribute_detail, detail, user_chips)
            fname = f"tribute_{t.name.lower().replace(' ', '_')}.png"
            f = buf_to_discord_file(buf, fname)
            await interaction.followup.send(file=f, ephemeral=True)
            return

        # ── Full board view ───────────────────────────────────────────────────
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            user_chips = user.chips

            trib_result = await session.execute(
                select(Tribute).order_by(Tribute.district)
            )
            tributes = trib_result.scalars().all()

            mkt_result = await session.execute(
                select(Market).where(Market.status == "OPEN").order_by(Market.id)
            )
            markets = mkt_result.scalars().all()

            bet_count_rows = await session.execute(
                select(Bet.market_id, func.count(Bet.id).label("cnt"))
                .where(Bet.parlay_id.is_(None))
                .join(Market, Bet.market_id == Market.id)
                .where(Market.status == "OPEN")
                .where(Market.type != "TRIBUTE_WINS")
                .group_by(Bet.market_id)
            )
            bet_counts: dict[int, int] = {
                row.market_id: row.cnt for row in bet_count_rows.all()
            }

        win_odds_map: dict[int, int] = {}
        for m in markets:
            if m.type == "TRIBUTE_WINS":
                win_odds_map[m.tribute_a_id] = m.odds

        non_win_markets = [m for m in markets if m.type != "TRIBUTE_WINS"]

        if bet_counts:
            by_bets = sorted(
                non_win_markets,
                key=lambda m: (-bet_counts.get(m.id, 0), abs(m.odds)),
            )
            featured_mkts = _balance_polarities(by_bets)
        else:
            featured_mkts = _select_variety_markets(non_win_markets)

        cards = [
            TributeCardData(
                tribute_id=t.id,
                name=t.name,
                district=t.district,
                gender=t.display_gender,
                training_score=t.training_score,
                status=t.status,
                win_odds=win_odds_map.get(t.id),
            )
            for t in tributes
        ]

        featured = [
            FeaturedMarket(label=m.label, odds=m.odds, market_type=m.type)
            for m in featured_mkts
        ]

        buf = await render_async(render_hot_odds, cards, featured, user_chips)
        f = buf_to_discord_file(buf, "hot_odds.png")
        await interaction.followup.send(file=f, ephemeral=True)

    # ── /tributes ─────────────────────────────────────────────────────────────

    @app_commands.command(name="tributes", description="View all tributes and their stats")
    async def tributes(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(select(Tribute).order_by(Tribute.district))
            tributes = result.scalars().all()

        if not tributes:
            await interaction.followup.send("No tributes in the arena yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏛️ HUNGER GAMES — TRIBUTE ROSTER",
            color=0xC9A227,
        )

        alive = [t for t in tributes if t.status == "ALIVE"]
        dead = [t for t in tributes if t.status == "DEAD"]
        victor = [t for t in tributes if t.status == "VICTOR"]

        if alive:
            alive_lines = []
            for t in alive:
                gender_icon = "♂" if t.display_gender == "M" else ("♀" if t.display_gender == "F" else "⚧")
                alive_lines.append(
                    f"**D{t.district}{t.display_gender} {t.name}** {gender_icon} — Score: `{t.training_score if t.training_score is not None else '?'}` | Kills: `{t.kills}`"
                )
            embed.add_field(name="🟢 ALIVE", value="\n".join(alive_lines), inline=False)

        if victor:
            for t in victor:
                embed.add_field(
                    name="👑 VICTOR",
                    value=f"**D{t.district}{t.display_gender} {t.name}** — Score: `{t.training_score if t.training_score is not None else '?'}` | Kills: `{t.kills}`",
                    inline=False,
                )

        if dead:
            dead_lines = []
            for t in dead:
                placement = f"#{t.placement}" if t.placement else "?"
                cause = t.death_cause or "Unknown"
                dead_lines.append(f"D{t.district}{t.display_gender} {t.name} — {placement} | {cause}")
            embed.add_field(name="💀 FALLEN", value="\n".join(dead_lines[:20]), inline=False)

        embed.set_footer(text=f"Total tributes: {len(tributes)} | Alive: {len(alive)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /markets ──────────────────────────────────────────────────────────────

    @app_commands.command(name="markets", description="Browse all open betting markets with pagination")
    async def markets(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt_result = await session.execute(
                select(Market).where(Market.status == "OPEN")
            )
            all_markets = mkt_result.scalars().all()

            trib_result = await session.execute(select(Tribute))
            tribute_map = {t.id: t for t in trib_result.scalars().all()}

            t_result = await session.execute(select(MarketTemplate))
            custom_type_labels = {f"CUSTOM_{t.id}": t.name for t in t_result.scalars().all()}

        if not all_markets:
            await interaction.followup.send("No open markets at the moment.", ephemeral=True)
            return

        sorted_mkts = sort_markets(all_markets, tribute_map)
        view = MarketPageView(sorted_mkts, tribute_map, is_admin=False, extra_type_labels=custom_type_labels)
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

    # ── /balance ──────────────────────────────────────────────────────────────

    @app_commands.command(name="balance", description="Check your chip balance and stats")
    async def balance(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            chips = user.chips
            wagered = user.total_wagered
            won = user.total_won

        embed = discord.Embed(
            title=f"⚡ {interaction.user.display_name}'s Balance",
            color=0xC9A227,
        )
        embed.add_field(name="Chips", value=fmt_chips(chips), inline=False)
        embed.add_field(name="Total Wagered", value=fmt_chips(wagered))
        embed.add_field(name="Total Won", value=fmt_chips(won))
        if wagered > 0:
            roi = ((won - wagered) / wagered) * 100
            embed.add_field(name="ROI", value=f"{roi:+.1f}%")
        embed.set_footer(text="May the odds be ever in your favor.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="View the top chip holders")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(
                select(User).order_by(User.chips.desc()).limit(10)
            )
            users = result.scalars().all()

        if not users:
            await interaction.followup.send("No players yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 CAPITOL LEADERBOARD — TOP BETTORS",
            color=0xFFD700,
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, u in enumerate(users):
            medal = medals[i] if i < 3 else f"**{i + 1}.**"
            lines.append(f"{medal} **{u.username}** — {fmt_chips(u.chips)}")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Balances update in real time.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DisplayCog(bot))

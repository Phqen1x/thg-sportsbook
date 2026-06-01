from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func

from bot.database.engine import get_session, get_setting
from bot.database.models import Market, Tribute, User
from bot.imaging.hot_odds import TributeCardData, FeaturedMarket, render_hot_odds
from bot.imaging.base import render_async, fetch_image_bytes, buf_to_discord_file
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_pct, market_type_label, safe_defer
from bot.utils.market_view import MarketPageView, sort_markets
from bot.odds.calculator import implied_probability

log = logging.getLogger("capitol.display")


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
    async def odds(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

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

        # Build win-odds lookup: tribute_id → odds for TRIBUTE_WINS market
        win_odds_map: dict[int, int] = {}
        for m in markets:
            if m.type == "TRIBUTE_WINS":
                win_odds_map[m.tribute_a_id] = m.odds

        # Featured markets = non-WIN open markets, sorted by abs(odds) desc (most interesting)
        featured_mkts = [m for m in markets if m.type != "TRIBUTE_WINS"]
        featured_mkts.sort(key=lambda m: abs(m.odds), reverse=True)

        # Fetch all face claim images concurrently
        face_bytes_map: dict[int, bytes | None] = {}
        import asyncio
        tasks = {}
        for t in tributes:
            if t.face_claim:
                tasks[t.id] = asyncio.create_task(fetch_image_bytes(t.face_claim))

        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for tid, result in zip(tasks.keys(), results):
                face_bytes_map[tid] = result if isinstance(result, bytes) else None

        cards = [
            TributeCardData(
                tribute_id=t.id,
                name=t.name,
                district=t.district,
                gender=t.gender,
                training_score=t.training_score,
                status=t.status,
                win_odds=win_odds_map.get(t.id),
                face_bytes=face_bytes_map.get(t.id),
            )
            for t in tributes
        ]

        featured = [
            FeaturedMarket(label=m.label, odds=m.odds, market_type=m.type)
            for m in featured_mkts[:8]
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
                gender_icon = "♂" if t.gender == "M" else "♀"
                alive_lines.append(
                    f"**D{t.district} {t.name}** {gender_icon} — Score: `{t.training_score}` | Kills: `{t.kills}`"
                )
            embed.add_field(name="🟢 ALIVE", value="\n".join(alive_lines), inline=False)

        if victor:
            for t in victor:
                embed.add_field(
                    name="👑 VICTOR",
                    value=f"**D{t.district} {t.name}** — Score: `{t.training_score}` | Kills: `{t.kills}`",
                    inline=False,
                )

        if dead:
            dead_lines = []
            for t in dead:
                placement = f"#{t.placement}" if t.placement else "?"
                cause = t.death_cause or "Unknown"
                dead_lines.append(f"D{t.district} {t.name} — {placement} | {cause}")
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

        if not all_markets:
            await interaction.followup.send("No open markets at the moment.", ephemeral=True)
            return

        sorted_mkts = sort_markets(all_markets, tribute_map)
        view = MarketPageView(sorted_mkts, tribute_map, is_admin=False)
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

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import config

log = logging.getLogger("capitol.activity")


class ActivityCog(commands.Cog):
    """Points members at the embedded Discord Activity (the in-Discord sportsbook UI)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="play",
        description="Open the Capitol Sportsbook Activity — markets, odds, bets & parlays.",
    )
    async def play(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="⚔ Capitol Sportsbook",
            description=(
                "Launch the **Capitol Sportsbook** Activity to browse markets and odds, "
                "place bets and parlays, tail public slips, and watch the leaderboard — "
                "all without leaving Discord.\n\n"
                "**To open it:** join a voice channel, click the **Activities** (rocket) "
                "button, and pick **Capitol Sportsbook**."
            ),
            color=0xC9A227,
        )
        embed.set_footer(text="May the odds be ever in your favor.")

        view: discord.ui.View | None = None
        if config.WEB_BASE_URL:
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="Open in browser",
                    style=discord.ButtonStyle.link,
                    url=f"{config.WEB_BASE_URL}/activity",
                )
            )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivityCog(bot))

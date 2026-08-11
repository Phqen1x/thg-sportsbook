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
        description="Open the Panem Sportsbook Activity — markets, odds, bets & parlays.",
    )
    async def play(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="⚔ Panem Sportsbook",
            description=(
                "Launch the **Panem Sportsbook** Activity or visit the "
                "[Website](https://panem-sportsbook.phqen1x.com) to browse markets and odds, "
                "place bets and build parlays, tail public parlay slips, and watch the "
                "leaderboards!\n\n"
                "**To open the activity:** Click the activity icon next to your chat bar "
                "and pick **Panem Sportsbook**.\n\n"
                "**To access the website,** click on the link below or type it into your "
                "address bar! The website requires you to log in via Discord, so there is "
                "no need to make a new log in or provide any personal information.\n\n"
                "https://panem-sportsbook.phqen1x.com"
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
                    url=f"{config.WEB_BASE_URL}",
                )
            )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivityCog(bot))

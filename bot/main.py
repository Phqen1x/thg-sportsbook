import asyncio
import logging
import sys

import discord
from discord.ext import commands

from bot import config
from bot.database.engine import get_setting, init_db
from bot.imaging.base import get_theme_by_name, set_active_theme

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("capitol")


class SportsBookBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        log.info("Initializing database...")
        await init_db()

        saved_theme = await get_setting("image_theme")
        if saved_theme:
            theme = get_theme_by_name(saved_theme)
            if theme:
                set_active_theme(theme)
                log.info(f"Loaded image theme: {theme.name}")

        log.info("Loading cogs...")
        await self.load_extension("bot.cogs.admin")
        await self.load_extension("bot.cogs.betting")
        await self.load_extension("bot.cogs.display")
        await self.load_extension("bot.cogs.activity")

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            # Clear the global scope so previously-published global commands are
            # deleted on sync; otherwise they show up alongside the guild copies
            # as duplicates.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            await self.tree.sync(guild=guild)
            log.info(f"Slash commands synced to dev guild {config.DEV_GUILD_ID}")
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info("Capitol Sportsbook is open for business.")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="🎰 Betting on tributes",
            )
        )


def main() -> None:
    bot = SportsBookBot()
    bot.run(config.BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()

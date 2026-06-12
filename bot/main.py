import asyncio
import logging
import sys
from typing import Optional

import discord
from discord import app_commands
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


class SportsBookCommandTree(app_commands.CommandTree):
    async def sync(self, *, guild: Optional[discord.abc.Snowflake] = None):
        if guild is not None:
            return await super().sync(guild=guild)

        # Discord forbids bulk-removing the Entry Point command (type 4) that
        # it auto-creates when the Activity feature is enabled. Fetch existing
        # global commands, preserve any Entry Points, and inject them into the
        # payload so the upsert doesn't try to delete them.
        existing = await self.client.http.get_global_commands(self.client.application_id)
        entry_points = [c for c in existing if c.get("type") == 4]
        if not entry_points:
            return await super().sync()

        commands = self._get_all_commands(guild=None)
        translator = self.translator
        if translator:
            payload = [await cmd.get_translated_payload(self, translator) for cmd in commands]
        else:
            payload = [cmd.to_dict(self) for cmd in commands]
        payload.extend(entry_points)

        data = await self._http.bulk_upsert_global_commands(self.client.application_id, payload=payload)
        return [app_commands.AppCommand(data=d, state=self._state) for d in data]


class SportsBookBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, tree_cls=SportsBookCommandTree)

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
            await self.tree.sync(guild=guild)
            log.info(f"Slash commands synced to dev guild {config.DEV_GUILD_ID}")

        await self.tree.sync()
        log.info("Slash commands synced globally")

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        from bot.cogs.admin import AdminCog
        from bot.utils.audit import post_audit_log
        if not isinstance(getattr(command, "binding", None), AdminCog):
            return
        await post_audit_log(self, interaction)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info("Panem Sportsbook is open for business.")
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

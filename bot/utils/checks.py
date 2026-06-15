import json

import discord
from discord import app_commands
from bot import config


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        # Per-guild admin role takes precedence over the global env var.
        if interaction.guild_id:
            from bot.database.engine import get_guild_setting, current_guild_id
            raw = await get_guild_setting(current_guild_id(), "admin_role_id")
            if raw:
                role_id = json.loads(raw)
                if any(r.id == role_id for r in member.roles):
                    return True
        if config.ADMIN_ROLE_ID:
            return any(r.id == config.ADMIN_ROLE_ID for r in member.roles)
        return False

    return app_commands.check(predicate)

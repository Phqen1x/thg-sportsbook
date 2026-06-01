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
        if config.ADMIN_ROLE_ID:
            return any(r.id == config.ADMIN_ROLE_ID for r in member.roles)
        return False

    return app_commands.check(predicate)

import json

import discord
from discord import app_commands
from bot import config


async def is_admin_member(member: discord.Member, guild_id: int | None = None) -> bool:
    """Plain (non-decorator) admin check, for use outside app_commands checks —
    e.g. component/button callbacks, which discord.py doesn't route through
    app_commands.check predicates."""
    if member.guild_permissions.administrator:
        return True
    # Per-guild admin role takes precedence over the global env var.
    gid = guild_id if guild_id is not None else member.guild.id if member.guild else None
    if gid:
        from bot.database.engine import get_guild_setting
        raw = await get_guild_setting(gid, "admin_role_id")
        if raw:
            role_id = json.loads(raw)
            if any(r.id == role_id for r in member.roles):
                return True
    if config.ADMIN_ROLE_ID:
        return any(r.id == config.ADMIN_ROLE_ID for r in member.roles)
    return False


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        from bot.database.engine import current_guild_id
        return await is_admin_member(member, current_guild_id())

    return app_commands.check(predicate)

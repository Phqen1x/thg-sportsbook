from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord.ext import commands


def _format_options(options: list[dict], resolved: dict | None = None) -> str:
    parts: list[str] = []
    for opt in options:
        if opt.get("type") in (1, 2):  # SUB_COMMAND / SUB_COMMAND_GROUP
            sub = _format_options(opt.get("options", []), resolved)
            if sub:
                parts.append(sub)
        else:
            name = opt["name"]
            value = opt.get("value", "")
            opt_type = opt.get("type")
            resolved = resolved or {}
            if opt_type == 6 and str(value) in (resolved.get("members") or {}):
                user_data = (resolved.get("users") or {}).get(str(value), {})
                value = f"@{user_data.get('global_name') or user_data.get('username', value)}"
            elif opt_type == 7 and str(value) in (resolved.get("channels") or {}):
                chan = resolved["channels"][str(value)]
                value = f"#{chan.get('name', value)}"
            elif opt_type == 8 and str(value) in (resolved.get("roles") or {}):
                role = resolved["roles"][str(value)]
                value = f"@{role.get('name', value)}"
            parts.append(f"**{name}:** {value}")
    return "\n".join(parts)


def _extract_target(
    interaction: discord.Interaction,
) -> discord.Member | discord.User | None:
    data = interaction.data or {}
    resolved = data.get("resolved") or {}

    def _scan(options: list[dict]) -> int | None:
        for opt in options:
            if opt.get("type") in (1, 2):
                result = _scan(opt.get("options", []))
                if result is not None:
                    return result
            elif opt.get("type") == 6:
                return int(opt["value"])
        return None

    user_id = _scan(data.get("options", []))
    if user_id is None:
        return None
    if interaction.guild:
        member = interaction.guild.get_member(user_id)
        if member:
            return member
    user_data = (resolved.get("users") or {}).get(str(user_id))
    if user_data:
        return interaction.client.get_user(user_id)
    return None


async def post_audit_log(
    bot: commands.Bot,
    interaction: discord.Interaction,
    target: discord.Member | discord.User | None = None,
) -> None:
    from bot.database.engine import get_guild_setting

    if not interaction.guild_id:
        return
    raw = await get_guild_setting(interaction.guild_id, "log_channel_id")
    if not raw:
        return
    channel = bot.get_channel(int(raw))
    if not isinstance(channel, discord.TextChannel):
        return

    data = interaction.data or {}
    options = data.get("options", [])
    resolved = data.get("resolved")
    options_text = _format_options(options, resolved)
    command_name = interaction.command.qualified_name if interaction.command else "unknown"

    embed = discord.Embed(
        title="Admin Action",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Command", value=f"`/{command_name}`", inline=True)
    actor = interaction.user
    embed.add_field(name="Executed by", value=actor.mention, inline=True)
    if target is None:
        target = _extract_target(interaction)
    if target:
        embed.add_field(name="Target", value=target.mention, inline=True)
    if options_text:
        embed.add_field(name="Options", value=options_text[:1024], inline=False)
    embed.set_footer(
        text=f"{actor.display_name} • {actor.id}",
        icon_url=actor.display_avatar.url,
    )
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

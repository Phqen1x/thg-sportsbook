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
    from bot.database.engine import get_guild_setting, current_guild_id, set_guild_context

    if not interaction.guild_id:
        return
    # on_app_command_completion dispatches in a separate task, so the command's
    # guild context did not propagate here — bind it from the interaction.
    set_guild_context(interaction.guild_id)
    raw = await get_guild_setting(current_guild_id(), "log_channel_id")
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


async def _log_channel(bot: commands.Bot, guild_id: int) -> discord.TextChannel | None:
    import json

    from bot.database.engine import get_guild_setting

    raw = await get_guild_setting(guild_id, "log_channel_id")
    if not raw:
        return None
    # Values are JSON-encoded, and a cleared setting is stored as the string
    # "null" — which is truthy, so `int(raw)` would blow up on it if we didn't
    # decode through json.loads first.
    channel_id = json.loads(raw)
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None


async def post_bet_log(
    bot: commands.Bot,
    member: discord.Member,
    kind: str,
    markets: list[str],
    wager: int,
    payout: int,
    is_tail: bool = False,
) -> None:
    """Logs a member-placed straight bet or parlay submission — every one,
    regardless of entry point (slash command, web dashboard, Discord Activity,
    or tailing another parlay)."""
    from bot.utils.action_views import build_request_view
    from bot.utils.formatters import fmt_chips

    guild_id = member.guild.id
    channel = await _log_channel(bot, guild_id)
    if channel is None:
        return

    title = "Parlay Submitted" if kind == "PARLAY" else "Bet Placed"
    if is_tail:
        title += " (Tail)"
    embed = discord.Embed(title=title, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(
        name="Market" if len(markets) == 1 else "Markets",
        value="\n".join(markets)[:1024] or "—",
        inline=False,
    )
    embed.add_field(name="Wager", value=fmt_chips(wager), inline=True)
    embed.add_field(name="Potential Payout", value=fmt_chips(payout), inline=True)
    embed.set_footer(text=f"{member.display_name} • {member.id}")

    # A blocked member can never reach this call (placement is rejected first),
    # so the button always starts in the "Block" state.
    view = build_request_view(None, guild_id, member.id, blocked=False)
    try:
        await channel.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def post_win_log(
    bot: commands.Bot,
    guild_id: int,
    user_id: int,
    kind: str,
    markets: list[str],
    wager: int,
    payout: int,
) -> None:
    """Logs a resolved WON bet/parlay and, if the payout clears the
    configurable big-win threshold, pings the guild's admin role."""
    import json

    from bot.database.engine import get_guild_setting, get_read_session
    from bot.utils.formatters import fmt_chips
    from bot.utils.restrictions import is_fully_restricted

    channel = await _log_channel(bot, guild_id)
    if channel is None:
        return

    async with get_read_session() as session:
        blocked = await is_fully_restricted(session, guild_id, user_id)

    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None

    title = "Parlay Won" if kind == "PARLAY" else "Bet Won"
    embed = discord.Embed(title=title, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    if user is not None:
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Member", value=f"{user.mention} (`{user_id}`)", inline=False)
        embed.set_footer(text=f"{user.display_name} • {user_id}")
    else:
        embed.add_field(name="Member", value=f"<@{user_id}> (`{user_id}`)", inline=False)
    embed.add_field(
        name="Market" if len(markets) == 1 else "Markets",
        value="\n".join(markets)[:1024] or "—",
        inline=False,
    )
    embed.add_field(name="Wager", value=fmt_chips(wager), inline=True)
    embed.add_field(name="Payout", value=fmt_chips(payout), inline=True)

    from bot.utils.action_views import build_request_view
    view = build_request_view(None, guild_id, user_id, blocked)

    content = None
    threshold_raw = await get_guild_setting(guild_id, "big_win_threshold")
    threshold = json.loads(threshold_raw) if threshold_raw else 500_000
    if payout >= threshold:
        role_raw = await get_guild_setting(guild_id, "admin_role_id")
        if role_raw:
            role_id = json.loads(role_raw)
            content = f"<@&{role_id}> — big win alert!"

    try:
        await channel.send(content=content, embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass

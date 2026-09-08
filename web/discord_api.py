from __future__ import annotations

import httpx

from web import config

DISCORD_API = "https://discord.com/api/v10"


def avatar_url(discord_id: int, avatar_hash: str | None) -> str:
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
    return f"https://cdn.discordapp.com/embed/avatars/{discord_id % 5}.png"


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.DISCORD_REDIRECT_URI,
            },
        )
        r.raise_for_status()
        return r.json()


async def exchange_code_activity(code: str) -> dict:
    """Exchange an OAuth code from the Embedded App SDK.

    Unlike the browser flow, the embedded-app ``authorize`` handshake has no
    redirect URI, so it must be omitted from the token exchange.
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        r.raise_for_status()
        return r.json()


async def get_user(access_token: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return r.json()


async def get_member(user_id: int, *, guild_id: int | None = None) -> dict | None:
    """Return the guild member dict, or None if not a member. Returns {} when no guild is known."""
    gid = guild_id or config.GUILD_ID
    if not gid:
        return {}
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{DISCORD_API}/guilds/{gid}/members/{user_id}",
            headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
        )
        return r.json() if r.status_code == 200 else None


async def list_guild_text_channels(guild_id: int | None = None) -> list[dict]:
    """Text / announcement channels in the guild (bot token — no privileged
    intent needed). Returns ``[{"id", "name", "position"}]`` sorted by position,
    or ``[]`` on any failure."""
    gid = int(guild_id or config.GUILD_ID or 0)
    if not gid:
        return []
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{DISCORD_API}/guilds/{gid}/channels",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
            )
        if r.status_code != 200:
            return []
        chans = [
            {"id": str(ch["id"]), "name": ch.get("name") or str(ch["id"]),
             "position": ch.get("position", 0)}
            for ch in r.json()
            if ch.get("type") in (0, 5)  # GUILD_TEXT, GUILD_ANNOUNCEMENT
        ]
        chans.sort(key=lambda c: c["position"])
        return chans
    except Exception:
        return []


async def list_guild_roles(guild_id: int | None = None) -> list[dict]:
    """Mentionable-ish roles in the guild (bot token). Returns
    ``[{"id", "name", "position"}]`` highest-first, excluding @everyone and
    managed/integration roles, or ``[]`` on any failure."""
    gid = int(guild_id or config.GUILD_ID or 0)
    if not gid:
        return []
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{DISCORD_API}/guilds/{gid}/roles",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
            )
        if r.status_code != 200:
            return []
        roles = [
            {"id": str(role["id"]), "name": role.get("name") or str(role["id"]),
             "position": role.get("position", 0)}
            for role in r.json()
            if str(role["id"]) != str(gid) and not role.get("managed")
        ]
        roles.sort(key=lambda x: -x["position"])
        return roles
    except Exception:
        return []


async def post_channel_message(
    channel_id: int, content: str, *, components: list | None = None,
    embeds: list | None = None, allowed_mentions: dict | None = None,
) -> dict | None:
    """POST a message to a channel with the bot token. ``components`` is the raw
    Discord component array (e.g. an action row with a button); ``embeds`` and
    ``allowed_mentions`` are passed straight through. Returns the created message
    dict, or None on failure."""
    payload: dict = {"content": content}
    if components:
        payload["components"] = components
    if embeds:
        payload["embeds"] = embeds
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
                json=payload,
            )
        return r.json() if r.status_code in (200, 201) else None
    except Exception:
        return None


async def check_admin(member: dict, *, guild_id: int | None = None) -> bool:
    """Check admin status from a pre-fetched member dict (see get_member).

    Uses ADMIN_ROLE_ID if configured; otherwise falls back to the server
    Administrator permission bit — one extra API call in that case.
    """
    gid = guild_id or config.GUILD_ID
    if not gid:
        return False
    member_role_ids = {int(x) for x in member.get("roles", [])}
    if config.ADMIN_ROLE_ID:
        return config.ADMIN_ROLE_ID in member_role_ids
    async with httpx.AsyncClient() as c:
        roles_r = await c.get(
            f"{DISCORD_API}/guilds/{gid}/roles",
            headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
        )
        if roles_r.status_code != 200:
            return False
        ADMINISTRATOR = 0x8
        for role in roles_r.json():
            if int(role["id"]) in member_role_ids:
                if int(role["permissions"]) & ADMINISTRATOR:
                    return True
        return False


async def can_use_bot(member: dict, user_id: int, *, guild_id: int | None = None) -> bool:
    """Return True if this member is allowed to use the bot's slash commands.

    Mirrors the command-permission overrides set in Discord's
    Server Settings -> Integrations UI, which are stored as application command
    permissions. We evaluate the app-wide ("all commands") override using the
    member's roles. Channel-specific overrides are ignored because website login
    has no channel context. Fails open (allows) when no overrides exist or the
    Discord API can't be reached, so a transient error never locks everyone out.
    """
    gid = guild_id or config.GUILD_ID
    if not gid or not config.DISCORD_CLIENT_ID:
        return True  # nothing to evaluate against
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{DISCORD_API}/applications/{config.DISCORD_CLIENT_ID}/guilds/{gid}/commands/permissions",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
            )
    except httpx.HTTPError:
        return True  # fail open on network error
    if r.status_code == 404:
        return True  # no command permissions configured -> bot open to everyone
    if r.status_code != 200:
        return True  # fail open on unexpected response

    app_id = config.DISCORD_CLIENT_ID
    # The "all commands" default override has id == application_id.
    overrides = next(
        (o["permissions"] for o in r.json() if str(o.get("id")) == str(app_id)),
        None,
    )
    if not overrides:
        return True  # no app-wide restriction

    ROLE, USER = 1, 2  # CHANNEL == 3 is ignored (no channel context at login)
    member_role_ids = {int(x) for x in member.get("roles", [])}
    member_role_ids.add(int(gid))  # @everyone role id == guild id; everyone has it

    # An explicit user override takes precedence over roles.
    for p in overrides:
        if p["type"] == USER and str(p["id"]) == str(user_id):
            return bool(p["permission"])

    # Otherwise, any applicable role that explicitly allows wins over a deny.
    applicable = [
        bool(p["permission"])
        for p in overrides
        if p["type"] == ROLE and int(p["id"]) in member_role_ids
    ]
    if applicable:
        return any(applicable)
    return True  # no override applies to this member -> default allowed


async def get_guild_name(guild_id: int) -> str | None:
    """Return the guild's name, or None on error."""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
        )
        return r.json().get("name") if r.status_code == 200 else None

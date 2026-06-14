from __future__ import annotations

import httpx

from web import config

DISCORD_API = "https://discord.com/api/v10"


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


async def get_guild_name(guild_id: int) -> str | None:
    """Return the guild's name, or None on error."""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
        )
        return r.json().get("name") if r.status_code == 200 else None

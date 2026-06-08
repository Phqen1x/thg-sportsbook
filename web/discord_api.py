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


async def get_member_roles(user_id: int) -> list[int]:
    if not config.GUILD_ID:
        return []
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{DISCORD_API}/guilds/{config.GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
        )
        if r.status_code != 200:
            return []
        return [int(x) for x in r.json().get("roles", [])]


async def is_admin(user_id: int) -> bool:
    if not config.ADMIN_ROLE_ID:
        return False
    return config.ADMIN_ROLE_ID in await get_member_roles(user_id)

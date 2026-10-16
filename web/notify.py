"""Bet-result DMs and win-log posts for resolutions triggered from the web
dashboard or Discord Activity.

The web process has no gateway connection, so it logs in a minimal REST-only
discord.py client (no websocket) and reuses the bot's own sender — the DMs and
log posts are identical to those from the Discord commands."""
from __future__ import annotations

import logging

import discord

from web import config

log = logging.getLogger("capitol.web.notify")

_client: discord.Client | None = None


async def _get_client() -> discord.Client:
    global _client
    if _client is None:
        client = discord.Client(intents=discord.Intents.none())
        await client.login(config.BOT_TOKEN)
        _client = client
    return _client


async def send_resolution_dms(notifications: list[dict]) -> None:
    if not notifications:
        return
    try:
        from bot.cogs.admin import _send_resolution_dms

        await _send_resolution_dms(await _get_client(), notifications)  # type: ignore[arg-type]
    except Exception:
        log.exception("Failed to send resolution notifications from web")

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from web import config, discord_api
from web.database import get_db
from web.session import SessionUser

_DISCORD_API = "https://discord.com/api/v10"


async def post_admin_action(
    actor: SessionUser,
    action: str,
    details: dict[str, str] | None = None,
    source: str = "Web Dashboard",
) -> None:
    if not config.BOT_TOKEN or not config.GUILD_ID:
        return
    try:
        async with get_db() as db:
            row = (await db.execute(
                text("SELECT value FROM game_settings WHERE key=:k"),
                {"k": f"{config.GUILD_ID}:log_channel_id"},
            )).fetchone()
        if not row:
            return
        channel_id = int(json.loads(row[0]))

        fields: list[dict] = [
            {"name": "Action", "value": action[:1024], "inline": True},
            {"name": "Executed by", "value": f"{actor.username} (`{actor.discord_id}`)", "inline": True},
            {"name": "Source", "value": source, "inline": True},
        ]
        if details:
            value = "\n".join(f"**{k}:** {v}" for k, v in details.items())
            fields.append({"name": "Details", "value": value[:1024], "inline": False})

        embed = {
            "title": "Admin Action",
            "color": 0xFFD700,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": f"{actor.username} • {actor.discord_id}"},
        }
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.post(
                f"{_DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
                json={"embeds": [embed]},
            )
    except Exception:
        pass


async def post_bet_log(
    guild_id: int,
    user_id: int,
    kind: str,
    markets: list[str],
    wager: int,
    payout: int,
    is_tail: bool = False,
) -> None:
    """Web-side counterpart to bot/utils/audit.py:post_bet_log — posts via raw
    REST since this process has no live discord.py Bot/Member object. Attaches
    the same Block/Unblock button (matching custom_id scheme) so a click is
    routed to the bot process's registered BlockToggleButton regardless of
    which process posted the message."""
    if not config.BOT_TOKEN:
        return
    try:
        async with get_db() as db:
            row = (await db.execute(
                text("SELECT value FROM game_settings WHERE key=:k"),
                {"k": f"{guild_id}:log_channel_id"},
            )).fetchone()
            if not row:
                return
            channel_id = int(json.loads(row[0]))

            restriction_row = (await db.execute(
                text(
                    "SELECT 1 FROM betting_restrictions "
                    "WHERE guild_id=:g AND discord_user_id=:u AND restriction_type='ALL'"
                ),
                {"g": guild_id, "u": user_id},
            )).fetchone()
        blocked = restriction_row is not None

        member = await discord_api.get_member(user_id, guild_id=guild_id) or {}
        user_data = member.get("user") or {}
        nickname = member.get("nick") or user_data.get("global_name") or user_data.get("username") or str(user_id)
        avatar_url = discord_api.avatar_url(user_id, user_data.get("avatar"))

        title = "Parlay Submitted" if kind == "PARLAY" else "Bet Placed"
        if is_tail:
            title += " (Tail)"
        embed = {
            "title": title,
            "color": 0x5865F2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thumbnail": {"url": avatar_url},
            "fields": [
                {"name": "Member", "value": f"{nickname} (`{user_id}`)", "inline": False},
                {
                    "name": "Market" if len(markets) == 1 else "Markets",
                    "value": "\n".join(markets)[:1024] or "—",
                    "inline": False,
                },
                {"name": "Wager", "value": f"{wager:,} chips", "inline": True},
                {"name": "Potential Payout", "value": f"{payout:,} chips", "inline": True},
            ],
            "footer": {"text": f"{nickname} • {user_id}"},
        }
        components = [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 3 if blocked else 4,
                "label": "Unblock" if blocked else "Block",
                "custom_id": f"blocktoggle:{guild_id}:{user_id}",
            }],
        }]
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.post(
                f"{_DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {config.BOT_TOKEN}"},
                json={"embeds": [embed], "components": components},
            )
    except Exception:
        pass

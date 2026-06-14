from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from web import config
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

"""Shared helpers for the full ("ALL") betting ban — the same underlying
BettingRestriction row backs both the /admin restrict ban|unban slash commands
and the Block/Unblock button on withdraw/deposit requests and bet/parlay logs,
so the two stay in sync rather than drifting as separate mechanisms.

Deliberately free of any bot.cogs import so both bot cogs and web routes can
share it without risking a circular import; the ``session`` argument just
needs to be any SQLAlchemy AsyncSession (bot's or web's)."""
from __future__ import annotations

from sqlalchemy import select

from bot.database.models import BettingRestriction


async def is_fully_restricted(session, guild_id: int, user_id: int) -> bool:
    result = await session.execute(
        select(BettingRestriction).where(
            BettingRestriction.guild_id == guild_id,
            BettingRestriction.discord_user_id == user_id,
            BettingRestriction.restriction_type == "ALL",
        )
    )
    return result.scalars().first() is not None


async def set_full_restriction(session, guild_id: int, user_id: int, blocked: bool) -> None:
    result = await session.execute(
        select(BettingRestriction).where(
            BettingRestriction.guild_id == guild_id,
            BettingRestriction.discord_user_id == user_id,
            BettingRestriction.restriction_type == "ALL",
        )
    )
    row = result.scalar_one_or_none()
    if blocked and row is None:
        session.add(BettingRestriction(guild_id=guild_id, discord_user_id=user_id, restriction_type="ALL"))
    elif not blocked and row is not None:
        await session.delete(row)

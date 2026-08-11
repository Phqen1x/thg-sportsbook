"""Shared helpers for the full ("ALL") betting ban — the same underlying
BettingRestriction row backs both the /admin restrict ban|unban slash commands
and the Block/Unblock button on withdraw/deposit requests and bet/parlay logs,
so the two stay in sync rather than drifting as separate mechanisms.

Deliberately free of any bot.cogs import so both bot cogs and web routes can
share it without risking a circular import; the ``session`` argument just
needs to be any SQLAlchemy AsyncSession (bot's or web's)."""
from __future__ import annotations

from sqlalchemy import select

from bot.database.models import BettingRestriction, PublicBetRestriction


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


async def is_public_bet_blocked(session, guild_id: int, user_id: int, role_ids: set[int]) -> bool:
    """True if ``user_id`` or any id in ``role_ids`` is blocked from posting
    PUBLIC parlays. Pure boolean OR — unlike exchange rates, a block has no
    "most specific wins" precedence to resolve."""
    user_match = (PublicBetRestriction.scope == "USER") & (PublicBetRestriction.target_id == user_id)
    role_match = (PublicBetRestriction.scope == "ROLE") & (PublicBetRestriction.target_id.in_(role_ids))
    result = await session.execute(
        select(PublicBetRestriction).where(
            PublicBetRestriction.guild_id == guild_id, user_match | role_match,
        )
    )
    return result.scalars().first() is not None


async def set_public_block(session, guild_id: int, scope: str, target_id: int, blocked: bool) -> bool:
    """Add/remove a public-parlay block for a role or user. Returns False if
    ``blocked`` didn't actually change anything (already in that state)."""
    result = await session.execute(
        select(PublicBetRestriction).where(
            PublicBetRestriction.guild_id == guild_id,
            PublicBetRestriction.scope == scope,
            PublicBetRestriction.target_id == target_id,
        )
    )
    row = result.scalar_one_or_none()
    if blocked and row is None:
        session.add(PublicBetRestriction(guild_id=guild_id, scope=scope, target_id=target_id))
        return True
    elif not blocked and row is not None:
        await session.delete(row)
        return True
    return False


async def list_public_blocks(session, guild_id: int) -> list[PublicBetRestriction]:
    result = await session.execute(
        select(PublicBetRestriction)
        .where(PublicBetRestriction.guild_id == guild_id)
        .order_by(PublicBetRestriction.scope)
    )
    return list(result.scalars().all())

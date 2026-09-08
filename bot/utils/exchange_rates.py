"""Exchange-rate multipliers.

Two independent things live here:

  * The global DEPOSIT / WITHDRAW conversion rates used by /deposit and
    /withdraw (``get_global_rate`` / ``set_global_rate``). These are
    server-wide only — per-role/per-user overrides no longer apply to
    conversions.

  * PAYOUT overrides (``ExchangeRateOverride`` rows with direction "PAYOUT",
    plus the "payout_rate" global): a multiplier applied to a WON bet/parlay's
    gross payout at settlement, resolved per bettor as their own USER override
    > the highest-position Discord role they hold that carries a ROLE override
    (mirrors how Discord's own permission overwrites resolve conflicts) > the
    global "payout_rate" (1.0 if never set). It is resolved once at submit time
    and frozen on the Bet/Parlay row (see effective_rate + member_role_ids).

Deliberately free of any bot.cogs import, matching bot/utils/restrictions.py,
so it can be shared by cogs and web routes without risking a circular import.
"""
from __future__ import annotations

import json
from typing import Iterable

import discord
from sqlalchemy import select

from bot.database.models import ExchangeRateOverride

DIRECTIONS = ("DEPOSIT", "WITHDRAW", "PAYOUT")
_GLOBAL_KEYS = {
    "DEPOSIT": "deposit_rate",
    "WITHDRAW": "withdraw_rate",
    "PAYOUT": "payout_rate",
}
DEFAULT_RATE = 1.0


async def get_global_rate(direction: str) -> float:
    from bot.database.engine import get_setting
    raw = await get_setting(_GLOBAL_KEYS[direction])
    return json.loads(raw) if raw else DEFAULT_RATE


async def set_global_rate(direction: str, rate: float) -> None:
    from bot.database.engine import set_setting
    await set_setting(_GLOBAL_KEYS[direction], rate)


def member_role_ids(member: discord.Member | None) -> list[int]:
    """Role ids for ``member``, highest position first, @everyone dropped — the
    order effective_rate walks so the member's top role wins on conflicts."""
    if not isinstance(member, discord.Member):
        return []
    return [role.id for role in reversed(member.roles) if not role.is_default()]


async def _get_override(session, guild_id: int, scope: str, target_id: int, direction: str) -> ExchangeRateOverride | None:
    result = await session.execute(
        select(ExchangeRateOverride).where(
            ExchangeRateOverride.guild_id == guild_id,
            ExchangeRateOverride.scope == scope,
            ExchangeRateOverride.target_id == target_id,
            ExchangeRateOverride.direction == direction,
        )
    )
    return result.scalar_one_or_none()


async def effective_rate(
    session, guild_id: int, *, user_id: int, role_ids: Iterable[int], direction: str = "PAYOUT"
) -> float:
    """Resolve the multiplier for a bettor: their USER override > the first
    ROLE override found walking ``role_ids`` (pass them highest-precedence
    first) > the global rate for ``direction``."""
    user_override = await _get_override(session, guild_id, "USER", user_id, direction)
    if user_override is not None:
        return user_override.rate

    for role_id in role_ids:
        role_override = await _get_override(session, guild_id, "ROLE", role_id, direction)
        if role_override is not None:
            return role_override.rate

    return await get_global_rate(direction)


async def set_override(session, guild_id: int, scope: str, target_id: int, direction: str, rate: float) -> None:
    existing = await _get_override(session, guild_id, scope, target_id, direction)
    if existing is not None:
        existing.rate = rate
    else:
        session.add(ExchangeRateOverride(
            guild_id=guild_id, scope=scope, target_id=target_id, direction=direction, rate=rate,
        ))


async def clear_override(session, guild_id: int, scope: str, target_id: int, direction: str) -> bool:
    existing = await _get_override(session, guild_id, scope, target_id, direction)
    if existing is None:
        return False
    await session.delete(existing)
    return True


async def list_overrides(session, guild_id: int) -> list[ExchangeRateOverride]:
    result = await session.execute(
        select(ExchangeRateOverride)
        .where(ExchangeRateOverride.guild_id == guild_id)
        .order_by(ExchangeRateOverride.direction, ExchangeRateOverride.scope)
    )
    return list(result.scalars().all())

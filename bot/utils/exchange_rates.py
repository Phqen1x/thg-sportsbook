"""Chip<->Panar conversion rates used by /withdraw and /deposit.

Resolution order for a given member + direction: their own USER-scoped
override > the highest-position Discord role they hold that has a
ROLE-scoped override (mirrors how Discord's own permission overwrites
resolve conflicts) > the global GameSetting default ("deposit_rate" /
"withdraw_rate", 1.0 if never set — preserving the original 1:1 exchange).

Deliberately free of any bot.cogs import, matching bot/utils/restrictions.py,
so it can be shared by cogs and (in the future) web routes without risking a
circular import.
"""
from __future__ import annotations

import json

import discord
from sqlalchemy import select

from bot.database.models import ExchangeRateOverride

DIRECTIONS = ("DEPOSIT", "WITHDRAW")
_GLOBAL_KEYS = {"DEPOSIT": "deposit_rate", "WITHDRAW": "withdraw_rate"}
DEFAULT_RATE = 1.0


async def get_global_rate(direction: str) -> float:
    from bot.database.engine import get_setting
    raw = await get_setting(_GLOBAL_KEYS[direction])
    return json.loads(raw) if raw else DEFAULT_RATE


async def set_global_rate(direction: str, rate: float) -> None:
    from bot.database.engine import set_setting
    await set_setting(_GLOBAL_KEYS[direction], rate)


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


async def effective_rate(session, guild_id: int, member: discord.Member, direction: str) -> float:
    """Resolve the rate that applies to ``member`` for ``direction``."""
    user_override = await _get_override(session, guild_id, "USER", member.id, direction)
    if user_override is not None:
        return user_override.rate

    # member.roles is ordered by position ascending (@everyone first); check
    # from the top down so the member's highest role wins on conflicts.
    for role in reversed(member.roles):
        if role.is_default():
            continue
        role_override = await _get_override(session, guild_id, "ROLE", role.id, direction)
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

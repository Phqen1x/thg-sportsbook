"""Maximum chip payout allowed for a single bet / a parlay, independently.

A wager whose payout would exceed the applicable cap is rejected outright by
the caller (never silently reduced) — see bot.cogs.betting's
_single_cap_error / _parlay_cap_error and their web/Activity equivalents.

Deliberately free of any bot.cogs import, matching bot/utils/exchange_rates.py,
so it can be shared by cogs and web routes without risking a circular import.
"""
from __future__ import annotations

import json

from bot import config
from bot.database.engine import get_setting, set_setting

_KEYS = {"SINGLE": "single_payout_cap", "PARLAY": "parlay_payout_cap"}
_DEFAULTS = {"SINGLE": config.SINGLE_PAYOUT_CAP, "PARLAY": config.PARLAY_PAYOUT_CAP}


async def get_payout_cap(kind: str) -> int:
    """``kind`` is "SINGLE" or "PARLAY"."""
    raw = await get_setting(_KEYS[kind])
    return int(json.loads(raw)) if raw else _DEFAULTS[kind]


async def set_payout_cap(kind: str, cap: int) -> None:
    await set_setting(_KEYS[kind], cap)

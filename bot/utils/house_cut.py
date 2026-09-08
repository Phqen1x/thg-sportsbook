"""Configurable house cut (rake) taken from winnings at settlement.

Applies only to *profit* (payout_if_win - wager) on a WON bet/parlay — the
returned stake is never touched. Three independently-adjustable levers:

  - a global default percentage (0 by default, i.e. no cut)
  - a per-market-type override, keyed by Market.type — straight bets only.
    Parlays span multiple market types, so (like
    bot.odds.calculator.resolve_cashout's item -> type -> global precedence,
    which likewise treats parlays as having no type tier) they never consult
    this tier.
  - a surcharge for bets/parlays whose American odds exceed a configurable
    threshold (e.g. "anything paying above +500 gets a bigger cut"). Applies
    to straight bets and parlays alike.

When more than one tier applies, the *higher* percentage wins — the house
never leaves money on the table by picking the smaller of two rates it
explicitly configured.

Deliberately free of any bot.cogs import, matching bot/utils/payout_caps.py
and bot/utils/exchange_rates.py, so it can be shared by cogs and web routes
without risking a circular import.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from bot.database.engine import get_setting, set_setting
from bot.odds.calculator import decimal_to_american

_GLOBAL_KEY = "house_cut_pct"
_BY_TYPE_KEY = "house_cut_by_type"
_HIGH_ODDS_THRESHOLD_KEY = "house_cut_high_odds_threshold"
_HIGH_ODDS_PCT_KEY = "house_cut_high_odds_pct"
_TOTAL_TAKEN_KEY = "house_cut_total_taken"


@dataclass
class HouseCutConfig:
    global_pct: float = 0.0
    by_type: dict[str, float] = field(default_factory=dict)
    high_odds_threshold: int | None = None
    high_odds_pct: float = 0.0


async def load_house_cut_config() -> HouseCutConfig:
    raw_global = await get_setting(_GLOBAL_KEY)
    raw_by_type = await get_setting(_BY_TYPE_KEY)
    raw_threshold = await get_setting(_HIGH_ODDS_THRESHOLD_KEY)
    raw_high_pct = await get_setting(_HIGH_ODDS_PCT_KEY)
    threshold_val = json.loads(raw_threshold) if raw_threshold else None
    return HouseCutConfig(
        global_pct=float(json.loads(raw_global)) if raw_global else 0.0,
        by_type=json.loads(raw_by_type) if raw_by_type else {},
        high_odds_threshold=int(threshold_val) if threshold_val is not None else None,
        high_odds_pct=float(json.loads(raw_high_pct)) if raw_high_pct else 0.0,
    )


async def set_house_cut_global(pct: float) -> None:
    await set_setting(_GLOBAL_KEY, pct)


async def set_house_cut_for_type(market_type: str, pct: float | None) -> dict[str, float]:
    """``pct=None`` removes the override, falling back to the global rate."""
    raw = await get_setting(_BY_TYPE_KEY)
    by_type: dict[str, float] = json.loads(raw) if raw else {}
    if pct is None:
        by_type.pop(market_type, None)
    else:
        by_type[market_type] = pct
    await set_setting(_BY_TYPE_KEY, by_type)
    return by_type


async def set_house_cut_high_odds_rule(threshold: int | None, pct: float) -> None:
    await set_setting(_HIGH_ODDS_THRESHOLD_KEY, threshold)
    await set_setting(_HIGH_ODDS_PCT_KEY, pct)


async def get_house_cut_total_taken() -> int:
    raw = await get_setting(_TOTAL_TAKEN_KEY)
    return int(json.loads(raw)) if raw else 0


async def record_house_cut_taken(amount: int) -> None:
    """Add ``amount`` to the running house-cut total (negative to undo a
    reversed resolution — see bot.cogs.admin._unresolve_market). Call once per
    resolution batch (a whole market's worth of bets, or one parlay) with the
    summed cut rather than per bet, so a popular market doesn't fire dozens of
    writes."""
    if amount == 0:
        return
    await set_setting(_TOTAL_TAKEN_KEY, await get_house_cut_total_taken() + amount)


def resolve_cut_pct(config: HouseCutConfig, *, market_type: str | None, odds: int) -> float:
    """``market_type=None`` (parlays) skips the per-type tier."""
    pct = config.by_type.get(market_type, config.global_pct) if market_type is not None else config.global_pct
    if config.high_odds_threshold is not None and odds > config.high_odds_threshold:
        pct = max(pct, config.high_odds_pct)
    return max(0.0, min(100.0, pct))


def house_cut_amount(profit: int, pct: float) -> int:
    if profit <= 0 or pct <= 0:
        return 0
    return min(profit, round(profit * pct / 100.0))


def parlay_effective_odds(total_wager: int, total_payout: int) -> int:
    """Derive the American odds a parlay's combined payout implies, for
    checking against the high-odds threshold — Parlay stores total_payout,
    not the combined odds that produced it."""
    if total_wager <= 0:
        return 100
    return decimal_to_american(total_payout / total_wager)


def boosted_gross(payout_if_win: int, payout_rate: float, cap: int | None) -> int:
    """The gross chips a WON wager pays before the house cut: its frozen
    payout_if_win scaled by the bettor's PAYOUT-rate multiplier
    (bot.utils.exchange_rates), then held to ``cap``.

    The ceiling is ``max(payout_if_win, cap)`` so a rate of 1.0 is always a
    no-op — a bet whose base payout already sits above a since-lowered cap still
    pays what it was placed for, and only the boost itself is capped."""
    gross = round(payout_if_win * payout_rate)
    if cap is not None:
        gross = min(gross, max(payout_if_win, cap))
    return gross


def net_payout(
    config: HouseCutConfig, *, wager: int, payout_if_win: int, market_type: str | None, odds: int,
    payout_rate: float = 1.0, cap: int | None = None,
) -> tuple[int, int]:
    """Return ``(net_payout, cut_amount)`` for a WON bet/parlay given an
    already-loaded config. Pure/sync so it's cheap to call per bet inside a
    settlement loop — load the config once per batch with
    ``load_house_cut_config`` and pass it in.

    ``payout_rate`` (the bettor's frozen PAYOUT multiplier) and ``cap`` (the
    applicable single/parlay payout cap) are applied to the gross payout first —
    see boosted_gross — then the house cut comes off the resulting profit."""
    gross = boosted_gross(payout_if_win, payout_rate, cap)
    pct = resolve_cut_pct(config, market_type=market_type, odds=odds)
    cut = house_cut_amount(gross - wager, pct)
    return gross - cut, cut


__all__ = [
    "HouseCutConfig",
    "load_house_cut_config",
    "set_house_cut_global",
    "set_house_cut_for_type",
    "set_house_cut_high_odds_rule",
    "get_house_cut_total_taken",
    "record_house_cut_taken",
    "resolve_cut_pct",
    "house_cut_amount",
    "parlay_effective_odds",
    "boosted_gross",
    "net_payout",
]

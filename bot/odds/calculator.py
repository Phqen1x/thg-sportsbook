from __future__ import annotations
import math

# ── Odds rail (soft-knee compression) ────────────────────────────────────────
# American odds magnitude is capped at ±ODDS_RAIL. Rather than a hard clamp at
# the rail (which made every sub-1% / super-99% probability collapse onto an
# identical ±9900), the magnitude is bent smoothly toward the rail above a
# "knee": everything below ODDS_KNEE passes through untouched (competitive
# lines are unaffected), and beyond it the curve asymptotes to ODDS_RAIL so the
# rail is reached only by genuinely extreme inputs. ODDS_TAU sets how quickly
# the tail approaches the rail (larger = longshots compressed harder).
ODDS_RAIL = 9900.0
ODDS_KNEE = 2500.0
ODDS_TAU = 11000.0
# Internal probability clamp, far below the rail so the soft knee (not the
# clamp) governs the tail. Symmetric so both extremes behave identically.
_PROB_MIN = 0.0002


def _soft_rail(magnitude: float) -> float:
    """Bend an American-odds magnitude toward ODDS_RAIL above ODDS_KNEE."""
    if magnitude <= ODDS_KNEE:
        return magnitude
    span = ODDS_RAIL - ODDS_KNEE
    return ODDS_KNEE + span * (1.0 - math.exp(-(magnitude - ODDS_KNEE) / ODDS_TAU))


def american_to_decimal(odds: int) -> float:
    if odds > 0:
        return odds / 100.0 + 1.0
    return 100.0 / abs(odds) + 1.0


def decimal_to_american(dec: float) -> int:
    if dec >= 2.0:
        return round((dec - 1.0) * 100)
    return round(-100.0 / (dec - 1.0))


def prob_to_american(prob: float) -> int:
    prob = max(_PROB_MIN, min(1.0 - _PROB_MIN, prob))
    if prob >= 0.5:
        raw = -(prob / (1.0 - prob)) * 100.0
    else:
        raw = ((1.0 - prob) / prob) * 100.0
    magnitude = _soft_rail(abs(raw))
    signed = -magnitude if raw < 0 else magnitude
    rounded = round(signed / 5.0) * 5
    return int(rounded)


def parlay_payout(wager: int, legs_odds: list[int]) -> int:
    combined = 1.0
    for o in legs_odds:
        combined *= american_to_decimal(o)
    return max(wager, round(wager * combined))


def combined_american(legs_odds: list[int]) -> int:
    if not legs_odds:
        return 100
    combined = 1.0
    for o in legs_odds:
        combined *= american_to_decimal(o)
    return decimal_to_american(combined)


def straight_payout(wager: int, odds: int) -> int:
    return max(wager, round(wager * american_to_decimal(odds)))


def cashout_value(original_wager: int, payout_if_win: int, rate: float) -> int:
    profit = payout_if_win - original_wager
    return max(1, round(original_wager + profit * rate))


def implied_probability(odds: int) -> float:
    dec = american_to_decimal(odds)
    return 1.0 / dec

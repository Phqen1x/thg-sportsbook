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
ODDS_RAIL = 999900.0
ODDS_KNEE = 50000.0
ODDS_TAU = 1000000.0
# Internal probability clamp, far below the rail so the soft knee (not the
# clamp) governs the tail. Symmetric so both extremes behave identically.
_PROB_MIN = 0.00001
# Parlays should be less extreme than a pure multiplication of independent legs.
# This dampener gently reduces the combined probability for multi-leg slips 
# (increasing the house edge/vig) in a more reasonable linear fashion.
PARLAY_VIG_PER_LEG = 0.96
PARLAY_VIG_MIN = 0.75


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
    combined_odds = combined_american(legs_odds)
    combined_decimal = american_to_decimal(combined_odds)
    return max(wager, round(wager * combined_decimal))


def combined_american(legs_odds: list[int]) -> int:
    if not legs_odds:
        return 100
    combined_prob = parlay_combined_probability(legs_odds)
    return prob_to_american(combined_prob)


def straight_payout(wager: int, odds: int) -> int:
    return max(wager, round(wager * american_to_decimal(odds)))


def max_wager_for_cap(decimal_odds: float, cap: int) -> int:
    """Largest integer wager whose payout at ``decimal_odds`` (see straight_payout
    / parlay_payout — always max(wager, round(wager * decimal_odds))) stays within
    ``cap``. Used to tell a member exactly how much they *could* wager when their
    attempted amount would breach a payout cap, rather than just rejecting it."""
    if cap <= 0 or decimal_odds <= 0:
        return 0
    wager = int(cap / decimal_odds)
    while wager > 0 and round(wager * decimal_odds) > cap:
        wager -= 1
    return max(wager, 0)


def cashout_value(original_wager: int, payout_if_win: int, rate: float) -> int:
    profit = payout_if_win - original_wager
    return max(1, round(original_wager + profit * rate))


def resolve_cashout(
    *,
    wager: int,
    payout_if_win: int,
    global_allowed: bool,
    global_rate: float,
    item_allowed: bool | None = None,
    item_rate: float | None = None,
    type_allowed: bool | None = None,
    type_rate: float | None = None,
) -> tuple[bool, int]:
    """Resolve cashout eligibility + amount via item -> type -> global precedence.

    ``item_*`` is the most specific override (a single Market or Parlay row);
    ``type_*`` is the market-type-level override (unused for parlays, which have
    no type tier). Returns ``(allowed, amount)`` — amount is 0 when not allowed.
    """
    if item_allowed is not None:
        allowed = item_allowed
        rate = item_rate if item_rate is not None else global_rate
    elif type_allowed is not None:
        allowed = type_allowed
        rate = type_rate if type_rate is not None else global_rate
    else:
        allowed = global_allowed
        rate = global_rate
    amount = cashout_value(wager, payout_if_win, rate) if allowed else 0
    return allowed, amount


def implied_probability(odds: int) -> float:
    dec = american_to_decimal(odds)
    return 1.0 / dec


def parlay_combined_probability(legs_odds: list[int]) -> float:
    if not legs_odds:
        return 0.5
    
    # Start with pure implied probabilities
    combined = 1.0
    for o in legs_odds:
        combined *= implied_probability(o)
    
    # Apply a light parlay vig (multiplicative reduction in probability)
    # per extra leg to keep odds more "reasonable" while maintaining a house edge.
    n = len(legs_odds)
    if n > 1:
        dampener = max(PARLAY_VIG_MIN, PARLAY_VIG_PER_LEG ** (n - 1))
        # Note: In probability terms, "vig" usually means increasing the 
        # probability of the legs (so the user gets worse odds).
        # Multiplying combined prob by e.g. 1.05 would do this.
        # But here we want the payout to be "more reasonable" (likely higher 
        # than the previous extreme dampening).
        # We'll stick to a very light adjustment to keep it grounded.
        return max(_PROB_MIN, min(0.99999, combined * (1.0 / dampener)))

    return combined

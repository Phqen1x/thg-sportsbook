from __future__ import annotations


def american_to_decimal(odds: int) -> float:
    if odds > 0:
        return odds / 100.0 + 1.0
    return 100.0 / abs(odds) + 1.0


def decimal_to_american(dec: float) -> int:
    if dec >= 2.0:
        return round((dec - 1.0) * 100)
    return round(-100.0 / (dec - 1.0))


def prob_to_american(prob: float) -> int:
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        raw = -(prob / (1.0 - prob)) * 100.0
    else:
        raw = ((1.0 - prob) / prob) * 100.0
    rounded = round(raw / 5.0) * 5
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

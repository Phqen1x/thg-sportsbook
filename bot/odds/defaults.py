from __future__ import annotations
from typing import TYPE_CHECKING

from bot.odds.calculator import prob_to_american, american_to_decimal

if TYPE_CHECKING:
    from bot.database.models import Tribute

PLACEMENT_MULTIPLIER = {
    2: 1.6,
    3: 2.2,
    4: 2.8,
    5: 3.5,
    6: 4.0,
    7: 4.5,
    8: 5.0,
}

DEFAULT_FALLBACK_ODDS = 200

# Weights for group influence on odds
_DISTRICT_ALPHA = 0.10
_ALLIANCE_ALPHA = 0.20

# Market types where group influence applies (single-tribute performance markets)
_GROUP_INFLUENCED_TYPES = {
    "TRIBUTE_WINS", "TRIBUTE_PLACEMENT", "TRIBUTE_TOP_N",
    "TRIBUTE_KILLS", "FIRST_BLOOD", "BLOODBATH_SURVIVOR",
    "KILLS_OU", "PLACEMENT_OU",
}


def _alive_scores(tributes: list["Tribute"]) -> list[int]:
    return [t.training_score for t in tributes if t.status == "ALIVE"]


def group_influence_factor(
    tribute: "Tribute",
    district_mates: list["Tribute"],
    alliance_mates: list["Tribute"],
    all_tributes: list["Tribute"],
) -> float:
    """
    Returns a multiplicative factor for the tribute's win probability based on
    their district and alliance peers. Uses raw training-score probabilities
    (not group-adjusted) to avoid circular influence.
    """
    total = sum(t.training_score for t in all_tributes if t.status == "ALIVE") or 1
    base_prob = tribute.training_score / total

    adj = 0.0

    alive_dm = [t for t in district_mates if t.id != tribute.id and t.status == "ALIVE"]
    if alive_dm:
        dm_avg_prob = sum(t.training_score for t in alive_dm) / (len(alive_dm) * total)
        adj += _DISTRICT_ALPHA * (dm_avg_prob - base_prob)

    alive_am = [t for t in alliance_mates if t.id != tribute.id and t.status == "ALIVE"]
    if alive_am:
        am_avg_prob = sum(t.training_score for t in alive_am) / (len(alive_am) * total)
        adj += _ALLIANCE_ALPHA * (am_avg_prob - base_prob)

    adjusted_prob = max(0.01, min(0.99, base_prob + adj))
    return adjusted_prob / max(base_prob, 0.01)


def apply_group_influence(
    base_odds: int,
    market_type: str,
    tribute: "Tribute",
    district_mates: list["Tribute"],
    alliance_mates: list["Tribute"],
    all_tributes: list["Tribute"],
) -> int:
    """Adjust American odds by group (district + alliance) influence."""
    if market_type not in _GROUP_INFLUENCED_TYPES:
        return base_odds
    if not district_mates and not alliance_mates:
        return base_odds

    factor = group_influence_factor(tribute, district_mates, alliance_mates, all_tributes)
    dec = american_to_decimal(base_odds)
    base_prob = 1.0 / dec
    adj_prob = max(0.01, min(0.99, base_prob * factor))
    return prob_to_american(adj_prob)


def default_odds(
    market_type: str,
    tribute_a: "Tribute",
    all_tributes: list["Tribute"],
    tribute_b: "Tribute | None" = None,
    placement_num: int | None = None,
    top_n: int | None = None,
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> int:
    alive_scores = _alive_scores(all_tributes)
    total = sum(alive_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    score_a = tribute_a.training_score if tribute_a.status == "ALIVE" else 1

    if market_type == "TRIBUTE_WINS":
        prob = score_a / total
        return prob_to_american(prob)

    if market_type == "TRIBUTE_PLACEMENT":
        place = placement_num or 2
        multiplier = PLACEMENT_MULTIPLIER.get(place, place * 0.8)
        prob = min(0.95, (score_a / total) * multiplier)
        return prob_to_american(prob)

    if market_type == "TRIBUTE_TOP_N":
        k = top_n or 3
        base_prob = score_a / total
        prob = min(0.95, base_prob * k * 0.7)
        return prob_to_american(prob)

    if market_type == "TRIBUTE_KILLS":
        sum_sq = sum(s * s for s in alive_scores) or 1
        prob = (score_a * score_a) / sum_sq
        return prob_to_american(prob)

    if market_type == "KILL_EVENT" and tribute_b is not None:
        score_b = tribute_b.training_score if tribute_b.status == "ALIVE" else 1
        prob = (score_a / total) * (score_b / total) * n * 0.4
        prob = min(0.90, max(0.01, prob))
        return prob_to_american(prob)

    if market_type == "BLOODBATH_SURVIVOR":
        prob = 1.0 - (score_a / total) * 0.5
        prob = min(0.90, max(0.10, prob))
        return prob_to_american(prob)

    if market_type == "FIRST_BLOOD":
        prob = score_a / total
        return prob_to_american(prob)

    if market_type == "KILLS_OU":
        # Expected kills proportional to training score relative to field
        kill_rate = (score_a / total) * max(1, n - 1) * 0.5
        line = ou_line if ou_line is not None else 0.5
        threshold = int(line + 0.5)  # 0.5 → 1, 1.5 → 2

        if ou_side == "OVER":
            if threshold <= 1:
                prob = min(0.90, max(0.05, kill_rate / (kill_rate + 0.5)))
            else:
                prob = min(0.85, max(0.05, kill_rate / (threshold + kill_rate * 0.5)))
        else:  # UNDER or None
            if threshold <= 1:
                prob = max(0.10, min(0.90, 0.5 / (kill_rate + 0.5)))
            else:
                prob = max(0.10, min(0.90, threshold / (threshold + kill_rate)))

        return prob_to_american(prob)

    if market_type == "PLACEMENT_OU":
        # UNDER line = finishes better (lower number), OVER = finishes worse (higher number)
        line = ou_line if ou_line is not None else (n / 2.0)

        if ou_side == "UNDER":
            prob = min(0.92, max(0.05, (score_a / total) * line * 0.85))
        else:  # OVER
            prob = min(0.92, max(0.05, 1.0 - (score_a / total) * line * 0.85))

        return prob_to_american(prob)

    return DEFAULT_FALLBACK_ODDS

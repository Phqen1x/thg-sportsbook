from __future__ import annotations
import math
from typing import TYPE_CHECKING

from bot.odds.calculator import prob_to_american, american_to_decimal

if TYPE_CHECKING:
    from bot.database.models import DistrictRecord, Tribute

DEFAULT_FALLBACK_ODDS = 200

# Per-cause probability multipliers by arena type for DEATH_CAUSE markets.
# Artificial arenas (traps, tech hazards) favour Gamemakers deaths;
# natural arenas (wilderness, beasts) favour Natural Causes and Mutt deaths.
ARENA_DEATH_CAUSE_FACTORS: dict[str, dict[str, float]] = {
    "ARTIFICIAL": {
        "Natural Causes":  0.60,
        "Mutt":            0.80,
        "Another Tribute": 1.00,
        "Gamemakers":      1.50,
    },
    "NATURAL": {
        "Natural Causes":  1.50,
        "Mutt":            1.25,
        "Another Tribute": 1.00,
        "Gamemakers":      0.60,
    },
}


def arena_death_factor(cause: str | None, arena_type: str | None) -> float:
    """Probability multiplier for a DEATH_CAUSE market given the current arena type."""
    if not cause or not arena_type:
        return 1.0
    return ARENA_DEATH_CAUSE_FACTORS.get(arena_type, {}).get(cause, 1.0)


# How much a district's historical combat/arena profile nudges DEATH_CAUSE probabilities.
# Max swing ±10% per cause at the extreme (e.g. a district with all-natural wins or 2x average kills).
HIST_DEATH_CAUSE_ALPHA = 0.20


def district_death_cause_factor(
    record: "DistrictRecord | None",
    all_records: "list[DistrictRecord]",
    num_games: int,
    cause: str | None,
) -> float:
    """Multiplicative probability factor for a DEATH_CAUSE market based on district history.

    * "Another Tribute": combat-heavy districts (high kills + bloodbath engagement) are
      more likely to die in tribute-on-tribute fights.
    * "Gamemakers": districts that historically win in man-made arenas handle Gamemaker
      devices better → less likely to die to them.
    * "Mutt" / "Natural Causes": districts that historically win in natural arenas are
      more comfortable in the wild → less likely to die to those hazards.
    """
    if record is None or num_games <= 0 or not cause:
        return 1.0

    if cause == "Another Tribute":
        signals: list[float] = []

        kills_set = [r for r in all_records if r.total_kills is not None]
        if kills_set and record.total_kills is not None:
            field_avg = sum(r.total_kills for r in kills_set) / len(kills_set) / num_games
            if field_avg > 0:
                signals.append(record.total_kills / num_games / field_avg - 1.0)

        bb_death_set = [r for r in all_records if r.bloodbath_deaths is not None]
        if bb_death_set and record.bloodbath_deaths is not None:
            bb_avg = sum(r.bloodbath_deaths for r in bb_death_set) / len(bb_death_set) / num_games
            if bb_avg > 0:
                signals.append(record.bloodbath_deaths / num_games / bb_avg - 1.0)

        bb_kill_set = [r for r in all_records if r.bloodbath_kills is not None]
        if bb_kill_set and record.bloodbath_kills is not None:
            bb_k_avg = sum(r.bloodbath_kills for r in bb_kill_set) / len(bb_kill_set) / num_games
            if bb_k_avg > 0:
                signals.append(record.bloodbath_kills / num_games / bb_k_avg - 1.0)

        if not signals:
            return 1.0
        deviation = sum(signals) / len(signals)
        return max(0.5, min(2.0, 1.0 + deviation * HIST_DEATH_CAUSE_ALPHA))

    if cause in ("Mutt", "Natural Causes"):
        if record.wins is None or record.wins <= 0 or record.manmade_arena_wins is None:
            return 1.0
        manmade_ratio = record.manmade_arena_wins / record.wins
        # More natural wins → smaller factor (district handles the wild better)
        natural_deviation = 0.5 - manmade_ratio
        return max(0.5, min(2.0, 1.0 - natural_deviation * HIST_DEATH_CAUSE_ALPHA))

    if cause == "Gamemakers":
        if record.wins is None or record.wins <= 0 or record.manmade_arena_wins is None:
            return 1.0
        manmade_ratio = record.manmade_arena_wins / record.wins
        # More manmade wins → smaller factor (district handles Gamemaker devices better)
        manmade_deviation = manmade_ratio - 0.5
        return max(0.5, min(2.0, 1.0 - manmade_deviation * HIST_DEATH_CAUSE_ALPHA))

    return 1.0


# The four standard death causes. "Another Tribute" covers tribute-on-tribute
# kills (the majority of deaths); the rest are environmental.
DEATH_CAUSES = ("Another Tribute", "Gamemakers", "Mutt", "Natural Causes")


def _death_cause_base_weight(cause: str) -> float:
    """Baseline share of a tribute's deaths attributable to `cause`, before any
    arena/district adjustment. Tribute kills account for TRIBUTE_KILL_FRACTION
    of deaths; the remainder splits evenly across the environmental causes."""
    if cause == "Another Tribute":
        return TRIBUTE_KILL_FRACTION
    return (1.0 - TRIBUTE_KILL_FRACTION) / 3.0


def death_cause_share(
    cause: str | None,
    p_dies: float,
    arena_type: str | None,
    record: "DistrictRecord | None",
    all_records: "list[DistrictRecord] | None",
    num_games: int,
) -> float:
    """Probability that a tribute dies of `cause` specifically.

    A tribute's total death probability is ``p_dies`` (= 1 - P(win)). The four
    causes *partition* that probability, so they sum back to ``p_dies`` — a
    tribute who is 60% to win has a 40% death budget split across the causes,
    not four independent longshots. Arena type and district history only shift
    the *relative* split between causes (via the existing factor functions),
    never the total.
    """
    if not cause or p_dies <= 0.0:
        return 0.0
    records = all_records or []
    causes = set(DEATH_CAUSES) | {cause}
    weights: dict[str, float] = {}
    for c in causes:
        weights[c] = (
            _death_cause_base_weight(c)
            * arena_death_factor(c, arena_type)
            * district_death_cause_factor(record, records, num_games, c)
        )
    total = sum(weights.values()) or 1.0
    return p_dies * weights.get(cause, 0.0) / total


# Weights for group influence on odds.
# _DISTRICT_ALPHA scales a convergence toward the district partner's strength.
# _ALLIANCE_ALPHA scales the alliance term, a signed sum of each ally's strength
# relative to the field average. Summing (not averaging) means the effect grows
# with alliance size, and the sign means strong allies boost while objectively
# weak allies penalise — in both cases scaled by how far from average they are.
_DISTRICT_ALPHA = 0.10
_ALLIANCE_ALPHA = 0.20

# How strongly an ally's *circumstantial* strength (district history, bloodbath
# record, reputation, sponsor modifiers, kill-boost) bleeds into a tribute's own
# odds. Like _ALLIANCE_ALPHA this is applied to a signed, size-scaled sum of each
# ally's surplus/deficit vs. the field average, so strong allies help and
# objectively weak allies (poor placement history, bloodbath-prone, cursed) hurt.
ALLIANCE_FACTOR_ALPHA = 0.20

# How much district historical performance influences current tribute odds.
# Win/survival markets use HIST_ALPHA; kill markets use HIST_KILL_ALPHA.
HIST_ALPHA = 0.30
HIST_KILL_ALPHA = 0.25

# District reputation (1 = highest/best, 5 = lowest, 3 = neutral). Each step away
# from neutral nudges a district's probability by REPUTATION_STEP, so the swing is
# deliberately small (rep 1 → +10%, rep 5 → −10%) and never a major game-changer.
REPUTATION_STEP = 0.05


def reputation_factor(reputation: int | None) -> float:
    """Multiplicative probability factor for a district's manually-set reputation.

    A higher reputation (lower number) boosts win/kill probability; a lower
    reputation (higher number) suppresses it. Reputation 3 is neutral (1.0).
    """
    if reputation is None:
        return 1.0
    rep = max(1, min(5, reputation))
    return 1.0 + (3 - rep) * REPUTATION_STEP

# Market types where group influence applies (single-tribute performance markets)
_GROUP_INFLUENCED_TYPES = {
    "TRIBUTE_WINS", "TRIBUTE_PLACEMENT", "TRIBUTE_TOP_N",
    "TRIBUTE_KILLS", "FIRST_BLOOD", "BLOODBATH_SURVIVOR",
    "KILLS_OU", "PLACEMENT_OU",
    "MAKES_FINAL_8", "MISSES_FINAL_8",
    "MAKES_FINAL_5", "MISSES_FINAL_5",
    "MAKES_FINALE", "MISSES_FINALE",
}


def _alive_scores(tributes: list["Tribute"]) -> list[int]:
    return [t.training_score or 6 for t in tributes if t.status == "ALIVE"]


def _gauss_pmf(k: int, mean: float, sigma: float = 2.5) -> float:
    """Discrete Gaussian probability P(score = k) via erf approximation."""
    lo = (k - 0.5 - mean) / (sigma * math.sqrt(2))
    hi = (k + 0.5 - mean) / (sigma * math.sqrt(2))
    return 0.5 * (math.erf(hi) - math.erf(lo))


# Fraction of the field eliminated in the opening bloodbath.
BLOODBATH_DEATH_FRACTION = 0.40
# Fraction of all game deaths attributable to tribute-on-tribute kills
# (the remainder are natural causes / Gamemakers / mutts).
TRIBUTE_KILL_FRACTION = 0.70

# ── Kill quality ────────────────────────────────────────────────────────────
# How much a kill boosts the killer's odds depends on how strong the victim was.
# Killing the field's *average* tribute is the 1.0x baseline; killing a heavy
# favourite is worth more (>1x) and killing a hopeless long-shot is worth less
# (<1x). The strength ratio is passed through a power curve so extreme
# favourites/long-shots don't produce runaway multipliers, then clamped.
KILL_QUALITY_SENSITIVITY = 0.5   # exponent on the strength ratio (0 = flat, 1 = linear)
KILL_QUALITY_MIN = 0.50          # floor: killing the weakest possible long-shot
KILL_QUALITY_MAX = 3.00          # cap: killing the overwhelming favourite


def kill_quality_multiplier(victim_win_prob: float, field_avg_prob: float) -> float:
    """Multiplier for how much a kill boosts the killer's odds, scaled by how
    strong the victim was.

    The victim's win probability is compared to the field-average win
    probability (``1 / number_of_tributes``):

    * average victim (ratio 1.0)  → 1.0x  (a normal, expected kill)
    * stronger-than-average victim → >1.0x (killing a contender matters more)
    * long-shot victim (ratio < 1) → <1.0x (killing a no-hoper matters less)

    So a tribute on +100 to win (a real threat) yields a sizeable boost, while
    finishing off someone at +1500 barely moves the killer's odds. The ratio is
    raised to ``KILL_QUALITY_SENSITIVITY`` to damp extremes and clamped to
    ``[KILL_QUALITY_MIN, KILL_QUALITY_MAX]``.
    """
    if victim_win_prob <= 0 or field_avg_prob <= 0:
        return 1.0
    ratio = victim_win_prob / field_avg_prob
    mult = ratio ** KILL_QUALITY_SENSITIVITY
    return max(KILL_QUALITY_MIN, min(KILL_QUALITY_MAX, mult))


# ── District-pair kill suppression ───────────────────────────────────────────
# Tributes from the same district are paired together and rarely fight each
# other. DISTRICT_PAIR_KILL_SUPPRESSION is the base multiplier on the raw
# KILL_EVENT probability when both tributes share a district.
# If they also belong to *opposing* alliances (each has an alliance_id and
# those ids differ), competing loyalties make conflict more plausible, so a
# less severe multiplier is used instead.
DISTRICT_PAIR_KILL_SUPPRESSION = 0.10
OPPOSING_ALLIANCE_KILL_SUPPRESSION = 0.35


def district_pair_kill_factor(tribute_a: "Tribute", tribute_b: "Tribute") -> float:
    """Probability suppression for a KILL_EVENT between same-district tributes.

    Returns 1.0 (no change) when the two tributes are from different districts.
    Returns DISTRICT_PAIR_KILL_SUPPRESSION for district partners with aligned
    or no alliance affiliation, and OPPOSING_ALLIANCE_KILL_SUPPRESSION when
    both belong to different alliances — less suppression because their
    competing alliance loyalties override the usual district bond.
    """
    if tribute_a.district != tribute_b.district:
        return 1.0
    if (
        tribute_a.alliance_id is not None
        and tribute_b.alliance_id is not None
        and tribute_a.alliance_id != tribute_b.alliance_id
    ):
        return OPPOSING_ALLIANCE_KILL_SUPPRESSION
    return DISTRICT_PAIR_KILL_SUPPRESSION


def _p_top_k(strength: float, k: int) -> float:
    """
    Field-aware probability a tribute finishes in the top ``k``, given their
    normalised strength (score / field-total, so the field sums to 1.0).

    Monotone in both strength and k and bounded to (0, 1). With k=1 it reduces
    exactly to the raw win probability, so Win ⊂ Top-N ⊂ Final-8/5/Finale are
    mutually consistent by construction (a tribute can never be more likely to
    win than to make the finale).
    """
    strength = max(0.0, min(1.0, strength))
    return 1.0 - (1.0 - strength) ** max(1, k)


def _poisson_at_least(m: int, lam: float) -> float:
    """P(X >= m) for X ~ Poisson(lam)."""
    if m <= 0:
        return 1.0
    lam = max(0.0, lam)
    cdf = 0.0          # accumulates P(X < m) = sum_{j=0}^{m-1} e^-lam lam^j / j!
    term = math.exp(-lam)
    for j in range(m):
        if j > 0:
            term *= lam / j
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def strength_hurts(market_type: str, ou_side: str | None) -> bool:
    """
    True when a *stronger* tribute should have a LOWER probability for this
    market — i.e. the "yes" side is a bet against the tribute. Used so group /
    modifier / historical adjustments push such markets in the correct
    (inverted) direction and keep OVER/UNDER and MAKES/MISSES complementary.
    """
    if market_type in ("MISSES_FINAL_8", "MISSES_FINAL_5", "MISSES_FINALE"):
        return True
    if market_type == "PLACEMENT_OU" and ou_side == "OVER":
        return True   # OVER the placement line = finishes worse
    if market_type == "KILLS_OU" and ou_side == "UNDER":
        return True   # UNDER the kill line = fewer kills
    if market_type == "TRAINING_SCORE_OU" and ou_side == "UNDER":
        return True
    return False


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

    District influence is a gentle convergence toward the district partner's
    strength. Alliance influence is a *signed*, size-scaled term: every living
    ally is measured against the field-average tribute, and their surpluses
    (strong allies) or deficits (objectively weak allies — a below-average
    training score) are summed. So a larger alliance amplifies the effect in
    whichever direction its members point: allying with strong tributes lifts a
    tribute's odds, while allying with weak tributes drags them down even when
    the tribute themselves is a favourite.
    """
    total = sum((t.training_score or 6) for t in all_tributes if t.status == "ALIVE") or 1
    n_alive = sum(1 for t in all_tributes if t.status == "ALIVE") or 1
    # The neutral benchmark an ally is judged against: an exactly average tribute
    # holds 1/n of the field's strength and contributes nothing in either direction.
    field_avg_prob = 1.0 / n_alive
    base_prob = (tribute.training_score or 6) / total

    adj = 0.0

    alive_dm = [t for t in district_mates if t.id != tribute.id and t.status == "ALIVE"]
    if alive_dm:
        dm_avg_prob = sum((t.training_score or 6) for t in alive_dm) / (len(alive_dm) * total)
        adj += _DISTRICT_ALPHA * (dm_avg_prob - base_prob)

    # Sum of each ally's strength surplus/deficit vs. the field average. Strong
    # allies (above average) push this positive → boost; weak allies (below
    # average) push it negative → penalty. More allies amplify the swing, so a
    # big alliance of contenders boosts hard while a big alliance of long-shots
    # is a real drag.
    alive_am = [t for t in alliance_mates if t.id != tribute.id and t.status == "ALIVE"]
    if alive_am:
        adj += _ALLIANCE_ALPHA * sum(
            (t.training_score or 6) / total - field_avg_prob for t in alive_am
        )

    adjusted_prob = max(0.01, min(0.99, base_prob + adj))
    return adjusted_prob / max(base_prob, 0.01)


def apply_group_influence(
    base_odds: int,
    market_type: str,
    tribute: "Tribute",
    district_mates: list["Tribute"],
    alliance_mates: list["Tribute"],
    all_tributes: list["Tribute"],
    ou_side: str | None = None,
) -> int:
    """Adjust American odds by group (district + alliance) influence."""
    if market_type not in _GROUP_INFLUENCED_TYPES and not market_type.startswith("CUSTOM_"):
        return base_odds
    if not district_mates and not alliance_mates:
        return base_odds

    factor = group_influence_factor(tribute, district_mates, alliance_mates, all_tributes)
    dec = american_to_decimal(base_odds)
    base_prob = 1.0 / dec
    if strength_hurts(market_type, ou_side):
        # The "yes" side is a bet against the tribute, so a strength-boosting
        # factor must push it DOWN: apply the factor to the complementary
        # probability and invert.
        comp_prob = 1.0 - base_prob
        adj_comp = max(0.01, min(0.99, comp_prob * factor))
        adj_prob = max(0.01, min(0.99, 1.0 - adj_comp))
    else:
        adj_prob = max(0.01, min(0.99, base_prob * factor))
    return prob_to_american(adj_prob)


def default_odds(
    market_type: str,
    tribute_a: "Tribute | None",
    all_tributes: list["Tribute"],
    tribute_b: "Tribute | None" = None,
    placement_num: int | None = None,
    top_n: int | None = None,
    ou_line: float | None = None,
    ou_side: str | None = None,
    hist_avg_score: float | None = None,
) -> int:
    alive_scores = _alive_scores(all_tributes)
    total = sum(alive_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    score_a = (
        (tribute_a.training_score or 6)
        if tribute_a is not None and tribute_a.status == "ALIVE"
        else 1
    )

    # Normalised strength: the tribute's share of total field strength, so the
    # whole field sums to 1.0. Average tribute sits at 1/n. This is the single
    # quantity every performance market is derived from.
    strength = score_a / total

    if market_type == "TRIBUTE_WINS":
        return prob_to_american(strength)  # = top-1 finish

    if market_type == "TRIBUTE_PLACEMENT":
        # Probability of finishing in EXACTLY this position:
        #   P(top k) - P(top k-1) = (1-s)^(k-1) * s
        place = placement_num or 2
        prob = _p_top_k(strength, place) - _p_top_k(strength, place - 1)
        return prob_to_american(prob)

    if market_type == "TRIBUTE_TOP_N":
        k = top_n or 3
        return prob_to_american(_p_top_k(strength, k))

    if market_type == "TRIBUTE_KILLS":
        # "Most kills" — a 1-of-field title, weighted sharply toward strength.
        sum_sq = sum(s * s for s in alive_scores) or 1
        prob = (score_a * score_a) / sum_sq
        return prob_to_american(prob)

    if market_type == "KILL_EVENT" and tribute_b is not None:
        # P(A specifically kills B) = P(B is killed by a tribute)
        #                             * A's share of that kill among the rest.
        # B's death probability scales with weakness (inverse strength), so a
        # weak victim is far likelier to be killed than a strong one — the same
        # weakness-weighting the bloodbath model uses. (Dividing B's score by
        # the whole-field total instead made every victim ~equally killable,
        # flattening the victim's strength out of the price entirely.)
        score_b = (tribute_b.training_score or 6) if tribute_b.status == "ALIVE" else 1
        inv_b = 1.0 / max(score_b, 1)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        p_b_killed = min(0.95, TRIBUTE_KILL_FRACTION * n * (inv_b / sum_inv))
        others_total = max(1, total - score_b)
        prob = p_b_killed * (score_a / others_total)
        return prob_to_american(prob)

    if market_type == "BLOODBATH_SURVIVOR":
        # The bloodbath kills ~BLOODBATH_DEATH_FRACTION of the field, spread by
        # weakness (inverse strength), so death prob averages that fraction and
        # strong tributes survive more often.
        inv = 1.0 / max(score_a, 1)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
        p_death = max(0.03, min(0.92, p_death))
        return prob_to_american(1.0 - p_death)

    if market_type == "FIRST_BLOOD":
        # A single 1-of-field event, weighted toward aggression (strength).
        sum_sq = sum(s * s for s in alive_scores) or 1
        prob = (score_a * score_a) / sum_sq
        return prob_to_american(prob)

    if market_type == "KILLS_OU":
        # Expected kills via Poisson. Total tribute kills in a game ≈
        # TRIBUTE_KILL_FRACTION * (n-1); each tribute's share scales with
        # strength² (stronger tributes win more fights).
        sum_sq = sum(s * s for s in alive_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = total_kills * (score_a * score_a) / sum_sq
        line = ou_line if ou_line is not None else 0.5
        m = int(math.floor(line)) + 1   # over 0.5 → ≥1, over 1.5 → ≥2
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(prob)

    if market_type == "PLACEMENT_OU":
        # UNDER the line = finishes that position or better (top k).
        line = ou_line if ou_line is not None else (n / 2.0)
        k = max(1, int(math.floor(line)))
        p_under = _p_top_k(strength, k)
        prob = p_under if ou_side == "UNDER" else 1.0 - p_under
        return prob_to_american(prob)

    # ── Milestone markets (all derived from the same top-k model) ───────────────

    _FINAL_K = {
        "MAKES_FINAL_8": 8, "MAKES_FINAL_5": 5, "MAKES_FINALE": 3,
        "MISSES_FINAL_8": 8, "MISSES_FINAL_5": 5, "MISSES_FINALE": 3,
    }
    if market_type in _FINAL_K:
        makes_prob = _p_top_k(strength, _FINAL_K[market_type])
        prob = makes_prob if market_type.startswith("MAKES") else 1.0 - makes_prob
        return prob_to_american(prob)

    # ── Pre-Games prop markets ─────────────────────────────────────────────────

    if market_type == "ARENA_TYPE":
        return 100  # even money (no vig); caller adjusts from historical arena counts

    if market_type == "EXACT_TRAINING_SCORE":
        guessed = placement_num or 7
        mean = hist_avg_score if hist_avg_score is not None else 6.5
        raw = _gauss_pmf(guessed, mean)
        total_prob = sum(_gauss_pmf(k, mean) for k in range(1, 13))
        prob = raw / max(total_prob, 0.001)
        return prob_to_american(max(0.005, min(0.99, prob)))

    if market_type == "COMBINED_DISTRICT_SCORE":
        guessed = placement_num or 13
        g = max(2, min(24, guessed))
        # Triangular PMF for the sum of two independent U[1,12] integers
        prob = (12 - abs(g - 13)) / 144.0
        return prob_to_american(max(0.005, prob))

    if market_type == "TRAINING_SCORE_OU":
        line = ou_line if ou_line is not None else 6.5
        mean = hist_avg_score if hist_avg_score is not None else 6.5
        x = (line - mean) / (2.5 * math.sqrt(2))
        p_over = 0.5 * (1.0 - math.erf(x))
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(prob)

    return DEFAULT_FALLBACK_ODDS


# ── District-level market odds ─────────────────────────────────────────────────

_DISTRICT_K_BOTH = {
    "DISTRICT_BOTH_FINAL_8": 8, "DISTRICT_BOTH_FINAL_5": 5, "DISTRICT_BOTH_FINALE": 3,
}
_DISTRICT_K_ONE = {
    "DISTRICT_ONE_FINAL_8": 8, "DISTRICT_ONE_FINAL_5": 5, "DISTRICT_ONE_FINALE": 3,
}

_ALLIANCE_K_ALL = {
    "ALLIANCE_ALL_FINAL_8": 8, "ALLIANCE_ALL_FINAL_5": 5, "ALLIANCE_ALL_FINALE": 3,
}
_ALLIANCE_K_ONE = {
    "ALLIANCE_ONE_FINAL_8": 8, "ALLIANCE_ONE_FINAL_5": 5, "ALLIANCE_ONE_FINALE": 3,
}


def district_default_odds(
    market_type: str,
    district_tributes: list["Tribute"],
    all_tributes: list["Tribute"],
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> int:
    """Compute default odds for district-level markets (no single tribute_a)."""
    alive = [t for t in district_tributes if t.status == "ALIVE"]
    if not alive:
        return DEFAULT_FALLBACK_ODDS

    alive_all_scores = _alive_scores(all_tributes)
    total = sum(alive_all_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    if market_type == "DISTRICT_VICTOR":
        prob = sum((t.training_score or 6) / total for t in alive)
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "DISTRICT_KILLS_OU":
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = sum(total_kills * ((t.training_score or 6) ** 2) / sum_sq for t in alive)
        line = ou_line if ou_line is not None else 1.5
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(prob)

    if market_type == "DISTRICT_BOTH_BLOODBATH":
        sum_inv = sum(1.0 / max(s, 1) for s in alive_all_scores) or 1.0
        p_both = 1.0
        for t in alive:
            inv = 1.0 / max(t.training_score or 6, 1)
            p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
            p_death = max(0.03, min(0.92, p_death))
            p_both *= (1.0 - p_death)
        return prob_to_american(max(0.01, min(0.99, p_both)))

    if market_type in _DISTRICT_K_BOTH:
        k = _DISTRICT_K_BOTH[market_type]
        p_both = 1.0
        for t in alive:
            p_both *= _p_top_k((t.training_score or 6) / total, k)
        return prob_to_american(max(0.01, min(0.99, p_both)))

    if market_type in _DISTRICT_K_ONE:
        k = _DISTRICT_K_ONE[market_type]
        p_none = 1.0
        for t in alive:
            p_none *= 1.0 - _p_top_k((t.training_score or 6) / total, k)
        return prob_to_american(max(0.01, min(0.99, 1.0 - p_none)))

    return DEFAULT_FALLBACK_ODDS


def alliance_default_odds(
    market_type: str,
    alliance_members: list["Tribute"],
    all_tributes: list["Tribute"],
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> int:
    """Compute default odds for alliance-level markets (no single tribute_a)."""
    alive = [t for t in alliance_members if t.status == "ALIVE"]
    if not alive:
        return DEFAULT_FALLBACK_ODDS

    alive_all_scores = _alive_scores(all_tributes)
    total = sum(alive_all_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    if market_type == "ALLIANCE_VICTOR":
        prob = sum((t.training_score or 6) / total for t in alive)
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "ALLIANCE_KILLS_OU":
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = sum(total_kills * ((t.training_score or 6) ** 2) / sum_sq for t in alive)
        line = ou_line if ou_line is not None else 1.5
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(prob)

    if market_type == "ALLIANCE_ALL_BLOODBATH":
        sum_inv = sum(1.0 / max(s, 1) for s in alive_all_scores) or 1.0
        p_all = 1.0
        for t in alive:
            inv = 1.0 / max(t.training_score or 6, 1)
            p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
            p_death = max(0.03, min(0.92, p_death))
            p_all *= (1.0 - p_death)
        return prob_to_american(max(0.01, min(0.99, p_all)))

    if market_type in _ALLIANCE_K_ALL:
        k = _ALLIANCE_K_ALL[market_type]
        p_all = 1.0
        for t in alive:
            p_all *= _p_top_k((t.training_score or 6) / total, k)
        return prob_to_american(max(0.01, min(0.99, p_all)))

    if market_type in _ALLIANCE_K_ONE:
        k = _ALLIANCE_K_ONE[market_type]
        p_none = 1.0
        for t in alive:
            p_none *= 1.0 - _p_top_k((t.training_score or 6) / total, k)
        return prob_to_american(max(0.01, min(0.99, 1.0 - p_none)))

    return DEFAULT_FALLBACK_ODDS

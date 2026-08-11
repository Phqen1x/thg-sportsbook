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

# Exponent applied to each market's field-relative modifier (the tribute's own
# boost set divided by the field average). Values < 1 temper the modifier so a
# single large assignment — a 2× career boost shared by most of the roster, or a
# 0.2× curse — pulls a tribute toward the rail more gently. 1.0 = full strength.
MODIFIER_TEMPER = 0.45

# How much district historical performance influences current tribute odds.
# Win/survival markets use HIST_ALPHA; kill markets use HIST_KILL_ALPHA.
HIST_ALPHA = 0.30
HIST_KILL_ALPHA = 0.25

# District reputation (1 = highest/best, 5 = lowest, 3 = neutral). Each step away
# from neutral nudges a district's probability by REPUTATION_STEP, so the swing is
# deliberately small (rep 1 → +10%, rep 5 → −10%) and never a major game-changer.
REPUTATION_STEP = 0.05


_FUNDING_MULTIPLIERS: dict[str, float] = {
    "rich":         1.5,
    "well_funded":  1.25,
    "under_funded": 0.75,
    "poor":         0.5,
}

# While the sponsorship window is OPEN (the "Sponsors Open" phase) the funding
# edge swells — rich and well-funded districts pull even further ahead as the
# Capitol's money floods in.
_FUNDING_MULTIPLIERS_SPONSORS_OPEN: dict[str, float] = {
    "rich":         3.5,
    "well_funded":  3.0,
    "under_funded": 0.75,
    "poor":         0.5,
}

# After the window CLOSES (the "Sponsors Closing" phase) the funding edge takes a
# significant hit but never disappears: rich/well-funded districts keep a smaller
# residual boost, and the penalty on under-funded/poor districts softens too.
_FUNDING_MULTIPLIERS_SPONSORS_CLOSED: dict[str, float] = {
    "rich":         1.5,
    "well_funded":  1.4,
    "under_funded": 0.85,
    "poor":         0.7,
}

_FUNDING_LABELS: dict[str, str] = {
    "rich":         "Rich",
    "well_funded":  "Well-Funded",
    "under_funded": "Under-Funded",
    "poor":         "Poor",
}


_DEBILITATION_WEIGHTS: dict[str, float] = {
    "DEBILITATED": 0.75,
    "MODERATELY_DEBILITATED": 0.55,
    "SEVERELY_DEBILITATED": 0.35,
}

_DEBILITATION_LABELS: dict[str, str] = {
    "DEBILITATED": "Debilitated",
    "MODERATELY_DEBILITATED": "Moderately Debilitated",
    "SEVERELY_DEBILITATED": "Severely Debilitated",
}


def debilitation_factor(level: str | None) -> float:
    """Multiplicative probability debuff for a tribute's debilitation level.

    Each tier halves the step above it — Debilitated is mild, Severely
    Debilitated is a steep penalty that sharply lengthens win/kill odds.
    """
    if level is None:
        return 1.0
    return _DEBILITATION_WEIGHTS.get(level, 1.0)


def debilitation_label(level: str | None) -> str:
    """Human-readable label for a debilitation level key."""
    if level is None:
        return "—"
    return _DEBILITATION_LABELS.get(level, level)


def funding_factor(funding_level: str | None, sponsor_state: str | None = None) -> float:
    """Multiplicative factor for a district's funding level.

    Rich/Well-Funded districts boost win/kill probability; Under-Funded/Poor
    suppress it. ``sponsor_state`` tracks the sponsorship window: "open" amplifies
    the funding edge (Sponsors Open phase), "closed" cuts it down to a residual
    (Sponsors Closing phase), and None (default, before sponsors open) uses the
    baseline multipliers.
    """
    if funding_level is None:
        return 1.0
    if sponsor_state == "open":
        table = _FUNDING_MULTIPLIERS_SPONSORS_OPEN
    elif sponsor_state == "closed":
        table = _FUNDING_MULTIPLIERS_SPONSORS_CLOSED
    else:
        table = _FUNDING_MULTIPLIERS
    return table.get(funding_level, 1.0)


def funding_label(funding_level: str | None) -> str:
    """Human-readable label for a funding level key."""
    if funding_level is None:
        return "—"
    return _FUNDING_LABELS.get(funding_level, funding_level)


# Tribute age. Valid range is 12–18, with 15 as the neutral midpoint. Each year
# of age lengthens a tribute's price off the bat: older tributes get a smaller
# win/kill probability factor (higher odds), younger tributes a larger one
# (lower odds). AGE_STEP keeps the swing modest — age 12 → +15%, age 18 → −15%.
AGE_MIN = 12
AGE_MAX = 18
AGE_NEUTRAL = 15
AGE_STEP = 0.05


# Prior-game experience. Each prior game adds a small flat bonus; the best
# placement ever adds a bonus scaled by how high it was (2 = runner-up = max,
# 24 = near-last = no placement bonus). Both components are intentionally small
# so experience is meaningful but never a deciding factor.
PRIOR_EXP_TIMES_STEP = 0.025   # probability multiplier bonus per prior game
PRIOR_EXP_TIMES_CAP = 4        # bonus stops growing after this many games
PRIOR_EXP_PLACEMENT_MAX = 0.10 # max bonus from best placement (achieved at placement 2)

# A reigning SADE Champion carries measurable competitive credibility — small but
# real boost applied to both win and kill probability.
SADE_CHAMPION_FACTOR = 1.08


def prior_experience_factor(times_played: int, highest_placement: int | None) -> float:
    """Multiplicative probability bonus for a tribute with prior Games experience.

    ``times_played`` is the number of previous Games entered (0 = rookie, neutral).
    ``highest_placement`` is their best finish (2–24; lower = better). A tribute who
    has never played returns 1.0. Maximum factor is ~1.20 for a four-time player who
    reached 2nd place.
    """
    if times_played <= 0:
        return 1.0
    play_bonus = min(times_played, PRIOR_EXP_TIMES_CAP) * PRIOR_EXP_TIMES_STEP
    if highest_placement is not None:
        placement = max(2, min(24, highest_placement))
        placement_bonus = ((24 - placement) / 22) * PRIOR_EXP_PLACEMENT_MAX
    else:
        placement_bonus = 0.0
    return 1.0 + play_bonus + placement_bonus


def age_factor(age: int | None, neutral: float = AGE_NEUTRAL) -> float:
    """Multiplicative probability factor for a tribute's age.

    Higher age → higher odds, i.e. a *lower* win/kill probability. ``neutral``
    is the breakeven age (factor = 1.0); each year above it suppresses and each
    year below boosts. Defaults to AGE_NEUTRAL (15); pass the global
    ``victor_avg_age`` GameSetting value to anchor to historical victor ages.
    """
    if age is None:
        return 1.0
    a = max(AGE_MIN, min(AGE_MAX, age))
    return 1.0 + (neutral - a) * AGE_STEP


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
    "TRIBUTE_KILLS", "FIRST_BLOOD", "BLOODBATH_SURVIVOR", "TRIBUTE_KILLED_BLOODBATH",
    "KILLS_OU", "PLACEMENT_OU",
    "MAKES_FINAL_8", "MISSES_FINAL_8",
    "MAKES_FINAL_5", "MISSES_FINAL_5",
    "TRIBUTE_RUNNER_UP",
    "FIRST_TRIBUTE_TO_DIE",
}


def _alive_scores(tributes: list["Tribute"]) -> list[int]:
    return [t.training_score or 6 for t in tributes if t.status == "ALIVE"]


# How tightly raw training scores are compressed toward the field mean before
# pricing. Raw scores in a full field of 24 give an average win probability of
# only ~1/24 (~4%), so even a modest strength edge pushes favourites toward the
# 99% rail and longshots below the 1% rail — leaving many markets pinned at the
# ±9900 cap. Pulling each score partway to the mean keeps the whole field inside
# a believable band (no tribute is ever a near-certainty or a no-hoper) while
# preserving their order and relative gaps. 1.0 = raw scores, 0.0 = everyone
# identical.
SCORE_COMPRESSION = 0.5


def _compressed_scores(tributes: list["Tribute"]) -> list[float]:
    """Alive training scores pulled toward the field mean by SCORE_COMPRESSION."""
    raw = _alive_scores(tributes)
    if not raw:
        return []
    mean = sum(raw) / len(raw)
    return [max(1.0, mean + SCORE_COMPRESSION * (s - mean)) for s in raw]


def _compress(score: float, mean: float) -> float:
    """Compress a single score toward the field mean by SCORE_COMPRESSION."""
    return max(1.0, mean + SCORE_COMPRESSION * (score - mean))


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

# kill_boost stores an additive sum of per-kill quality contributions. This
# constant scales that sum to a final multiplier, keeping even heavy hitters
# from dominating odds: sum=4.0 → ×2.0 rather than exponential compounding.
KILL_BOOST_SCALE = 0.25


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


# ── District-pair / alliance kill suppression ────────────────────────────────
# Tributes from the same district are paired together and rarely fight each
# other. DISTRICT_PAIR_KILL_SUPPRESSION is the base multiplier on the raw
# KILL_EVENT probability when both tributes share a district.
# Allies are voluntarily bonded rather than forced together like district
# partners, so cross-district allies get a lighter (but still significant)
# ALLIANCE_PAIR_KILL_SUPPRESSION instead of the district figure.
# If two same-district tributes belong to *opposing* alliances (each has an
# alliance_id and those ids differ), competing loyalties make conflict more
# plausible, so a less severe multiplier is used instead.
DISTRICT_PAIR_KILL_SUPPRESSION = 0.10
ALLIANCE_PAIR_KILL_SUPPRESSION = 0.20
OPPOSING_ALLIANCE_KILL_SUPPRESSION = 0.35


def kill_boost_dr_factor(kills_before: int, national_kill_record: int | None) -> float:
    """Diminishing-returns factor for each kill's contribution to kill_boost.

    Full value (1.0) for kills while the tribute's total is below half the
    national record; decays as 1/(1+excess) once past that point. Tributes
    who have fought half as many battles as the all-time record face real
    injury accumulation, so marginal kills stop moving odds as meaningfully.
    """
    half = max(1, (national_kill_record or 7) // 2)
    if kills_before < half:
        return 1.0
    excess = kills_before - half + 1
    return 1.0 / (1.0 + excess)


def district_pair_kill_factor(tribute_a: "Tribute", tribute_b: "Tribute") -> float:
    """Probability suppression for a KILL_EVENT between bonded tributes.

    Returns 1.0 (no change) when the two tributes share neither a district nor
    an alliance. Returns DISTRICT_PAIR_KILL_SUPPRESSION for district partners
    with aligned or no alliance affiliation, ALLIANCE_PAIR_KILL_SUPPRESSION for
    allies from different districts, and OPPOSING_ALLIANCE_KILL_SUPPRESSION
    when same-district tributes belong to different alliances — less
    suppression because their competing alliance loyalties override the usual
    district bond.
    """
    same_district = tribute_a.district == tribute_b.district
    same_alliance = (
        tribute_a.alliance_id is not None
        and tribute_a.alliance_id == tribute_b.alliance_id
    )
    opposing_alliance = (
        tribute_a.alliance_id is not None
        and tribute_b.alliance_id is not None
        and tribute_a.alliance_id != tribute_b.alliance_id
    )
    if same_district and opposing_alliance:
        return OPPOSING_ALLIANCE_KILL_SUPPRESSION
    if same_district:
        return DISTRICT_PAIR_KILL_SUPPRESSION
    if same_alliance:
        return ALLIANCE_PAIR_KILL_SUPPRESSION
    return 1.0


def _p_top_k(strength: float, k: int) -> float:
    """
    Field-aware probability a tribute finishes in the top ``k``, given their
    normalised strength (score / field-total, so the field sums to 1.0).

    Monotone in both strength and k and bounded to (0, 1). With k=1 it reduces
    exactly to the raw win probability, so Win ⊂ Top-N ⊂ Final-8/5 are
    mutually consistent by construction (a tribute can never be more likely to
    win than to make the Final 5).
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
    if market_type in ("MISSES_FINAL_8", "MISSES_FINAL_5"):
        return True
    if market_type == "PLACEMENT_OU" and ou_side == "OVER":
        return True   # OVER the placement line = finishes worse
    if market_type == "KILLS_OU" and ou_side == "UNDER":
        return True   # UNDER the kill line = fewer kills
    if market_type == "TRAINING_SCORE_OU" and ou_side == "UNDER":
        return True
    if market_type in ("FIRST_IN_ALLIANCE_DEATH", "FIRST_TRIBUTE_TO_DIE",
                        "LOWEST_TRAINING_SCORE"):
        return True   # stronger tribute is LESS likely for these markets
    if market_type in ("TRIBUTE_KILLED_BLOODBATH", "PARTNER_SCORE_LOWER",
                        "PARTNER_PLACE_LOWER"):
        return True   # complementary to BLOODBATH_SURVIVOR / *_HIGHER — a
                       # strength boost must push these DOWN, not up
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
    raw_scores = _alive_scores(all_tributes)
    n = len(raw_scores) or 1
    field_mean = (sum(raw_scores) / len(raw_scores)) if raw_scores else 6.0
    # Compress scores toward the field mean so no tribute prices as a near-certain
    # winner or a no-hoper (see SCORE_COMPRESSION). Every market below derives from
    # these compressed scores, so the compression is applied consistently.
    alive_scores = _compressed_scores(all_tributes)
    total = sum(alive_scores) or 1

    score_a = (
        _compress(tribute_a.training_score or 6, field_mean)
        if tribute_a is not None and tribute_a.status == "ALIVE"
        else 1.0
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
        score_b = _compress(tribute_b.training_score or 6, field_mean) if tribute_b.status == "ALIVE" else 1.0
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

    if market_type == "TRIBUTE_KILLED_BLOODBATH":
        inv = 1.0 / max(score_a, 1)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
        p_death = max(0.03, min(0.92, p_death))
        return prob_to_american(p_death)

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

    if market_type == "TRIBUTE_RUNNER_UP":
        # P(exactly 2nd place) = P(top 2) - P(top 1)
        prob = _p_top_k(strength, 2) - _p_top_k(strength, 1)
        return prob_to_american(max(0.005, prob))

    if market_type == "FIRST_TRIBUTE_TO_DIE":
        # P(this tribute dies first) ∝ weakness (inverse strength)
        inv = 1.0 / max(score_a, 1)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        return prob_to_american(max(0.01, min(0.99, inv / sum_inv)))

    if market_type == "HIGHEST_TRAINING_SCORE":
        # Before training scores are revealed, use the district's historical average
        # as the effective score so districts with stronger scoring histories price
        # as genuine favourites rather than all collapsing to the same flat odds.
        if tribute_a is not None and tribute_a.training_score is None and hist_avg_score is not None:
            effective_a: float = hist_avg_score
        else:
            effective_a = float(score_a)
        sum_cubed = sum(s ** 3 for s in alive_scores) or 1
        prob = (effective_a ** 3) / sum_cubed
        return prob_to_american(max(0.005, min(0.99, prob)))

    if market_type == "LOWEST_TRAINING_SCORE":
        if tribute_a is not None and tribute_a.training_score is None and hist_avg_score is not None:
            effective_a = hist_avg_score
        else:
            effective_a = float(score_a)
        inv = 1.0 / max(effective_a, 1)
        sum_inv_sq = sum((1.0 / max(s, 1)) ** 2 for s in alive_scores) or 1.0
        prob = (inv ** 2) / sum_inv_sq
        return prob_to_american(max(0.005, min(0.99, prob)))

    if market_type == "FIRST_IN_ALLIANCE_DEATH":
        # P(this tribute is the first to die within their alliance)
        if tribute_a is not None and tribute_a.alliance_id is not None:
            alliance_alive = [
                t for t in all_tributes
                if t.alliance_id == tribute_a.alliance_id and t.status == "ALIVE"
            ]
            if alliance_alive:
                sum_inv_alliance = sum(1.0 / max(t.training_score or 6, 1) for t in alliance_alive)
                inv_a = 1.0 / max(score_a, 1)
                prob = inv_a / max(sum_inv_alliance, 0.0001)
                return prob_to_american(max(0.01, min(0.99, prob)))
        # Fallback: field-relative weakness
        inv = 1.0 / max(score_a, 1)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        return prob_to_american(max(0.01, min(0.99, inv / sum_inv)))

    # ── Milestone markets (all derived from the same top-k model) ───────────────

    _FINAL_K = {
        "MAKES_FINAL_8": 8, "MAKES_FINAL_5": 5,
        "MISSES_FINAL_8": 8, "MISSES_FINAL_5": 5,
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

    if market_type in (
        "PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER",
        "PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER",
    ):
        # Head-to-head probability: tribute_a's share of the two-tribute strength pool.
        # Same formula for score and placement comparisons — training score is the
        # best pre-Games proxy for relative finishing order as well.
        # Both sides must be compressed the same way (score_a already is, above) —
        # otherwise "A higher than B" and "B lower than A" price different
        # probabilities for the same real-world outcome, since whichever tribute
        # lands in the tribute_a slot gets its score dampened and the other doesn't.
        score_b = (
            _compress(tribute_b.training_score or 6, field_mean)
            if tribute_b is not None and tribute_b.status == "ALIVE"
            else 1.0
        )
        p_a_wins = score_a / max(score_a + score_b, 1)
        prob = p_a_wins if market_type in ("PARTNER_SCORE_HIGHER", "PARTNER_PLACE_HIGHER") else 1.0 - p_a_wins
        return prob_to_american(max(0.01, min(0.99, prob)))

    return DEFAULT_FALLBACK_ODDS


# ── District-level market odds ─────────────────────────────────────────────────

_DISTRICT_K_BOTH = {
    "DISTRICT_BOTH_FINAL_8": 8, "DISTRICT_BOTH_FINAL_5": 5,
}
_DISTRICT_K_ONE = {
    "DISTRICT_ONE_FINAL_8": 8, "DISTRICT_ONE_FINAL_5": 5,
}

_ALLIANCE_K_ALL = {
    "ALLIANCE_ALL_FINAL_8": 8, "ALLIANCE_ALL_FINAL_5": 5,
}
_ALLIANCE_K_ONE = {
    "ALLIANCE_ONE_FINAL_8": 8, "ALLIANCE_ONE_FINAL_5": 5,
}


def _victor_probability(
    members: list["Tribute"],
    all_tributes: list["Tribute"],
    win_factors: "dict[int, float] | None",
) -> float:
    """Probability that the eventual victor comes from ``members``.

    Priced exactly like the single-tribute TRIBUTE_WINS market (see
    ``default_odds``): each alive member's share of the *compressed* field
    strength, scaled by their field-relative, tempered win modifier. A
    district/alliance wins iff one of its members wins, and those victor events
    are mutually exclusive, so the group probability is the sum of the members'
    individual win probabilities.

    Compressing the scores and tempering the modifier relative to the field is
    what keeps these victor lines off the ±9900 cap, the same fix applied to the
    per-tribute victor market.
    """
    raw = _alive_scores(all_tributes)
    if not raw:
        return 0.0
    field_mean = sum(raw) / len(raw)
    compressed_total = sum(_compressed_scores(all_tributes)) or 1.0
    if win_factors:
        alive_ids = [t.id for t in all_tributes if t.status == "ALIVE"]
        wf_vals = [win_factors.get(i, 1.0) for i in alive_ids]
        avg_wf = (sum(wf_vals) / len(wf_vals)) if wf_vals else 1.0
    else:
        avg_wf = 1.0

    prob = 0.0
    for t in members:
        if t.status != "ALIVE":
            continue
        cscore = _compress(t.training_score or 6, field_mean)
        wf = win_factors.get(t.id, 1.0) if win_factors else 1.0
        # Field-relative + tempered modifier, mirroring the per-tribute path: a
        # member carrying the field-average boost is left unchanged, and extremes
        # are pulled toward neutral by MODIFIER_TEMPER.
        relative = wf / max(avg_wf, 0.01)
        mfactor = relative ** MODIFIER_TEMPER
        prob += (cscore / compressed_total) * mfactor
    return prob


def district_default_odds(
    market_type: str,
    district_tributes: list["Tribute"],
    all_tributes: list["Tribute"],
    ou_line: float | None = None,
    ou_side: str | None = None,
    win_factors: "dict[int, float] | None" = None,
    kill_factors: "dict[int, float] | None" = None,
) -> int:
    """Compute default odds for district-level markets (no single tribute_a).

    win_factors / kill_factors are per-tribute combined modifier maps (debilitation,
    district funding, historical performance, reputation, etc.) keyed by tribute id.
    A factor of 1.0 is neutral; omit to price on raw training scores only.
    """
    alive = [t for t in district_tributes if t.status == "ALIVE"]
    if not alive:
        return DEFAULT_FALLBACK_ODDS

    def _wf(t: "Tribute") -> float:
        return win_factors.get(t.id, 1.0) if win_factors else 1.0

    def _kf(t: "Tribute") -> float:
        return kill_factors.get(t.id, 1.0) if kill_factors else 1.0

    alive_all_scores = _alive_scores(all_tributes)
    total = sum(alive_all_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    if market_type == "DISTRICT_VICTOR":
        prob = _victor_probability(alive, all_tributes, win_factors)
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "DISTRICT_KILLS_OU":
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = sum(total_kills * ((t.training_score or 6) ** 2) / sum_sq * _kf(t) for t in alive)
        line = ou_line if ou_line is not None else 1.5
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "DISTRICT_BOTH_BLOODBATH":
        sum_inv = sum(1.0 / max(s, 1) for s in alive_all_scores) or 1.0
        p_both = 1.0
        for t in alive:
            inv = 1.0 / max(t.training_score or 6, 1)
            p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
            p_death = max(0.03, min(0.92, p_death))
            p_survive = (1.0 - p_death) * _wf(t)
            p_both *= max(0.03, min(0.97, p_survive))
        return prob_to_american(max(0.01, min(0.99, p_both)))

    if market_type == "DISTRICT_WIPED_BLOODBATH":
        sum_inv = sum(1.0 / max(s, 1) for s in alive_all_scores) or 1.0
        p_all_dead = 1.0
        for t in alive:
            inv = 1.0 / max(t.training_score or 6, 1)
            p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
            p_death = max(0.03, min(0.92, p_death))
            p_die = p_death * _wf(t)
            p_all_dead *= max(0.03, min(0.97, p_die))
        return prob_to_american(max(0.01, min(0.99, p_all_dead)))

    if market_type in _DISTRICT_K_BOTH:
        k = _DISTRICT_K_BOTH[market_type]
        p_both = 1.0
        for t in alive:
            p_both *= max(0.001, min(0.999, _p_top_k((t.training_score or 6) / total, k) * _wf(t)))
        return prob_to_american(max(0.01, min(0.99, p_both)))

    if market_type in _DISTRICT_K_ONE:
        k = _DISTRICT_K_ONE[market_type]
        p_none = 1.0
        for t in alive:
            p_none *= 1.0 - max(0.001, min(0.999, _p_top_k((t.training_score or 6) / total, k) * _wf(t)))
        return prob_to_american(max(0.01, min(0.99, 1.0 - p_none)))

    if market_type == "DISTRICT_HIGHEST_SCORE":
        # P(this district has the highest combined training score) ∝ sum(scores)³
        # Win factors serve as a proxy for scoring aptitude when no training scores are set.
        dist_scores: dict[int, float] = {}
        for t in all_tributes:
            if t.status == "ALIVE":
                dist_scores[t.district] = (
                    dist_scores.get(t.district, 0) + (t.training_score or 6) * _wf(t)
                )
        sum_cubed = sum(s ** 3 for s in dist_scores.values()) or 1
        dist_score = sum((t.training_score or 6) * _wf(t) for t in alive)
        prob = (dist_score ** 3) / sum_cubed
        return prob_to_american(max(0.005, min(0.99, prob)))

    if market_type == "FIRST_DISTRICT_WIPE":
        # P(this district is first wiped) ∝ product of member inverse strengths
        if not alive:
            return DEFAULT_FALLBACK_ODDS
        dist_prods: dict[int, float] = {}
        for t in all_tributes:
            if t.status == "ALIVE":
                d = t.district
                effective = max((t.training_score or 6) * _wf(t), 0.1)
                dist_prods[d] = dist_prods.get(d, 1.0) * (1.0 / effective)
        sum_prods = sum(dist_prods.values()) or 1.0
        prod_inv = 1.0
        for t in alive:
            effective = max((t.training_score or 6) * _wf(t), 0.1)
            prod_inv *= 1.0 / effective
        prob = prod_inv / sum_prods
        return prob_to_american(max(0.01, min(0.99, prob)))

    return DEFAULT_FALLBACK_ODDS


def pre_reaping_district_odds(
    district_records: "dict[int, DistrictRecord]",
) -> dict[int, int]:
    """Opening DISTRICT_VICTOR odds for all 12 districts before any tribute has
    been reaped — there's no roster or training score to price from yet, so
    this is the only district-market pricing path driven purely by each
    district's historical reputation rather than the current field.

    Each district starts from a flat 1/12 field share, nudged by
    ``reputation_factor`` (bounded to ±10% per district at the reputation
    extremes), then the 12 shares are renormalized back to a probability
    distribution so the field still sums to ~100% before conversion.
    """
    baseline = 1.0 / 12
    raw = {
        d: baseline * reputation_factor(
            district_records[d].reputation if d in district_records else None
        )
        for d in range(1, 13)
    }
    total = sum(raw.values()) or 1.0
    return {
        d: prob_to_american(max(0.01, min(0.99, p / total)))
        for d, p in raw.items()
    }


def alliance_default_odds(
    market_type: str,
    alliance_members: list["Tribute"],
    all_tributes: list["Tribute"],
    ou_line: float | None = None,
    ou_side: str | None = None,
    win_factors: "dict[int, float] | None" = None,
    kill_factors: "dict[int, float] | None" = None,
    top_n: int | None = None,
) -> int:
    """Compute default odds for alliance-level markets (no single tribute_a).

    win_factors / kill_factors are per-tribute combined modifier maps (debilitation,
    district funding, custom modifiers, history, etc.).  A factor of 1.0 is neutral;
    a debilitated member at 0.55 drags the alliance down; a well-funded member at
    2.25 lifts it.  Omit to price on raw training scores only.
    """
    alive = [t for t in alliance_members if t.status == "ALIVE"]
    if not alive:
        return DEFAULT_FALLBACK_ODDS

    def _wf(t: "Tribute") -> float:
        return win_factors.get(t.id, 1.0) if win_factors else 1.0

    def _kf(t: "Tribute") -> float:
        return kill_factors.get(t.id, 1.0) if kill_factors else 1.0

    alive_all_scores = _alive_scores(all_tributes)
    total = sum(alive_all_scores) or 1
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1

    if market_type == "ALLIANCE_VICTOR":
        prob = _victor_probability(alive, all_tributes, win_factors)
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "ALLIANCE_KILLS_OU":
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = sum(
            total_kills * ((t.training_score or 6) ** 2) / sum_sq * _kf(t)
            for t in alive
        )
        line = ou_line if ou_line is not None else 1.5
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(prob)

    if market_type in _ALLIANCE_K_ALL:
        k = _ALLIANCE_K_ALL[market_type]
        p_all = 1.0
        for t in alive:
            p_member = _p_top_k((t.training_score or 6) / total, k)
            p_all *= max(0.001, min(0.999, p_member * _wf(t)))
        return prob_to_american(max(0.01, min(0.99, p_all)))

    if market_type in _ALLIANCE_K_ONE:
        k = _ALLIANCE_K_ONE[market_type]
        p_none = 1.0
        for t in alive:
            p_member = _p_top_k((t.training_score or 6) / total, k)
            p_none *= 1.0 - max(0.001, min(0.999, p_member * _wf(t)))
        return prob_to_american(max(0.01, min(0.99, 1.0 - p_none)))

    if market_type == "ALLIANCE_RUNNER_UP":
        # P(any member of this alliance finishes exactly 2nd)
        p_none = 1.0
        for t in alive:
            s = (t.training_score or 6) / total
            p_2nd = max(0.001, (_p_top_k(s, 2) - _p_top_k(s, 1)) * _wf(t))
            p_none *= max(0.001, 1.0 - p_2nd)
        return prob_to_american(max(0.01, min(0.99, 1.0 - p_none)))

    if market_type == "FIRST_ALLIANCE_WIPED":
        # P(this alliance is the first wiped) ≈ proportional to combined member weakness
        alive_all_scores_inv = [1.0 / max(s, 1) for s in alive_all_scores]
        sum_inv_all = sum(alive_all_scores_inv) or 1.0
        alliance_weakness = sum(1.0 / max(t.training_score or 6, 1) * _wf(t) for t in alive)
        prob = alliance_weakness / sum_inv_all
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "ALLIANCE_MOST_KILLS":
        # P(this alliance gets most kills) ≈ proportion of total kill-strength (strength²)
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        alliance_ks = sum((t.training_score or 6) ** 2 * _kf(t) for t in alive)
        prob = alliance_ks / sum_sq
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "EXACT_ALLIANCE_KILLS":
        # Poisson PMF at guessed kill count (top_n)
        sum_sq = sum(s * s for s in alive_all_scores) or 1
        total_kills = TRIBUTE_KILL_FRACTION * max(1, n - 1)
        lam = sum(total_kills * ((t.training_score or 6) ** 2) / sum_sq * _kf(t) for t in alive)
        k = top_n if top_n is not None else 2
        if lam <= 0 or k < 0:
            return DEFAULT_FALLBACK_ODDS
        pmf = math.exp(-lam) * (lam ** k) / math.factorial(k)
        return prob_to_american(max(0.005, min(0.99, pmf)))

    return DEFAULT_FALLBACK_ODDS


# ── Game-level prop market odds ────────────────────────────────────────────────

def game_default_odds(
    market_type: str,
    all_tributes: list["Tribute"],
    placement_num: int | None = None,
    ou_line: float | None = None,
    ou_side: str | None = None,
) -> int:
    """Compute default odds for game-level prop markets (no tribute/district/alliance)."""
    alive_scores = _alive_scores(all_tributes)
    n = len([t for t in all_tributes if t.status == "ALIVE"]) or 1
    total = sum(alive_scores) or 1

    if market_type == "BLOODBATH_KILLS_OU":
        lam = BLOODBATH_DEATH_FRACTION * TRIBUTE_KILL_FRACTION * n
        line = ou_line if ou_line is not None else round(lam - 0.5, 1)
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "BLOODBATH_DEATHS_OU":
        lam = BLOODBATH_DEATH_FRACTION * n
        line = ou_line if ou_line is not None else round(lam - 0.5, 1)
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "EXACT_BLOODBATH_DEATHS":
        lam = BLOODBATH_DEATH_FRACTION * n
        k = placement_num if placement_num is not None else max(1, round(lam))
        if lam <= 0 or k < 0:
            return DEFAULT_FALLBACK_ODDS
        pmf = math.exp(-lam) * (lam ** k) / math.factorial(k)
        return prob_to_american(max(0.005, min(0.99, pmf)))

    if market_type == "ARENA_TRAP_DEATHS_OU":
        # Expected Gamemaker/trap deaths ≈ (1 - TRIBUTE_KILL_FRACTION) * 0.5 * (n-1)
        lam = (1.0 - TRIBUTE_KILL_FRACTION) * 0.5 * max(1, n - 1)
        line = ou_line if ou_line is not None else max(0.5, round(lam - 0.5, 1))
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "ARENA_ENV_DEATHS_OU":
        # Expected environmental (Natural Causes + Mutt) deaths ≈ same fraction as trap
        lam = (1.0 - TRIBUTE_KILL_FRACTION) * 0.5 * max(1, n - 1)
        line = ou_line if ou_line is not None else max(0.5, round(lam - 0.5, 1))
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "GAMES_DURATION":
        # Gaussian PMF around 7-day expected duration (sigma = 1.5)
        days = placement_num if placement_num is not None else 7
        mean_days = 7.0
        sigma = 1.5
        lo = (days - 0.5 - mean_days) / (sigma * math.sqrt(2))
        hi = (days + 0.5 - mean_days) / (sigma * math.sqrt(2))
        pmf = max(0.0, 0.5 * (math.erf(hi) - math.erf(lo)))
        return prob_to_american(max(0.005, min(0.99, pmf)))

    if market_type == "SOLO_TRIBUTES_OU":
        # Current count of unallied alive tributes as the expected value (Poisson approx)
        lam = float(sum(1 for t in all_tributes if t.status == "ALIVE" and t.alliance_id is None))
        line = ou_line if ou_line is not None else max(0.5, lam - 0.5)
        if lam <= 0:
            prob = 0.0 if ou_side == "OVER" else 1.0
            return prob_to_american(max(0.01, min(0.99, prob)))
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type == "BLOODBATH_NO_DEATHS":
        # P(nobody dies in bloodbath) = product of P(each tribute survives)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        p_all_survive = 1.0
        for s in alive_scores:
            inv = 1.0 / max(s, 1)
            p_death = BLOODBATH_DEATH_FRACTION * n * (inv / sum_inv)
            p_death = max(0.03, min(0.92, p_death))
            p_all_survive *= (1.0 - p_death)
        return prob_to_american(max(0.005, min(0.99, p_all_survive)))

    if market_type == "NUM_TENS_OU":
        # Expected count of tributes scoring 10+ (~12% base rate per tribute)
        lam = n * 0.12
        line = ou_line if ou_line is not None else max(0.5, round(lam - 0.5, 1))
        m = int(math.floor(line)) + 1
        p_over = _poisson_at_least(m, lam)
        prob = p_over if ou_side == "OVER" else 1.0 - p_over
        return prob_to_american(max(0.01, min(0.99, prob)))

    if market_type in ("ARENA_IS_NATURAL", "ARENA_IS_ARTIFICIAL"):
        return 100  # even money by default; admin adjusts from historical arena counts

    if market_type == "DISTRICT_PARTNER_KILL":
        # P(any tribute kills their district partner during the Games)
        sum_inv = sum(1.0 / max(s, 1) for s in alive_scores) or 1.0
        districts = {t.district for t in all_tributes if t.status == "ALIVE"}
        p_none = 1.0
        for d in districts:
            d_alive = [t for t in all_tributes if t.district == d and t.status == "ALIVE"]
            if len(d_alive) < 2:
                continue
            t1, t2 = d_alive[0], d_alive[1]
            s1 = t1.training_score or 6
            s2 = t2.training_score or 6
            inv2 = 1.0 / max(s2, 1)
            p_b_killed = min(0.95, TRIBUTE_KILL_FRACTION * n * (inv2 / sum_inv))
            p_a_kills_b = p_b_killed * (s1 / max(total - s2, 1)) * DISTRICT_PAIR_KILL_SUPPRESSION
            inv1 = 1.0 / max(s1, 1)
            p_a_killed = min(0.95, TRIBUTE_KILL_FRACTION * n * (inv1 / sum_inv))
            p_b_kills_a = p_a_killed * (s2 / max(total - s1, 1)) * DISTRICT_PAIR_KILL_SUPPRESSION
            p_pair_kill = 1.0 - (1.0 - p_a_kills_b) * (1.0 - p_b_kills_a)
            p_none *= (1.0 - p_pair_kill)
        prob = 1.0 - p_none
        return prob_to_american(max(0.01, min(0.99, prob)))

    # GAMES_FEAST, GAMES_BETRAYAL, ANY_BB_DOUBLE_KILL — no modellable prior

    return DEFAULT_FALLBACK_ODDS


# ── Multi-outcome roster markets (e.g. "Fifth Career Alliance Member") ──────
# A multi_outcome MarketTemplate prices one Market row per eligible tribute from
# a weighted blend of curated historical factors instead of the win/kill/
# placement formulas above, and entirely independent of them — the probability
# distribution is normalised over just the template's eligible tributes, never
# the whole field, so this can't be pulled toward (or pull on) any other
# market's odds. Every factor is a multiplicative signal centered at 1.0
# (neutral); ratio-based ones are clamped the same way _district_historical_
# factor's _component() helper clamps district ratios in bot/cogs/admin.py.

MULTI_OUTCOME_RATIO_MIN = 0.2
MULTI_OUTCOME_RATIO_MAX = 5.0


def _ratio_factor(value: float | None, group_values: list[float], invert: bool = False) -> float:
    """Clamped ratio of `value` to the mean of `group_values`."""
    if value is None or not group_values:
        return 1.0
    avg = sum(group_values) / len(group_values)
    if avg <= 0:
        return 1.0
    if invert:
        # A zero stat is the best possible outcome for an inverted (penalty)
        # metric (e.g. avg_placement); treat it as the max ratio instead of
        # dividing by zero.
        ratio = MULTI_OUTCOME_RATIO_MAX if value <= 0 else (avg / value)
    else:
        ratio = value / avg
    return max(MULTI_OUTCOME_RATIO_MIN, min(MULTI_OUTCOME_RATIO_MAX, ratio))


def _f_tribute_kills(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    return _ratio_factor(float(tribute.kills), [float(t.kills) for t in eligible])


def _f_tribute_bloodbath_kills(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    return _ratio_factor(float(tribute.bloodbath_kills), [float(t.bloodbath_kills) for t in eligible])


def _f_tribute_kill_boost(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    def _kb(t: "Tribute") -> float:
        return max(0.5, 1.0 + t.kill_boost * KILL_BOOST_SCALE)
    return _ratio_factor(_kb(tribute), [_kb(t) for t in eligible])


def _f_training_score(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    raw = [(t.training_score or 6) for t in eligible if t.status == "ALIVE"] or [6.0]
    mean = sum(raw) / len(raw)
    scores = [_compress(s, mean) for s in raw]
    score = _compress(tribute.training_score or 6, mean) if tribute.status == "ALIVE" else mean
    return _ratio_factor(score, scores)


def _f_prior_experience(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    return prior_experience_factor(tribute.times_played, tribute.highest_placement)


def _f_age(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    return age_factor(tribute.age)


def _f_district_kill_record(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    if dr is None or dr.kill_record is None:
        return 1.0
    vals = [float(r.kill_record) for r in all_dr if r.kill_record is not None]
    return _ratio_factor(float(dr.kill_record), vals)


def _f_district_bloodbath_kills(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    if dr is None or dr.bloodbath_kills is None:
        return 1.0
    vals = [float(r.bloodbath_kills) for r in all_dr if r.bloodbath_kills is not None]
    return _ratio_factor(float(dr.bloodbath_kills), vals)


def _f_district_avg_placement(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    if dr is None or dr.avg_placement is None:
        return 1.0
    vals = [float(r.avg_placement) for r in all_dr if r.avg_placement is not None]
    # Lower average placement number is a better historical record, so invert.
    return _ratio_factor(float(dr.avg_placement), vals, invert=True)


def _district_wins(record: "DistrictRecord") -> float | None:
    if record.wins is not None:
        return float(record.wins)
    if record.victor_male_count is not None or record.victor_female_count is not None:
        return float((record.victor_male_count or 0) + (record.victor_female_count or 0))
    return None


def _f_district_victor_count(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    if dr is None:
        return 1.0
    d_wins = _district_wins(dr)
    vals = [w for w in (_district_wins(r) for r in all_dr) if w is not None]
    if d_wins is None or not vals:
        return 1.0
    return _ratio_factor(d_wins, vals)


def _f_district_funding_level(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    return funding_factor(dr.funding_level if dr else None)


def _f_district_reputation(tribute: "Tribute", eligible: list["Tribute"], dr_map: dict, all_dr: list) -> float:
    dr = dr_map.get(tribute.district)
    return reputation_factor(dr.reputation if dr else None)


# key -> (display label, compute(tribute, eligible, dr_map, all_dr) -> multiplier).
# This exact set of keys drives the `/admin market_type weight` command's
# factor choices — the curated list the admin can prioritize/deprioritize per
# roster market template.
MULTI_OUTCOME_FACTORS: dict[str, tuple[str, "callable"]] = {
    "tribute_kills":            ("Tribute Kills",             _f_tribute_kills),
    "tribute_bloodbath_kills":  ("Tribute Bloodbath Kills",   _f_tribute_bloodbath_kills),
    "tribute_kill_boost":       ("Tribute Kill Quality",      _f_tribute_kill_boost),
    "training_score":           ("Training Score",            _f_training_score),
    "prior_experience":         ("Prior Games Experience",    _f_prior_experience),
    "age_factor":               ("Age",                       _f_age),
    "district_kill_record":     ("District Kill Record",      _f_district_kill_record),
    "district_bloodbath_kills": ("District Bloodbath Kills",  _f_district_bloodbath_kills),
    "district_avg_placement":   ("District Avg Placement",    _f_district_avg_placement),
    "district_victor_count":    ("District Victor Count",     _f_district_victor_count),
    "district_funding_level":   ("District Funding Level",    _f_district_funding_level),
    "district_reputation":      ("District Reputation",       _f_district_reputation),
}


def multi_outcome_odds(
    eligible: list["Tribute"],
    dr_map: dict[int, "DistrictRecord"],
    all_dr: list["DistrictRecord"],
    weight_overrides: dict[str, float] | None,
) -> dict[int, int]:
    """American odds for every eligible tribute in a multi_outcome roster
    market. Each factor's clamped multiplier is combined via a weighted
    geometric mean (not a raw product) so the combined signal stays bounded
    near the individual factors' range regardless of how many are active —
    the same "tempered" idea as MODIFIER_TEMPER in bot/cogs/admin.py, made
    admin-tunable per factor. A weight of 0 drops a factor out entirely; the
    resulting probabilities are normalised over `eligible` only, then
    converted with prob_to_american (which applies the project's usual
    soft-rail/score-compression safeguards).
    """
    weights = weight_overrides or {}
    combined: dict[int, float] = {}
    for t in eligible:
        log_sum = 0.0
        weight_sum = 0.0
        for key, (_label, fn) in MULTI_OUTCOME_FACTORS.items():
            w = weights.get(key, 1.0)
            if w == 0:
                continue
            factor = max(0.01, fn(t, eligible, dr_map, all_dr))
            log_sum += w * math.log(factor)
            weight_sum += w
        combined[t.id] = math.exp(log_sum / weight_sum) if weight_sum > 0 else 1.0

    total = sum(combined.values()) or 1.0
    return {
        tid: prob_to_american(max(0.001, min(0.999, val / total)))
        for tid, val in combined.items()
    }

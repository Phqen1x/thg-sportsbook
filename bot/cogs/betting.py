from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot import config
from bot.database.engine import get_session, get_setting, current_guild_id
from bot.database.models import (
    Alliance, Bet, BettingRestriction, Market, MarketTemplate, Parlay, ParlayTemplate,
    ParlayTemplateLeg, PendingParlayLeg, Tribute, User,
)
from bot.imaging.bet_slip import ParlayLegData, render_parlay_slip
from bot.imaging.my_bets import (
    BetRowData, ParlayData, render_my_bets, render_tail_board, render_tail_detail,
    TAIL_BOARD_PER_PAGE,
)
from bot.imaging.base import render_async, buf_to_discord_file
from bot.odds.calculator import (
    straight_payout, parlay_payout, combined_american, resolve_cashout
)
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_odds_with_mult, safe_defer
from bot.utils.market_view import _TYPE_LABELS, _TYPE_ORDER, _type_section

log = logging.getLogger("capitol.betting")

MAX_PARLAY_LEGS = 10
PARLAY_PAYOUT_CAP = 10_000_000

# Minimum chips a member may withdraw or deposit in a single panars exchange.
EXCHANGE_MIN = 5000

BETTING_PAUSED_MSG = (
    "🛑 Betting is currently paused by an admin. Try again once it's resumed."
)


async def _betting_paused() -> bool:
    raw = await get_setting("betting_paused")
    return json.loads(raw) if raw else False


_MAKES_MILESTONES = {"MAKES_FINAL_8", "MAKES_FINAL_5"}
_ALL_MILESTONES = {
    "MAKES_FINAL_8", "MISSES_FINAL_8",
    "MAKES_FINAL_5", "MISSES_FINAL_5",
}
_MILESTONE_GROUP = {
    "MAKES_FINAL_8": "FINAL_8", "MISSES_FINAL_8": "FINAL_8",
    "MAKES_FINAL_5": "FINAL_5", "MISSES_FINAL_5": "FINAL_5",
}

_DISTRICT_MILESTONE_GROUP = {
    "DISTRICT_BOTH_FINAL_8": "DISTRICT_FINAL_8", "DISTRICT_ONE_FINAL_8": "DISTRICT_FINAL_8",
    "DISTRICT_BOTH_FINAL_5": "DISTRICT_FINAL_5", "DISTRICT_ONE_FINAL_5": "DISTRICT_FINAL_5",
}

_ALLIANCE_MILESTONE_GROUP = {
    "ALLIANCE_ALL_FINAL_8": "ALLIANCE_FINAL_8", "ALLIANCE_ONE_FINAL_8": "ALLIANCE_FINAL_8",
    "ALLIANCE_ALL_FINAL_5": "ALLIANCE_FINAL_5", "ALLIANCE_ONE_FINAL_5": "ALLIANCE_FINAL_5",
}

# Market types that are scoped to a district (placement_num = district number).
_DISTRICT_MARKET_TYPES = {
    "DISTRICT_VICTOR",
    "DISTRICT_KILLS_OU",
    "DISTRICT_BOTH_BLOODBATH",
    "DISTRICT_WIPED_BLOODBATH",
    "DISTRICT_BOTH_FINAL_8",
    "DISTRICT_ONE_FINAL_8",
    "DISTRICT_BOTH_FINAL_5",
    "DISTRICT_ONE_FINAL_5",
    "DISTRICT_HIGHEST_SCORE",
    "FIRST_DISTRICT_WIPE",
}

# Market types that are scoped to an alliance (placement_num = alliance_id).
_ALLIANCE_MARKET_TYPES = {
    "ALLIANCE_VICTOR",
    "ALLIANCE_KILLS_OU",
    "ALLIANCE_ALL_FINAL_8",
    "ALLIANCE_ONE_FINAL_8",
    "ALLIANCE_ALL_FINAL_5",
    "ALLIANCE_ONE_FINAL_5",
    "FIRST_ALLIANCE_WIPED",
    "ALLIANCE_MOST_KILLS",
    "EXACT_ALLIANCE_KILLS",
    "ALLIANCE_RUNNER_UP",
}


# Every market type that constrains where a tribute finishes. A victor bet is
# just an exact-placement bet on 1st, so it counts as a placement market too.
_PLACEMENT_TYPES = {"TRIBUTE_WINS", "TRIBUTE_PLACEMENT", "TRIBUTE_TOP_N", "PLACEMENT_OU"}

# Every "who wins the Games" market. Exactly one tribute wins, so these are all
# correlated — picking the individual victor (TRIBUTE_WINS) implies their district
# and alliance win too. At most one may share a parlay; stacking them just inflates
# the odds without making the slip meaningfully harder to hit.
_VICTOR_TYPES = {"TRIBUTE_WINS", "DISTRICT_VICTOR", "ALLIANCE_VICTOR"}


def _parse_id(raw: str) -> int | None:
    """Parse an ID coming from an autocomplete field. Returns None if the user
    typed free text instead of selecting a real choice (Discord then sends the
    raw text as the value, which is not a valid integer ID)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _exact_placement(m: Market) -> int | None:
    """The single finishing position a market pins down, or None if it covers a
    range (top-N / over-under) rather than one exact spot."""
    if m.type == "TRIBUTE_WINS":
        return 1
    if m.type == "TRIBUTE_PLACEMENT":
        return m.placement_num
    return None


def _implied_milestones(m: Market) -> set[str]:
    """Milestone types that are guaranteed true whenever market m wins."""
    if m.tribute_a_id is None:
        return set()
    if m.type in {"TRIBUTE_WINS", "TRIBUTE_RUNNER_UP"}:
        return {"MAKES_FINAL_5", "MAKES_FINAL_8"}
    if m.type == "TRIBUTE_PLACEMENT" and m.placement_num is not None:
        result: set[str] = set()
        if m.placement_num <= 5:
            result.add("MAKES_FINAL_5")
        if m.placement_num <= 8:
            result.add("MAKES_FINAL_8")
        return result
    if m.type == "TRIBUTE_TOP_N" and m.top_n is not None:
        result = set()
        if m.top_n <= 5:
            result.add("MAKES_FINAL_5")
        if m.top_n <= 8:
            result.add("MAKES_FINAL_8")
        return result
    return set()


def _placement_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Return an error string if adding new_mkt would violate placement parlay rules.

    A tribute can only finish in one position, so two placement bets on the SAME
    tribute conflict — the lone exception being an opposite over/under pair (e.g.
    over 3rd AND under 12th, which together describe a finishing window). Across
    DIFFERENT tributes placement bets are fine, except two bets that pin the same
    exact position (two victors, or two tributes both finishing exactly Nth),
    since only one tribute can occupy a given spot.
    """
    if new_mkt.type not in _PLACEMENT_TYPES:
        return None
    for m in existing_markets:
        if m.type not in _PLACEMENT_TYPES:
            continue
        same_tribute = (
            m.tribute_a_id is not None and m.tribute_a_id == new_mkt.tribute_a_id
        )
        if same_tribute:
            # Opposite-side placement over/unders together describe a window and
            # are the only allowed pairing on a single tribute.
            if (
                m.type == "PLACEMENT_OU"
                and new_mkt.type == "PLACEMENT_OU"
                and m.ou_side and new_mkt.ou_side
                and m.ou_side != new_mkt.ou_side
            ):
                continue
            return (
                "You can't parlay two placement bets on the same tribute — a "
                "tribute only finishes in one position, so victor, exact "
                "placement, top-N, and placement over/under bets all conflict "
                "with each other. (The only exception is an opposite over/under "
                "pair, e.g. over 3rd **and** under 12th.)"
            )
        ea, eb = _exact_placement(m), _exact_placement(new_mkt)
        if ea is not None and ea == eb:
            if ea == 1:
                return "You can't parlay two victor bets — only one tribute can win the Games."
            return (
                f"You can't parlay two bets on a tribute finishing exactly "
                f"{_ordinal(ea)} — only one tribute can take that position."
            )
    return None


def _milestone_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Return an error string if adding new_mkt would violate milestone parlay rules."""
    if new_mkt.type not in _ALL_MILESTONES:
        return None
    same = [
        m for m in existing_markets
        if m.tribute_a_id == new_mkt.tribute_a_id and m.type in _ALL_MILESTONES
    ]
    new_group = _MILESTONE_GROUP[new_mkt.type]
    for m in same:
        if _MILESTONE_GROUP[m.type] == new_group:
            return (
                f"Cannot combine two milestone markets for the same phase on one tribute "
                f"(both target {new_group.replace('_', ' ').title()})."
            )
    if new_mkt.type in _MAKES_MILESTONES:
        for m in same:
            if m.type in _MAKES_MILESTONES:
                return "Cannot include two 'makes milestone' bets for the same tribute in one parlay."
    return None


def _district_milestone_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Prevent parlaying BOTH and ONE district milestone markets for the same district+phase.

    If both survive, ONE is guaranteed to win, making the parlay degenerate.
    """
    if new_mkt.type not in _DISTRICT_MILESTONE_GROUP:
        return None
    new_group = _DISTRICT_MILESTONE_GROUP[new_mkt.type]
    new_district = new_mkt.placement_num
    for m in existing_markets:
        if m.type not in _DISTRICT_MILESTONE_GROUP:
            continue
        if _DISTRICT_MILESTONE_GROUP[m.type] != new_group:
            continue
        if m.placement_num != new_district:
            continue
        return (
            "Cannot combine 'both make it' and 'at least one makes it' district markets "
            "for the same district and phase — one outcome is guaranteed by the other."
        )
    return None


def _alliance_milestone_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Prevent parlaying ALL and ONE alliance milestone markets for the same alliance+phase."""
    if new_mkt.type not in _ALLIANCE_MILESTONE_GROUP:
        return None
    new_group = _ALLIANCE_MILESTONE_GROUP[new_mkt.type]
    new_alliance = new_mkt.placement_num
    for m in existing_markets:
        if m.type not in _ALLIANCE_MILESTONE_GROUP:
            continue
        if _ALLIANCE_MILESTONE_GROUP[m.type] != new_group:
            continue
        if m.placement_num != new_alliance:
            continue
        return (
            "Cannot combine 'all make it' and 'at least one makes it' alliance markets "
            "for the same alliance and phase — one outcome is guaranteed by the other."
        )
    return None


# ── Survival-depth model ──────────────────────────────────────────────────────
# A tribute can only travel one path through the Games, so "this tribute survives
# / advances / wins" and "this tribute is eliminated early / dies" can never both
# come true. We rank the stages a tribute can reach; a market that GUARANTEES the
# tribute reaches some stage conflicts with one that GUARANTEES it is eliminated
# before that same stage.
_SURVIVAL_BLOODBATH = 0
_SURVIVAL_FINAL_8 = 1
_SURVIVAL_FINAL_5 = 2
_SURVIVAL_FINALE = 3
_SURVIVAL_WIN = 4

_SURVIVAL_CONFLICT_MSG = (
    "You can't parlay a tribute advancing and being eliminated at the same time — "
    "betting the same tribute to die (or get knocked out early) and to survive, "
    "make a later stage, or win the Games can never both come true."
)


def _survival_reaches(m: Market) -> int | None:
    """Deepest stage a tribute is guaranteed to reach if this market wins."""
    if m.type == "TRIBUTE_WINS":
        return _SURVIVAL_WIN
    if m.type == "TRIBUTE_RUNNER_UP":
        return _SURVIVAL_FINALE
    if m.type == "TRIBUTE_PLACEMENT":
        if m.placement_num == 1:
            return _SURVIVAL_WIN
        if m.placement_num == 2:
            return _SURVIVAL_FINALE
        return None
    return {
        "MAKES_FINAL_5": _SURVIVAL_FINAL_5,
        "MAKES_FINAL_8": _SURVIVAL_FINAL_8,
        "BLOODBATH_SURVIVOR": _SURVIVAL_BLOODBATH,
    }.get(m.type)


def _survival_eliminated_before(m: Market) -> int | None:
    """Stage a tribute is guaranteed NOT to reach if this market wins."""
    if m.type == "FIRST_TRIBUTE_TO_DIE":
        return _SURVIVAL_FINAL_8
    return {
        "MISSES_FINAL_5": _SURVIVAL_FINAL_5,
        "MISSES_FINAL_8": _SURVIVAL_FINAL_8,
        "TRIBUTE_KILLED_BLOODBATH": _SURVIVAL_BLOODBATH,
    }.get(m.type)


def _survival_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block parlaying a tribute surviving/advancing/winning against that same
    tribute being eliminated. E.g. you can't bet a tribute wins AND is eliminated
    before the finale — a winner reaches every stage."""
    if new_mkt.tribute_a_id is None:
        return None
    new_reach = _survival_reaches(new_mkt)
    new_elim = _survival_eliminated_before(new_mkt)
    if new_reach is None and new_elim is None:
        return None
    for m in existing_markets:
        if m.tribute_a_id is None or m.tribute_a_id != new_mkt.tribute_a_id:
            continue
        # New bet advances past a stage the existing bet says they never reach.
        if new_reach is not None:
            e = _survival_eliminated_before(m)
            if e is not None and new_reach >= e:
                return _SURVIVAL_CONFLICT_MSG
        # Existing bet advances past a stage the new bet says they never reach.
        if new_elim is not None:
            r = _survival_reaches(m)
            if r is not None and r >= new_elim:
                return _SURVIVAL_CONFLICT_MSG
    return None


def _placement_milestone_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block parlaying a placement bet with a survival milestone that is
    guaranteed if the placement hits — those legs are never independent.

    Wins/runner-up imply both MAKES_FINAL_5 and MAKES_FINAL_8; a top-5
    placement implies MAKES_FINAL_8. The reverse direction is checked too
    so order of addition doesn't matter.
    """
    _MSG = (
        "You can't parlay a placement bet with a milestone that's guaranteed "
        "if the placement hits — e.g. winning implies making top 5 and top 8, "
        "so those milestone legs add no independent risk."
    )
    implied = _implied_milestones(new_mkt)
    if implied and new_mkt.tribute_a_id is not None:
        for m in existing_markets:
            if m.tribute_a_id != new_mkt.tribute_a_id:
                continue
            if m.type in implied:
                return _MSG
    if new_mkt.type in {"MAKES_FINAL_5", "MAKES_FINAL_8"} and new_mkt.tribute_a_id is not None:
        for m in existing_markets:
            if m.tribute_a_id != new_mkt.tribute_a_id:
                continue
            if new_mkt.type in _implied_milestones(m):
                return _MSG
    return None


# ── Training-score model ──────────────────────────────────────────────────────
# A tribute receives exactly one training score, so every training-score bet on
# the same tribute pins down (or constrains) that single number. Two such bets
# can only share a parlay if they could both come true for one score — which here
# means they must not contradict each other, and we never allow an over and an
# under leg on the same tribute. Across DIFFERENT tributes two exact-score bets on
# the SAME score are blocked, mirroring exact-placement bets.
_TRAINING_EXACT = "EXACT_TRAINING_SCORE"
_TRAINING_OU = "TRAINING_SCORE_OU"
_TRAINING_TYPES = {_TRAINING_EXACT, _TRAINING_OU}
_TRAINING_OU_DEFAULT_LINE = 6.5

_TRAINING_EXACT_OU_CONFLICT_MSG = (
    "You can't parlay an exact training-score bet with an over/under that the same "
    "tribute's score would contradict — e.g. exact 10 and under 6.5 can never both win."
)


def _training_score_satisfies_ou(score: int, ou: Market) -> bool:
    """Whether an exact training score would win the given over/under leg."""
    line = ou.ou_line if ou.ou_line is not None else _TRAINING_OU_DEFAULT_LINE
    return score > line if ou.ou_side == "OVER" else score <= line


def _training_score_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block training-score legs that can't coexist on one parlay slip.

    A tribute has a single training score, so on the SAME tribute we reject two
    exact-score bets, an exact-score bet the over/under leg would contradict, and
    any over+under pair. Across DIFFERENT tributes we reject two exact-score bets
    on the same score value (only one tribute can land that exact number)."""
    if new_mkt.type not in _TRAINING_TYPES:
        return None
    for m in existing_markets:
        if m.type not in _TRAINING_TYPES:
            continue
        same_tribute = (
            m.tribute_a_id is not None and m.tribute_a_id == new_mkt.tribute_a_id
        )
        if same_tribute:
            if m.type == _TRAINING_EXACT and new_mkt.type == _TRAINING_EXACT:
                return (
                    "You can't parlay two exact training-score bets on the same "
                    "tribute — a tribute only receives one training score."
                )
            # Exact score paired with an over/under the score would lose.
            if m.type == _TRAINING_EXACT and new_mkt.type == _TRAINING_OU:
                if m.placement_num is not None and not _training_score_satisfies_ou(
                    m.placement_num, new_mkt
                ):
                    return _TRAINING_EXACT_OU_CONFLICT_MSG
            if m.type == _TRAINING_OU and new_mkt.type == _TRAINING_EXACT:
                if new_mkt.placement_num is not None and not _training_score_satisfies_ou(
                    new_mkt.placement_num, m
                ):
                    return _TRAINING_EXACT_OU_CONFLICT_MSG
            # Never allow an over and an under leg on the same tribute.
            if (
                m.type == _TRAINING_OU
                and new_mkt.type == _TRAINING_OU
                and m.ou_side
                and new_mkt.ou_side
                and m.ou_side != new_mkt.ou_side
            ):
                return (
                    "You can't parlay an over and an under training-score bet on "
                    "the same tribute."
                )
        elif (
            m.type == _TRAINING_EXACT
            and new_mkt.type == _TRAINING_EXACT
            and m.placement_num is not None
            and m.placement_num == new_mkt.placement_num
        ):
            return (
                f"You can't parlay two bets on tributes scoring exactly "
                f"{new_mkt.placement_num} in training — pick a different score for one of them."
            )
    return None


_DISTRICT_SCORE_TYPE = "COMBINED_DISTRICT_SCORE"


def _district_score_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block two different exact combined-score guesses for the same district.

    A district's combined training score is one single number, so two markets
    naming different totals for the same tribute pair can never both win. The pair
    is compared as an unordered set since a/b ordering in the DB doesn't matter
    (see COMBINED_DISTRICT_SCORE market creation in admin.py)."""
    if new_mkt.type != _DISTRICT_SCORE_TYPE:
        return None
    if new_mkt.tribute_a_id is None or new_mkt.tribute_b_id is None:
        return None
    new_pair = frozenset((new_mkt.tribute_a_id, new_mkt.tribute_b_id))
    for m in existing_markets:
        if m.type != _DISTRICT_SCORE_TYPE:
            continue
        if m.tribute_a_id is None or m.tribute_b_id is None:
            continue
        if frozenset((m.tribute_a_id, m.tribute_b_id)) != new_pair:
            continue
        if m.placement_num != new_mkt.placement_num:
            return (
                "You can't parlay two different district combined-score guesses "
                "for the same district — only one exact total can be correct."
            )
    return None


# The guessed_sum range for COMBINED_DISTRICT_SCORE runs 2–24 (see market
# creation in admin.py) — 2 is the theoretical floor of a two-tribute combined
# training score.
_DISTRICT_SCORE_FLOOR = 2


def _district_score_vs_highest_conflict(
    existing_markets: list[Market],
    new_mkt: Market,
    tribute_by_id: dict[int, "Tribute"] | None,
) -> str | None:
    """Block a district's rock-bottom combined-score guess from sharing a parlay
    with that same district being bet to have the field's HIGHEST combined score.

    2 is the lowest possible combined training score, so a district guessed at
    the floor can't also be the district with the highest total. Needs a tribute
    lookup to resolve which district a COMBINED_DISTRICT_SCORE market belongs to
    (the market itself only stores the tribute pair, not the district number);
    callers that can't cheaply supply one pass None and this check is skipped."""
    if not tribute_by_id:
        return None

    def _district_of(m: Market) -> int | None:
        if m.tribute_a_id is None:
            return None
        t = tribute_by_id.get(m.tribute_a_id)
        return t.district if t else None

    def _check(score_mkt: Market, highest_mkt: Market) -> str | None:
        if (
            score_mkt.type != _DISTRICT_SCORE_TYPE
            or score_mkt.placement_num != _DISTRICT_SCORE_FLOOR
        ):
            return None
        if highest_mkt.type != "DISTRICT_HIGHEST_SCORE":
            return None
        d = _district_of(score_mkt)
        if d is None or highest_mkt.placement_num != d:
            return None
        return (
            f"You can't parlay D{d}'s combined training score at the rock-bottom "
            f"total ({_DISTRICT_SCORE_FLOOR}) together with D{d} having the highest "
            f"combined score in the field — the lowest possible total can't also "
            f"be the highest."
        )

    for m in existing_markets:
        conflict = _check(new_mkt, m) or _check(m, new_mkt)
        if conflict:
            return conflict
    return None


async def tribute_lookup_for_markets(db, markets: list[Market]) -> dict[int, Tribute]:
    """Fetch the Tribute rows referenced by a set of markets' tribute_a/b ids,
    keyed by id. Callers should build this once (outside any per-candidate loop)
    and pass the same dict into every _parlay_conflict() call so district-aware
    rules like _district_score_vs_highest_conflict can resolve without N+1
    queries. Takes a plain AsyncSession so it works from bot or web sessions."""
    tids = {m.tribute_a_id for m in markets if m.tribute_a_id is not None}
    tids |= {m.tribute_b_id for m in markets if m.tribute_b_id is not None}
    if not tids:
        return {}
    rows = (await db.execute(select(Tribute).where(Tribute.id.in_(tids)))).scalars().all()
    return {t.id: t for t in rows}


_PARTNER_SCORE_TYPES = {"PARTNER_SCORE_HIGHER", "PARTNER_SCORE_LOWER"}
_PARTNER_PLACE_TYPES = {"PARTNER_PLACE_HIGHER", "PARTNER_PLACE_LOWER"}
_PARTNER_COMPARISON_TYPES = _PARTNER_SCORE_TYPES | _PARTNER_PLACE_TYPES


def _partner_comparison_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block parlays where the same outcome must be both true and false, or where
    both partners in a pair are claimed to beat the other.

    Two markets conflict when they share the same {tribute_a, tribute_b} pair and:
    (a) same tribute_a, opposite sides (HIGHER + LOWER) — the tribute can't both
        score/place higher AND lower than their partner, or
    (b) swapped tribute_a/tribute_b, same direction (HIGHER + HIGHER, or LOWER + LOWER)
        — both partners can't simultaneously beat the other.
    """
    if new_mkt.type not in _PARTNER_COMPARISON_TYPES:
        return None
    if new_mkt.tribute_a_id is None or new_mkt.tribute_b_id is None:
        return None

    new_pair = frozenset((new_mkt.tribute_a_id, new_mkt.tribute_b_id))
    new_is_score = new_mkt.type in _PARTNER_SCORE_TYPES
    category = "training score" if new_is_score else "placement"

    for m in existing_markets:
        if m.type not in _PARTNER_COMPARISON_TYPES:
            continue
        if m.tribute_a_id is None or m.tribute_b_id is None:
            continue
        if frozenset((m.tribute_a_id, m.tribute_b_id)) != new_pair:
            continue
        if (m.type in _PARTNER_SCORE_TYPES) != new_is_score:
            continue  # score and place markets don't conflict with each other

        if m.tribute_a_id == new_mkt.tribute_a_id:
            # Same tribute, opposite directions (one HIGHER, one LOWER)
            return (
                f"You can't parlay both a higher and lower district partner {category} "
                f"bet on the same tribute — they can never both be true."
            )
        else:
            # Swapped perspectives
            if m.type == new_mkt.type:
                # Same direction (A beats B AND B beats A) — impossible
                return (
                    f"You can't parlay both district partners each {category}ing higher "
                    f"(or lower) than the other — only one can come out ahead."
                )
            else:
                # Opposite direction from a swapped pair is the same statement:
                # "A scores higher than B" ≡ "B scores lower than A"
                return (
                    f"You can't parlay two equivalent district partner {category} "
                    f"bets — stating that one tribute beats the other is the same "
                    f"outcome regardless of which side you bet from."
                )

    return None


_FIELD_EXTREME_SCORE_TYPES = {"HIGHEST_TRAINING_SCORE", "LOWEST_TRAINING_SCORE"}


def _extreme_score_partner_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Block a field-extreme training-score bet from contradicting a partner bet.

    A tribute bet to receive the LOWEST training score in the whole field can't
    also be bet to score higher than their district partner — the lowest score in
    the field can't beat anyone, including a partner. Symmetrically, a tribute bet
    to receive the HIGHEST score can't also be bet to score lower than their
    partner."""

    def _check(extreme: Market, partner: Market) -> str | None:
        if extreme.type not in _FIELD_EXTREME_SCORE_TYPES or extreme.tribute_a_id is None:
            return None
        if partner.type not in _PARTNER_SCORE_TYPES:
            return None
        tid = extreme.tribute_a_id
        asserts_higher = (
            (partner.type == "PARTNER_SCORE_HIGHER" and partner.tribute_a_id == tid)
            or (partner.type == "PARTNER_SCORE_LOWER" and partner.tribute_b_id == tid)
        )
        asserts_lower = (
            (partner.type == "PARTNER_SCORE_LOWER" and partner.tribute_a_id == tid)
            or (partner.type == "PARTNER_SCORE_HIGHER" and partner.tribute_b_id == tid)
        )
        if extreme.type == "LOWEST_TRAINING_SCORE" and asserts_higher:
            return (
                "You can't parlay a tribute to get the lowest training score in the "
                "field and also bet them to score higher than their district partner "
                "— the lowest score can't beat anyone."
            )
        if extreme.type == "HIGHEST_TRAINING_SCORE" and asserts_lower:
            return (
                "You can't parlay a tribute to get the highest training score in the "
                "field and also bet them to score lower than their district partner "
                "— the highest score can't lose to anyone."
            )
        return None

    for m in existing_markets:
        conflict = _check(new_mkt, m) or _check(m, new_mkt)
        if conflict:
            return conflict
    return None


def _victor_conflict(existing_markets: list[Market], new_mkt: Market) -> str | None:
    """Allow at most one "who wins the Games" market per parlay.

    Individual-tribute, district, and alliance victor bets are all correlated —
    only one tribute wins, and that result determines the winning district and
    alliance at the same time. Stacking them (e.g. "D1 wins" + "D1M wins") just
    inflates the odds without making the slip meaningfully harder to hit, so any
    victor leg blocks adding another of any victor type.
    """
    if new_mkt.type not in _VICTOR_TYPES:
        return None
    if any(m.type in _VICTOR_TYPES for m in existing_markets):
        return (
            "You can't parlay more than one victor bet — only one tribute wins the "
            "Games, and that single result decides the winning tribute, district, "
            "and alliance together. Pick just one victor market per parlay."
        )
    return None


def _parlay_conflict(
    existing_markets: list[Market],
    new_mkt: Market,
    tribute_by_id: dict[int, "Tribute"] | None = None,
) -> str | None:
    """Single gate for every parlay leg-compatibility rule. Returns an error
    string if `new_mkt` can't legally share a parlay with `existing_markets`.
    `tribute_by_id` is optional context (tribute id -> Tribute) some rules need
    to resolve which district a market belongs to; pass one via
    tribute_lookup_for_markets() where practical, or omit to skip those rules."""
    return (
        _milestone_conflict(existing_markets, new_mkt)
        or _placement_conflict(existing_markets, new_mkt)
        or _placement_milestone_conflict(existing_markets, new_mkt)
        or _district_milestone_conflict(existing_markets, new_mkt)
        or _alliance_milestone_conflict(existing_markets, new_mkt)
        or _survival_conflict(existing_markets, new_mkt)
        or _training_score_conflict(existing_markets, new_mkt)
        or _district_score_conflict(existing_markets, new_mkt)
        or _district_score_vs_highest_conflict(existing_markets, new_mkt, tribute_by_id)
        or _partner_comparison_conflict(existing_markets, new_mkt)
        or _extreme_score_partner_conflict(existing_markets, new_mkt)
        or _victor_conflict(existing_markets, new_mkt)
    )


async def add_markets_to_pending_slip(
    db, guild_id: int, discord_id: int, candidate_markets: list[Market]
) -> tuple[int, int]:
    """Copy as many ``candidate_markets`` as possible into a user's pending
    parlay slip — used to seed a slip from a tail-board template/parlay so the
    member can edit it before submitting, instead of only being able to tail it
    as a fixed package. Skips markets already in the slip, past the leg cap, or
    that conflict with a leg already there. Returns (added, skipped) counts.
    Takes a plain AsyncSession so it works from either the bot's get_session()
    or the web's get_db()."""
    existing_legs = (await db.execute(
        select(PendingParlayLeg).where(
            PendingParlayLeg.guild_id == guild_id, PendingParlayLeg.user_id == discord_id,
        )
    )).scalars().all()
    existing_market_ids = {l.market_id for l in existing_legs}
    existing_markets = []
    for l in existing_legs:
        mkt = await db.get(Market, l.market_id)
        if mkt:
            existing_markets.append(mkt)

    tribute_by_id = await tribute_lookup_for_markets(db, existing_markets + candidate_markets)

    added = 0
    skipped = 0
    for mkt in candidate_markets:
        if mkt.id in existing_market_ids:
            skipped += 1
            continue
        if len(existing_market_ids) >= MAX_PARLAY_LEGS:
            skipped += 1
            continue
        if _parlay_conflict(existing_markets, mkt, tribute_by_id):
            skipped += 1
            continue
        db.add(PendingParlayLeg(guild_id=guild_id, user_id=discord_id, market_id=mkt.id))
        existing_markets.append(mkt)
        existing_market_ids.add(mkt.id)
        added += 1
    return added, skipped


async def _get_or_create_user(session, member: discord.Member, guild_id: int) -> User:
    result = await session.execute(
        select(User).where(User.guild_id == guild_id, User.discord_id == member.id)
    )
    u = result.scalar_one_or_none()
    if u is None:
        default_raw = await get_setting("default_chips")
        default = json.loads(default_raw) if default_raw else 1000
        u = User(guild_id=guild_id, discord_id=member.id, username=member.display_name, chips=default)
        session.add(u)
        await session.flush()
    else:
        u.username = member.display_name
    return u


# ── Cascading subject/market-type narrowing for /bet and /parlay add ──────────
# subject_type + subject + market_type are optional filters that only affect the
# autocomplete shown for market_id — they're read out of interaction.namespace by
# the market_id (and market_type/subject) autocomplete callbacks below, exactly
# like cashout_type narrows cashout_autocomplete. Everything stays skippable: a
# user can ignore all three and just free-text search market_id like before.

SUBJECT_TYPE_CHOICES = [
    app_commands.Choice(name="Tribute", value="tribute"),
    app_commands.Choice(name="District", value="district"),
    app_commands.Choice(name="Alliance", value="alliance"),
    app_commands.Choice(name="Game Prop", value="props"),
    app_commands.Choice(name="Custom", value="custom"),
]


def _market_matches_subject(
    m: "Market",
    subject_type: str | None,
    subject: str | None,
) -> bool:
    if not subject_type:
        return True
    if _type_section(m.type) != subject_type:
        return False
    if not subject:
        return True
    try:
        sid = int(subject)
    except (TypeError, ValueError):
        return True
    if subject_type == "tribute":
        return m.tribute_a_id == sid or m.tribute_b_id == sid
    if subject_type in ("district", "alliance"):
        return m.placement_num == sid
    return True


def _ns_subject_params(interaction: discord.Interaction) -> tuple[str | None, str | None, str | None]:
    ns = interaction.namespace
    raw_subject_type = getattr(ns, "subject_type", None)
    subject_type = raw_subject_type.value if isinstance(raw_subject_type, app_commands.Choice) else raw_subject_type
    return (
        subject_type or None,
        getattr(ns, "subject", None) or None,
        getattr(ns, "market_type", None) or None,
    )


async def market_subject_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Tributes / districts / alliances that currently have an OPEN market,
    scoped to whichever subject_type the user already picked."""
    subject_type, _, _ = _ns_subject_params(interaction)
    if subject_type not in ("tribute", "district", "alliance"):
        return []

    async with get_session() as session:
        result = await session.execute(select(Market).where(Market.status == "OPEN"))
        markets = result.scalars().all()

        if subject_type == "tribute":
            tribute_ids = {
                tid
                for m in markets
                if _type_section(m.type) == "tribute"
                for tid in (m.tribute_a_id, m.tribute_b_id)
                if tid is not None
            }
            if not tribute_ids:
                return []
            trib_result = await session.execute(
                select(Tribute).where(Tribute.id.in_(tribute_ids))
            )
            tributes = trib_result.scalars().all()
            choices = []
            for t in sorted(tributes, key=lambda t: (t.district, t.display_gender, t.name)):
                label = f"D{t.district}{t.display_gender} {t.name}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=str(t.id)))
            return choices[:25]

        if subject_type == "district":
            districts = {
                m.placement_num
                for m in markets
                if _type_section(m.type) == "district" and m.placement_num is not None
            }
            choices = []
            for d in sorted(districts):
                label = f"District {d}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label, value=str(d)))
            return choices[:25]

        # alliance
        alliance_ids = {
            m.placement_num
            for m in markets
            if _type_section(m.type) == "alliance" and m.placement_num is not None
        }
        if not alliance_ids:
            return []
        all_result = await session.execute(
            select(Alliance).where(Alliance.id.in_(alliance_ids))
        )
        choices = []
        for a in all_result.scalars().all():
            if current.lower() in a.name.lower():
                choices.append(app_commands.Choice(name=a.name[:100], value=str(a.id)))
        return choices[:25]


async def market_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Distinct market types among currently-OPEN markets, scoped to whichever
    subject_type/subject the user already picked."""
    subject_type, subject, _ = _ns_subject_params(interaction)
    async with get_session() as session:
        result = await session.execute(select(Market).where(Market.status == "OPEN"))
        markets = result.scalars().all()
        tmpl_result = await session.execute(select(MarketTemplate))
        custom_labels = {f"CUSTOM_{t.id}": t.name for t in tmpl_result.scalars().all()}

    type_labels = {**_TYPE_LABELS, **custom_labels}
    seen: dict[str, None] = {}
    for m in markets:
        if not _market_matches_subject(m, subject_type, subject):
            continue
        seen.setdefault(m.type, None)

    choices = []
    for t in sorted(seen, key=lambda x: _TYPE_ORDER.get(x, 99)):
        label = type_labels.get(t, t)
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=t))
    return choices[:25]


async def open_market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    subject_type, subject, market_type = _ns_subject_params(interaction)
    async with get_session() as session:
        result = await session.execute(
            select(Market).where(Market.status == "OPEN").order_by(Market.id)
        )
        markets = result.scalars().all()
    choices = []
    for m in markets:
        if not _market_matches_subject(m, subject_type, subject):
            continue
        if market_type and m.type != market_type:
            continue
        if current.lower() in m.label.lower():
            choices.append(app_commands.Choice(name=m.label[:100], value=str(m.id)))
    return choices[:25]


async def parlay_market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Open markets that can still be legally added to the caller's pending
    parlay slip. Markets already on the slip — or that would conflict with a leg
    already there (e.g. the opposing side of a bet already taken) — are hidden."""
    subject_type, subject, market_type = _ns_subject_params(interaction)
    uid = interaction.user.id
    gid = current_guild_id()
    async with get_session() as session:
        result = await session.execute(
            select(Market).where(Market.status == "OPEN").order_by(Market.id)
        )
        markets = list(result.scalars().all())
        legs_result = await session.execute(
            select(PendingParlayLeg).where(
                PendingParlayLeg.guild_id == gid,
                PendingParlayLeg.user_id == uid,
            )
        )
        leg_market_ids = {leg.market_id for leg in legs_result.scalars().all()}
        existing_mkts = []
        if leg_market_ids:
            slip_result = await session.execute(
                select(Market).where(Market.id.in_(leg_market_ids))
            )
            existing_mkts = list(slip_result.scalars().all())

        tribute_by_id = await tribute_lookup_for_markets(session, existing_mkts + markets)

    choices = []
    for m in markets:
        if m.id in leg_market_ids:
            continue
        if not _market_matches_subject(m, subject_type, subject):
            continue
        if market_type and m.type != market_type:
            continue
        if current.lower() not in m.label.lower():
            continue
        if _parlay_conflict(existing_mkts, m, tribute_by_id):
            continue
        choices.append(app_commands.Choice(name=m.label[:100], value=str(m.id)))
        if len(choices) >= 25:
            break
    return choices


async def user_bet_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    gid = current_guild_id()
    async with get_session() as session:
        result = await session.execute(
            select(Bet).where(
                Bet.guild_id == gid, Bet.user_id == uid,
                Bet.status == "PENDING", Bet.parlay_id == None,
            )
        )
        bets = result.scalars().all()
    choices = []
    for b in bets:
        async with get_session() as session:
            mkt = await session.get(Market, b.market_id)
        label = f"#{b.id} {mkt.label if mkt else '?'} ({fmt_odds(b.odds_at_placement)})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(b.id)))
    return choices[:25]


async def user_parlay_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    async with get_session() as session:
        result = await session.execute(
            select(Parlay).where(Parlay.user_id == uid, Parlay.status == "PENDING")
        )
        parlays = result.scalars().all()
    choices = []
    for p in parlays:
        label = f"Parlay #{p.id} — {fmt_chips(p.total_wager)} wager"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(p.id)))
    return choices[:25]


async def user_public_parlay_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    gid = current_guild_id()
    async with get_session() as session:
        result = await session.execute(
            select(Parlay).where(
                Parlay.guild_id == gid,
                Parlay.user_id == uid,
                Parlay.status == "PENDING",
                Parlay.is_public == True,  # noqa: E712
            ).order_by(Parlay.placed_at.desc())
        )
        parlays = result.scalars().all()
    choices = []
    for p in parlays:
        label = f"Parlay #{p.id} — {fmt_chips(p.total_wager)} wager"
        if not current or current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(p.id)))
    return choices[:25]


async def cashout_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    uid = interaction.user.id
    gid = current_guild_id()
    cashout_type = getattr(interaction.namespace, "cashout_type", "BET")

    if cashout_type == "PARLAY":
        async with get_session() as session:
            result = await session.execute(
                select(Parlay).where(
                    Parlay.guild_id == gid, Parlay.user_id == uid,
                    Parlay.status == "PENDING",
                )
            )
            parlays = result.scalars().all()
        choices = []
        for p in parlays:
            label = f"Parlay #{p.id} — {fmt_chips(p.total_wager)} wager"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=str(p.id)))
        return choices[:25]

    async with get_session() as session:
        result = await session.execute(
            select(Bet, Market)
            .join(Market, Bet.market_id == Market.id)
            .where(
                Bet.guild_id == gid, Bet.user_id == uid,
                Bet.status == "PENDING", Bet.parlay_id == None,
            )
        )
        rows = result.all()
    choices = []
    for bet, mkt in rows:
        if current.lower() in mkt.label.lower():
            choices.append(app_commands.Choice(name=mkt.label[:100], value=str(bet.id)))
    return choices[:25]


async def _get_restriction_msg(session, user_id: int, mkt: Market, guild_id: int = 0) -> str | None:
    """Return an error string if the user is restricted from betting on this market."""
    result = await session.execute(
        select(BettingRestriction).where(
            BettingRestriction.guild_id == guild_id,
            BettingRestriction.discord_user_id == user_id,
        )
    )
    restrictions = result.scalars().all()
    if not restrictions:
        return None

    for r in restrictions:
        if r.restriction_type == "ALL":
            return "You are not permitted to place bets in this server."

    blocked_districts = {r.district for r in restrictions if r.restriction_type == "DISTRICT"}
    blocked_tribute_ids = {r.tribute_id for r in restrictions if r.restriction_type == "TRIBUTE"}

    if blocked_tribute_ids and (mkt.tribute_a_id in blocked_tribute_ids or mkt.tribute_b_id in blocked_tribute_ids):
        return "You are not permitted to bet on markets involving that tribute."

    if blocked_districts:
        for tid in (mkt.tribute_a_id, mkt.tribute_b_id):
            if tid is not None:
                tribute = await session.get(Tribute, tid)
                if tribute and tribute.district in blocked_districts:
                    return f"You are not permitted to bet on markets involving District {tribute.district} tributes."

        if mkt.type in _DISTRICT_MARKET_TYPES and mkt.placement_num in blocked_districts:
            return f"You are not permitted to bet on markets involving District {mkt.placement_num}."

        if mkt.type in _ALLIANCE_MARKET_TYPES:
            members_result = await session.execute(
                select(Tribute).where(Tribute.alliance_id == mkt.placement_num)
            )
            for member in members_result.scalars().all():
                if member.district in blocked_districts:
                    return (
                        f"You are not permitted to bet on markets involving "
                        f"District {member.district} tributes."
                    )

    return None


# ── Parlay tailing ────────────────────────────────────────────────────────────
# A "tailable" parlay is either an admin/auto ParlayTemplate or another member's
# public, still-pending Parlay. In both cases the legs are live markets, so the
# odds shown and locked in are always the markets' *current* odds — never the
# odds from when the template/parlay was first built.


async def _gather_tailable(session, guild_id: int) -> tuple[list[dict], list[dict]]:
    """Return ``(featured, member)`` lists of tailable-parlay entries.

    Each entry is a dict with ``key``, ``name``, ``sub``, ``tag``,
    ``market_ids``, ``labels`` and ``odds_list``. Only parlays whose every leg
    market is still OPEN (and that still have at least 2 legs) are included, so
    everything returned is actually tailable at live odds.
    """
    featured: list[dict] = []
    member: list[dict] = []

    tpl_result = await session.execute(
        select(ParlayTemplate)
        .where(ParlayTemplate.active == True)  # noqa: E712
        .order_by(ParlayTemplate.source.desc(), ParlayTemplate.id)
    )
    for tpl in tpl_result.scalars().all():
        legs_result = await session.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == tpl.id)
            .order_by(ParlayTemplateLeg.sort_order)
        )
        mkts: list[Market] = []
        ok = True
        for leg in legs_result.scalars().all():
            m = await session.get(Market, leg.market_id)
            if not m or m.status != "OPEN":
                ok = False
                break
            mkts.append(m)
        if not ok or len(mkts) < 2:
            continue
        tag = tpl.difficulty or tpl.source
        if tag == "ADMIN":
            tag = "GAMEMAKER"

        featured.append({
            "key": f"T{tpl.id}",
            "name": tpl.name,
            "sub": tpl.description or "",
            "tag": tag,
            "market_ids": [m.id for m in mkts],
            "labels": [m.label for m in mkts],
            "odds_list": [m.odds for m in mkts],
            "owner_id": None,
            "source_parlay_id": None,
        })

    p_result = await session.execute(
        select(Parlay)
        .where(
            Parlay.guild_id == guild_id,
            Parlay.status == "PENDING",
            Parlay.is_public == True,  # noqa: E712
        )
        .order_by(Parlay.placed_at.desc())
    )
    for parlay in p_result.scalars().all():
        leg_result = await session.execute(select(Bet).where(Bet.parlay_id == parlay.id))
        mkts = []
        ok = True
        for b in leg_result.scalars().all():
            m = await session.get(Market, b.market_id)
            if not m or m.status != "OPEN":
                ok = False
                break
            mkts.append(m)
        if not ok or len(mkts) < 2:
            continue
        owner_result = await session.execute(
            select(User).where(User.guild_id == guild_id, User.discord_id == parlay.user_id)
        )
        owner = owner_result.scalar_one_or_none()
        default_name = f"{owner.username if owner else 'Member'}'s Parlay #{parlay.id}"
        member.append({
            "key": f"P{parlay.id}",
            "name": parlay.name or default_name,
            "sub": f"Tailing {owner.username}'s {len(mkts)}-leg parlay" if owner else "",
            "tag": "MEMBER",
            "market_ids": [m.id for m in mkts],
            "labels": [m.label for m in mkts],
            "odds_list": [m.odds for m in mkts],
            "owner_id": parlay.user_id,
            "source_parlay_id": parlay.id,
        })
        if len(member) >= 15:
            break

    return featured, member


async def _validate_tail_markets(session, user_id: int, market_ids: list[int], guild_id: int = 0) -> tuple[str | None, list[Market]]:
    """Load and validate the legs of a parlay the user is about to tail."""
    markets: list[Market] = []
    for mid in market_ids:
        m = await session.get(Market, mid)
        if not m or m.status != "OPEN":
            return "One of these legs is no longer open. Try a different parlay.", []
        markets.append(m)
    if len(markets) < 2:
        return "This parlay no longer has enough open legs to tail.", []
    tribute_by_id = await tribute_lookup_for_markets(session, markets)
    for i, mkt in enumerate(markets):
        conflict = _parlay_conflict(markets[:i], mkt, tribute_by_id)
        if conflict:
            return conflict, []
        restriction = await _get_restriction_msg(session, user_id, mkt, guild_id)
        if restriction:
            return restriction, []
    return None, markets


async def _tail_load_slip(session, user: User, market_ids: list[int]) -> tuple[str | None, int]:
    """Replace the user's pending slip with the tailed parlay's legs."""
    err, markets = await _validate_tail_markets(session, user.discord_id, market_ids, user.guild_id)
    if err:
        return err, 0
    existing = await session.execute(
        select(PendingParlayLeg).where(
            PendingParlayLeg.guild_id == user.guild_id,
            PendingParlayLeg.user_id == user.discord_id,
        )
    )
    for leg in existing.scalars().all():
        await session.delete(leg)
    for mkt in markets:
        session.add(PendingParlayLeg(
            guild_id=user.guild_id, user_id=user.discord_id, market_id=mkt.id,
        ))
    return None, len(markets)


async def _tail_submit(
    session,
    user: User,
    market_ids: list[int],
    wager: int,
    tailed_from_user_id: int | None = None,
    tailed_from_parlay_id: int | None = None,
) -> tuple[str | None, dict | None]:
    """Build and commit a parlay from ``market_ids`` at current odds.

    ``tailed_from_user_id``/``tailed_from_parlay_id`` record provenance when this
    parlay was built off another member's board listing (never a template), so the
    original poster can be notified when it resolves — see `_check_parlay`.
    """
    if await _betting_paused():
        return BETTING_PAUSED_MSG, None
    if user.chips < wager:
        return f"Insufficient chips. You have **{fmt_chips(user.chips)}**.", None
    err, markets = await _validate_tail_markets(session, user.discord_id, market_ids, user.guild_id)
    if err:
        return err, None

    all_odds = [m.odds for m in markets]
    total_payout = parlay_payout(wager, all_odds)
    if total_payout > PARLAY_PAYOUT_CAP:
        return (
            f"Parlay payout cannot exceed **{fmt_chips(PARLAY_PAYOUT_CAP)}**. "
            "Reduce your wager or remove legs.",
            None,
        )

    user.chips -= wager
    user.total_wagered += wager

    # Tailed copies are private by default so the board isn't flooded with clones.
    if tailed_from_user_id == user.discord_id:
        tailed_from_user_id = None
        tailed_from_parlay_id = None
    parlay = Parlay(
        guild_id=user.guild_id,
        user_id=user.discord_id,
        total_wager=wager,
        total_payout=total_payout,
        is_public=False,
        tailed_from_user_id=tailed_from_user_id,
        tailed_from_parlay_id=tailed_from_parlay_id,
    )
    session.add(parlay)
    await session.flush()

    leg_data: list[ParlayLegData] = []
    for i, mkt in enumerate(markets, 1):
        session.add(Bet(
            guild_id=user.guild_id,
            user_id=user.discord_id,
            parlay_id=parlay.id,
            market_id=mkt.id,
            wager=wager,
            odds_at_placement=mkt.odds,
            payout_if_win=total_payout,
        ))
        leg_data.append(ParlayLegData(leg_num=i, market_label=mkt.label, odds=mkt.odds))

    return None, {
        "parlay_id": parlay.id,
        "leg_data": leg_data,
        "payout": total_payout,
        "balance": user.chips,
        "wager": wager,
    }


class TailWagerModal(discord.ui.Modal, title="Tail this parlay"):
    def __init__(self, entry: dict) -> None:
        super().__init__()
        self.entry = entry
        payout_100 = parlay_payout(100, entry["odds_list"])
        self.wager_input = discord.ui.TextInput(
            label="Wager (chips)",
            placeholder=f"e.g. 100 (pays {fmt_chips(payout_100)})",
            required=True,
            max_length=7,
        )
        self.add_item(self.wager_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.wager_input.value.strip().replace(",", "")
        if not raw.isdigit() or int(raw) <= 0:
            await interaction.response.send_message(
                "Enter a positive whole number of chips.", ephemeral=True
            )
            return
        wager = int(raw)
        if wager > 500_000:
            await interaction.response.send_message(
                "Maximum wager is 500,000 chips.", ephemeral=True
            )
            return
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            err, res = await _tail_submit(
                session, user, self.entry["market_ids"], wager,
                tailed_from_user_id=self.entry.get("owner_id"),
                tailed_from_parlay_id=self.entry.get("source_parlay_id"),
            )
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return

        buf = await render_async(
            render_parlay_slip, res["leg_data"], res["wager"], res["payout"], True
        )
        f = buf_to_discord_file(buf, f"parlay_{res['parlay_id']}.png")
        await interaction.followup.send(
            f"**Tailed!** Parlay #{res['parlay_id']} submitted — wagered "
            f"**{fmt_chips(res['wager'])}** for a potential **{fmt_chips(res['payout'])}**.\n"
            f"Remaining balance: {fmt_chips(res['balance'])}",
            file=f,
            ephemeral=True,
        )


class TailView(discord.ui.View):
    """Board for browsing and tailing featured + member parlays at live odds."""

    def __init__(self, featured: list[dict], member: list[dict]) -> None:
        super().__init__(timeout=300)
        self.featured = featured
        self.member = member
        self.entries = (featured + member)[:25]
        self.by_key = {e["key"]: e for e in self.entries}
        self.selected: str | None = None
        self.message: discord.Message | None = None

        # The board image shows at most per_page parlays; the rest live on
        # further pages reachable via the ◀ / ▶ buttons below.
        self.per_page = TAIL_BOARD_PER_PAGE
        self.page = 0
        self.total_pages = max(1, -(-len(self.featured + self.member) // self.per_page))

        options = []
        for e in self.entries:
            is_featured = e["key"].startswith("T")
            combined = combined_american(e["odds_list"])
            options.append(discord.SelectOption(
                label=e["name"][:100],
                value=e["key"],
                description=(
                    f"{fmt_odds(combined)} · Pays {fmt_chips(parlay_payout(100, e['odds_list']))} "
                    f"per 100 · {len(e['market_ids'])} legs"
                )[:100],
                emoji="⭐" if is_featured else "👤",
            ))
        self.select = discord.ui.Select(
            placeholder="Choose a parlay to tail…", options=options, row=0
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.btn_slip = discord.ui.Button(
            label="Add to Slip", emoji="📋",
            style=discord.ButtonStyle.secondary, row=1, disabled=True,
        )
        self.btn_slip.callback = self._on_add_slip
        self.add_item(self.btn_slip)

        self.btn_tail = discord.ui.Button(
            label="Tail & Bet", emoji="🎯",
            style=discord.ButtonStyle.success, row=1, disabled=True,
        )
        self.btn_tail.callback = self._on_tail
        self.add_item(self.btn_tail)

        # Page navigation — only shown when the board spans more than one page.
        self.btn_prev = discord.ui.Button(
            label="Prev", emoji="◀",
            style=discord.ButtonStyle.secondary, row=2, disabled=True,
        )
        self.btn_prev.callback = self._on_prev_page
        self.btn_next = discord.ui.Button(
            label="Next Page", emoji="▶",
            style=discord.ButtonStyle.secondary, row=2,
        )
        self.btn_next.callback = self._on_next_page
        if self.total_pages > 1:
            self.add_item(self.btn_prev)
            self.add_item(self.btn_next)

    def _update_page_buttons(self) -> None:
        self.btn_prev.disabled = self.page <= 0
        self.btn_next.disabled = self.page >= self.total_pages - 1

    async def _show_page(self, interaction: discord.Interaction, new_page: int) -> None:
        """Re-render the board at ``new_page`` and swap it back into the message.

        Paging returns to the board overview (from any detail view) but keeps the
        current selection intact — the dropdown lists every parlay regardless of
        which page the image is showing.
        """
        self.page = max(0, min(new_page, self.total_pages - 1))
        self._update_page_buttons()
        buf = await render_async(
            render_tail_board, self.featured, self.member, self.page, self.per_page
        )
        f = buf_to_discord_file(buf, f"tail_board_{self.page + 1}.png")
        try:
            await interaction.response.edit_message(attachments=[f], view=self)
        except discord.NotFound:
            pass

    async def _on_prev_page(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page - 1)

    async def _on_next_page(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page + 1)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected = self.select.values[0]
        self.btn_slip.disabled = False
        self.btn_tail.disabled = False
        for opt in self.select.options:
            opt.default = (opt.value == self.selected)

        entry = self.by_key[self.selected]
        buf = await render_async(render_tail_detail, entry)
        f = buf_to_discord_file(buf, f"tail_{self.selected}.png")

        try:
            # When selecting a parlay, we switch from the board PNG to the detail PNG.
            # We clear the previous attachments and add the new focus slip.
            await interaction.response.edit_message(attachments=[f], view=self)
        except discord.NotFound:
            pass

    async def _on_add_slip(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            await interaction.response.send_message("Pick a parlay first.", ephemeral=True)
            return
        entry = self.by_key[self.selected]
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            err, n = await _tail_load_slip(session, user, entry["market_ids"])
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await interaction.followup.send(
            f"Loaded **{n}** legs from **{entry['name']}** onto your slip (previous slip cleared).\n"
            "Use `/parlay view` to preview or `/parlay submit` to lock in at live odds.\n"
            "💡 To keep your parlay private (off the tail board), use `/parlay submit public:False`.",
            ephemeral=True,
        )

    async def _on_tail(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            await interaction.response.send_message("Pick a parlay first.", ephemeral=True)
            return
        await interaction.response.send_modal(TailWagerModal(self.by_key[self.selected]))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


async def _resolve_cashout_target(session, user: User, cashout_type: str, cid: int):
    """Look up a bet/parlay cashout target and compute eligibility + amount.

    Used for both the preview (read-only) and the confirm step (right before
    mutating), so the precedence/eligibility check only lives in one place.
    Returns ``(ok, error, amount, label, target)`` — ``target`` is the live Bet
    or Parlay ORM object attached to ``session`` when ``ok``, else ``None``.
    """
    global_allowed_raw = await get_setting("cashout_allowed")
    global_allowed = json.loads(global_allowed_raw) if global_allowed_raw else False
    global_rate_raw = await get_setting("cashout_rate")
    global_rate = json.loads(global_rate_raw) if global_rate_raw else 0.65

    if cashout_type == "BET":
        b = await session.get(Bet, cid)
        if not b or b.user_id != user.discord_id or b.guild_id != user.guild_id:
            return False, "Bet not found.", 0, None, None
        if b.status != "PENDING" or b.parlay_id is not None:
            return False, "You can only cash out pending straight bets.", 0, None, None

        mkt = await session.get(Market, b.market_id)
        by_type_raw = await get_setting("cashout_by_type")
        cashout_by_type: dict = json.loads(by_type_raw) if by_type_raw else {}
        type_override = cashout_by_type.get(mkt.type) if mkt else None
        allowed, amount = resolve_cashout(
            wager=b.wager, payout_if_win=b.payout_if_win,
            global_allowed=global_allowed, global_rate=global_rate,
            item_allowed=mkt.cashout_allowed if mkt else None,
            item_rate=mkt.cashout_rate if mkt else None,
            type_allowed=type_override["allowed"] if type_override else None,
            type_rate=type_override.get("rate") if type_override else None,
        )
        label = mkt.label if mkt else "Unknown"
        if not allowed:
            return False, "Early cashout is not available for this bet.", 0, label, None
        return True, None, amount, label, b

    else:  # PARLAY
        p = await session.get(Parlay, cid)
        if not p or p.user_id != user.discord_id or p.guild_id != user.guild_id:
            return False, "Parlay not found.", 0, None, None
        if p.status != "PENDING":
            return False, "That parlay is no longer pending.", 0, None, None

        allowed, amount = resolve_cashout(
            wager=p.total_wager, payout_if_win=p.total_payout,
            global_allowed=global_allowed, global_rate=global_rate,
            item_allowed=p.cashout_allowed, item_rate=p.cashout_rate,
        )
        label = f"Parlay #{cid}"
        if not allowed:
            return False, "Early cashout is not available.", 0, label, None
        return True, None, amount, label, p


class CashoutConfirmView(discord.ui.View):
    """Shows the computed cashout amount and only mutates state on Confirm."""

    def __init__(self, cashout_type: str, cid: int, user_discord_id: int) -> None:
        super().__init__(timeout=60)
        self.cashout_type = cashout_type
        self.cid = cid
        self.user_discord_id = user_discord_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_discord_id:
            await interaction.response.send_message(
                "This isn't your cashout to confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm Cashout", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            ok, error, amount, label, target = await _resolve_cashout_target(
                session, user, self.cashout_type, self.cid
            )
            if not ok:
                await interaction.response.send_message(error, ephemeral=True)
                return

            target.status = "CASHED_OUT"
            target.cashout_amount = amount
            user.chips += amount
            if self.cashout_type == "PARLAY":
                legs_result = await session.execute(
                    select(Bet).where(Bet.parlay_id == target.id, Bet.status == "PENDING")
                )
                for leg in legs_result.scalars().all():
                    leg.status = "CASHED_OUT"
                    leg.cashout_amount = amount
            new_balance = user.chips

        embed = discord.Embed(title="Cashed Out!", color=0x4CAF50)
        embed.add_field(name="Market/Parlay", value=label, inline=False)
        embed.add_field(name="Cashout Amount", value=fmt_chips(amount))
        embed.add_field(name="New Balance", value=fmt_chips(new_balance))
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content="Cashout cancelled.", embed=None, view=self)
        except discord.NotFound:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class BettingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, ValueError):
            log.warning(f"Betting command input error: {original}")
            msg = "Invalid selection. Please pick an option from the autocomplete list."
        else:
            log.error(f"Betting command error: {error}", exc_info=error)
            msg = "An error occurred. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass

    # ── /bet ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="bet", description="Place a straight single bet on an open market")
    @app_commands.describe(
        subject_type="Narrow down by subject (optional — skip straight to market_id to search everything)",
        subject="Specific tribute/district/alliance to narrow further (optional)",
        market_type="Market category to narrow further (optional)",
        market_id="Market to bet on",
        amount="Amount of chips to wager",
    )
    @app_commands.choices(subject_type=SUBJECT_TYPE_CHOICES)
    @app_commands.autocomplete(
        subject=market_subject_autocomplete,
        market_type=market_type_autocomplete,
        market_id=open_market_autocomplete,
    )
    async def bet(
        self,
        interaction: discord.Interaction,
        subject_type: app_commands.Choice[str] | None = None,
        subject: str | None = None,
        market_type: str | None = None,
        market_id: str | None = None,
        amount: app_commands.Range[int, 1, 500000] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if await _betting_paused():
            await interaction.followup.send(BETTING_PAUSED_MSG, ephemeral=True)
            return

        if market_id is None:
            await interaction.followup.send(
                "Pick a market from the `market_id` autocomplete list — use `subject_type` / "
                "`subject` / `market_type` first to narrow it down, or just start typing a "
                "market name directly to search everything.",
                ephemeral=True,
            )
            return
        if amount is None:
            await interaction.followup.send("Specify an `amount` of chips to wager.", ephemeral=True)
            return

        mid = _parse_id(market_id)
        if mid is None:
            await interaction.followup.send(
                "Invalid market chosen. Please pick a market from the autocomplete list.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            mkt = await session.get(Market, mid)
            if not mkt or mkt.status != "OPEN":
                await interaction.followup.send("That market is not open for betting.", ephemeral=True)
                return

            restriction = await _get_restriction_msg(session, interaction.user.id, mkt, current_guild_id())
            if restriction:
                await interaction.followup.send(restriction, ephemeral=True)
                return

            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            if user.chips < amount:
                await interaction.followup.send(
                    f"Insufficient chips. You have **{fmt_chips(user.chips)}** but need **{fmt_chips(amount)}**.",
                    ephemeral=True,
                )
                return

            payout = straight_payout(amount, mkt.odds)
            user.chips -= amount
            user.total_wagered += amount

            b = Bet(
                guild_id=user.guild_id,
                user_id=user.discord_id,
                market_id=mkt.id,
                wager=amount,
                odds_at_placement=mkt.odds,
                payout_if_win=payout,
            )
            session.add(b)
            await session.flush()
            bet_id = b.id
            label = mkt.label
            odds = mkt.odds
            new_balance = user.chips

        embed = discord.Embed(title="Bet Placed!", color=0x4CAF50)
        embed.add_field(name="Market", value=label, inline=False)
        embed.add_field(name="Wager", value=fmt_chips(amount))
        embed.add_field(name="Odds", value=fmt_odds(odds))
        embed.add_field(name="Potential Payout", value=fmt_chips(payout))
        embed.add_field(name="Bet ID", value=f"#{bet_id}")
        embed.set_footer(text=f"Remaining balance: {fmt_chips(new_balance)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /parlay ───────────────────────────────────────────────────────────────

    parlay_group = app_commands.Group(name="parlay", description="Build and submit a parlay bet")

    @parlay_group.command(name="add", description="Add a market to your pending parlay slip")
    @app_commands.describe(
        subject_type="Narrow down by subject (optional — skip straight to market_id to search everything)",
        subject="Specific tribute/district/alliance to narrow further (optional)",
        market_type="Market category to narrow further (optional)",
        market_id="Market to add to your parlay slip",
    )
    @app_commands.choices(subject_type=SUBJECT_TYPE_CHOICES)
    @app_commands.autocomplete(
        subject=market_subject_autocomplete,
        market_type=market_type_autocomplete,
        market_id=parlay_market_autocomplete,
    )
    async def parlay_add(
        self,
        interaction: discord.Interaction,
        subject_type: app_commands.Choice[str] | None = None,
        subject: str | None = None,
        market_type: str | None = None,
        market_id: str | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if market_id is None:
            await interaction.followup.send(
                "Pick a market from the `market_id` autocomplete list — use `subject_type` / "
                "`subject` / `market_type` first to narrow it down, or just start typing a "
                "market name directly to search everything.",
                ephemeral=True,
            )
            return

        mid = _parse_id(market_id)
        if mid is None:
            await interaction.followup.send(
                "Invalid market chosen. Please pick a market from the autocomplete list.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            mkt = await session.get(Market, mid)
            if not mkt or mkt.status != "OPEN":
                await interaction.followup.send("That market is not open.", ephemeral=True)
                return

            restriction = await _get_restriction_msg(session, interaction.user.id, mkt, current_guild_id())
            if restriction:
                await interaction.followup.send(restriction, ephemeral=True)
                return

            user = await _get_or_create_user(session, interaction.user, current_guild_id())

            dup = await session.execute(
                select(PendingParlayLeg).where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                    PendingParlayLeg.market_id == mkt.id,
                )
            )
            if dup.scalar_one_or_none():
                await interaction.followup.send("That market is already on your slip.", ephemeral=True)
                return

            existing_legs_result = await session.execute(
                select(PendingParlayLeg).where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                )
            )
            existing_legs = existing_legs_result.scalars().all()
            if len(existing_legs) >= MAX_PARLAY_LEGS:
                await interaction.followup.send(
                    f"Maximum {MAX_PARLAY_LEGS} legs per parlay.", ephemeral=True
                )
                return

            # Leg-compatibility validation against the legs already on the slip
            existing_mkts: list[Market] = []
            for leg in existing_legs:
                leg_mkt = await session.get(Market, leg.market_id)
                if leg_mkt:
                    existing_mkts.append(leg_mkt)
            tribute_by_id = await tribute_lookup_for_markets(session, existing_mkts + [mkt])
            conflict = _parlay_conflict(existing_mkts, mkt, tribute_by_id)
            if conflict:
                await interaction.followup.send(conflict, ephemeral=True)
                return

            session.add(PendingParlayLeg(
                guild_id=user.guild_id, user_id=user.discord_id, market_id=mkt.id,
            ))
            mkt_label = mkt.label
            mkt_odds = mkt.odds

        await interaction.followup.send(
            f"Added **{mkt_label}** ({fmt_odds(mkt_odds)}) to your parlay slip.\n"
            "Use `/parlay view` to preview or `/parlay submit` to lock in.",
            ephemeral=True,
        )

    @parlay_group.command(name="view", description="Preview your current parlay slip")
    async def parlay_view(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                )
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if not legs:
                await interaction.followup.send(
                    "Your parlay slip is empty. Use `/parlay add` to add markets.", ephemeral=True
                )
                return

            leg_data: list[ParlayLegData] = []
            for i, leg in enumerate(legs, 1):
                mkt = await session.get(Market, leg.market_id)
                if mkt:
                    leg_data.append(ParlayLegData(
                        leg_num=i,
                        market_label=mkt.label,
                        odds=mkt.odds,
                    ))

        if not leg_data:
            await interaction.followup.send("Could not load parlay data.", ephemeral=True)
            return

        preview_wager = 100
        preview_payout = parlay_payout(preview_wager, [l.odds for l in leg_data])
        combined = combined_american([l.odds for l in leg_data])
        buf = await render_async(render_parlay_slip, leg_data, preview_wager, preview_payout, False)
        f = buf_to_discord_file(buf, "parlay_slip.png")
        await interaction.followup.send(
            f"Your parlay slip ({len(leg_data)} legs). "
            f"Odds: **{fmt_odds_with_mult(combined)}**. "
            f"100 chips pays **{fmt_chips(preview_payout)}**. "
            f"Use `/parlay submit` with your wager to lock in.",
            file=f,
            ephemeral=True,
        )

    @parlay_group.command(name="submit", description="Submit your parlay with a wager amount")
    @app_commands.describe(
        wager="Amount of chips to wager on this parlay",
        public="List this parlay on the tailing board for others to copy (default: yes)",
        name="Custom title shown on the tail board (default: \"{you}'s Parlay #{id}\")",
    )
    async def parlay_submit(
        self,
        interaction: discord.Interaction,
        wager: app_commands.Range[int, 1, 500000],
        public: bool = True,
        name: app_commands.Range[str, 1, 80] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if await _betting_paused():
            await interaction.followup.send(BETTING_PAUSED_MSG, ephemeral=True)
            return

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            if user.chips < wager:
                await interaction.followup.send(
                    f"Insufficient chips. You have **{fmt_chips(user.chips)}**.", ephemeral=True
                )
                return

            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                )
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if len(legs) < 2:
                await interaction.followup.send(
                    "A parlay requires at least 2 legs. Use `/parlay add` to add more.", ephemeral=True
                )
                return

            markets: list[Market] = []
            for leg in legs:
                mkt = await session.get(Market, leg.market_id)
                if not mkt or mkt.status != "OPEN":
                    await interaction.followup.send(
                        f"Market '{mkt.label if mkt else leg.market_id}' is no longer open. Remove it and resubmit.",
                        ephemeral=True,
                    )
                    return
                markets.append(mkt)

            # Final leg-compatibility validation pass before committing
            tribute_by_id = await tribute_lookup_for_markets(session, markets)
            for i, mkt in enumerate(markets):
                conflict = _parlay_conflict(markets[:i], mkt, tribute_by_id)
                if conflict:
                    await interaction.followup.send(conflict, ephemeral=True)
                    return

            all_odds = [m.odds for m in markets]
            total_payout = parlay_payout(wager, all_odds)
            if total_payout > PARLAY_PAYOUT_CAP:
                return (
                    f"Parlay payout cannot exceed **{fmt_chips(PARLAY_PAYOUT_CAP)}**. "
                    "Reduce your wager or remove legs.",
                    None,
                )

            user.chips -= wager
            user.total_wagered += wager

            parlay = Parlay(
                guild_id=user.guild_id,
                user_id=user.discord_id,
                name=name,
                total_wager=wager,
                total_payout=total_payout,
                is_public=public,
            )
            session.add(parlay)
            await session.flush()

            leg_data: list[ParlayLegData] = []
            for i, (leg, mkt) in enumerate(zip(legs, markets), 1):
                b = Bet(
                    guild_id=user.guild_id,
                    user_id=user.discord_id,
                    parlay_id=parlay.id,
                    market_id=mkt.id,
                    wager=wager,
                    odds_at_placement=mkt.odds,
                    payout_if_win=total_payout,
                )
                session.add(b)
                leg_data.append(ParlayLegData(leg_num=i, market_label=mkt.label, odds=mkt.odds))
                await session.delete(leg)

            parlay_id = parlay.id
            new_balance = user.chips

        buf = await render_async(render_parlay_slip, leg_data, wager, total_payout, True)
        f = buf_to_discord_file(buf, f"parlay_{parlay_id}.png")
        listed = (
            "📣 Listed on the tailing board for others to copy."
            if public else "🔒 Kept private — not listed for tailing."
        )
        await interaction.followup.send(
            f"**Parlay #{parlay_id} submitted!** Wagered **{fmt_chips(wager)}** for a potential **{fmt_chips(total_payout)}**.\n"
            f"Remaining balance: {fmt_chips(new_balance)}\n{listed}",
            file=f,
            ephemeral=True,
        )

    @parlay_group.command(name="remove", description="Remove a leg from your pending parlay slip by position")
    @app_commands.describe(leg_number="Leg number to remove (see /parlay view)")
    async def parlay_remove(
        self,
        interaction: discord.Interaction,
        leg_number: app_commands.Range[int, 1, 10],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            legs_result = await session.execute(
                select(PendingParlayLeg)
                .where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                )
                .order_by(PendingParlayLeg.added_at)
            )
            legs = legs_result.scalars().all()

            if leg_number > len(legs):
                await interaction.followup.send(
                    f"You only have {len(legs)} leg(s) on your slip.", ephemeral=True
                )
                return

            leg_to_remove = legs[leg_number - 1]
            mkt = await session.get(Market, leg_to_remove.market_id)
            label = mkt.label if mkt else "Unknown market"
            await session.delete(leg_to_remove)

        await interaction.followup.send(
            f"Removed leg {leg_number} (**{label}**) from your slip.", ephemeral=True
        )

    @parlay_group.command(name="clear", description="Clear your entire pending parlay slip")
    async def parlay_clear(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            legs_result = await session.execute(
                select(PendingParlayLeg).where(
                    PendingParlayLeg.guild_id == user.guild_id,
                    PendingParlayLeg.user_id == user.discord_id,
                )
            )
            for leg in legs_result.scalars().all():
                await session.delete(leg)

        await interaction.followup.send("Parlay slip cleared.", ephemeral=True)

    @parlay_group.command(
        name="tail",
        description="Browse and copy featured or other members' parlays at live odds",
    )
    async def parlay_tail(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            featured, member = await _gather_tailable(session, current_guild_id())

        if not featured and not member:
            await interaction.followup.send(
                "No parlays are available to tail right now. "
                "Featured parlays appear when a phase opens — check back soon.",
                ephemeral=True,
            )
            return

        buf = await render_async(render_tail_board, featured, member)
        f = buf_to_discord_file(buf, "tail_board.png")
        
        view = TailView(featured, member)
        msg = await interaction.followup.send(
            file=f, view=view, ephemeral=True
        )
        view.message = msg

    @parlay_group.command(
        name="unlist",
        description="Remove your parlay from the public tail board",
    )
    @app_commands.describe(parlay_id="Your listed parlay to remove from the tail board")
    @app_commands.autocomplete(parlay_id=user_public_parlay_autocomplete)
    async def parlay_unlist(self, interaction: discord.Interaction, parlay_id: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        pid = _parse_id(parlay_id)
        if pid is None:
            await interaction.followup.send(
                "Invalid selection. Please pick a parlay from the autocomplete list.",
                ephemeral=True,
            )
            return
        async with get_session() as session:
            parlay = await session.get(Parlay, pid)
            if parlay is None or parlay.user_id != interaction.user.id or parlay.guild_id != current_guild_id():
                await interaction.followup.send("Parlay not found.", ephemeral=True)
                return
            if not parlay.is_public:
                await interaction.followup.send(
                    f"Parlay #{pid} is already private — it's not listed on the tail board.",
                    ephemeral=True,
                )
                return
            parlay.is_public = False
        await interaction.followup.send(
            f"🔒 Parlay #{pid} removed from the tail board. Others can no longer copy it.",
            ephemeral=True,
        )

    # ── /cashout ──────────────────────────────────────────────────────────────

    @app_commands.command(name="cashout", description="Cash out a pending bet or parlay early")
    @app_commands.describe(
        cashout_type="Cash out a single bet or an entire parlay",
        cashout_id="Market or parlay to cash out",
    )
    @app_commands.choices(cashout_type=[
        app_commands.Choice(name="Single Bet",   value="BET"),
        app_commands.Choice(name="Parlay",        value="PARLAY"),
    ])
    @app_commands.autocomplete(cashout_id=cashout_autocomplete)
    async def cashout(
        self,
        interaction: discord.Interaction,
        cashout_type: app_commands.Choice[str],
        cashout_id: str,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        cid = _parse_id(cashout_id)
        if cid is None:
            await interaction.followup.send(
                "Invalid selection. Please pick from the autocomplete list.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())
            ok, error, amount, label, _target = await _resolve_cashout_target(
                session, user, cashout_type.value, cid
            )

        if not ok:
            await interaction.followup.send(error, ephemeral=True)
            return

        embed = discord.Embed(title="Confirm Cashout", color=0x5B9BD5)
        embed.add_field(name="Market/Parlay", value=label, inline=False)
        embed.add_field(name="Cashout Amount", value=fmt_chips(amount))
        view = CashoutConfirmView(cashout_type.value, cid, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /withdraw · /deposit ────────────────────────────────────────────────────
    # Chips are the in-bot currency; "panars" is the external economy run by
    # another bot (/admin1 award-deprive). These two commands bridge the two:
    # the bot can't run the external command itself, so it posts a copy-paste
    # command into a designated admin channel for an admin to action. The
    # exchange is 1 chip ↔ 1 panar.

    async def _exchange_channel(
        self, interaction: discord.Interaction
    ) -> discord.abc.Messageable | None:
        """Resolve the withdraw/deposit admin channel, or None.

        Checks per-guild setting first (set via `/admin settings withdraw_channel`),
        then falls back to the WITHDRAW_CHANNEL_ID env var.
        """
        if interaction.guild is None:
            return None
        from bot.database.engine import get_guild_setting
        raw = await get_guild_setting(current_guild_id(), "withdraw_channel_id")
        if not raw:
            raw = await get_setting("withdraw_channel_id")
        channel_id = json.loads(raw) if raw else config.WITHDRAW_CHANNEL_ID
        if not channel_id:
            return None
        return interaction.guild.get_channel(channel_id)

    @app_commands.command(
        name="withdraw",
        description="Convert chips back into panars (sends an admin payout request)",
    )
    @app_commands.describe(amount="Amount of chips to withdraw (minimum 5,000)")
    async def withdraw(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, EXCHANGE_MIN, 100_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        channel = await self._exchange_channel(interaction)
        if channel is None:
            await interaction.followup.send(
                "Withdrawals aren't set up yet — ask an admin to configure the "
                "payout channel.",
                ephemeral=True,
            )
            return

        member = interaction.user
        async with get_session() as session:
            user = await _get_or_create_user(session, member, current_guild_id())
            if user.chips < amount:
                await interaction.followup.send(
                    f"Insufficient chips. You have **{fmt_chips(user.chips)}** but "
                    f"asked to withdraw **{fmt_chips(amount)}**.",
                    ephemeral=True,
                )
                return

            user.chips -= amount
            new_balance = user.chips

            # Post the payout request inside the transaction: if the send fails,
            # the chip debit rolls back so we never take chips without notifying
            # admins to pay out the panars.
            await channel.send(
                f"💸 **Withdrawal request** — {member.mention} is converting "
                f"**{fmt_chips(amount)}** into Panars. Their chip balance has "
                f"already been debited. Run this to pay them out:\n"
                f"`/admin1 award-deprive citizen:{member.mention} operation:Award "
                f"resource:Panars amount:{amount}`"
            )

        await interaction.followup.send(
            f"Withdrawal request for **{fmt_chips(amount)}** submitted. The amount "
            f"has been debited from your balance (now **{fmt_chips(new_balance)}**) "
            "and an admin will pay out your Panars shortly.",
            ephemeral=True,
        )

    @app_commands.command(
        name="deposit",
        description="Convert panars into chips (sends an admin top-up request)",
    )
    @app_commands.describe(amount="Amount of panars to deposit (minimum 5,000)")
    async def deposit(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, EXCHANGE_MIN, 100_000_000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        channel = await self._exchange_channel(interaction)
        if channel is None:
            await interaction.followup.send(
                "Deposits aren't set up yet — ask an admin to configure the "
                "exchange channel.",
                ephemeral=True,
            )
            return

        # Chips are NOT credited here — an admin takes the panars and credits the
        # chips so nobody can mint chips they didn't pay for.
        member = interaction.user
        await channel.send(
            f"🏦 **Deposit request** — {member.mention} wants to convert "
            f"**{amount:,} Panars** into chips. Take their Panars, then credit "
            "the chips:\n"
            f"`/admin1 award-deprive citizen:{member.mention} operation:Deprive "
            f"resource:Panars amount:{amount}`\n"
            f"`/admin settings chips_give user:{member.mention} amount:{amount}`"
        )

        await interaction.followup.send(
            f"Deposit request for **{amount:,} Panars** submitted. An admin will "
            f"take your Panars and credit **{fmt_chips(amount)}** to your balance "
            "shortly.",
            ephemeral=True,
        )

    # ── /mybets ───────────────────────────────────────────────────────────────

    @app_commands.command(name="mybets", description="View your bets as a styled card")
    @app_commands.choices(filter_by=[
        app_commands.Choice(name="All",     value="ALL"),
        app_commands.Choice(name="Pending", value="PENDING"),
        app_commands.Choice(name="Won",     value="WON"),
        app_commands.Choice(name="Lost",    value="LOST"),
    ])
    async def mybets(
        self,
        interaction: discord.Interaction,
        filter_by: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        filter_val = filter_by.value if filter_by else "ALL"

        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user, current_guild_id())

            straight_q = select(Bet).where(
                Bet.guild_id == user.guild_id, Bet.user_id == user.discord_id, Bet.parlay_id == None,
            )
            parlay_q = select(Parlay).where(
                Parlay.guild_id == user.guild_id, Parlay.user_id == user.discord_id,
            )
            if filter_val != "ALL":
                straight_q = straight_q.where(Bet.status == filter_val)
                parlay_q = parlay_q.where(Parlay.status == filter_val)

            straight_result = await session.execute(straight_q.order_by(Bet.placed_at.desc()).limit(20))
            straight_bets_orm = straight_result.scalars().all()

            parlay_result = await session.execute(parlay_q.order_by(Parlay.placed_at.desc()).limit(10))
            parlays_orm = parlay_result.scalars().all()

            straight_rows: list[BetRowData] = []
            for b in straight_bets_orm:
                mkt = await session.get(Market, b.market_id)
                straight_rows.append(BetRowData(
                    bet_id=b.id,
                    market_label=mkt.label if mkt else f"Market #{b.market_id}",
                    wager=b.wager,
                    odds=b.odds_at_placement,
                    payout=b.payout_if_win,
                    status=b.status,
                ))

            parlay_data: list[ParlayData] = []
            for p in parlays_orm:
                legs_result = await session.execute(
                    select(Bet).where(Bet.parlay_id == p.id).order_by(Bet.placed_at)
                )
                leg_rows: list[BetRowData] = []
                for b in legs_result.scalars().all():
                    mkt = await session.get(Market, b.market_id)
                    leg_rows.append(BetRowData(
                        bet_id=b.id,
                        market_label=mkt.label if mkt else f"Market #{b.market_id}",
                        wager=b.wager,
                        odds=b.odds_at_placement,
                        payout=b.payout_if_win,
                        status=b.status,
                    ))
                combo = combined_american([l.odds for l in leg_rows]) if leg_rows else 0
                parlay_data.append(ParlayData(
                    parlay_id=p.id,
                    total_wager=p.total_wager,
                    total_payout=p.total_payout,
                    combined_odds=combo,
                    status=p.status,
                    legs=leg_rows,
                ))

            chips = user.chips
            username = user.username

        buf = await render_async(render_my_bets, username, chips, straight_rows, parlay_data, filter_val)
        f = buf_to_discord_file(buf, "my_bets.png")
        await interaction.followup.send(file=f, ephemeral=True)

    # ── /featured ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="featured",
        description="Browse Gamemaker-curated parlays available to tail at live odds",
    )
    async def featured(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            featured_list, member_list = await _gather_tailable(session)

        if not featured_list:
            await interaction.followup.send(
                "No featured parlays are available right now. Check back after the next phase opens.",
                ephemeral=True,
            )
            return

        buf = await render_async(render_tail_board, featured_list, member_list)
        f = buf_to_discord_file(buf, "featured.png")
        view = TailView(featured_list, member_list)
        msg = await interaction.followup.send(file=f, view=view, ephemeral=True)
        view.message = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BettingCog(bot))

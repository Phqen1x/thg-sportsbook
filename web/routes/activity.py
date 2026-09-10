"""JSON API for the embedded Discord Activity.

All endpoints return JSON (not HTML) and authenticate via a Bearer token minted by
``POST /api/activity/token`` after the server-side OAuth + admin verification. The
business logic mirrors the cookie-session routes in ``web/routes/public.py`` and
``member.py`` and the live-ops admin actions in ``web/routes/admin.py``; the betting
and odds maths are reused unchanged from ``bot/``.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select, text

from bot.cogs.betting import (
    _betting_paused, _parlay_conflict, _single_cap_error, _parlay_cap_error,
    add_markets_to_pending_slip, tribute_lookup_for_markets,
    BETTING_BLOCKED_MSG, BETTING_PAUSED_MSG, MAX_PARLAY_LEGS,
)
from bot.cogs.display import LEADERBOARD_CATEGORIES, _leaderboard_rows
from bot.database.models import (
    Alliance, Bet, BettingPhase, BonusBetLot, BonusGrant, BoostShopItem,
    BoostShopPurchase, DepositMatchClaim,
    DepositMatchPromo, DistrictRecord, ExchangeRateOverride, Market, MarketTemplate,
    Parlay, PendingParlayLeg, ParlayTemplate, ParlayTemplateLeg,
    ProfitBoostGrant, ProfitBoostTemplate, ProfitBoostToken, PromoClaimDrop,
    PublicBetRestriction, Tribute, User,
)
from bot.odds.calculator import (
    combined_american, parlay_payout, resolve_cashout, straight_payout,
)
from bot.utils.economy import economy_totals
from bot.utils.exchange_rates import (
    effective_rate, get_global_rate, list_overrides, set_global_rate, set_override,
)
from bot.utils.house_cut import (
    get_house_cut_total_taken, load_house_cut_config, net_payout, parlay_effective_odds,
    record_house_cut_taken, set_house_cut_for_type, set_house_cut_global,
    set_house_cut_high_odds_rule,
)
from bot.utils.payout_caps import get_payout_cap, set_payout_cap
from bot.utils import promos
from bot.utils.restrictions import (
    is_fully_restricted, is_public_bet_blocked, list_public_blocks, set_public_block,
)
from web import config, discord_api
from web.activity_auth import bearer_admin, bearer_user, mint_token
from web.audit import post_admin_action, post_bet_log
from web.database import get_db, get_request_guild, set_request_guild
from web.deps import live_role_ids
from web.routes.public import _parlay_flavor
from web.session import SessionUser

router = APIRouter(prefix="/api/activity", tags=["activity"])


# ── Serializers ──────────────────────────────────────────────────────────────

def _tribute_dict(t: Tribute) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "district": t.district,
        "gender": t.display_gender,
        "age": t.age,
        "training_score": t.training_score,
        "kills": t.kills,
        "status": t.status,
        "placement": t.placement,
        "alliance_id": t.alliance_id,
        "face_claim": t.face_claim,
    }


def _market_dict(m: Market, tributes: dict[int, Tribute] | None = None, bet_count: int = 0) -> dict:
    tributes = tributes or {}
    ta = tributes.get(m.tribute_a_id) if m.tribute_a_id else None
    tb = tributes.get(m.tribute_b_id) if m.tribute_b_id else None
    return {
        "id": m.id,
        "type": m.type,
        "label": m.label,
        "odds": m.odds,
        "status": m.status,
        "result": m.result,
        "tribute_a_id": m.tribute_a_id,
        "tribute_b_id": m.tribute_b_id,
        "tribute_a": ta.name if ta else None,
        "tribute_b": tb.name if tb else None,
        "cause": m.cause,
        "placement_num": m.placement_num,
        "top_n": m.top_n,
        "ou_line": m.ou_line,
        "ou_side": m.ou_side,
        "cashout_allowed": m.cashout_allowed,
        "cashout_rate": m.cashout_rate,
        "odds_override": m.odds_override,
        "bet_count": bet_count,
    }


def _user_dict(u: User) -> dict:
    return {
        "discord_id": str(u.discord_id),
        "username": u.username,
        "chips": u.chips,
        "total_wagered": u.total_wagered,
        "total_won": u.total_won,
    }


def _bet_dict(b: Bet) -> dict:
    return {
        "id": b.id,
        "market_id": b.market_id,
        "parlay_id": b.parlay_id,
        "wager": b.wager,
        "odds_at_placement": b.odds_at_placement,
        "payout_if_win": b.payout_if_win,
        # un-boosted payout on the same stake — for the My Bets boost comparison
        "raw_payout": straight_payout(b.wager, b.odds_at_placement) if b.parlay_id is None else b.payout_if_win,
        "status": b.status,
        "cashout_amount": b.cashout_amount,
        "bonus_bet_amount": b.bonus_bet_amount,
        "profit_boost_pct": b.profit_boost_pct,
        "placed_at": b.placed_at.isoformat() if b.placed_at else None,
    }


def _parlay_dict(p: Parlay) -> dict:
    return {
        "id": p.id,
        "total_wager": p.total_wager,
        "total_payout": p.total_payout,
        "status": p.status,
        "cashout_amount": p.cashout_amount,
        "is_public": p.is_public,
        "bonus_bet_amount": p.bonus_bet_amount,
        "profit_boost_pct": p.profit_boost_pct,
        "placed_at": p.placed_at.isoformat() if p.placed_at else None,
    }


# ── Shared helpers ───────────────────────────────────────────────────────────

_GUILD_ID = get_request_guild


async def _paused() -> bool:
    """Wrap _betting_paused() with the guild-context bind it needs.

    _betting_paused() reaches the DB through bot.database.engine (get_setting ->
    get_session()), which resolves the active guild from *its own* contextvar —
    separate from web/database.py's request-guild context that the rest of this
    module uses. Without binding it first, current_guild_id() reads 0 and
    get_session() raises "No guild context set", which isn't an HTTPException
    and so surfaces as a bare 500 instead of the intended paused-betting message."""
    from bot.database.engine import set_guild_context
    set_guild_context(_GUILD_ID())
    return await _betting_paused()


async def _single_cap_error_activity(amount: int, odds: int, payout_override: int | None = None) -> str | None:
    """Guild-context-bound wrapper — see _paused() for why this is needed.
    Strips the Discord-markdown ** the shared helper wraps chip amounts in,
    since this surface renders errors as plain text."""
    from bot.database.engine import set_guild_context
    set_guild_context(_GUILD_ID())
    err = await _single_cap_error(amount, odds, payout_override=payout_override)
    return err.replace("**", "") if err else None


async def _parlay_cap_error_activity(wager: int, odds_list: list[int], payout_override: int | None = None) -> str | None:
    """Guild-context-bound wrapper — see _paused() for why this is needed.
    Strips the Discord-markdown ** the shared helper wraps chip amounts in,
    since this surface renders errors as plain text."""
    from bot.database.engine import set_guild_context
    set_guild_context(_GUILD_ID())
    err = await _parlay_cap_error(wager, odds_list, payout_override=payout_override)
    return err.replace("**", "") if err else None


async def _payout_rate_activity(db, user: SessionUser, role_ids=None) -> float:
    """The bettor's frozen PAYOUT multiplier for a wager placed via the Activity.
    Guild-context-bound (see _paused) so the global-rate fallback can read
    settings through bot.database.engine."""
    from bot.database.engine import set_guild_context
    set_guild_context(_GUILD_ID())
    if role_ids is None:
        role_ids = await live_role_ids(user.discord_id, user.guild_id)
    return await effective_rate(db, _GUILD_ID(), user_id=user.discord_id, role_ids=role_ids)


async def _fetch_user(db, discord_id: int) -> User | None:
    result = await db.execute(
        select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == discord_id)
    )
    return result.scalar_one_or_none()


async def _get_or_create_user(db, session_user: SessionUser) -> User:
    result = await db.execute(
        select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == session_user.discord_id)
    )
    u = result.scalar_one_or_none()
    if u is None:
        default_raw = (await db.execute(
            text("SELECT value FROM game_settings WHERE key='default_chips'")
        )).fetchone()
        default = json.loads(default_raw[0]) if default_raw else 0
        u = User(
            guild_id=_GUILD_ID(), discord_id=session_user.discord_id,
            username=session_user.username, chips=default,
        )
        db.add(u)
        await db.flush()
        from bot.utils.promos import apply_first_touch_grants
        await apply_first_touch_grants(db, _GUILD_ID(), session_user.discord_id)
    return u


async def _phase_name(db) -> str | None:
    row = (await db.execute(text("SELECT value FROM game_settings WHERE key='current_phase_id'"))).fetchone()
    if not row:
        return None
    phase = await db.get(BettingPhase, int(row[0]))
    return phase.name if phase else None


async def _cashout_settings(db) -> tuple[bool, float, dict]:
    """Global cashout settings, shared by the bet/parlay cashout preview + POST routes."""
    row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_allowed'"))).fetchone()
    global_allowed = (row[0].lower() == "true") if row else False
    rate_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_rate'"))).fetchone()
    global_rate = float(rate_row[0]) if rate_row else 0.65
    by_type_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_by_type'"))).fetchone()
    cashout_by_type: dict = json.loads(by_type_row[0]) if by_type_row else {}
    return global_allowed, global_rate, cashout_by_type


def _bet_cashout_preview(bet: Bet, market: Market | None, settings: tuple[bool, float, dict]) -> tuple[bool, int]:
    if bet.bonus_bet_amount:
        return (False, 0)  # bonus-funded wagers can't be cashed out
    global_allowed, global_rate, cashout_by_type = settings
    type_override = cashout_by_type.get(market.type) if market else None
    return resolve_cashout(
        wager=bet.wager, payout_if_win=bet.payout_if_win,
        global_allowed=global_allowed, global_rate=global_rate,
        item_allowed=market.cashout_allowed if market else None,
        item_rate=market.cashout_rate if market else None,
        type_allowed=type_override["allowed"] if type_override else None,
        type_rate=type_override.get("rate") if type_override else None,
    )


def _parlay_cashout_preview(parlay: Parlay, settings: tuple[bool, float, dict]) -> tuple[bool, int]:
    if parlay.bonus_bet_amount:
        return (False, 0)  # bonus-funded wagers can't be cashed out
    global_allowed, global_rate, _by_type = settings
    return resolve_cashout(
        wager=parlay.total_wager, payout_if_win=parlay.total_payout,
        global_allowed=global_allowed, global_rate=global_rate,
        item_allowed=parlay.cashout_allowed, item_rate=parlay.cashout_rate,
    )


# ── Auth handshake ───────────────────────────────────────────────────────────

@router.post("/token")
async def token(
    code: Annotated[str, Body(embed=True)],
    guild_id: Annotated[str | None, Body(embed=True)] = None,
):
    """Exchange the Embedded App SDK OAuth code for a signed activity token.

    Identity is read from Discord with the access token and admin status is checked
    server-side with the bot token — neither is supplied by the client.
    The ``guild_id`` the activity is running in is provided by the client so that
    the correct per-guild database is used for all subsequent API calls.
    guild_id is sent as a string because Discord snowflakes exceed JS Number.MAX_SAFE_INTEGER.
    """
    # Parse guild_id safely — the client sends it as a string to avoid JS float precision loss.
    gid: int | None = int(guild_id) if guild_id else None

    if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Discord OAuth is not configured.")

    # Lock the Activity to a single server when GUILD_ID is configured. Without
    # this check the client-reported guild_id (whichever server the Activity was
    # actually launched from) was trusted as-is, so the Activity would happily
    # authenticate — and spin up a fresh per-guild database for — any server the
    # bot is in, regardless of GUILD_ID in .env.
    if config.GUILD_ID and gid != config.GUILD_ID:
        raise HTTPException(
            status_code=403,
            detail="This Activity is only available in the official server.",
        )
    try:
        tokens = await discord_api.exchange_code_activity(code)
        access_token = tokens["access_token"]
        user_data = await discord_api.get_user(access_token)
        uid = int(user_data["id"])
        member = await discord_api.get_member(uid, guild_id=gid)
        if member is None:
            raise HTTPException(status_code=403, detail="You must be a member of the server to use this.")
        is_admin = await discord_api.check_admin(member, guild_id=gid)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Discord authorization failed.")

    user = SessionUser(
        discord_id=uid,
        username=user_data.get("global_name") or user_data.get("username", "Unknown"),
        avatar=user_data.get("avatar"),
        is_admin=is_admin,
        guild_id=gid,
    )
    # Point DB context at the activity's guild so _get_or_create_user uses the right file.
    set_request_guild(gid or 0)
    async with get_db() as db:
        from bot.database.engine import get_tribute_lock, TRIBUTE_LOCK_MESSAGE
        if await get_tribute_lock(db, gid or 0, uid) is not None:
            raise HTTPException(status_code=403, detail=TRIBUTE_LOCK_MESSAGE)
        db_user = await _get_or_create_user(db, user)
        db_user.username = user.username
        await db.commit()

    return {
        "token": mint_token(user),
        "access_token": access_token,  # only for the SDK authenticate() call
        "user": {
            "discord_id": str(user.discord_id),
            "username": user.username,
            "avatar": user.avatar,
            "avatar_url": user.avatar_url,
            "is_admin": user.is_admin,
        },
    }


# ── Member: read ─────────────────────────────────────────────────────────────

@router.get("/me")
async def me(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        await db.commit()

        # ROI must be derived from the same total_won/total_wagered counters the
        # /balance Discord command reads, not re-derived from bets/parlays here —
        # a separate recomputation drifted out of sync with the persisted totals
        # (e.g. legacy rows scoped differently) and was showing -100% ROI for
        # users total_won already correctly credited chips for.
        wagered = db_user.total_wagered
        roi = ((db_user.total_won - wagered) / wagered * 100) if wagered else 0.0

        await promos.expire_stale(db, _GUILD_ID(), user.discord_id)
        bonus_bal = await promos.bonus_balance(db, _GUILD_ID(), user.discord_id)
        bonus_exp = await promos.next_bonus_expiry(db, _GUILD_ID(), user.discord_id)
        active_boosts = await promos.active_boost_tokens(db, _GUILD_ID(), user.discord_id)
        await db.commit()

    return {
        **_user_dict(db_user),
        "is_admin": user.is_admin,
        "avatar_url": user.avatar_url,
        "roi": round(roi, 1),
        "bonus_bet_balance": bonus_bal,
        "bonus_bet_next_expiry": bonus_exp.isoformat() if bonus_exp else None,
        "bonus_wagered": db_user.bonus_wagered,
        "bonus_won": db_user.bonus_won,
        "active_boosts": [
            {"id": t.id, "pct": t.boost_pct, "scope_type": t.scope_type,
             "scope_id": t.scope_id,
             "expires_at": t.expires_at.isoformat() if t.expires_at else None}
            for t in active_boosts
        ],
        # Exposed here (rather than only on the admin-gated /admin/payout-caps
        # endpoint) so the bet/parlay wager UI can preview an accurate payout —
        # every member already fetches /me on load and after each bet.
        "single_payout_cap": await get_payout_cap("SINGLE"),
        "parlay_payout_cap": await get_payout_cap("PARLAY"),
    }


@router.get("/markets")
async def markets(
    user: SessionUser = Depends(bearer_user),
    status: str = "open",
    type_filter: str = "",
):
    async with get_db() as db:
        q = select(Market).order_by(Market.created_at.desc())
        if status == "open":
            q = q.where(Market.status == "OPEN")
        elif status == "resolved":
            q = q.where(Market.status == "RESOLVED")
        elif status == "closed":
            q = q.where(Market.status == "CLOSED")
        if type_filter:
            q = q.where(Market.type == type_filter)

        markets_list = (await db.execute(q)).scalars().all()

        tribute_ids = {m.tribute_a_id for m in markets_list if m.tribute_a_id} | \
                      {m.tribute_b_id for m in markets_list if m.tribute_b_id}
        tributes_map: dict[int, Tribute] = {}
        if tribute_ids:
            t_rows = (await db.execute(select(Tribute).where(Tribute.id.in_(tribute_ids)))).scalars().all()
            tributes_map = {t.id: t for t in t_rows}

        bet_counts_raw = (await db.execute(
            select(Bet.market_id, func.count(Bet.id)).group_by(Bet.market_id)
        )).all()
        bet_counts = {row[0]: row[1] for row in bet_counts_raw}

        phase_name = await _phase_name(db)

    return {
        "markets": [_market_dict(m, tributes_map, bet_counts.get(m.id, 0)) for m in markets_list],
        "phase_name": phase_name,
    }


@router.get("/tributes")
async def tributes(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        tributes_list = (await db.execute(
            select(Tribute).order_by(Tribute.district, Tribute.gender)
        )).scalars().all()
        win_markets_raw = (await db.execute(
            select(Market).where(Market.type == "TRIBUTE_WINS", Market.status == "OPEN")
        )).scalars().all()
        win_markets = {m.tribute_a_id: m.id for m in win_markets_raw}
        win_odds = {m.tribute_a_id: m.odds for m in win_markets_raw}
        alliances_raw = (await db.execute(select(Alliance))).scalars().all()
        alliances = {a.id: a.name for a in alliances_raw}

    return {
        "tributes": [
            {
                **_tribute_dict(t),
                "alliance": alliances.get(t.alliance_id),
                "win_market_id": win_markets.get(t.id),
                "win_odds": win_odds.get(t.id),
            }
            for t in tributes_list
        ],
    }


@router.get("/leaderboard")
async def leaderboard(user: SessionUser = Depends(bearer_user), category: str = "CHIPS"):
    if category not in dict(LEADERBOARD_CATEGORIES):
        category = "CHIPS"
    gid = _GUILD_ID()

    async with get_db() as db:
        title, kind, rows = await _leaderboard_rows(db, gid, category, 100)

        usernames: dict[int, str] = {}
        ids = {uid for uid, _ in rows}
        if ids:
            user_result = await db.execute(
                select(User.discord_id, User.username).where(
                    User.guild_id == gid, User.discord_id.in_(ids)
                )
            )
            usernames = {uid: name for uid, name in user_result.all()}

    return {
        "category": category,
        "title": title,
        "value_kind": kind,
        "categories": [{"value": v, "label": l} for v, l in LEADERBOARD_CATEGORIES],
        "users": [
            {
                "rank": i + 1,
                "discord_id": str(uid),
                "username": usernames.get(uid, "Member"),
                "value": value,
                "is_me": uid == user.discord_id,
            }
            for i, (uid, value) in enumerate(rows)
        ],
    }


@router.get("/my-bets")
async def my_bets(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        straight_bets = (await db.execute(
            select(Bet)
            .where(
                Bet.guild_id == _GUILD_ID(), Bet.user_id == user.discord_id,
                Bet.parlay_id.is_(None),
            )
            .order_by(Bet.placed_at.desc())
        )).scalars().all()
        parlays = (await db.execute(
            select(Parlay)
            .where(Parlay.guild_id == _GUILD_ID(), Parlay.user_id == user.discord_id)
            .order_by(Parlay.placed_at.desc())
        )).scalars().all()

        market_ids = {b.market_id for b in straight_bets}
        markets_map: dict[int, Market] = {}

        parlay_out = []
        for p in parlays:
            legs = (await db.execute(
                select(Bet).where(Bet.parlay_id == p.id).order_by(Bet.id)
            )).scalars().all()
            for leg in legs:
                market_ids.add(leg.market_id)
            leg_odds = [l.odds_at_placement for l in legs]
            raw_total = parlay_payout(p.total_wager, leg_odds) if leg_odds else p.total_payout
            parlay_out.append({
                **_parlay_dict(p), "legs": [_bet_dict(b) for b in legs],
                "raw_total_payout": raw_total,
            })

        if market_ids:
            mkts = (await db.execute(select(Market).where(Market.id.in_(market_ids)))).scalars().all()
            markets_map = {m.id: m for m in mkts}

        settings = await _cashout_settings(db)
        straight_out = []
        for b in straight_bets:
            d = _bet_dict(b)
            if b.status == "PENDING":
                allowed, amount = _bet_cashout_preview(b, markets_map.get(b.market_id), settings)
                d["cashout_preview"] = amount if allowed else None
            straight_out.append(d)
        for p, po in zip(parlays, parlay_out):
            if p.status == "PENDING":
                allowed, amount = _parlay_cashout_preview(p, settings)
                po["cashout_preview"] = amount if allowed else None

    return {
        "straight_bets": straight_out,
        "parlays": parlay_out,
        "markets": {str(mid): _market_dict(m) for mid, m in markets_map.items()},
    }


# ── Member: straight bets ────────────────────────────────────────────────────

@router.post("/bet")
async def place_bet(
    user: SessionUser = Depends(bearer_user),
    market_id: Annotated[int, Body()] = 0,
    wager: Annotated[int, Body()] = 0,
    bonus_amount: Annotated[int, Body()] = 0,
    profit_boost_token_id: Annotated[int | None, Body()] = None,
):
    if wager < 1 and bonus_amount < 1:
        raise HTTPException(status_code=400, detail="Enter a wager or apply Bonus Chips.")
    if await _paused():
        raise HTTPException(status_code=423, detail=BETTING_PAUSED_MSG)

    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            raise HTTPException(status_code=400, detail="Market is not open for betting.")

        db_user = await _get_or_create_user(db, user)
        if await is_fully_restricted(db, _GUILD_ID(), user.discord_id):
            raise HTTPException(status_code=403, detail=BETTING_BLOCKED_MSG)

        existing = (await db.execute(
            select(Bet).where(
                Bet.guild_id == _GUILD_ID(),
                Bet.user_id == user.discord_id,
                Bet.market_id == market_id,
                Bet.status == "PENDING",
                Bet.parlay_id.is_(None),
            )
        )).scalars().first()
        if existing:
            raise HTTPException(status_code=400, detail="You already have a pending bet on this market.")

        info, perr = await promos.validate_wager_promos(
            db, _GUILD_ID(), user.discord_id, db_user.chips,
            wager=wager, bonus_amount=bonus_amount, boost_token_id=profit_boost_token_id,
            markets=[market], is_parlay=False,
        )
        if perr:
            raise HTTPException(status_code=400, detail=perr)
        payout = info["payout"]
        total_stake = info["total_stake"]
        net_if_win = info["net_if_win"]

        cap_err = await _single_cap_error_activity(total_stake, market.odds, payout_override=payout)
        if cap_err:
            raise HTTPException(status_code=400, detail=cap_err)

        bet = Bet(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            market_id=market_id,
            wager=total_stake,
            odds_at_placement=market.odds,
            payout_if_win=payout,
            payout_rate_at_placement=await _payout_rate_activity(db, user),
            status="PENDING",
            bonus_bet_amount=info["bonus_amount"],
            profit_boost_pct=info["boost_pct"],
            profit_boost_token_id=info["boost_token"].id if info["boost_token"] else None,
        )
        db_user.chips -= info["real_part"]
        db_user.total_wagered += info["real_part"]
        db.add(bet)
        await db.flush()
        await promos.commit_wager_promos(db, _GUILD_ID(), db_user, info, bet_id=bet.id)
        await db.commit()
        chips_left = db_user.chips
        market_label = market.label
        bonus_used = info["bonus_amount"]

    asyncio.create_task(post_bet_log(_GUILD_ID(), user.discord_id, "BET", [market_label], total_stake, net_if_win))
    msg = f"Bet placed! Win {net_if_win:,} chips if correct."
    if bonus_used:
        msg = f"Bonus Chips bet placed! Win {net_if_win:,} chips (winnings only — bonus stake not returned)."
    return {"ok": True, "payout_if_win": net_if_win, "chips": chips_left, "message": msg}


@router.post("/cashout/bet/{bet_id}")
async def cashout_bet(bet_id: int, user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        bet = await db.get(Bet, bet_id)
        if not bet or bet.user_id != user.discord_id or bet.guild_id != _GUILD_ID() or bet.status != "PENDING":
            raise HTTPException(status_code=400, detail="Bet not found or not cashout-eligible.")

        market = await db.get(Market, bet.market_id)
        settings = await _cashout_settings(db)
        allowed, amount = _bet_cashout_preview(bet, market, settings)
        if not allowed:
            raise HTTPException(status_code=400, detail="Cashout is not currently allowed.")

        db_user = await _fetch_user(db, user.discord_id)
        if db_user:
            db_user.chips += amount
        bet.status = "CASHED_OUT"
        bet.cashout_amount = amount
        await db.commit()
        chips_left = db_user.chips if db_user else None

    return {"ok": True, "amount": amount, "chips": chips_left,
            "message": f"Cashed out for {amount:,} chips."}


@router.post("/cashout/parlay/{parlay_id}")
async def cashout_parlay(parlay_id: int, user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        parlay = await db.get(Parlay, parlay_id)
        if not parlay or parlay.user_id != user.discord_id or parlay.guild_id != _GUILD_ID() or parlay.status != "PENDING":
            raise HTTPException(status_code=400, detail="Parlay not found or not cashout-eligible.")

        settings = await _cashout_settings(db)
        allowed, amount = _parlay_cashout_preview(parlay, settings)
        if not allowed:
            raise HTTPException(status_code=400, detail="Cashout is not currently allowed.")

        db_user = await _fetch_user(db, user.discord_id)
        if db_user:
            db_user.chips += amount
        parlay.status = "CASHED_OUT"
        parlay.cashout_amount = amount

        legs = (await db.execute(select(Bet).where(Bet.parlay_id == parlay_id))).scalars().all()
        for leg in legs:
            leg.status = "CASHED_OUT"
            leg.cashout_amount = 0
        await db.commit()
        chips_left = db_user.chips if db_user else None

    return {"ok": True, "amount": amount, "chips": chips_left,
            "message": f"Parlay cashed out for {amount:,} chips."}


# ── Member: parlay slip ──────────────────────────────────────────────────────

@router.get("/parlay")
async def parlay_view(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        await db.commit()

        legs = (await db.execute(
            select(PendingParlayLeg)
            .where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
            .order_by(PendingParlayLeg.added_at)
        )).scalars().all()

        leg_markets: dict[int, Market] = {}
        for leg in legs:
            mkt = await db.get(Market, leg.market_id)
            if mkt:
                leg_markets[leg.id] = mkt

        odds_list = [leg_markets[l.id].odds for l in legs if l.id in leg_markets]
        combined = combined_american(odds_list) if len(odds_list) >= 2 else None

    return {
        "chips": db_user.chips,
        "max_legs": MAX_PARLAY_LEGS,
        "combined_odds": combined,
        "legs": [
            {
                "leg_id": l.id,
                "market": _market_dict(leg_markets[l.id]) if l.id in leg_markets else None,
            }
            for l in legs
        ],
    }


@router.post("/parlay/add/{market_id}")
async def parlay_add(market_id: int, user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            raise HTTPException(status_code=400, detail="Market is not open.")

        existing_legs = (await db.execute(
            select(PendingParlayLeg).where(
                PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id,
            )
        )).scalars().all()

        if len(existing_legs) >= MAX_PARLAY_LEGS:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_PARLAY_LEGS} legs reached.")
        if any(l.market_id == market_id for l in existing_legs):
            raise HTTPException(status_code=400, detail="This market is already in your slip.")

        existing_markets = []
        for l in existing_legs:
            mkt = await db.get(Market, l.market_id)
            if mkt:
                existing_markets.append(mkt)

        tribute_by_id = await tribute_lookup_for_markets(db, existing_markets + [market])
        conflict = _parlay_conflict(existing_markets, market, tribute_by_id)
        if conflict:
            raise HTTPException(status_code=400, detail=conflict)

        db.add(PendingParlayLeg(guild_id=_GUILD_ID(), user_id=user.discord_id, market_id=market_id))
        await db.commit()

    return {"ok": True, "message": "Market added to parlay slip."}


@router.post("/parlay/remove/{leg_id}")
async def parlay_remove(leg_id: int, user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        leg = await db.get(PendingParlayLeg, leg_id)
        if leg and leg.user_id == user.discord_id and leg.guild_id == _GUILD_ID():
            await db.delete(leg)
            await db.commit()
    return {"ok": True}


@router.post("/parlay/clear")
async def parlay_clear(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        legs = (await db.execute(
            select(PendingParlayLeg).where(
                PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id,
            )
        )).scalars().all()
        for leg in legs:
            await db.delete(leg)
        await db.commit()
    return {"ok": True}


@router.post("/parlay/submit")
async def parlay_submit(
    user: SessionUser = Depends(bearer_user),
    wager: Annotated[int, Body()] = 0,
    is_public: Annotated[bool, Body()] = False,
    bonus_amount: Annotated[int, Body()] = 0,
    profit_boost_token_id: Annotated[int | None, Body()] = None,
):
    if wager < 1 and bonus_amount < 1:
        raise HTTPException(status_code=400, detail="Enter a wager or apply Bonus Chips.")
    if await _paused():
        raise HTTPException(status_code=423, detail=BETTING_PAUSED_MSG)

    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        if await is_fully_restricted(db, _GUILD_ID(), user.discord_id):
            raise HTTPException(status_code=403, detail=BETTING_BLOCKED_MSG)

        legs_raw = (await db.execute(
            select(PendingParlayLeg)
            .where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
            .order_by(PendingParlayLeg.added_at)
        )).scalars().all()

        if len(legs_raw) < 2:
            raise HTTPException(status_code=400, detail="A parlay requires at least 2 legs.")

        leg_markets = []
        for l in legs_raw:
            mkt = await db.get(Market, l.market_id)
            if not mkt or mkt.status != "OPEN":
                raise HTTPException(status_code=400, detail="One or more markets are no longer open.")
            leg_markets.append(mkt)

        odds_list = [m.odds for m in leg_markets]
        info, perr = await promos.validate_wager_promos(
            db, _GUILD_ID(), user.discord_id, db_user.chips,
            wager=wager, bonus_amount=bonus_amount, boost_token_id=profit_boost_token_id,
            markets=leg_markets, is_parlay=True,
        )
        if perr:
            raise HTTPException(status_code=400, detail=perr)
        total_payout = info["payout"]
        total_stake = info["total_stake"]
        net_if_win = info["net_if_win"]

        cap_err = await _parlay_cap_error_activity(total_stake, odds_list, payout_override=total_payout)
        if cap_err:
            raise HTTPException(status_code=400, detail=cap_err)

        role_ids = await live_role_ids(user.discord_id, user.guild_id)
        downgraded = is_public and await is_public_bet_blocked(db, _GUILD_ID(), user.discord_id, role_ids)
        is_public = is_public and not downgraded

        p = Parlay(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            total_wager=total_stake,
            total_payout=total_payout,
            payout_rate_at_placement=await _payout_rate_activity(db, user, role_ids),
            status="PENDING",
            is_public=is_public,
            bonus_bet_amount=info["bonus_amount"],
            profit_boost_pct=info["boost_pct"],
            profit_boost_token_id=info["boost_token"].id if info["boost_token"] else None,
        )
        db.add(p)
        await db.flush()

        for mkt in leg_markets:
            db.add(Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id, parlay_id=p.id, market_id=mkt.id,
                wager=total_stake, odds_at_placement=mkt.odds, payout_if_win=0, status="PENDING",
            ))

        db_user.chips -= info["real_part"]
        db_user.total_wagered += info["real_part"]
        for l in legs_raw:
            await db.delete(l)
        await promos.commit_wager_promos(db, _GUILD_ID(), db_user, info, parlay_id=p.id)
        await db.commit()
        chips_left = db_user.chips
        labels = [m.label for m in leg_markets]
        bonus_used = info["bonus_amount"]

    asyncio.create_task(post_bet_log(_GUILD_ID(), user.discord_id, "PARLAY", labels, total_stake, net_if_win))
    message = f"Parlay submitted! Potential payout: {net_if_win:,} chips."
    if bonus_used:
        message += f" {bonus_used:,} staked as Bonus Chips — winnings only on a win."
    if downgraded:
        message += " Kept private — public posting is restricted for you."
    return {"ok": True, "total_payout": net_if_win, "chips": chips_left, "message": message}


@router.post("/parlay/feature")
async def parlay_feature(
    user: SessionUser = Depends(bearer_user),
    name: Annotated[str, Body()] = "",
    description: Annotated[str, Body()] = "",
):
    """Admin-only: turn the caller's current parlay slip into a GM-featured,
    tailable, no-wager tail-board entry — same underlying model as the bot's
    `/admin parlay save_slip`, just reachable from the Activity's Parlay tab."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    if not name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    async with get_db() as db:
        legs = (await db.execute(
            select(PendingParlayLeg)
            .where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
            .order_by(PendingParlayLeg.added_at)
        )).scalars().all()
        if len(legs) < 2:
            raise HTTPException(status_code=400, detail="Your slip needs at least 2 legs to feature.")

        tpl = ParlayTemplate(
            name=name.strip()[:100],
            description=(description.strip()[:500] or None),
            source="ADMIN",
            active=True,
        )
        db.add(tpl)
        await db.flush()
        for i, leg in enumerate(legs):
            db.add(ParlayTemplateLeg(template_id=tpl.id, market_id=leg.market_id, sort_order=i))
            await db.delete(leg)
        await db.commit()
        tpl_id, tpl_name = tpl.id, tpl.name

    asyncio.create_task(post_admin_action(
        user, "Featured parlay created", {"name": tpl_name}, source="Discord Activity",
    ))
    return {"ok": True, "message": f"Featured parlay #{tpl_id} is now live on the tail board.", "id": tpl_id}


# ── Member: tail board ───────────────────────────────────────────────────────

@router.get("/tail")
async def tail_board(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        await db.commit()

        templates_raw = (await db.execute(
            select(ParlayTemplate).where(ParlayTemplate.active == True).order_by(ParlayTemplate.created_at.desc())
        )).scalars().all()

        tpl_leg_markets: dict[int, list] = {}
        out = []
        for tpl in templates_raw:
            legs = (await db.execute(
                select(ParlayTemplateLeg)
                .where(ParlayTemplateLeg.template_id == tpl.id)
                .order_by(ParlayTemplateLeg.sort_order)
            )).scalars().all()
            leg_markets = []
            for leg in legs:
                mkt = await db.get(Market, leg.market_id)
                if mkt:
                    leg_markets.append(mkt)
            tpl_leg_markets[tpl.id] = leg_markets
            odds_list = [m.odds for m in leg_markets]
            combined = combined_american(odds_list) if len(odds_list) >= 2 else None
            out.append({
                "id": tpl.id,
                "kind": "template",
                "name": tpl.name,
                "description": tpl.description,
                "difficulty": tpl.difficulty,
                "source": tpl.source,
                "combined_odds": combined,
                "legs": [_market_dict(m) for m in leg_markets],
            })

        # Generate dynamic flavor text from tribute/district history
        act_tid_set: set[int] = set()
        for mkts in tpl_leg_markets.values():
            for m in mkts:
                if m.tribute_a_id: act_tid_set.add(m.tribute_a_id)
                if m.tribute_b_id: act_tid_set.add(m.tribute_b_id)

        act_tributes_map: dict = {}
        act_alliance_names: dict = {}
        act_district_records: dict = {}
        if act_tid_set:
            pt_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(act_tid_set))
            )).scalars().all()
            act_tributes_map = {t.id: t for t in pt_rows}
            aid_set = {t.alliance_id for t in pt_rows if t.alliance_id}
            if aid_set:
                a_rows = (await db.execute(
                    select(Alliance).where(Alliance.id.in_(aid_set))
                )).scalars().all()
                act_alliance_names = {a.id: a.name for a in a_rows}
            act_districts = {t.district for t in pt_rows}
            dr_rows = (await db.execute(
                select(DistrictRecord).where(DistrictRecord.district.in_(act_districts))
            )).scalars().all()
            act_district_records = {dr.district: dr for dr in dr_rows}

        for entry in out:
            name, desc = _parlay_flavor(
                tpl_leg_markets[entry["id"]], act_tributes_map, act_alliance_names,
                act_district_records, entry["name"], entry["description"],
            )
            entry["name"] = name
            entry["description"] = desc

        # Public, still-pending member parlays are also tailable (matches
        # /parlay tail in the bot and the /tail page on the web dashboard) —
        # they live in a separate id space from ParlayTemplate, so callers use
        # entry["kind"] to route tail/add-to-slip requests to the right endpoint.
        mp_rows = (await db.execute(
            select(Parlay)
            .where(Parlay.guild_id == _GUILD_ID(), Parlay.status == "PENDING", Parlay.is_public == True)  # noqa: E712
            .order_by(Parlay.placed_at.desc())
            .limit(15)
        )).scalars().all()

        for mp in mp_rows:
            legs = (await db.execute(
                select(Bet).where(Bet.parlay_id == mp.id).order_by(Bet.id)
            )).scalars().all()
            leg_markets = []
            ok = True
            for b in legs:
                mkt = await db.get(Market, b.market_id)
                if not mkt or mkt.status != "OPEN":
                    ok = False
                    break
                leg_markets.append(mkt)
            if not ok or len(leg_markets) < 2:
                continue

            owner = (await db.execute(
                select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == mp.user_id)
            )).scalar_one_or_none()
            owner_name = owner.username if owner else "Member"
            odds_list = [m.odds for m in leg_markets]

            out.append({
                "id": mp.id,
                "kind": "member",
                "name": mp.name or f"{owner_name}'s Parlay #{mp.id}",
                "description": f"Tailing {owner_name}'s {len(leg_markets)}-leg parlay",
                "difficulty": "MEMBER",
                "source": "MEMBER",
                "combined_odds": combined_american(odds_list),
                "legs": [_market_dict(m) for m in leg_markets],
            })

    return {"chips": db_user.chips, "templates": out}


@router.post("/tail/{template_id}")
async def tail_parlay(
    template_id: int,
    user: SessionUser = Depends(bearer_user),
    wager: Annotated[int, Body(embed=True)] = 0,
):
    if wager < 1:
        raise HTTPException(status_code=400, detail="Wager must be at least 1 chip.")
    if await _paused():
        raise HTTPException(status_code=423, detail=BETTING_PAUSED_MSG)

    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, template_id)
        if not tpl or not tpl.active:
            raise HTTPException(status_code=404, detail="Template not found.")

        db_user = await _get_or_create_user(db, user)
        if await is_fully_restricted(db, _GUILD_ID(), user.discord_id):
            raise HTTPException(status_code=403, detail=BETTING_BLOCKED_MSG)

        legs = (await db.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == template_id)
            .order_by(ParlayTemplateLeg.sort_order)
        )).scalars().all()

        leg_markets = []
        for leg in legs:
            mkt = await db.get(Market, leg.market_id)
            if not mkt or mkt.status != "OPEN":
                raise HTTPException(status_code=400, detail="One or more markets in this template are no longer open.")
            leg_markets.append(mkt)

        if len(leg_markets) < 2:
            raise HTTPException(status_code=400, detail="Template has insufficient open markets.")
        if db_user.chips < wager:
            raise HTTPException(status_code=400, detail="Insufficient chips.")

        odds_list = [m.odds for m in leg_markets]
        cap_err = await _parlay_cap_error_activity(wager, odds_list)
        if cap_err:
            raise HTTPException(status_code=400, detail=cap_err)
        total_payout = parlay_payout(wager, odds_list)

        p = Parlay(
            guild_id=_GUILD_ID(), user_id=user.discord_id, total_wager=wager, total_payout=total_payout,
            payout_rate_at_placement=await _payout_rate_activity(db, user),
            status="PENDING", is_public=False,
        )
        db.add(p)
        await db.flush()
        for mkt in leg_markets:
            db.add(Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id, parlay_id=p.id, market_id=mkt.id,
                wager=wager, odds_at_placement=mkt.odds, payout_if_win=0, status="PENDING",
            ))
        db_user.chips -= wager
        db_user.total_wagered += wager
        await db.commit()
        chips_left = db_user.chips
        labels = [m.label for m in leg_markets]

    asyncio.create_task(post_bet_log(_GUILD_ID(), user.discord_id, "PARLAY", labels, wager, total_payout, is_tail=True))
    return {"ok": True, "total_payout": total_payout, "chips": chips_left,
            "message": f"Parlay tailed! Potential payout: {total_payout:,} chips."}


@router.post("/tail/{template_id}/add-to-slip")
async def add_template_to_slip(template_id: int, user: SessionUser = Depends(bearer_user)):
    """Copy a tail-board template's legs into the caller's own parlay slip so
    they can edit (add/remove legs, change wager) before submitting, instead of
    only being able to tail the template as a fixed package."""
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, template_id)
        if not tpl or not tpl.active:
            raise HTTPException(status_code=404, detail="Template not found.")

        tpl_legs = (await db.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == template_id)
            .order_by(ParlayTemplateLeg.sort_order)
        )).scalars().all()

        tpl_markets = []
        for leg in tpl_legs:
            mkt = await db.get(Market, leg.market_id)
            if mkt and mkt.status == "OPEN":
                tpl_markets.append(mkt)
        if not tpl_markets:
            raise HTTPException(status_code=400, detail="No open markets in this parlay to add.")

        added, skipped = await add_markets_to_pending_slip(
            db, _GUILD_ID(), user.discord_id, tpl_markets
        )
        await db.commit()

    if added == 0:
        raise HTTPException(
            status_code=400,
            detail="Couldn't add any legs — they're already in your slip or conflict with what's there.",
        )
    message = f"Added {added} leg{'s' if added != 1 else ''} to your parlay slip."
    if skipped:
        message += f" Skipped {skipped} (already in slip or conflicting)."
    return {"ok": True, "message": message, "added": added, "skipped": skipped}


@router.post("/tail/parlay/{parlay_id}")
async def tail_member_parlay(
    parlay_id: int,
    user: SessionUser = Depends(bearer_user),
    wager: Annotated[int, Body(embed=True)] = 0,
):
    """Tail another member's public, still-pending parlay — lives in its own id
    space from ParlayTemplate, hence the separate /tail/parlay/ route."""
    if wager < 1:
        raise HTTPException(status_code=400, detail="Wager must be at least 1 chip.")
    if await _paused():
        raise HTTPException(status_code=423, detail=BETTING_PAUSED_MSG)

    async with get_db() as db:
        source = await db.get(Parlay, parlay_id)
        if not source or not source.is_public or source.status != "PENDING":
            raise HTTPException(status_code=404, detail="Parlay not found or no longer available.")

        db_user = await _get_or_create_user(db, user)
        if await is_fully_restricted(db, _GUILD_ID(), user.discord_id):
            raise HTTPException(status_code=403, detail=BETTING_BLOCKED_MSG)

        legs = (await db.execute(
            select(Bet).where(Bet.parlay_id == parlay_id).order_by(Bet.id)
        )).scalars().all()

        leg_markets = []
        for b in legs:
            mkt = await db.get(Market, b.market_id)
            if not mkt or mkt.status != "OPEN":
                raise HTTPException(status_code=400, detail="One or more markets in this parlay are no longer open.")
            leg_markets.append(mkt)

        if len(leg_markets) < 2:
            raise HTTPException(status_code=400, detail="Parlay has insufficient open markets.")
        if db_user.chips < wager:
            raise HTTPException(status_code=400, detail="Insufficient chips.")

        odds_list = [m.odds for m in leg_markets]
        cap_err = await _parlay_cap_error_activity(wager, odds_list)
        if cap_err:
            raise HTTPException(status_code=400, detail=cap_err)
        total_payout = parlay_payout(wager, odds_list)

        # Tailed copies are private by default; record provenance (unless the
        # member tailed their own board listing) so the original poster gets
        # credit on the "Most Tailed Parlays" leaderboard — mirrors the bot's
        # `/parlay tail` flow in bot/cogs/betting.py.
        tailed_from_user_id = source.user_id if source.user_id != user.discord_id else None
        tailed_from_parlay_id = source.id if tailed_from_user_id is not None else None

        p = Parlay(
            guild_id=_GUILD_ID(), user_id=user.discord_id, total_wager=wager, total_payout=total_payout,
            payout_rate_at_placement=await _payout_rate_activity(db, user),
            status="PENDING", is_public=False,
            tailed_from_user_id=tailed_from_user_id, tailed_from_parlay_id=tailed_from_parlay_id,
        )
        db.add(p)
        await db.flush()
        for mkt in leg_markets:
            db.add(Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id, parlay_id=p.id, market_id=mkt.id,
                wager=wager, odds_at_placement=mkt.odds, payout_if_win=0, status="PENDING",
            ))
        db_user.chips -= wager
        db_user.total_wagered += wager
        await db.commit()
        chips_left = db_user.chips
        labels = [m.label for m in leg_markets]

    asyncio.create_task(post_bet_log(_GUILD_ID(), user.discord_id, "PARLAY", labels, wager, total_payout, is_tail=True))
    return {"ok": True, "total_payout": total_payout, "chips": chips_left,
            "message": f"Parlay tailed! Potential payout: {total_payout:,} chips."}


@router.post("/tail/parlay/{parlay_id}/add-to-slip")
async def add_member_parlay_to_slip(parlay_id: int, user: SessionUser = Depends(bearer_user)):
    """Copy a public member parlay's legs into the caller's own parlay slip so
    they can edit it before submitting, instead of only being able to tail it."""
    async with get_db() as db:
        source = await db.get(Parlay, parlay_id)
        if not source or not source.is_public or source.status != "PENDING":
            raise HTTPException(status_code=404, detail="Parlay not found or no longer available.")

        legs = (await db.execute(
            select(Bet).where(Bet.parlay_id == parlay_id).order_by(Bet.id)
        )).scalars().all()

        leg_markets = []
        for b in legs:
            mkt = await db.get(Market, b.market_id)
            if mkt and mkt.status == "OPEN":
                leg_markets.append(mkt)
        if not leg_markets:
            raise HTTPException(status_code=400, detail="No open markets in this parlay to add.")

        added, skipped = await add_markets_to_pending_slip(
            db, _GUILD_ID(), user.discord_id, leg_markets
        )
        await db.commit()

    if added == 0:
        raise HTTPException(
            status_code=400,
            detail="Couldn't add any legs — they're already in your slip or conflict with what's there.",
        )
    message = f"Added {added} leg{'s' if added != 1 else ''} to your parlay slip."
    if skipped:
        message += f" Skipped {skipped} (already in slip or conflicting)."
    return {"ok": True, "message": message, "added": added, "skipped": skipped}


# ── Admin: live-game operations ──────────────────────────────────────────────
# Kept deliberately small (live ops). Full admin parity remains in the web
# dashboard; new actions can be added here and surfaced by the generic admin UI.

async def _settle_parlay(db, parlay_id: int) -> None:
    parlay = await db.get(Parlay, parlay_id)
    if not parlay or parlay.status != "PENDING":
        return
    legs = (await db.execute(select(Bet).where(Bet.parlay_id == parlay_id))).scalars().all()
    statuses = [leg.status for leg in legs]
    if "LOST" in statuses:
        parlay.status = "LOST"
        await promos.award_settle_rebate(
            db, parlay.guild_id, parlay.user_id,
            wager_placed=max(0, parlay.total_wager - parlay.bonus_bet_amount),
            won=False, parlay_id=parlay.id,
        )
    elif "PENDING" not in statuses:
        active = [l for l in legs if l.status != "VOIDED"]
        if all(l.status == "WON" for l in active) and active:
            parlay.status = "WON"
            hc_config = await load_house_cut_config()
            paid, cut = net_payout(
                hc_config, wager=parlay.total_wager, payout_if_win=parlay.total_payout,
                market_type=None, odds=parlay_effective_odds(parlay.total_wager, parlay.total_payout),
                payout_rate=parlay.payout_rate_at_placement, cap=await get_payout_cap("PARLAY"),
            )
            parlay.house_cut = cut
            await record_house_cut_taken(cut)
            db_user = await _fetch_user(db, parlay.user_id)
            credited, real_won, bonus_won = promos.split_won_credit(
                parlay.total_wager, parlay.bonus_bet_amount, paid
            )
            if db_user:
                db_user.chips += credited
                db_user.total_won += real_won
                db_user.bonus_won += bonus_won
            await promos.award_settle_rebate(
                db, parlay.guild_id, parlay.user_id,
                wager_placed=max(0, parlay.total_wager - parlay.bonus_bet_amount),
                won=True, parlay_id=parlay.id,
            )
        elif all(l.status == "VOIDED" for l in legs):
            parlay.status = "WON"
            db_user = await _fetch_user(db, parlay.user_id)
            refund = promos.void_chip_refund(parlay.total_wager, parlay.bonus_bet_amount)
            if db_user:
                db_user.chips += refund
            if parlay.bonus_bet_amount:
                await promos.refund_bonus(
                    db, parlay.guild_id, parlay.user_id, parlay.bonus_bet_amount
                )
            if parlay.profit_boost_token_id:
                await promos.restore_boost_for_parlay(db, parlay.guild_id, parlay.id)


@router.get("/banners")
async def banners(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        row = (await db.execute(text("SELECT value FROM game_settings WHERE key='activity_banners'"))).fetchone()
    items = json.loads(row[0]) if row else []
    return {"banners": items}


@router.post("/admin/banners/add")
async def admin_banners_add(
    admin: SessionUser = Depends(bearer_admin),
    title: Annotated[str, Body()] = "",
    subtitle: Annotated[str, Body()] = "",
    emoji: Annotated[str, Body()] = "🏆",
    color: Annotated[str, Body()] = "",
    cta: Annotated[str, Body()] = "",
):
    if not title.strip():
        raise HTTPException(status_code=400, detail="Title is required.")
    import time
    banner = {
        "id": str(int(time.time() * 1000)),
        "title": title.strip()[:80],
        "subtitle": subtitle.strip()[:120],
        "emoji": emoji.strip()[:8] or "🏆",
        "color": color.strip()[:20],
        "cta": cta.strip()[:30],
    }
    async with get_db() as db:
        row = (await db.execute(text("SELECT value FROM game_settings WHERE key='activity_banners'"))).fetchone()
        existing = json.loads(row[0]) if row else []
        existing.append(banner)
        if row:
            await db.execute(
                text("UPDATE game_settings SET value=:v WHERE key='activity_banners'"),
                {"v": json.dumps(existing)},
            )
        else:
            await db.execute(
                text("INSERT INTO game_settings (key, value) VALUES ('activity_banners', :v)"),
                {"v": json.dumps(existing)},
            )
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Banner added", {"title": title.strip()[:80]}, source="Discord Activity"))
    return {"ok": True, "message": "Banner added.", "banner": banner}


@router.delete("/admin/banners/{banner_id}")
async def admin_banners_delete(banner_id: str, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        row = (await db.execute(text("SELECT value FROM game_settings WHERE key='activity_banners'"))).fetchone()
        existing = json.loads(row[0]) if row else []
        updated = [b for b in existing if b.get("id") != banner_id]
        if row:
            await db.execute(
                text("UPDATE game_settings SET value=:v WHERE key='activity_banners'"),
                {"v": json.dumps(updated)},
            )
            await db.commit()
    asyncio.create_task(post_admin_action(admin, "Banner removed", {"banner_id": banner_id}, source="Discord Activity"))
    return {"ok": True, "message": "Banner removed."}


@router.get("/admin/parlays")
async def admin_parlays_list(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        templates_raw = (await db.execute(
            select(ParlayTemplate).order_by(ParlayTemplate.created_at.desc())
        )).scalars().all()
        out = []
        for tpl in templates_raw:
            legs = (await db.execute(
                select(ParlayTemplateLeg)
                .where(ParlayTemplateLeg.template_id == tpl.id)
                .order_by(ParlayTemplateLeg.sort_order)
            )).scalars().all()
            leg_out = []
            odds_list = []
            for leg in legs:
                mkt = await db.get(Market, leg.market_id)
                if not mkt:
                    continue
                leg_out.append({"leg_id": leg.id, "market_id": mkt.id, "label": mkt.label, "odds": mkt.odds})
                if mkt.status == "OPEN":
                    odds_list.append(mkt.odds)
            out.append({
                "id": tpl.id,
                "name": tpl.name,
                "description": tpl.description,
                "difficulty": tpl.difficulty,
                "active": tpl.active,
                "source": tpl.source,
                "combined_odds": combined_american(odds_list) if len(odds_list) >= 2 else None,
                "legs": leg_out,
            })
    return {"templates": out}


@router.post("/admin/parlays/create")
async def admin_parlays_create(
    admin: SessionUser = Depends(bearer_admin),
    name: Annotated[str, Body()] = "",
    description: Annotated[str, Body()] = "",
):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    async with get_db() as db:
        tpl = ParlayTemplate(
            name=name.strip()[:100],
            description=(description.strip() or None),
            source="ADMIN",
            active=False,
        )
        db.add(tpl)
        await db.commit()
        await db.refresh(tpl)
    asyncio.create_task(post_admin_action(admin, "Parlay template created", {"name": tpl.name}, source="Discord Activity"))
    return {"ok": True, "message": "Template created.", "id": tpl.id}


@router.post("/admin/parlays/{tpl_id}/add-leg")
async def admin_parlays_add_leg(
    tpl_id: int,
    admin: SessionUser = Depends(bearer_admin),
    market_id: Annotated[int, Body()] = 0,
):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found.")
        new_mkt = await db.get(Market, market_id)
        if not new_mkt:
            raise HTTPException(status_code=404, detail="Market not found.")

        existing_legs = (await db.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == tpl_id)
            .order_by(ParlayTemplateLeg.sort_order)
        )).scalars().all()
        if any(leg.market_id == market_id for leg in existing_legs):
            raise HTTPException(status_code=400, detail="That market is already a leg on this template.")

        existing_markets = []
        for leg in existing_legs:
            mkt = await db.get(Market, leg.market_id)
            if mkt:
                existing_markets.append(mkt)

        tribute_by_id = await tribute_lookup_for_markets(db, existing_markets + [new_mkt])
        conflict = _parlay_conflict(existing_markets, new_mkt, tribute_by_id)
        if conflict:
            raise HTTPException(status_code=400, detail=conflict)

        leg = ParlayTemplateLeg(template_id=tpl_id, market_id=market_id, sort_order=len(existing_legs))
        db.add(leg)
        await db.commit()
    asyncio.create_task(post_admin_action(
        admin, "Parlay template leg added", {"template": tpl.name, "market_id": str(market_id)}, source="Discord Activity",
    ))
    return {"ok": True, "message": "Leg added."}


@router.post("/admin/parlays/{tpl_id}/remove-leg")
async def admin_parlays_remove_leg(
    tpl_id: int,
    admin: SessionUser = Depends(bearer_admin),
    leg_id: Annotated[int, Body()] = 0,
):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        leg = await db.get(ParlayTemplateLeg, leg_id)
        if not tpl or not leg or leg.template_id != tpl_id:
            raise HTTPException(status_code=404, detail="Leg not found.")
        await db.delete(leg)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Parlay template leg removed", {"template": tpl.name}, source="Discord Activity"))
    return {"ok": True, "message": "Leg removed."}


@router.post("/admin/parlays/{tpl_id}/toggle")
async def admin_parlays_toggle(tpl_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found.")
        tpl.active = not tpl.active
        await db.commit()
        active = tpl.active
    asyncio.create_task(post_admin_action(admin, "Parlay template toggled", {"template": tpl.name, "active": str(active)}, source="Discord Activity"))
    return {"ok": True, "message": f"Template {'activated' if active else 'deactivated'}.", "active": active}


@router.delete("/admin/parlays/{tpl_id}")
async def admin_parlays_delete(tpl_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found.")
        name = tpl.name
        await db.delete(tpl)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Parlay template deleted", {"template": name}, source="Discord Activity"))
    return {"ok": True, "message": "Template deleted."}


# ── Exchange rates + public-parlay blocks ──────────────────────────────────────
# Named to avoid colliding with the existing /admin/economy (aggregate totals, below).

def _override_dict(o: ExchangeRateOverride) -> dict:
    return {"id": o.id, "scope": o.scope, "target_id": str(o.target_id), "direction": o.direction, "rate": o.rate}


def _block_dict(b: PublicBetRestriction) -> dict:
    return {"id": b.id, "scope": b.scope, "target_id": str(b.target_id)}


@router.get("/admin/payout-caps")
async def admin_payout_caps(admin: SessionUser = Depends(bearer_admin)):
    return {
        "single_payout_cap": await get_payout_cap("SINGLE"),
        "parlay_payout_cap": await get_payout_cap("PARLAY"),
    }


@router.post("/admin/payout-caps")
async def admin_payout_caps_set(
    admin: SessionUser = Depends(bearer_admin),
    single_payout_cap: Annotated[int, Body()] = 0,
    parlay_payout_cap: Annotated[int, Body()] = 0,
):
    if single_payout_cap <= 0 or parlay_payout_cap <= 0:
        raise HTTPException(status_code=400, detail="Payout caps must be positive.")
    await set_payout_cap("SINGLE", single_payout_cap)
    await set_payout_cap("PARLAY", parlay_payout_cap)
    asyncio.create_task(post_admin_action(
        admin, "Payout caps updated",
        {"single": str(single_payout_cap), "parlay": str(parlay_payout_cap)},
        source="Discord Activity",
    ))
    return {"ok": True, "message": "Payout caps updated."}


@router.get("/admin/house-cut")
async def admin_house_cut(admin: SessionUser = Depends(bearer_admin)):
    cfg = await load_house_cut_config()
    async with get_db() as db:
        tpls = (await db.execute(
            select(MarketTemplate).where(MarketTemplate.active == True)
            .order_by(MarketTemplate.is_builtin.desc(), MarketTemplate.name)
        )).scalars().all()
    market_types = [
        {"value": t.type_key or f"CUSTOM_{t.id}",
         "label": t.name if t.is_builtin else f"[Custom] {t.name}"}
        for t in tpls
    ]
    labels = {mt["value"]: mt["label"] for mt in market_types}
    return {
        "global_pct": cfg.global_pct,
        "high_odds_threshold": cfg.high_odds_threshold,
        "high_odds_pct": cfg.high_odds_pct,
        "by_type": [
            {"type": k, "label": labels.get(k, k), "pct": v}
            for k, v in sorted(cfg.by_type.items())
        ],
        "market_types": market_types,
        "total_taken": await get_house_cut_total_taken(),
    }


@router.post("/admin/house-cut")
async def admin_house_cut_set(
    admin: SessionUser = Depends(bearer_admin),
    global_pct: Annotated[float, Body()] = 0.0,
    high_odds_threshold: Annotated[int | None, Body()] = None,
    high_odds_pct: Annotated[float, Body()] = 0.0,
):
    if not 0.0 <= global_pct <= 100.0 or not 0.0 <= high_odds_pct <= 100.0:
        raise HTTPException(status_code=400, detail="House-cut percentages must be between 0 and 100.")
    if high_odds_threshold is not None and high_odds_threshold < 1:
        raise HTTPException(status_code=400, detail="High-odds threshold must be a positive number, or omitted to disable.")
    await set_house_cut_global(global_pct)
    await set_house_cut_high_odds_rule(high_odds_threshold, high_odds_pct)
    asyncio.create_task(post_admin_action(
        admin, "House cut updated",
        {"global_pct": str(global_pct), "high_odds_threshold": str(high_odds_threshold),
         "high_odds_pct": str(high_odds_pct)},
        source="Discord Activity",
    ))
    return {"ok": True, "message": "House cut updated."}


@router.post("/admin/house-cut/type")
async def admin_house_cut_type_set(
    admin: SessionUser = Depends(bearer_admin),
    market_type: Annotated[str, Body()] = "",
    pct: Annotated[float | None, Body()] = None,
):
    market_type = market_type.strip()
    if not market_type:
        raise HTTPException(status_code=400, detail="market_type is required.")
    if pct is not None and not 0.0 <= pct <= 100.0:
        raise HTTPException(status_code=400, detail="Per-type house cut must be between 0 and 100.")
    await set_house_cut_for_type(market_type, pct)
    asyncio.create_task(post_admin_action(
        admin, "House cut type override cleared" if pct is None else "House cut type override set",
        {"market_type": market_type, "pct": "—" if pct is None else str(pct)},
        source="Discord Activity",
    ))
    return {"ok": True, "message": "Per-type house cut cleared." if pct is None else "Per-type house cut updated."}


@router.get("/admin/exchange-rates")
async def admin_exchange_rates(admin: SessionUser = Depends(bearer_admin)):
    deposit_rate = await get_global_rate("DEPOSIT")
    withdraw_rate = await get_global_rate("WITHDRAW")
    payout_rate = await get_global_rate("PAYOUT")
    async with get_db() as db:
        overrides = await list_overrides(db, _GUILD_ID())
    return {
        "global_deposit_rate": deposit_rate,
        "global_withdraw_rate": withdraw_rate,
        "global_payout_rate": payout_rate,
        "overrides": [_override_dict(o) for o in overrides],
    }


@router.post("/admin/exchange-rates/global")
async def admin_exchange_rates_global(
    admin: SessionUser = Depends(bearer_admin),
    deposit_rate: Annotated[float, Body()] = 1.0,
    withdraw_rate: Annotated[float, Body()] = 1.0,
    payout_rate: Annotated[float, Body()] = 1.0,
):
    if deposit_rate <= 0 or withdraw_rate <= 0 or payout_rate <= 0:
        raise HTTPException(status_code=400, detail="Rates must be positive.")
    await set_global_rate("DEPOSIT", deposit_rate)
    await set_global_rate("WITHDRAW", withdraw_rate)
    await set_global_rate("PAYOUT", payout_rate)
    asyncio.create_task(post_admin_action(
        admin, "Global exchange rates updated",
        {"deposit_rate": str(deposit_rate), "withdraw_rate": str(withdraw_rate),
         "payout_rate": str(payout_rate)},
        source="Discord Activity",
    ))
    return {"ok": True, "message": "Global rates updated."}


@router.post("/admin/exchange-rates")
async def admin_exchange_rates_set(
    admin: SessionUser = Depends(bearer_admin),
    scope: Annotated[str, Body()] = "USER",
    target_id: Annotated[str, Body()] = "",
    direction: Annotated[str, Body()] = "PAYOUT",
    rate: Annotated[float, Body()] = 1.0,
):
    if scope not in ("ROLE", "USER") or direction != "PAYOUT":
        raise HTTPException(status_code=400, detail="Invalid scope or direction.")
    if rate <= 0:
        raise HTTPException(status_code=400, detail="Rate must be positive.")
    try:
        tid = int(target_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Target ID must be a number.")
    async with get_db() as db:
        await set_override(db, _GUILD_ID(), scope, tid, direction, rate)
        await db.commit()
    asyncio.create_task(post_admin_action(
        admin, "Exchange rate override set",
        {"scope": scope, "target_id": str(tid), "direction": direction, "rate": str(rate)},
        source="Discord Activity",
    ))
    return {"ok": True, "message": "Rate override saved."}


@router.delete("/admin/exchange-rates/{override_id}")
async def admin_exchange_rates_delete(override_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        row = await db.get(ExchangeRateOverride, override_id)
        if row:
            await db.delete(row)
            await db.commit()
    asyncio.create_task(post_admin_action(admin, "Exchange rate override removed", {"id": str(override_id)}, source="Discord Activity"))
    return {"ok": True, "message": "Override removed."}


@router.get("/admin/public-blocks")
async def admin_public_blocks(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        blocks = await list_public_blocks(db, _GUILD_ID())
    return {"blocks": [_block_dict(b) for b in blocks]}


@router.post("/admin/public-blocks")
async def admin_public_blocks_set(
    admin: SessionUser = Depends(bearer_admin),
    scope: Annotated[str, Body()] = "USER",
    target_id: Annotated[str, Body()] = "",
):
    if scope not in ("ROLE", "USER"):
        raise HTTPException(status_code=400, detail="Invalid scope.")
    try:
        tid = int(target_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Target ID must be a number.")
    async with get_db() as db:
        await set_public_block(db, _GUILD_ID(), scope, tid, True)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Public-parlay block added", {"scope": scope, "target_id": str(tid)}, source="Discord Activity"))
    return {"ok": True, "message": "Block added."}


@router.delete("/admin/public-blocks/{block_id}")
async def admin_public_blocks_delete(block_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        row = await db.get(PublicBetRestriction, block_id)
        if row:
            await db.delete(row)
            await db.commit()
    asyncio.create_task(post_admin_action(admin, "Public-parlay block removed", {"id": str(block_id)}, source="Discord Activity"))
    return {"ok": True, "message": "Block removed."}


@router.get("/admin/users")
async def admin_users(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        users = (await db.execute(
            select(User).where(User.guild_id == _GUILD_ID()).order_by(User.chips.desc())
        )).scalars().all()
    return {"users": [_user_dict(u) for u in users]}


@router.get("/admin/economy")
async def admin_economy(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        economy = await economy_totals(db, _GUILD_ID())
    return economy


@router.get("/admin/game/status")
async def admin_game_status(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        active_raw = (await db.execute(
            text("SELECT value FROM game_settings WHERE key='game_active'")
        )).fetchone()
        game_active = bool(active_raw) and json.loads(active_raw[0])
        phase_name = await _phase_name(db)
    return {"game_active": game_active, "phase_name": phase_name}


@router.post("/admin/game/start")
async def admin_game_start(admin: SessionUser = Depends(bearer_admin)):
    # _start_game() lives in the bot cog and is shared verbatim with the
    # /game start Discord command so the two never drift. It reaches the DB
    # through bot.database.engine (get_setting/set_setting/get_session), which
    # resolves the guild from its own contextvar rather than web/database.py's
    # — bind that here, scoped to this request's task, before calling in.
    from bot.cogs.admin import _start_game
    from bot.database.engine import set_guild_context

    set_guild_context(get_request_guild())
    result = await _start_game()
    if result.get("error") == "already_active":
        raise HTTPException(
            status_code=400,
            detail="The Games are already running. End them first before starting a new one.",
        )

    message = f"Opened {result['opened']} market(s)"
    if result["phase_name"]:
        message += f" ({result['phase_name']} phase)"
    message += ". May the odds be ever in your favor."
    if result["auto_parlays"]:
        message += f" {result['auto_parlays']} auto-parlay(s) posted to the tailing board."

    asyncio.create_task(post_admin_action(
        admin, "Game started",
        {"phase": result["phase_name"] or "—", "markets opened": str(result["opened"])},
        source="Discord Activity",
    ))
    return {"ok": True, "message": message, **result}


@router.post("/admin/market/{market_id}/open")
async def admin_market_open(market_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")
        m.status = "OPEN"
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Market opened", {"market": m.label}, source="Discord Activity"))
    return {"ok": True, "message": "Market opened."}


@router.post("/admin/market/{market_id}/close")
async def admin_market_close(market_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")
        m.status = "CLOSED"
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Market closed", {"market": m.label}, source="Discord Activity"))
    return {"ok": True, "message": "Market closed."}


@router.post("/admin/market/{market_id}/resolve")
async def admin_market_resolve(
    market_id: int,
    admin: SessionUser = Depends(bearer_admin),
    result: Annotated[str, Body(embed=True)] = "",
):
    # result: "true" | "false" | "void"
    bool_result: bool | None = None if result == "void" else (result == "true")

    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")

        m.status = "RESOLVED"
        m.result = bool_result

        bets = (await db.execute(
            select(Bet).where(Bet.market_id == market_id, Bet.status == "PENDING")
        )).scalars().all()

        hc_config = await load_house_cut_config()
        single_cap = await get_payout_cap("SINGLE")
        cut_taken = 0
        for bet in bets:
            if bool_result is None:
                bet.status = "VOIDED"
                db_user = await _fetch_user(db, bet.user_id)
                refund = promos.void_chip_refund(bet.wager, bet.bonus_bet_amount)
                if db_user:
                    db_user.chips += refund
                if bet.bonus_bet_amount and bet.parlay_id is None:
                    await promos.refund_bonus(db, bet.guild_id, bet.user_id, bet.bonus_bet_amount)
                if bet.profit_boost_token_id and bet.parlay_id is None:
                    await promos.restore_boost_for_bet(db, bet.guild_id, bet.id)
            elif bool_result and bet.parlay_id is None:
                bet.status = "WON"
                paid, cut = net_payout(
                    hc_config, wager=bet.wager, payout_if_win=bet.payout_if_win,
                    market_type=m.type, odds=bet.odds_at_placement,
                    payout_rate=bet.payout_rate_at_placement, cap=single_cap,
                )
                bet.house_cut = cut
                cut_taken += cut
                db_user = await _fetch_user(db, bet.user_id)
                credited, real_won, bonus_won = promos.split_won_credit(
                    bet.wager, bet.bonus_bet_amount, paid
                )
                if db_user:
                    db_user.chips += credited
                    db_user.total_won += real_won
                    db_user.bonus_won += bonus_won
                await promos.award_settle_rebate(
                    db, bet.guild_id, bet.user_id,
                    wager_placed=max(0, bet.wager - bet.bonus_bet_amount),
                    won=True, bet_id=bet.id,
                )
            elif not bool_result and bet.parlay_id is None:
                bet.status = "LOST"
                await promos.award_settle_rebate(
                    db, bet.guild_id, bet.user_id,
                    wager_placed=max(0, bet.wager - bet.bonus_bet_amount),
                    won=False, bet_id=bet.id,
                )
            elif bet.parlay_id is not None:
                bet.status = "WON" if bool_result else ("VOIDED" if bool_result is None else "LOST")
                await _settle_parlay(db, bet.parlay_id)

        await record_house_cut_taken(cut_taken)
        await db.commit()
        settled = len(bets)

    label = "WON" if bool_result else ("VOIDED" if bool_result is None else "LOST")
    asyncio.create_task(post_admin_action(admin, "Market resolved", {"market": m.label, "result": label, "bets settled": str(settled)}, source="Discord Activity"))
    return {"ok": True, "message": f"Market resolved as {label}. {settled} bets settled."}


@router.post("/admin/chips/give")
async def admin_chips_give(
    admin: SessionUser = Depends(bearer_admin),
    discord_id: Annotated[str, Body()] = "",
    amount: Annotated[int, Body()] = 0,
):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    try:
        uid = int(discord_id.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Discord ID.")
    async with get_db() as db:
        db_user = await _fetch_user(db, uid)
        if not db_user:
            raise HTTPException(status_code=404, detail="User not found in database.")
        db_user.chips += amount
        await db.commit()
        name = db_user.username
    asyncio.create_task(post_admin_action(admin, "Chips given", {"user": name, "amount": f"{amount:,}"}, source="Discord Activity"))
    return {"ok": True, "message": f"Gave {amount:,} chips to {name}."}


@router.post("/admin/chips/take")
async def admin_chips_take(
    admin: SessionUser = Depends(bearer_admin),
    discord_id: Annotated[str, Body()] = "",
    amount: Annotated[int, Body()] = 0,
):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    try:
        uid = int(discord_id.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Discord ID.")
    async with get_db() as db:
        db_user = await _fetch_user(db, uid)
        if not db_user:
            raise HTTPException(status_code=404, detail="User not found.")
        db_user.chips = max(0, db_user.chips - amount)
        await db.commit()
        name = db_user.username
    asyncio.create_task(post_admin_action(admin, "Chips taken", {"user": name, "amount": f"{amount:,}"}, source="Discord Activity"))
    return {"ok": True, "message": f"Took {amount:,} chips from {name}."}


@router.post("/admin/tribute/{tribute_id}/kill")
async def admin_tribute_kill(
    tribute_id: int,
    admin: SessionUser = Depends(bearer_admin),
    death_cause: Annotated[str, Body()] = "Another Tribute",
    killed_by_id: Annotated[str, Body()] = "",
    placement: Annotated[int, Body()] = 0,
):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t or t.status != "ALIVE":
            raise HTTPException(status_code=400, detail="Tribute not found or already dead.")
        in_bloodbath = await _phase_name(db) == "Bloodbath"
        t.status = "DEAD"
        t.death_cause = death_cause
        t.placement = placement if placement > 0 else None
        if killed_by_id.strip():
            killer = await db.get(Tribute, int(killed_by_id))
            t.killed_by_id = int(killed_by_id)
            if killer:
                killer.kills = (killer.kills or 0) + 1
                if in_bloodbath:
                    killer.bloodbath_kills = (killer.bloodbath_kills or 0) + 1
        await db.commit()
        name = t.name
    asyncio.create_task(post_admin_action(admin, "Tribute eliminated", {"tribute": name, "cause": death_cause}, source="Discord Activity"))
    return {"ok": True, "message": f"{name} has been eliminated."}


@router.post("/admin/tribute/{tribute_id}/victor")
async def admin_tribute_victor(tribute_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t:
            raise HTTPException(status_code=404, detail="Tribute not found.")
        t.status = "VICTOR"
        t.placement = 1
        await db.commit()
        name = t.name
    asyncio.create_task(post_admin_action(admin, "Victor crowned", {"tribute": name}, source="Discord Activity"))
    return {"ok": True, "message": f"{name} crowned Victor!"}


@router.post("/admin/tribute/{tribute_id}/unkill")
async def admin_tribute_unkill(tribute_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t or t.status == "ALIVE":
            raise HTTPException(status_code=400, detail="Tribute not found or already alive.")
        in_bloodbath = await _phase_name(db) == "Bloodbath"
        if t.killed_by_id:
            killer = await db.get(Tribute, t.killed_by_id)
            if killer:
                killer.kills = max(0, (killer.kills or 1) - 1)
                if in_bloodbath and (killer.bloodbath_kills or 0) > 0:
                    killer.bloodbath_kills -= 1
        t.status = "ALIVE"
        t.death_cause = None
        t.placement = None
        t.killed_by_id = None
        await db.commit()
        name = t.name
    asyncio.create_task(post_admin_action(admin, "Tribute revived", {"tribute": name}, source="Discord Activity"))
    return {"ok": True, "message": f"{name} revived."}


@router.post("/admin/market/{market_id}/reopen")
async def admin_market_reopen(market_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")
        m.status = "CLOSED"
        m.result = None
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Market reopened", {"market": m.label}, source="Discord Activity"))
    return {"ok": True, "message": "Market reopened as Closed."}


@router.post("/admin/market/{market_id}/set-odds")
async def admin_market_set_odds(
    market_id: int,
    admin: SessionUser = Depends(bearer_admin),
    odds: Annotated[int, Body(embed=True)] = -110,
):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")
        m.odds = odds
        m.odds_override = True
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Market odds set", {"market": m.label, "odds": f"{odds:+d}"}, source="Discord Activity"))
    return {"ok": True, "message": f"Odds set to {odds:+d}."}


@router.post("/admin/market/{market_id}/clear-override")
async def admin_market_clear_override(market_id: int, admin: SessionUser = Depends(bearer_admin)):
    from bot.cogs.admin import _recalculate_markets
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            raise HTTPException(status_code=404, detail="Market not found.")
        m.odds_override = False
        await _recalculate_markets(db)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Market override cleared", {"market": m.label}, source="Discord Activity"))
    return {"ok": True, "message": "Override cleared and odds recalculated."}


@router.post("/admin/markets/recalc")
async def admin_markets_recalc(admin: SessionUser = Depends(bearer_admin)):
    from bot.cogs.admin import _recalculate_markets
    async with get_db() as db:
        await _recalculate_markets(db)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Odds recalculated", source="Discord Activity"))
    return {"ok": True, "message": "Odds recalculated."}


@router.post("/admin/markets/bulk-close")
async def admin_markets_bulk_close(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        markets = (await db.execute(select(Market).where(Market.status == "OPEN"))).scalars().all()
        for m in markets:
            m.status = "CLOSED"
        await db.commit()
        count = len(markets)
    asyncio.create_task(post_admin_action(admin, "Bulk market close", {"markets closed": str(count)}, source="Discord Activity"))
    return {"ok": True, "message": f"Closed {count} open markets."}


@router.post("/admin/chips/give-all")
async def admin_chips_give_all(
    admin: SessionUser = Depends(bearer_admin),
    amount: Annotated[int, Body(embed=True)] = 0,
):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    async with get_db() as db:
        users = (await db.execute(
            select(User).where(User.guild_id == _GUILD_ID())
        )).scalars().all()
        for u in users:
            u.chips += amount
        await db.commit()
        count = len(users)
    asyncio.create_task(post_admin_action(admin, "Chips given to all", {"amount": f"{amount:,}", "players": str(count)}, source="Discord Activity"))
    return {"ok": True, "message": f"Gave {amount:,} chips to {count} players."}


@router.post("/admin/chips/set")
async def admin_chips_set(
    admin: SessionUser = Depends(bearer_admin),
    discord_id: Annotated[str, Body()] = "",
    amount: Annotated[int, Body()] = 0,
):
    if amount < 0:
        raise HTTPException(status_code=400, detail="Amount cannot be negative.")
    try:
        uid = int(discord_id.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Discord ID.")
    async with get_db() as db:
        db_user = await _fetch_user(db, uid)
        if not db_user:
            raise HTTPException(status_code=404, detail="User not found.")
        db_user.chips = amount
        await db.commit()
        name = db_user.username
    asyncio.create_task(post_admin_action(admin, "Chips set", {"user": name, "amount": f"{amount:,}"}, source="Discord Activity"))
    return {"ok": True, "message": f"Set {name} balance to {amount:,}."}


# ── Promotions admin (Discord Activity) ──────────────────────────────────────


def _promo_expiry_hours(days, hours) -> int | None:
    total = int(days or 0) * 24 + int(hours or 0)
    return total if total > 0 else None


def _iso_to_naive_utc(s: str) -> datetime:
    """Parse an ISO datetime (with or without a zone) to a naive-UTC datetime —
    the form every promo timestamp is stored and compared in. A bare
    ``datetime-local`` string (no zone) is taken as already-UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def _promo_resolve_target_ids(scope: str, target_id) -> tuple[list[int], str | None]:
    """Only per-user grants are supported. Bulk role/server distribution goes
    through a claim drop (/promo drop in Discord) instead of enumerating members
    (which would need the SERVER MEMBERS privileged intent)."""
    if (scope or "").upper() != "USER":
        return [], ("Only per-user grants are supported here — use /promo drop "
                    "in Discord to reach a role or the whole server.")
    try:
        return [int(str(target_id).strip())], None
    except (TypeError, ValueError):
        return [], "Invalid Discord user ID."


@router.get("/my-boosts")
async def my_boosts(market_id: int = 0, user: SessionUser = Depends(bearer_user)):
    """Profit-boost tokens the caller can apply — filtered to a market if given,
    else every active token with a short scope description."""
    async with get_db() as db:
        gid = _GUILD_ID()
        await promos.expire_stale(db, gid, user.discord_id)
        await db.commit()
        markets = []
        if market_id:
            m = await db.get(Market, market_id)
            if m is not None:
                markets = [m]
        if markets:
            toks = await promos.eligible_boost_tokens(db, gid, user.discord_id, markets)
        else:
            toks = await promos.active_boost_tokens(db, gid, user.discord_id)
        bal = await promos.bonus_balance(db, gid, user.discord_id)
    def _desc(t):
        s = t.scope_type.title()
        if t.scope_type == "DISTRICT":
            s = f"District {t.scope_id}"
        elif t.scope_type == "ALLIANCE":
            s = f"Alliance #{t.scope_id}"
        return f"+{t.boost_pct:g}% · {s}"
    return {
        "bonus_balance": bal,
        "boosts": [
            {"id": t.id, "label": _desc(t), "pct": t.boost_pct,
             "scope_type": t.scope_type, "scope_id": t.scope_id,
             "max_wager": t.max_wager,
             "expires_at": t.expires_at.isoformat() if t.expires_at else None}
            for t in toks
        ],
    }


@router.get("/my-bonus-lots")
async def my_bonus_lots(user: SessionUser = Depends(bearer_user)):
    """Per-lot breakdown of the caller's Bonus Chip balance (soonest expiry
    first), for the balance-screen drill-down."""
    _SRC_LABEL = {
        "SIGNUP": "Sign-up bonus", "GRANT_USER": "Admin grant", "ADMIN": "Admin grant",
        "REFUND": "Refunded from a voided bet", "CLAIM": "Claim drop",
    }
    async with get_db() as db:
        gid = _GUILD_ID()
        await promos.expire_stale(db, gid, user.discord_id)
        await db.commit()
        lots = await promos.active_bonus_lots(db, gid, user.discord_id)
        total = sum(l.amount_remaining for l in lots)
    return {
        "total": total,
        "lots": [
            {"amount_remaining": l.amount_remaining,
             "original_amount": l.original_amount,
             "source": _SRC_LABEL.get(l.source, l.source.title()),
             "expires_at": l.expires_at.isoformat() if l.expires_at else None}
            for l in lots
        ],
    }


@router.get("/admin/promos")
async def admin_promos(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        gid = _GUILD_ID()
        await promos.expire_stale(db, gid)
        await db.commit()
        lots = (await db.execute(
            select(BonusBetLot).where(BonusBetLot.guild_id == gid, BonusBetLot.status == "ACTIVE")
        )).scalars().all()
        agg: dict[int, dict] = {}
        for lot in lots:
            e = agg.setdefault(lot.discord_user_id, {"uid": str(lot.discord_user_id), "total": 0, "expiry": None})
            e["total"] += lot.amount_remaining
            if lot.expires_at and (e["expiry"] is None or lot.expires_at.isoformat() < e["expiry"]):
                e["expiry"] = lot.expires_at.isoformat()
        ft_bonus = (await db.execute(
            select(BonusGrant).where(
                BonusGrant.guild_id == gid, BonusGrant.scope == "FIRST_TOUCH",
                BonusGrant.active == True,  # noqa: E712
            )
        )).scalars().all()
        templates = (await db.execute(
            select(ProfitBoostTemplate).where(ProfitBoostTemplate.guild_id == gid)
            .order_by(ProfitBoostTemplate.id.desc())
        )).scalars().all()
        tokens = (await db.execute(
            select(ProfitBoostToken).where(
                ProfitBoostToken.guild_id == gid, ProfitBoostToken.status == "ACTIVE"
            ).order_by(ProfitBoostToken.id.desc())
        )).scalars().all()
        names: dict[int, str] = {}
        name_uids = set(agg) | {t.discord_user_id for t in tokens}
        if name_uids:
            for uid, nm in (await db.execute(
                select(User.discord_id, User.username).where(
                    User.guild_id == gid, User.discord_id.in_(list(name_uids))
                )
            )).all():
                names[uid] = nm
        for uid, e in agg.items():
            e["name"] = names.get(uid, str(uid))
        dpromos = (await db.execute(
            select(DepositMatchPromo).where(DepositMatchPromo.guild_id == gid)
            .order_by(DepositMatchPromo.id.desc())
        )).scalars().all()
        claims: dict[int, dict] = {}
        if dpromos:
            for c in (await db.execute(
                select(DepositMatchClaim).where(
                    DepositMatchClaim.promo_id.in_([p.id for p in dpromos])
                )
            )).scalars().all():
                d = claims.setdefault(c.promo_id, {"members": 0, "matched": 0})
                d["members"] += 1
                d["matched"] += c.total_matched
        shop_rows = await promos.shop_listings(db, gid, active_only=False)
    now = datetime.utcnow()
    rebate = await _rebate_config_payload()
    channels = await discord_api.list_guild_text_channels(gid)
    roles = await discord_api.list_guild_roles(gid)
    return {
        "channels": channels,
        "roles": roles,
        "bonus_users": sorted(agg.values(), key=lambda x: -x["total"]),
        "ft_bonus": [{"id": g.id, "amount": g.amount, "expiry_hours": g.expiry_hours, "note": g.note} for g in ft_bonus],
        "templates": [
            {"id": t.id, "name": t.name, "boost_pct": t.boost_pct, "scope_type": t.scope_type,
             "scope_id": t.scope_id, "max_wager": t.max_wager,
             "grant_on_first_touch": t.grant_on_first_touch, "active": t.active}
            for t in templates
        ],
        "tokens": [
            {"id": t.id, "uid": str(t.discord_user_id),
             "name": names.get(t.discord_user_id, str(t.discord_user_id)),
             "boost_pct": t.boost_pct,
             "scope_type": t.scope_type, "scope_id": t.scope_id,
             "expires_at": t.expires_at.isoformat() if t.expires_at else None}
            for t in tokens
        ],
        "deposit_promos": [
            {"id": p.id, "name": p.name, "match_pct": p.match_pct,
             "max_match_per_user": p.max_match_per_user,
             "match_bonus_expiry_days": p.match_bonus_expiry_days,
             "starts_at": p.starts_at.isoformat(), "ends_at": p.ends_at.isoformat(),
             "role_id": str(p.role_id) if p.role_id else None,
             "live": (p.active and p.starts_at <= now < p.ends_at),
             "claims": claims.get(p.id, {"members": 0, "matched": 0})}
            for p in dpromos
        ],
        "rebate": rebate,
        "shop_items": [_shop_item_json(it, tpl) for it, tpl in shop_rows],
    }


@router.post("/admin/promos/claim-drop")
async def admin_promos_claim_drop(
    admin: SessionUser = Depends(bearer_admin),
    channel_id: Annotated[str, Body()] = "",
    reward_kind: Annotated[str, Body()] = "BONUS",
    bonus_amount: Annotated[int, Body()] = 0,
    boost_template_id: Annotated[int, Body()] = 0,
    message: Annotated[str, Body()] = "",
    ping_role_id: Annotated[str, Body()] = "",
    max_claims: Annotated[int, Body()] = 0,
    duration_hours: Annotated[int, Body()] = 0,
    reward_expiry_days: Annotated[int, Body()] = 0,
):
    """Post a channel message with a persistent Claim button. The bot registers
    the button handler on startup, so it services clicks regardless of the fact
    that the web app posted the message."""
    kind = (reward_kind or "BONUS").upper()
    message = (message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="The message can't be empty.")
    if len(message) > 1900:
        raise HTTPException(status_code=400, detail="Keep the message under 1900 characters.")
    if kind not in ("BONUS", "BOOST"):
        raise HTTPException(status_code=400, detail="Invalid reward type.")
    try:
        chan_id = int(str(channel_id).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Pick a channel.")

    role_id = int(str(ping_role_id).strip()) if str(ping_role_id).strip().isdigit() else None
    reward_expiry_hours = int(reward_expiry_days) * 24 if reward_expiry_days else None
    max_claims_val = int(max_claims) if max_claims else None

    gid = _GUILD_ID()
    tpl_id = None
    reward_label = ""
    async with get_db() as db:
        if kind == "BONUS":
            if bonus_amount <= 0:
                raise HTTPException(status_code=400, detail="Set a positive bonus amount.")
            reward_label = f"{bonus_amount:,} Bonus Chips"
        else:
            tpl = await db.get(ProfitBoostTemplate, int(boost_template_id or 0))
            if tpl is None or tpl.guild_id != gid:
                raise HTTPException(status_code=400, detail="Pick a boost template.")
            tpl_id = tpl.id
            reward_label = (
                f"+{tpl.boost_pct:g}% profit boost "
                f"({promos.boost_scope_text(tpl.scope_type, tpl.scope_id)})"
            )
        expires_at = (
            datetime.utcnow() + timedelta(hours=duration_hours) if duration_hours else None
        )
        drop = PromoClaimDrop(
            guild_id=gid, channel_id=chan_id, reward_kind=kind,
            bonus_amount=int(bonus_amount) if kind == "BONUS" else None,
            boost_template_id=tpl_id,
            reward_expiry_hours=reward_expiry_hours,
            ping_role_id=role_id,
            message_text=message[:2000],
            max_claims=max_claims_val,
            expires_at=expires_at, created_by=admin.discord_id,
        )
        db.add(drop)
        await db.flush()
        drop_id = drop.id
        await db.commit()

    components = [{
        "type": 1,
        "components": [{
            "type": 2, "style": 3, "label": "🎁 Claim",
            "custom_id": f"promoclaim:{drop_id}",
        }],
    }]
    embed = promos.build_claim_drop_embed(
        message, reward_label, reward_expiry_hours=reward_expiry_hours,
        max_claims=max_claims_val,
    )
    sent = await discord_api.post_channel_message(
        chan_id, f"<@&{role_id}>" if role_id else "", components=components,
        embeds=[embed],
        allowed_mentions={"roles": [str(role_id)]} if role_id else {"parse": []},
    )
    if sent is None:
        async with get_db() as db:
            orphan = await db.get(PromoClaimDrop, drop_id)
            if orphan is not None:
                await db.delete(orphan)
                await db.commit()
        raise HTTPException(
            status_code=502,
            detail="Couldn't post in that channel — check the bot is present with Send Messages permission.",
        )

    async with get_db() as db:
        saved = await db.get(PromoClaimDrop, drop_id)
        if saved is not None:
            saved.message_id = int(sent["id"])
            await db.commit()

    asyncio.create_task(post_admin_action(
        admin, "Promo claim drop posted",
        {"reward": reward_label, "channel": str(chan_id)}, source="Discord Activity",
    ))
    limits = []
    if role_id:
        limits.append("with role ping")
    if max_claims:
        limits.append(f"first {max_claims:,} claimers")
    if duration_hours:
        limits.append(f"open {duration_hours}h")
    tail = f" ({', '.join(limits)})" if limits else ""
    return {"ok": True, "message": f"Posted a claim drop for {reward_label}{tail}."}


@router.post("/admin/promos/bonus/grant")
async def admin_promos_bonus_grant(
    admin: SessionUser = Depends(bearer_admin),
    scope: Annotated[str, Body()] = "USER",
    target_id: Annotated[str, Body()] = "",
    amount: Annotated[int, Body()] = 0,
    expiry_days: Annotated[int, Body()] = 0,
    expiry_hours: Annotated[int, Body()] = 0,
    note: Annotated[str, Body()] = "",
):
    scope = scope.upper()
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    hours = _promo_expiry_hours(expiry_days, expiry_hours)
    async with get_db() as db:
        gid = _GUILD_ID()
        grant = BonusGrant(
            guild_id=gid, scope=scope,
            target_id=int(target_id) if scope == "USER" and str(target_id).strip().isdigit() else None,
            amount=amount, expiry_hours=hours, note=(note or "").strip()[:200] or None,
            created_by=admin.discord_id,
        )
        db.add(grant)
        await db.flush()
        if scope == "FIRST_TOUCH":
            await db.commit()
            return {"ok": True, "message": "First-interaction bonus rule added."}
        ids, err = await _promo_resolve_target_ids(scope, target_id)
        if err:
            raise HTTPException(status_code=400, detail=err)
        n = await promos.grant_bonus_to_users(db, gid, ids, amount, hours, "GRANT_USER", grant_id=grant.id)
        grant.recipients_count = n
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Bonus Chips granted", {"scope": scope, "amount": f"{amount:,}", "recipients": str(n)}, source="Discord Activity"))
    return {"ok": True, "message": f"Granted {amount:,} Bonus Chips to {n} member(s)."}


@router.post("/admin/promos/bonus/deduct")
async def admin_promos_bonus_deduct(
    admin: SessionUser = Depends(bearer_admin),
    scope: Annotated[str, Body()] = "USER",
    target_id: Annotated[str, Body()] = "",
    amount: Annotated[int, Body()] = 0,
):
    scope = scope.upper()
    async with get_db() as db:
        ids, err = await _promo_resolve_target_ids(scope, target_id)
        if err:
            raise HTTPException(status_code=400, detail=err)
        n = await promos.deduct_bonus_from_users(db, _GUILD_ID(), ids, amount if amount > 0 else 0)
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Bonus Chips deducted", {"scope": scope, "members": str(n)}, source="Discord Activity"))
    return {"ok": True, "message": f"Deducted Bonus Chips from {n} member(s)."}


@router.post("/admin/promos/bonus/user/{uid}/revoke")
async def admin_promos_bonus_user_revoke(uid: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        await promos.deduct_bonus_from_users(db, _GUILD_ID(), [uid], 0)
        await db.commit()
    return {"ok": True, "message": "Revoked that member's Bonus Chips."}


@router.delete("/admin/promos/bonus/first-touch/{grant_id}")
async def admin_promos_bonus_ft_delete(grant_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        g = await db.get(BonusGrant, grant_id)
        if g:
            g.active = False
            await db.commit()
    return {"ok": True, "message": "Rule removed."}


@router.post("/admin/promos/boost-template")
async def admin_promos_boost_template_new(
    admin: SessionUser = Depends(bearer_admin),
    name: Annotated[str, Body()] = "",
    boost_pct: Annotated[float, Body()] = 0.0,
    scope_type: Annotated[str, Body()] = "ANY",
    scope_id: Annotated[str, Body()] = "",
    max_wager: Annotated[str, Body()] = "",
    grant_on_first_touch: Annotated[bool, Body()] = False,
):
    if not name.strip() or boost_pct <= 0:
        raise HTTPException(status_code=400, detail="Name and a positive boost % are required.")
    scope_type = scope_type.upper()
    async with get_db() as db:
        db.add(ProfitBoostTemplate(
            guild_id=_GUILD_ID(), name=name.strip()[:100], boost_pct=boost_pct,
            scope_type=scope_type if scope_type in ("ANY", "DISTRICT", "ALLIANCE") else "ANY",
            scope_id=int(scope_id) if str(scope_id).strip().isdigit() and scope_type != "ANY" else None,
            max_wager=int(max_wager) if str(max_wager).strip().isdigit() else None,
            grant_on_first_touch=bool(grant_on_first_touch), created_by=admin.discord_id,
        ))
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Profit boost template created", {"name": name.strip()[:100]}, source="Discord Activity"))
    return {"ok": True, "message": "Profit boost template created."}


@router.post("/admin/promos/boost-template/{tpl_id}/toggle")
async def admin_promos_boost_template_toggle(tpl_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        t = await db.get(ProfitBoostTemplate, tpl_id)
        if t:
            t.active = not t.active
            await db.commit()
    return {"ok": True, "message": "Template updated."}


@router.delete("/admin/promos/boost-template/{tpl_id}")
async def admin_promos_boost_template_delete(tpl_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        t = await db.get(ProfitBoostTemplate, tpl_id)
        if t:
            await db.delete(t)
            await db.commit()
    return {"ok": True, "message": "Template deleted."}


@router.post("/admin/promos/boost/grant")
async def admin_promos_boost_grant(
    admin: SessionUser = Depends(bearer_admin),
    template_id: Annotated[int, Body()] = 0,
    scope: Annotated[str, Body()] = "USER",
    target_id: Annotated[str, Body()] = "",
    expiry_days: Annotated[int, Body()] = 0,
    expiry_hours: Annotated[int, Body()] = 0,
):
    scope = scope.upper()
    hours = _promo_expiry_hours(expiry_days, expiry_hours)
    async with get_db() as db:
        gid = _GUILD_ID()
        tpl = await db.get(ProfitBoostTemplate, template_id)
        if not tpl or tpl.guild_id != gid:
            raise HTTPException(status_code=400, detail="Pick a boost template.")
        grant = ProfitBoostGrant(
            guild_id=gid, template_id=tpl.id, scope=scope,
            target_id=int(target_id) if scope == "USER" and str(target_id).strip().isdigit() else None,
            expiry_hours=hours, created_by=admin.discord_id,
        )
        db.add(grant)
        await db.flush()
        if scope == "FIRST_TOUCH":
            await db.commit()
            return {"ok": True, "message": "First-interaction boost rule added."}
        ids, err = await _promo_resolve_target_ids(scope, target_id)
        if err:
            raise HTTPException(status_code=400, detail=err)
        n = await promos.grant_boost_to_users(db, gid, tpl, ids, hours, admin.discord_id, grant_id=grant.id)
        grant.recipients_count = n
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Profit boosts granted", {"template": tpl.name, "recipients": str(n)}, source="Discord Activity"))
    return {"ok": True, "message": f"Granted the boost to {n} member(s)."}


@router.post("/admin/promos/boost/token/{tok_id}/revoke")
async def admin_promos_boost_token_revoke(tok_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        t = await db.get(ProfitBoostToken, tok_id)
        if t and t.status == "ACTIVE":
            t.status = "REVOKED"
            await db.commit()
    return {"ok": True, "message": "Boost token revoked."}


@router.post("/admin/promos/deposit-match")
async def admin_promos_deposit_match_new(
    admin: SessionUser = Depends(bearer_admin),
    name: Annotated[str, Body()] = "",
    match_pct: Annotated[float, Body()] = 100.0,
    max_match_per_user: Annotated[int, Body()] = 0,
    match_bonus_expiry_days: Annotated[int, Body()] = 0,
    starts_at: Annotated[str, Body()] = "",
    ends_at: Annotated[str, Body()] = "",
    role_id: Annotated[str, Body()] = "",
):
    if not name.strip() or match_pct <= 0 or max_match_per_user <= 0:
        raise HTTPException(status_code=400, detail="Name, match % and per-user cap are required.")
    try:
        sa = _iso_to_naive_utc(starts_at) if starts_at else datetime.utcnow()
        ea = _iso_to_naive_utc(ends_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start/end time.")
    if ea <= sa:
        raise HTTPException(status_code=400, detail="End must be after start.")
    async with get_db() as db:
        db.add(DepositMatchPromo(
            guild_id=_GUILD_ID(), name=name.strip()[:100], match_pct=match_pct,
            max_match_per_user=max_match_per_user, starts_at=sa, ends_at=ea,
            match_bonus_expiry_days=int(match_bonus_expiry_days) or None,
            role_id=int(role_id) if str(role_id).strip().isdigit() else None,
            created_by=admin.discord_id,
        ))
        await db.commit()
    asyncio.create_task(post_admin_action(admin, "Deposit match promo created", {"name": name.strip()[:100]}, source="Discord Activity"))
    return {"ok": True, "message": "Deposit match promo created."}


@router.post("/admin/promos/deposit-match/{promo_id}/end")
async def admin_promos_deposit_match_end(promo_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        p = await db.get(DepositMatchPromo, promo_id)
        if p:
            p.ends_at = datetime.utcnow()
            p.active = False
            await db.commit()
    return {"ok": True, "message": "Promo ended."}


@router.delete("/admin/promos/deposit-match/{promo_id}")
async def admin_promos_deposit_match_delete(promo_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        p = await db.get(DepositMatchPromo, promo_id)
        if p:
            await db.delete(p)
            await db.commit()
    return {"ok": True, "message": "Promo deleted."}


# ── Bonus Chip rebate (admin config) ─────────────────────────────────────────

_REBATE_KEYS = (
    "bonus_rebate_mode", "bonus_rebate_win_pct", "bonus_rebate_loss_pct",
    "bonus_rebate_win_flat", "bonus_rebate_loss_flat", "bonus_rebate_expiry_days",
)


async def _rebate_config_payload() -> dict:
    from bot.database.engine import get_setting
    out = {}
    for k in _REBATE_KEYS:
        raw = await get_setting(k)
        try:
            out[k] = json.loads(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            out[k] = raw
    if out.get("bonus_rebate_mode") not in ("OFF", "PCT", "FLAT"):
        out["bonus_rebate_mode"] = "OFF"
    return out


@router.post("/admin/promos/rebate")
async def admin_promos_rebate_save(
    admin: SessionUser = Depends(bearer_admin),
    mode: Annotated[str, Body()] = "OFF",
    win_pct: Annotated[float, Body()] = 0.0,
    loss_pct: Annotated[float, Body()] = 0.0,
    win_flat: Annotated[int, Body()] = 0,
    loss_flat: Annotated[int, Body()] = 0,
    expiry_days: Annotated[int, Body()] = 0,
):
    from bot.database.engine import set_setting
    mode = (mode or "OFF").upper()
    if mode not in ("OFF", "PCT", "FLAT"):
        raise HTTPException(status_code=400, detail="Invalid mode.")
    if not (0 <= win_pct <= 100 and 0 <= loss_pct <= 100):
        raise HTTPException(status_code=400, detail="Percentages must be 0–100.")
    await set_setting("bonus_rebate_mode", mode)
    await set_setting("bonus_rebate_win_pct", float(win_pct))
    await set_setting("bonus_rebate_loss_pct", float(loss_pct))
    await set_setting("bonus_rebate_win_flat", max(0, int(win_flat)))
    await set_setting("bonus_rebate_loss_flat", max(0, int(loss_flat)))
    await set_setting("bonus_rebate_expiry_days", int(expiry_days) or None)
    asyncio.create_task(post_admin_action(
        admin, "Bonus Chip rebate updated", {"mode": mode}, source="Discord Activity"
    ))
    return {"ok": True, "message": "Rebate settings saved."}


# ── Profit-boost shop ───────────────────────────────────────────────────────


def _shop_item_json(item: BoostShopItem, tpl: ProfitBoostTemplate | None) -> dict:
    return {
        "id": item.id, "template_id": item.boost_template_id,
        "price_bonus_bets": item.price_bonus_bets,
        "expiry_days": item.expiry_days, "per_user_limit": item.per_user_limit,
        "sort_order": item.sort_order, "active": item.active,
        "boost_pct": tpl.boost_pct if tpl else None,
        "scope_type": tpl.scope_type if tpl else None,
        "scope_id": tpl.scope_id if tpl else None,
        "max_wager": tpl.max_wager if tpl else None,
        "name": tpl.name if tpl else "(deleted boost)",
        "scope_label": promos.boost_scope_text(tpl.scope_type, tpl.scope_id) if tpl else "",
    }


@router.get("/shop")
async def shop(user: SessionUser = Depends(bearer_user)):
    async with get_db() as db:
        gid = _GUILD_ID()
        db_user = await _fetch_user(db, user.discord_id)
        await promos.expire_stale(db, gid, user.discord_id)
        await db.commit()
        bonus_bal = await promos.bonus_balance(db, gid, user.discord_id)
        listings = await promos.shop_listings(db, gid, active_only=True)
        bought = {}
        if listings:
            for pid, cnt in (await db.execute(
                select(BoostShopPurchase.item_id, func.count())
                .where(
                    BoostShopPurchase.guild_id == gid,
                    BoostShopPurchase.discord_user_id == user.discord_id,
                    BoostShopPurchase.item_id.in_([i.id for i, _ in listings]),
                ).group_by(BoostShopPurchase.item_id)
            )).all():
                bought[pid] = cnt
    items = []
    for item, tpl in listings:
        d = _shop_item_json(item, tpl)
        d["owned"] = bought.get(item.id, 0)
        d["sold_out"] = (
            item.per_user_limit is not None and bought.get(item.id, 0) >= item.per_user_limit
        )
        d["affordable"] = bonus_bal >= item.price_bonus_bets
        items.append(d)
    return {"bonus_balance": bonus_bal, "items": items}


@router.post("/shop/buy")
async def shop_buy(
    user: SessionUser = Depends(bearer_user),
    item_id: Annotated[int, Body(embed=True)] = 0,
):
    async with get_db() as db:
        gid = _GUILD_ID()
        if await is_fully_restricted(db, gid, user.discord_id):
            raise HTTPException(status_code=403, detail="You're blocked from betting in this server.")
        info, err = await promos.purchase_boost(db, gid, user.discord_id, int(item_id))
        if err is not None:
            await db.rollback()
            raise HTTPException(status_code=400, detail=err)
        await db.commit()
    asyncio.create_task(post_admin_action(
        user, "Profit boost purchased",
        {"item": str(item_id), "price": str(info["price"])}, source="Discord Activity",
    ))
    return {"ok": True, "message": info["message"]}


@router.post("/admin/promos/shop-item")
async def admin_shop_item_new(
    admin: SessionUser = Depends(bearer_admin),
    boost_template_id: Annotated[int, Body()] = 0,
    price_bonus_bets: Annotated[int, Body()] = 0,
    expiry_days: Annotated[int, Body()] = 0,
    per_user_limit: Annotated[int, Body()] = 0,
):
    if price_bonus_bets <= 0:
        raise HTTPException(status_code=400, detail="Set a positive Bonus Chip price.")
    async with get_db() as db:
        gid = _GUILD_ID()
        tpl = await db.get(ProfitBoostTemplate, int(boost_template_id or 0))
        if tpl is None or tpl.guild_id != gid:
            raise HTTPException(status_code=400, detail="Pick a boost template.")
        nxt = ((await db.execute(
            select(func.coalesce(func.max(BoostShopItem.sort_order), 0)).where(
                BoostShopItem.guild_id == gid
            )
        )).scalar_one() or 0) + 1
        db.add(BoostShopItem(
            guild_id=gid, boost_template_id=tpl.id,
            price_bonus_bets=int(price_bonus_bets),
            expiry_days=int(expiry_days) or None,
            per_user_limit=int(per_user_limit) or None,
            sort_order=nxt, created_by=admin.discord_id,
        ))
        await db.commit()
    return {"ok": True, "message": "Shop item added."}


@router.post("/admin/promos/shop-item/{item_id}")
async def admin_shop_item_update(
    item_id: int,
    admin: SessionUser = Depends(bearer_admin),
    price_bonus_bets: Annotated[int, Body(embed=True)] = 0,
):
    if price_bonus_bets <= 0:
        raise HTTPException(status_code=400, detail="Set a positive price.")
    async with get_db() as db:
        it = await db.get(BoostShopItem, item_id)
        if it is None or it.guild_id != _GUILD_ID():
            raise HTTPException(status_code=404, detail="Item not found.")
        it.price_bonus_bets = int(price_bonus_bets)
        await db.commit()
    return {"ok": True, "message": "Price updated."}


@router.post("/admin/promos/shop-item/{item_id}/toggle")
async def admin_shop_item_toggle(item_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        it = await db.get(BoostShopItem, item_id)
        if it is None or it.guild_id != _GUILD_ID():
            raise HTTPException(status_code=404, detail="Item not found.")
        it.active = not it.active
        await db.commit()
    return {"ok": True, "message": "Shop item " + ("enabled." if it.active else "disabled.")}


@router.delete("/admin/promos/shop-item/{item_id}")
async def admin_shop_item_delete(item_id: int, admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        it = await db.get(BoostShopItem, item_id)
        if it is not None and it.guild_id == _GUILD_ID():
            await db.delete(it)
            await db.commit()
    return {"ok": True, "message": "Shop item removed."}

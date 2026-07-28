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
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select, text

from bot.cogs.betting import (
    _parlay_conflict, add_markets_to_pending_slip, tribute_lookup_for_markets,
    MAX_PARLAY_LEGS, PARLAY_PAYOUT_CAP,
)
from bot.cogs.display import LEADERBOARD_CATEGORIES, _leaderboard_rows
from bot.database.models import (
    Alliance, Bet, BettingPhase, DistrictRecord, Market, Parlay, PendingParlayLeg,
    ParlayTemplate, ParlayTemplateLeg, Tribute, User,
)
from bot.odds.calculator import (
    combined_american, parlay_payout, resolve_cashout, straight_payout,
)
from web import config, discord_api
from web.activity_auth import bearer_admin, bearer_user, mint_token
from web.audit import post_admin_action
from web.database import get_db, get_request_guild, set_request_guild
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
        "status": b.status,
        "cashout_amount": b.cashout_amount,
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
        "placed_at": p.placed_at.isoformat() if p.placed_at else None,
    }


# ── Shared helpers ───────────────────────────────────────────────────────────

_GUILD_ID = get_request_guild


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
        default = json.loads(default_raw[0]) if default_raw else config.DEFAULT_CHIPS
        u = User(
            guild_id=_GUILD_ID(), discord_id=session_user.discord_id,
            username=session_user.username, chips=default,
        )
        db.add(u)
        await db.flush()
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

    return {
        **_user_dict(db_user),
        "is_admin": user.is_admin,
        "avatar_url": user.avatar_url,
        "roi": round(roi, 1),
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
            parlay_out.append({**_parlay_dict(p), "legs": [_bet_dict(b) for b in legs]})

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
):
    if wager < 1:
        raise HTTPException(status_code=400, detail="Wager must be at least 1 chip.")

    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            raise HTTPException(status_code=400, detail="Market is not open for betting.")

        db_user = await _get_or_create_user(db, user)
        if db_user.chips < wager:
            raise HTTPException(status_code=400, detail="Insufficient chips.")

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

        payout = straight_payout(wager, market.odds)
        bet = Bet(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            market_id=market_id,
            wager=wager,
            odds_at_placement=market.odds,
            payout_if_win=payout,
            status="PENDING",
        )
        db_user.chips -= wager
        db_user.total_wagered += wager
        db.add(bet)
        await db.commit()
        chips_left = db_user.chips

    return {"ok": True, "payout_if_win": payout, "chips": chips_left,
            "message": f"Bet placed! Win {payout:,} chips if correct."}


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
):
    if wager < 1:
        raise HTTPException(status_code=400, detail="Wager must be at least 1 chip.")

    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)

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

        if db_user.chips < wager:
            raise HTTPException(status_code=400, detail="Insufficient chips.")

        odds_list = [m.odds for m in leg_markets]
        total_payout = min(parlay_payout(wager, odds_list), PARLAY_PAYOUT_CAP)

        p = Parlay(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            total_wager=wager,
            total_payout=total_payout,
            status="PENDING",
            is_public=is_public,
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
        for l in legs_raw:
            await db.delete(l)
        await db.commit()
        chips_left = db_user.chips

    return {"ok": True, "total_payout": total_payout, "chips": chips_left,
            "message": f"Parlay submitted! Potential payout: {total_payout:,} chips."}


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
            odds_list = [m.odds for m in leg_markets if m.status == "OPEN"]
            combined = combined_american(odds_list) if len(odds_list) >= 2 else None
            out.append({
                "id": tpl.id,
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
            if entry["source"] == "AI" and entry["description"]:
                pass  # preserve the AI-generated name and description as-is
            else:
                name, desc = _parlay_flavor(
                    tpl_leg_markets[entry["id"]], act_tributes_map, act_alliance_names,
                    act_district_records, entry["name"], entry["description"],
                )
                entry["name"] = name
                entry["description"] = desc

    return {"chips": db_user.chips, "templates": out}


@router.post("/tail/{template_id}")
async def tail_parlay(
    template_id: int,
    user: SessionUser = Depends(bearer_user),
    wager: Annotated[int, Body(embed=True)] = 0,
):
    if wager < 1:
        raise HTTPException(status_code=400, detail="Wager must be at least 1 chip.")

    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, template_id)
        if not tpl or not tpl.active:
            raise HTTPException(status_code=404, detail="Template not found.")

        db_user = await _get_or_create_user(db, user)

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
        total_payout = min(parlay_payout(wager, odds_list), PARLAY_PAYOUT_CAP)

        p = Parlay(
            guild_id=_GUILD_ID(), user_id=user.discord_id, total_wager=wager, total_payout=total_payout,
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
    elif "PENDING" not in statuses:
        active = [l for l in legs if l.status != "VOIDED"]
        if all(l.status == "WON" for l in active) and active:
            parlay.status = "WON"
            db_user = await _fetch_user(db, parlay.user_id)
            if db_user:
                db_user.chips += parlay.total_payout
                db_user.total_won += parlay.total_payout
        elif all(l.status == "VOIDED" for l in legs):
            parlay.status = "WON"
            db_user = await _fetch_user(db, parlay.user_id)
            if db_user:
                db_user.chips += parlay.total_wager


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


@router.get("/admin/users")
async def admin_users(admin: SessionUser = Depends(bearer_admin)):
    async with get_db() as db:
        users = (await db.execute(
            select(User).where(User.guild_id == _GUILD_ID()).order_by(User.chips.desc())
        )).scalars().all()
    return {"users": [_user_dict(u) for u in users]}


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

        for bet in bets:
            if bool_result is None:
                bet.status = "VOIDED"
                db_user = await _fetch_user(db, bet.user_id)
                if db_user:
                    db_user.chips += bet.wager
            elif bool_result and bet.parlay_id is None:
                bet.status = "WON"
                db_user = await _fetch_user(db, bet.user_id)
                if db_user:
                    db_user.chips += bet.payout_if_win
                    db_user.total_won += bet.payout_if_win
            elif not bool_result and bet.parlay_id is None:
                bet.status = "LOST"
            elif bet.parlay_id is not None:
                bet.status = "WON" if bool_result else ("VOIDED" if bool_result is None else "LOST")
                await _settle_parlay(db, bet.parlay_id)

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

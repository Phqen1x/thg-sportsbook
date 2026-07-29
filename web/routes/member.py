from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, text

from bot.cogs.betting import (
    _betting_paused, _parlay_conflict, add_markets_to_pending_slip, tribute_lookup_for_markets,
    PARLAY_PAYOUT_CAP, MAX_PARLAY_LEGS,
)
from bot.database.models import Alliance, Bet, DistrictRecord, Market, Parlay, PendingParlayLeg, ParlayTemplate, ParlayTemplateLeg, Tribute, User
from web.routes.public import _parlay_flavor
from bot.odds.calculator import straight_payout, parlay_payout, combined_american, resolve_cashout
from web.database import get_db, get_request_guild
from web.deps import require_user
from web.session import SessionUser
from web import config as _web_config

router = APIRouter(tags=["member"])

_GUILD_ID = get_request_guild


async def _paused() -> bool:
    """Wrap _betting_paused() with the guild-context bind it needs.

    _betting_paused() reaches the DB through bot.database.engine (get_setting ->
    get_session()), which resolves the active guild from *its own* contextvar —
    separate from web/database.py's request-guild context that the rest of this
    module uses. Without binding it first, current_guild_id() reads 0 and
    get_session() raises "No guild context set", which isn't caught by the form
    routes' redirect-on-error handling and surfaces as a bare 500 instead of the
    intended paused-betting message."""
    from bot.database.engine import set_guild_context
    set_guild_context(_GUILD_ID())
    return await _betting_paused()


async def _cashout_settings(db) -> tuple[bool, float, dict]:
    """Global cashout settings, shared by the bet/parlay cashout preview + POST routes."""
    row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_allowed'"))).fetchone()
    global_allowed = (row[0].lower() == "true") if row else False
    rate_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_rate'"))).fetchone()
    global_rate = float(rate_row[0]) if rate_row else 0.65
    by_type_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='cashout_by_type'"))).fetchone()
    cashout_by_type: dict = json.loads(by_type_row[0]) if by_type_row else {}
    return global_allowed, global_rate, cashout_by_type


async def _bet_cashout_preview(db, bet: Bet, market: Market | None, settings: tuple[bool, float, dict]) -> tuple[bool, int]:
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


async def _parlay_cashout_preview(parlay: Parlay, settings: tuple[bool, float, dict]) -> tuple[bool, int]:
    global_allowed, global_rate, _by_type = settings
    return resolve_cashout(
        wager=parlay.total_wager, payout_if_win=parlay.total_payout,
        global_allowed=global_allowed, global_rate=global_rate,
        item_allowed=parlay.cashout_allowed, item_rate=parlay.cashout_rate,
    )


async def _get_or_create_user(db, session_user: SessionUser) -> User:
    result = await db.execute(
        select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == session_user.discord_id)
    )
    u = result.scalar_one_or_none()
    if u is None:
        default_raw = (await db.execute(
            __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='default_chips'")
        )).fetchone()
        default = json.loads(default_raw[0]) if default_raw else _web_config.DEFAULT_CHIPS
        u = User(
            guild_id=_GUILD_ID(), discord_id=session_user.discord_id,
            username=session_user.username, chips=default,
        )
        db.add(u)
        await db.flush()
    return u


_PAUSED_ERROR = "Betting+is+currently+paused+by+an+admin.+Try+again+once+its+resumed."


def _redirect(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if error:
        return RedirectResponse(f"{url}{sep}error={error}", status_code=303)
    if msg:
        return RedirectResponse(f"{url}{sep}success={msg}", status_code=303)
    return RedirectResponse(url, status_code=303)


def _wants_json(request: Request) -> bool:
    """True when the client asked for a JSON response (our fetch()-based
    progressive enhancement in web/static/app.js) rather than a plain form
    post-back that expects the usual redirect."""
    return "application/json" in request.headers.get("accept", "")


def _respond(request: Request, url: str, *, msg: str = "", error: str = ""):
    """Like _redirect, but answers JSON in place for AJAX callers so the page
    doesn't navigate away — existing error/success strings use "+" in place of
    spaces (see call sites below), so undo that for the human-readable JSON."""
    if _wants_json(request):
        if error:
            return JSONResponse({"ok": False, "detail": error.replace("+", " ")}, status_code=400)
        return JSONResponse({"ok": True, "message": msg.replace("+", " ")})
    return _redirect(url, msg=msg, error=error)


# ── Balance ────────────────────────────────────────────────────────────────────

@router.get("/balance")
async def balance(
    request: Request,
    user: SessionUser = Depends(require_user),
    success: str = "",
    error: str = "",
):
    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        await db.commit()

        # Stats
        bets = (await db.execute(
            select(Bet).where(Bet.guild_id == _GUILD_ID(), Bet.user_id == user.discord_id, Bet.parlay_id.is_(None))
        )).scalars().all()
        parlays = (await db.execute(
            select(Parlay).where(Parlay.guild_id == _GUILD_ID(), Parlay.user_id == user.discord_id)
        )).scalars().all()

        # ROI must be derived from the same total_won/total_wagered counters the
        # /balance Discord command reads, not re-derived from bets/parlays here —
        # a separate recomputation drifted out of sync with the persisted totals
        # (e.g. legacy rows scoped differently) and was showing -100% ROI for
        # users total_won already correctly credited chips for.
        wagered = db_user.total_wagered
        roi = ((db_user.total_won - wagered) / wagered * 100) if wagered else 0.0

    return request.app.state.templates.TemplateResponse("balance.html", {
        "request": request,
        "user": user,
        "db_user": db_user,
        "bets": bets,
        "parlays": parlays,
        "roi": roi,
        "success": success,
        "error": error,
    })


# ── My Bets ────────────────────────────────────────────────────────────────────

@router.get("/my-bets")
async def my_bets(
    request: Request,
    user: SessionUser = Depends(require_user),
    success: str = "",
    error: str = "",
):
    async with get_db() as db:
        straight_bets = (await db.execute(
            select(Bet)
            .where(Bet.guild_id == _GUILD_ID(), Bet.user_id == user.discord_id, Bet.parlay_id.is_(None))
            .order_by(Bet.placed_at.desc())
        )).scalars().all()

        parlays = (await db.execute(
            select(Parlay)
            .where(Parlay.guild_id == _GUILD_ID(), Parlay.user_id == user.discord_id)
            .order_by(Parlay.placed_at.desc())
        )).scalars().all()

        # Load markets for straight bets
        market_ids = {b.market_id for b in straight_bets}
        if market_ids:
            mkts = (await db.execute(
                select(Market).where(Market.id.in_(market_ids))
            )).scalars().all()
            markets_map = {m.id: m for m in mkts}
        else:
            markets_map = {}

        # Load parlay legs
        parlay_legs: dict[int, list[Bet]] = {}
        parlay_markets: dict[int, Market] = {}
        for p in parlays:
            legs = (await db.execute(
                select(Bet).where(Bet.parlay_id == p.id).order_by(Bet.id)
            )).scalars().all()
            parlay_legs[p.id] = legs
            for leg in legs:
                if leg.market_id not in parlay_markets:
                    mkt = await db.get(Market, leg.market_id)
                    if mkt:
                        parlay_markets[leg.market_id] = mkt

        # Cashout previews shown before the user confirms (only for PENDING items).
        settings = await _cashout_settings(db)
        bet_cashout_preview: dict[int, int] = {}
        for b in straight_bets:
            if b.status != "PENDING":
                continue
            allowed, amount = await _bet_cashout_preview(db, b, markets_map.get(b.market_id), settings)
            if allowed:
                bet_cashout_preview[b.id] = amount

        parlay_cashout_preview: dict[int, int] = {}
        for p in parlays:
            if p.status != "PENDING":
                continue
            allowed, amount = await _parlay_cashout_preview(p, settings)
            if allowed:
                parlay_cashout_preview[p.id] = amount

    return request.app.state.templates.TemplateResponse("my_bets.html", {
        "request": request,
        "user": user,
        "straight_bets": straight_bets,
        "parlays": parlays,
        "markets_map": markets_map,
        "parlay_legs": parlay_legs,
        "parlay_markets": parlay_markets,
        "bet_cashout_preview": bet_cashout_preview,
        "parlay_cashout_preview": parlay_cashout_preview,
        "success": success,
        "error": error,
    })


# ── Single Bet ─────────────────────────────────────────────────────────────────

@router.get("/bet/{market_id}")
async def bet_form(
    request: Request,
    market_id: int,
    user: SessionUser = Depends(require_user),
    error: str = "",
):
    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            return _redirect("/markets", error="Market+is+not+open+for+betting.")

        db_user = await _get_or_create_user(db, user)
        await db.commit()

        tribute_a = await db.get(Tribute, market.tribute_a_id) if market.tribute_a_id else None
        tribute_b = await db.get(Tribute, market.tribute_b_id) if market.tribute_b_id else None

        existing = (await db.execute(
            select(Bet).where(
                Bet.guild_id == _GUILD_ID(),
                Bet.user_id == user.discord_id,
                Bet.market_id == market_id,
                Bet.status == "PENDING",
                Bet.parlay_id.is_(None),
            )
        )).scalars().first()

    return request.app.state.templates.TemplateResponse("bet.html", {
        "request": request,
        "user": user,
        "market": market,
        "db_user": db_user,
        "tribute_a": tribute_a,
        "tribute_b": tribute_b,
        "existing": existing,
        "error": error,
    })


@router.post("/bet/{market_id}")
async def place_bet(
    request: Request,
    market_id: int,
    user: SessionUser = Depends(require_user),
    wager: Annotated[int, Form()] = 0,
):
    if wager < 1:
        return _redirect(f"/bet/{market_id}", error="Wager+must+be+at+least+1+chip.")
    if await _paused():
        return _redirect(f"/bet/{market_id}", error=_PAUSED_ERROR)

    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            return _redirect("/markets", error="Market+is+not+open.")

        db_user = await _get_or_create_user(db, user)

        if db_user.chips < wager:
            return _redirect(f"/bet/{market_id}", error="Insufficient+chips.")

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
            return _redirect(f"/bet/{market_id}", error="You+already+have+a+pending+bet+on+this+market.")

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

    return _redirect("/my-bets", msg=f"Bet+placed!+Win+{payout:,}+chips+if+correct.")


# ── Cashout ────────────────────────────────────────────────────────────────────

@router.post("/cashout/bet/{bet_id}")
async def cashout_bet(request: Request, bet_id: int, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        bet = await db.get(Bet, bet_id)
        if not bet or bet.user_id != user.discord_id or bet.guild_id != _GUILD_ID() or bet.status != "PENDING":
            return _redirect("/my-bets", error="Bet+not+found+or+not+cashout-eligible.")

        market = await db.get(Market, bet.market_id)
        settings = await _cashout_settings(db)
        allowed, amount = await _bet_cashout_preview(db, bet, market, settings)
        if not allowed:
            return _redirect("/my-bets", error="Cashout+is+not+currently+allowed.")

        db_user = (await db.execute(select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == user.discord_id))).scalar_one_or_none()
        if db_user:
            db_user.chips += amount
        bet.status = "CASHED_OUT"
        bet.cashout_amount = amount
        await db.commit()

    return _redirect("/my-bets", msg=f"Cashed+out+for+{amount:,}+chips.")


@router.post("/cashout/parlay/{parlay_id}")
async def cashout_parlay(request: Request, parlay_id: int, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        parlay = await db.get(Parlay, parlay_id)
        if not parlay or parlay.user_id != user.discord_id or parlay.guild_id != _GUILD_ID() or parlay.status != "PENDING":
            return _redirect("/my-bets", error="Parlay+not+found+or+not+cashout-eligible.")

        settings = await _cashout_settings(db)
        allowed, amount = await _parlay_cashout_preview(parlay, settings)
        if not allowed:
            return _redirect("/my-bets", error="Cashout+is+not+currently+allowed.")

        db_user = (await db.execute(select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == user.discord_id))).scalar_one_or_none()
        if db_user:
            db_user.chips += amount
        parlay.status = "CASHED_OUT"
        parlay.cashout_amount = amount

        legs = (await db.execute(
            select(Bet).where(Bet.parlay_id == parlay_id)
        )).scalars().all()
        for leg in legs:
            leg.status = "CASHED_OUT"
            leg.cashout_amount = 0

        await db.commit()

    return _redirect("/my-bets", msg=f"Parlay+cashed+out+for+{amount:,}+chips.")


# ── Parlay Slip ────────────────────────────────────────────────────────────────

@router.get("/parlay")
async def parlay_view(
    request: Request,
    user: SessionUser = Depends(require_user),
    success: str = "",
    error: str = "",
):
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

        # Combined odds
        if len(legs) >= 2:
            odds_list = [leg_markets[l.id].odds for l in legs if l.id in leg_markets]
            combined = combined_american(odds_list) if odds_list else None
        else:
            combined = None

    return request.app.state.templates.TemplateResponse("parlay.html", {
        "request": request,
        "user": user,
        "db_user": db_user,
        "legs": legs,
        "leg_markets": leg_markets,
        "combined": combined,
        "max_legs": MAX_PARLAY_LEGS,
        "success": success,
        "error": error,
    })


@router.post("/parlay/add/{market_id}")
async def parlay_add(market_id: int, request: Request, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            return _respond(request, "/parlay", error="Market+is+not+open.")

        existing_legs = (await db.execute(
            select(PendingParlayLeg).where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
        )).scalars().all()

        if len(existing_legs) >= MAX_PARLAY_LEGS:
            return _respond(request, "/parlay", error=f"Maximum+{MAX_PARLAY_LEGS}+legs+reached.")

        if any(l.market_id == market_id for l in existing_legs):
            return _respond(request, "/parlay", error="This+market+is+already+in+your+slip.")

        existing_markets = []
        for l in existing_legs:
            mkt = await db.get(Market, l.market_id)
            if mkt:
                existing_markets.append(mkt)

        tribute_by_id = await tribute_lookup_for_markets(db, existing_markets + [market])
        conflict = _parlay_conflict(existing_markets, market, tribute_by_id)
        if conflict:
            return _respond(request, "/parlay", error=conflict.replace(" ", "+"))

        leg = PendingParlayLeg(guild_id=_GUILD_ID(), user_id=user.discord_id, market_id=market_id)
        db.add(leg)
        await db.commit()

    return _respond(request, "/parlay", msg="Market+added+to+parlay+slip.")


@router.post("/parlay/remove/{leg_id}")
async def parlay_remove(leg_id: int, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        leg = await db.get(PendingParlayLeg, leg_id)
        if leg and leg.user_id == user.discord_id and leg.guild_id == _GUILD_ID():
            await db.delete(leg)
            await db.commit()
    return _redirect("/parlay")


@router.post("/parlay/clear")
async def parlay_clear(user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        legs = (await db.execute(
            select(PendingParlayLeg).where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
        )).scalars().all()
        for leg in legs:
            await db.delete(leg)
        await db.commit()
    return _redirect("/parlay")


@router.post("/parlay/submit")
async def parlay_submit(
    user: SessionUser = Depends(require_user),
    wager: Annotated[int, Form()] = 0,
    is_public: Annotated[str, Form()] = "",
):
    if wager < 1:
        return _redirect("/parlay", error="Wager+must+be+at+least+1+chip.")
    if await _paused():
        return _redirect("/parlay", error=_PAUSED_ERROR)

    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)

        legs_raw = (await db.execute(
            select(PendingParlayLeg)
            .where(PendingParlayLeg.guild_id == _GUILD_ID(), PendingParlayLeg.user_id == user.discord_id)
            .order_by(PendingParlayLeg.added_at)
        )).scalars().all()

        if len(legs_raw) < 2:
            return _redirect("/parlay", error="A+parlay+requires+at+least+2+legs.")

        leg_markets = []
        for l in legs_raw:
            mkt = await db.get(Market, l.market_id)
            if not mkt or mkt.status != "OPEN":
                return _redirect("/parlay", error="One+or+more+markets+are+no+longer+open.")
            leg_markets.append(mkt)

        if db_user.chips < wager:
            return _redirect("/parlay", error="Insufficient+chips.")

        odds_list = [m.odds for m in leg_markets]
        total_payout = min(parlay_payout(wager, odds_list), PARLAY_PAYOUT_CAP)

        public = is_public == "on"
        p = Parlay(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            total_wager=wager,
            total_payout=total_payout,
            status="PENDING",
            is_public=public,
        )
        db.add(p)
        await db.flush()

        for mkt in leg_markets:
            bet = Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id,
                parlay_id=p.id,
                market_id=mkt.id,
                wager=wager,
                odds_at_placement=mkt.odds,
                payout_if_win=0,
                status="PENDING",
            )
            db.add(bet)

        db_user.chips -= wager
        db_user.total_wagered += wager

        for l in legs_raw:
            await db.delete(l)

        await db.commit()

    return _redirect("/my-bets", msg=f"Parlay+submitted!+Potential+payout:+{total_payout:,}+chips.")


# ── Tail Board ─────────────────────────────────────────────────────────────────

@router.get("/tail")
async def tail_board(
    request: Request,
    user: SessionUser = Depends(require_user),
    success: str = "",
    error: str = "",
):
    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)
        await db.commit()

        templates_raw = (await db.execute(
            select(ParlayTemplate).where(ParlayTemplate.active == True).order_by(ParlayTemplate.created_at.desc())
        )).scalars().all()

        tpl_legs: dict[int, list] = {}
        tpl_markets: dict[int, Market] = {}
        for tpl in templates_raw:
            legs = (await db.execute(
                select(ParlayTemplateLeg)
                .where(ParlayTemplateLeg.template_id == tpl.id)
                .order_by(ParlayTemplateLeg.sort_order)
            )).scalars().all()
            tpl_legs[tpl.id] = legs
            for leg in legs:
                if leg.market_id not in tpl_markets:
                    mkt = await db.get(Market, leg.market_id)
                    if mkt:
                        tpl_markets[leg.market_id] = mkt

        # Build dynamic parlay flavor text for each template
        tail_tid_set: set[int] = set()
        for mkt in tpl_markets.values():
            if mkt.tribute_a_id: tail_tid_set.add(mkt.tribute_a_id)
            if mkt.tribute_b_id: tail_tid_set.add(mkt.tribute_b_id)

        tail_tributes_map: dict = {}
        tail_alliance_names: dict = {}
        tail_district_records: dict = {}
        if tail_tid_set:
            pt_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(tail_tid_set))
            )).scalars().all()
            tail_tributes_map = {t.id: t for t in pt_rows}

            aid_set = {t.alliance_id for t in pt_rows if t.alliance_id}
            if aid_set:
                a_rows = (await db.execute(
                    select(Alliance).where(Alliance.id.in_(aid_set))
                )).scalars().all()
                tail_alliance_names = {a.id: a.name for a in a_rows}

            parlay_districts = {t.district for t in pt_rows}
            dr_rows = (await db.execute(
                select(DistrictRecord).where(DistrictRecord.district.in_(parlay_districts))
            )).scalars().all()
            tail_district_records = {dr.district: dr for dr in dr_rows}

        tpl_flavor: dict[int, dict] = {}
        for tpl in templates_raw:
            if tpl.source == "AI" and tpl.description:
                tpl_flavor[tpl.id] = {"name": tpl.name, "description": tpl.description}
            else:
                leg_mkts = [tpl_markets[leg.market_id] for leg in tpl_legs.get(tpl.id, []) if leg.market_id in tpl_markets]
                name, desc = _parlay_flavor(
                    leg_mkts, tail_tributes_map, tail_alliance_names,
                    tail_district_records, tpl.name, tpl.description,
                )
                tpl_flavor[tpl.id] = {"name": name, "description": desc}

        # Combined odds per template, for live payout preview
        tpl_combined: dict[int, int] = {}
        for tpl in templates_raw:
            odds_list = [
                tpl_markets[leg.market_id].odds
                for leg in tpl_legs.get(tpl.id, [])
                if leg.market_id in tpl_markets
            ]
            tpl_combined[tpl.id] = combined_american(odds_list) if odds_list else None

        # Member public parlays
        mp_rows = (await db.execute(
            select(Parlay)
            .where(Parlay.guild_id == _GUILD_ID(), Parlay.status == "PENDING", Parlay.is_public == True)  # noqa: E712
            .order_by(Parlay.placed_at.desc())
            .limit(20)
        )).scalars().all()

        member_parlays: list[Parlay] = []
        member_parlay_legs: dict[int, list[Bet]] = {}
        member_parlay_markets: dict[int, Market] = {}
        member_parlay_owners: dict[int, str] = {}
        member_parlay_combined: dict[int, int] = {}

        for mp in mp_rows:
            legs = (await db.execute(
                select(Bet).where(Bet.parlay_id == mp.id).order_by(Bet.id)
            )).scalars().all()
            mkts: list[Market] = []
            ok = True
            for b in legs:
                mkt = await db.get(Market, b.market_id)
                if not mkt or mkt.status != "OPEN":
                    ok = False
                    break
                mkts.append(mkt)
            if not ok or len(mkts) < 2:
                continue
            member_parlays.append(mp)
            member_parlay_legs[mp.id] = list(legs)
            member_parlay_combined[mp.id] = combined_american([m.odds for m in mkts])
            for mkt in mkts:
                member_parlay_markets[mkt.id] = mkt
            owner = (await db.execute(
                select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == mp.user_id)
            )).scalar_one_or_none()
            member_parlay_owners[mp.id] = owner.username if owner else "Member"

    return request.app.state.templates.TemplateResponse("tail.html", {
        "request": request,
        "user": user,
        "db_user": db_user,
        "templates": templates_raw,
        "tpl_legs": tpl_legs,
        "tpl_markets": tpl_markets,
        "tpl_flavor": tpl_flavor,
        "tpl_combined": tpl_combined,
        "member_parlays": member_parlays,
        "member_parlay_legs": member_parlay_legs,
        "member_parlay_markets": member_parlay_markets,
        "member_parlay_owners": member_parlay_owners,
        "member_parlay_combined": member_parlay_combined,
        "success": success,
        "error": error,
    })


@router.post("/tail/{template_id}")
async def tail_parlay(
    template_id: int,
    user: SessionUser = Depends(require_user),
    wager: Annotated[int, Form()] = 0,
):
    if wager < 1:
        return _redirect("/tail", error="Wager+must+be+at+least+1+chip.")
    if await _paused():
        return _redirect("/tail", error=_PAUSED_ERROR)

    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, template_id)
        if not tpl or not tpl.active:
            return _redirect("/tail", error="Template+not+found.")

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
                return _redirect("/tail", error="One+or+more+markets+in+this+template+are+no+longer+open.")
            leg_markets.append(mkt)

        if len(leg_markets) < 2:
            return _redirect("/tail", error="Template+has+insufficient+open+markets.")

        if db_user.chips < wager:
            return _redirect("/tail", error="Insufficient+chips.")

        odds_list = [m.odds for m in leg_markets]
        total_payout = min(parlay_payout(wager, odds_list), PARLAY_PAYOUT_CAP)

        p = Parlay(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            total_wager=wager,
            total_payout=total_payout,
            status="PENDING",
            is_public=False,
        )
        db.add(p)
        await db.flush()

        for mkt in leg_markets:
            bet = Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id,
                parlay_id=p.id,
                market_id=mkt.id,
                wager=wager,
                odds_at_placement=mkt.odds,
                payout_if_win=0,
                status="PENDING",
            )
            db.add(bet)

        db_user.chips -= wager
        db_user.total_wagered += wager
        await db.commit()

    return _redirect("/my-bets", msg=f"Parlay+tailed!+Potential+payout:+{total_payout:,}+chips.")


@router.post("/tail/parlay/{parlay_id}")
async def tail_member_parlay(
    parlay_id: int,
    user: SessionUser = Depends(require_user),
    wager: Annotated[int, Form()] = 0,
):
    if wager < 1:
        return _redirect("/tail", error="Wager+must+be+at+least+1+chip.")
    if await _paused():
        return _redirect("/tail", error=_PAUSED_ERROR)

    async with get_db() as db:
        source = await db.get(Parlay, parlay_id)
        if not source or not source.is_public or source.status != "PENDING":
            return _redirect("/tail", error="Parlay+not+found+or+no+longer+available.")

        db_user = await _get_or_create_user(db, user)

        legs = (await db.execute(
            select(Bet).where(Bet.parlay_id == parlay_id).order_by(Bet.id)
        )).scalars().all()

        leg_markets = []
        for b in legs:
            mkt = await db.get(Market, b.market_id)
            if not mkt or mkt.status != "OPEN":
                return _redirect("/tail", error="One+or+more+markets+in+this+parlay+are+no+longer+open.")
            leg_markets.append(mkt)

        if len(leg_markets) < 2:
            return _redirect("/tail", error="Parlay+has+insufficient+open+markets.")

        if db_user.chips < wager:
            return _redirect("/tail", error="Insufficient+chips.")

        odds_list = [m.odds for m in leg_markets]
        total_payout = min(parlay_payout(wager, odds_list), PARLAY_PAYOUT_CAP)

        p = Parlay(
            guild_id=_GUILD_ID(),
            user_id=user.discord_id,
            total_wager=wager,
            total_payout=total_payout,
            status="PENDING",
            is_public=False,
        )
        db.add(p)
        await db.flush()

        for mkt in leg_markets:
            bet = Bet(
                guild_id=_GUILD_ID(),
                user_id=user.discord_id,
                parlay_id=p.id,
                market_id=mkt.id,
                wager=wager,
                odds_at_placement=mkt.odds,
                payout_if_win=0,
                status="PENDING",
            )
            db.add(bet)

        db_user.chips -= wager
        db_user.total_wagered += wager
        await db.commit()

    return _redirect("/my-bets", msg=f"Parlay+tailed!+Potential+payout:+{total_payout:,}+chips.")


def _add_to_slip_response(request: Request, added: int, skipped: int):
    if added == 0:
        return _respond(
            request, "/tail",
            error="Couldn't+add+any+legs+—+they're+already+in+your+slip+or+conflict+with+what's+there.",
        )
    message = f"Added+{added}+leg{'s' if added != 1 else ''}+to+your+parlay+slip."
    if skipped:
        message += f"+Skipped+{skipped}+(already+in+slip+or+conflicting)."
    return _respond(request, "/tail", msg=message)


@router.post("/tail/{template_id}/add-to-slip")
async def tail_template_add_to_slip(
    template_id: int, request: Request, user: SessionUser = Depends(require_user)
):
    """Copy a featured template's legs into the caller's own parlay slip so they
    can edit it (add/remove legs, change wager) before submitting, instead of
    only being able to tail it as a fixed package."""
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, template_id)
        if not tpl or not tpl.active:
            return _respond(request, "/tail", error="Template+not+found.")

        legs = (await db.execute(
            select(ParlayTemplateLeg)
            .where(ParlayTemplateLeg.template_id == template_id)
            .order_by(ParlayTemplateLeg.sort_order)
        )).scalars().all()

        tpl_markets = []
        for leg in legs:
            mkt = await db.get(Market, leg.market_id)
            if mkt and mkt.status == "OPEN":
                tpl_markets.append(mkt)
        if not tpl_markets:
            return _respond(request, "/tail", error="No+open+markets+in+this+parlay+to+add.")

        added, skipped = await add_markets_to_pending_slip(
            db, _GUILD_ID(), user.discord_id, tpl_markets
        )
        await db.commit()

    return _add_to_slip_response(request, added, skipped)


@router.post("/tail/parlay/{parlay_id}/add-to-slip")
async def tail_member_parlay_add_to_slip(
    parlay_id: int, request: Request, user: SessionUser = Depends(require_user)
):
    """Copy a public member parlay's legs into the caller's own parlay slip so
    they can edit it before submitting, instead of only being able to tail it."""
    async with get_db() as db:
        source = await db.get(Parlay, parlay_id)
        if not source or not source.is_public or source.status != "PENDING":
            return _respond(request, "/tail", error="Parlay+not+found+or+no+longer+available.")

        legs = (await db.execute(
            select(Bet).where(Bet.parlay_id == parlay_id).order_by(Bet.id)
        )).scalars().all()

        tpl_markets = []
        for b in legs:
            mkt = await db.get(Market, b.market_id)
            if mkt and mkt.status == "OPEN":
                tpl_markets.append(mkt)
        if not tpl_markets:
            return _respond(request, "/tail", error="No+open+markets+in+this+parlay+to+add.")

        added, skipped = await add_markets_to_pending_slip(
            db, _GUILD_ID(), user.discord_id, tpl_markets
        )
        await db.commit()

    return _add_to_slip_response(request, added, skipped)

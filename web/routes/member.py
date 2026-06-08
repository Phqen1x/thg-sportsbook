from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from bot.cogs.betting import _parlay_conflict, PARLAY_PAYOUT_CAP, MAX_PARLAY_LEGS
from bot.database.models import Bet, Market, Parlay, PendingParlayLeg, ParlayTemplate, ParlayTemplateLeg, Tribute, User
from bot.odds.calculator import straight_payout, parlay_payout, combined_american, cashout_value
from web.database import get_db
from web.deps import optional_user, require_user
from web.session import SessionUser

router = APIRouter(tags=["member"])


async def _get_or_create_user(db, session_user: SessionUser) -> User:
    u = await db.get(User, session_user.discord_id)
    if u is None:
        from web import config
        default_raw = (await db.execute(
            __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='default_chips'")
        )).fetchone()
        default = json.loads(default_raw[0]) if default_raw else config.DEFAULT_CHIPS
        u = User(discord_id=session_user.discord_id, username=session_user.username, chips=default)
        db.add(u)
        await db.flush()
    return u


def _redirect(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if error:
        return RedirectResponse(f"{url}{sep}error={error}", status_code=303)
    if msg:
        return RedirectResponse(f"{url}{sep}success={msg}", status_code=303)
    return RedirectResponse(url, status_code=303)


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
            select(Bet).where(Bet.user_id == user.discord_id, Bet.parlay_id.is_(None))
        )).scalars().all()
        parlays = (await db.execute(
            select(Parlay).where(Parlay.user_id == user.discord_id)
        )).scalars().all()

        won = sum(b.payout_if_win for b in bets if b.status == "WON")
        won += sum(p.total_payout for p in parlays if p.status == "WON")
        wagered = db_user.total_wagered
        roi = ((won - wagered) / wagered * 100) if wagered else 0.0

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
            .where(Bet.user_id == user.discord_id, Bet.parlay_id.is_(None))
            .order_by(Bet.placed_at.desc())
        )).scalars().all()

        parlays = (await db.execute(
            select(Parlay)
            .where(Parlay.user_id == user.discord_id)
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

    return request.app.state.templates.TemplateResponse("my_bets.html", {
        "request": request,
        "user": user,
        "straight_bets": straight_bets,
        "parlays": parlays,
        "markets_map": markets_map,
        "parlay_legs": parlay_legs,
        "parlay_markets": parlay_markets,
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

    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            return _redirect("/markets", error="Market+is+not+open.")

        db_user = await _get_or_create_user(db, user)

        if db_user.chips < wager:
            return _redirect(f"/bet/{market_id}", error="Insufficient+chips.")

        existing = (await db.execute(
            select(Bet).where(
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
        if not bet or bet.user_id != user.discord_id or bet.status != "PENDING":
            return _redirect("/my-bets", error="Bet+not+found+or+not+cashout-eligible.")

        market = await db.get(Market, bet.market_id)
        rate = (market.cashout_rate if market and market.cashout_rate is not None else None)
        if rate is None:
            # Use global default
            row = (await db.execute(
                __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='cashout_rate'")
            )).fetchone()
            rate = float(row[0]) if row else 0.65

        allowed_row = (await db.execute(
            __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='cashout_allowed'")
        )).fetchone()
        if allowed_row and allowed_row[0].lower() == "false":
            if not market or not market.cashout_allowed:
                return _redirect("/my-bets", error="Cashout+is+not+currently+allowed.")

        amount = cashout_value(bet.wager, bet.payout_if_win, rate)
        db_user = await db.get(User, user.discord_id)
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
        if not parlay or parlay.user_id != user.discord_id or parlay.status != "PENDING":
            return _redirect("/my-bets", error="Parlay+not+found+or+not+cashout-eligible.")

        allowed_row = (await db.execute(
            __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='cashout_allowed'")
        )).fetchone()
        if allowed_row and allowed_row[0].lower() == "false":
            return _redirect("/my-bets", error="Cashout+is+not+currently+allowed.")

        row = (await db.execute(
            __import__("sqlalchemy").text("SELECT value FROM game_settings WHERE key='cashout_rate'")
        )).fetchone()
        rate = float(row[0]) if row else 0.65

        amount = cashout_value(parlay.total_wager, parlay.total_payout, rate)
        db_user = await db.get(User, user.discord_id)
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
            .where(PendingParlayLeg.user_id == user.discord_id)
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
async def parlay_add(market_id: int, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        market = await db.get(Market, market_id)
        if not market or market.status != "OPEN":
            return _redirect("/parlay", error="Market+is+not+open.")

        existing_legs = (await db.execute(
            select(PendingParlayLeg).where(PendingParlayLeg.user_id == user.discord_id)
        )).scalars().all()

        if len(existing_legs) >= MAX_PARLAY_LEGS:
            return _redirect("/parlay", error=f"Maximum+{MAX_PARLAY_LEGS}+legs+reached.")

        if any(l.market_id == market_id for l in existing_legs):
            return _redirect("/parlay", error="This+market+is+already+in+your+slip.")

        existing_markets = []
        for l in existing_legs:
            mkt = await db.get(Market, l.market_id)
            if mkt:
                existing_markets.append(mkt)

        conflict = _parlay_conflict(existing_markets, market)
        if conflict:
            return _redirect(f"/parlay", error=conflict.replace(" ", "+"))

        leg = PendingParlayLeg(user_id=user.discord_id, market_id=market_id)
        db.add(leg)
        await db.commit()

    return _redirect("/parlay", msg="Market+added+to+parlay+slip.")


@router.post("/parlay/remove/{leg_id}")
async def parlay_remove(leg_id: int, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        leg = await db.get(PendingParlayLeg, leg_id)
        if leg and leg.user_id == user.discord_id:
            await db.delete(leg)
            await db.commit()
    return _redirect("/parlay")


@router.post("/parlay/clear")
async def parlay_clear(user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        legs = (await db.execute(
            select(PendingParlayLeg).where(PendingParlayLeg.user_id == user.discord_id)
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

    async with get_db() as db:
        db_user = await _get_or_create_user(db, user)

        legs_raw = (await db.execute(
            select(PendingParlayLeg)
            .where(PendingParlayLeg.user_id == user.discord_id)
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

    return request.app.state.templates.TemplateResponse("tail.html", {
        "request": request,
        "user": user,
        "db_user": db_user,
        "templates": templates_raw,
        "tpl_legs": tpl_legs,
        "tpl_markets": tpl_markets,
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

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text

from bot.database.models import Alliance, Bet, BettingPhase, Market, Tribute, User
from web.database import get_db
from web.deps import optional_user
from web.session import SessionUser

router = APIRouter(tags=["public"])


async def _phase_name(db) -> str | None:
    row = (await db.execute(text("SELECT value FROM game_settings WHERE key='active_phase_id'"))).fetchone()
    if not row:
        return None
    phase = await db.get(BettingPhase, int(row[0]))
    return phase.name if phase else None


@router.get("/")
async def home(request: Request, user: SessionUser | None = Depends(optional_user)):
    async with get_db() as db:
        tributes = (await db.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(Tribute.district)
        )).scalars().all()

        win_markets_raw = (await db.execute(
            select(Market).where(Market.type == "TRIBUTE_WINS", Market.status == "OPEN")
        )).scalars().all()
        win_markets = {m.tribute_a_id: m for m in win_markets_raw}

        leaderboard = (await db.execute(
            select(User).order_by(User.chips.desc()).limit(10)
        )).scalars().all()

        open_count = (await db.execute(
            select(func.count(Market.id)).where(Market.status == "OPEN")
        )).scalar() or 0

        alive_count = (await db.execute(
            select(func.count(Tribute.id)).where(Tribute.status == "ALIVE")
        )).scalar() or 0

        phase_name = await _phase_name(db)

    return request.app.state.templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "tributes": tributes,
        "win_markets": win_markets,
        "leaderboard": leaderboard,
        "open_count": open_count,
        "alive_count": alive_count,
        "phase_name": phase_name,
    })


@router.get("/tributes")
async def tributes(request: Request, user: SessionUser | None = Depends(optional_user)):
    async with get_db() as db:
        tributes_list = (await db.execute(
            select(Tribute).order_by(Tribute.district, Tribute.gender)
        )).scalars().all()

        win_markets_raw = (await db.execute(
            select(Market).where(Market.type == "TRIBUTE_WINS", Market.status == "OPEN")
        )).scalars().all()
        win_markets = {m.tribute_a_id: m for m in win_markets_raw}

        alliances_raw = (await db.execute(select(Alliance))).scalars().all()
        alliances = {a.id: a.name for a in alliances_raw}

    return request.app.state.templates.TemplateResponse("tributes.html", {
        "request": request,
        "user": user,
        "tributes": tributes_list,
        "win_markets": win_markets,
        "alliances": alliances,
    })


@router.get("/markets")
async def markets(
    request: Request,
    user: SessionUser | None = Depends(optional_user),
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
        tributes_map: dict = {}
        if tribute_ids:
            t_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(tribute_ids))
            )).scalars().all()
            tributes_map = {t.id: t for t in t_rows}

        # Bet counts per market (for display)
        bet_counts_raw = (await db.execute(
            select(Bet.market_id, func.count(Bet.id)).group_by(Bet.market_id)
        )).all()
        bet_counts = {row[0]: row[1] for row in bet_counts_raw}

        phase_name = await _phase_name(db)

    return request.app.state.templates.TemplateResponse("markets.html", {
        "request": request,
        "user": user,
        "markets": markets_list,
        "tributes_map": tributes_map,
        "bet_counts": bet_counts,
        "status": status,
        "type_filter": type_filter,
        "phase_name": phase_name,
    })


@router.get("/leaderboard")
async def leaderboard(request: Request, user: SessionUser | None = Depends(optional_user)):
    async with get_db() as db:
        users = (await db.execute(
            select(User).order_by(User.chips.desc()).limit(100)
        )).scalars().all()

    return request.app.state.templates.TemplateResponse("leaderboard.html", {
        "request": request,
        "user": user,
        "users": users,
    })

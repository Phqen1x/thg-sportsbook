from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, text

from bot.database.models import (
    Alliance, Bet, BettingPhase, ExchangeRateOverride, Market, Modifier, ModifierAssignment,
    Parlay, ParlayTemplate, ParlayTemplateLeg, PublicBetRestriction, Tribute, User,
)
from bot.odds.calculator import parlay_payout
from bot.utils.economy import economy_totals
from bot.utils.exchange_rates import clear_override, list_overrides, set_override
from bot.utils.restrictions import list_public_blocks, set_public_block
from web import config as _web_config
from web.audit import post_admin_action
from web.database import get_db, get_request_guild
from web.deps import require_admin
from web.session import SessionUser

_GUILD_ID = get_request_guild

router = APIRouter(prefix="/admin", tags=["admin"])

_MAX_GIVE_ALL = 100_000


def _redirect(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if error:
        return RedirectResponse(f"{url}{sep}error={error}", status_code=303)
    if msg:
        return RedirectResponse(f"{url}{sep}success={msg}", status_code=303)
    return RedirectResponse(url, status_code=303)


MARKET_TYPES = [
    ("TRIBUTE_WINS", "Tribute Wins (Victor)"),
    ("TRIBUTE_PLACEMENT", "Tribute Placement (Exact)"),
    ("TRIBUTE_TOP_N", "Tribute Top-N Finish"),
    ("TRIBUTE_RUNNER_UP", "Tribute Runner-Up (2nd Place)"),
    ("FIRST_TRIBUTE_TO_DIE", "First Tribute to Die"),
    ("TRIBUTE_KILLS", "Top Killer"),
    ("KILL_EVENT", "Kill Event (A kills B)"),
    ("DEATH_CAUSE", "Death Cause"),
    ("FIRST_BLOOD", "First Blood"),
    ("BLOODBATH_SURVIVOR", "Bloodbath Survivor"),
    ("TRIBUTE_KILLED_BLOODBATH", "Killed in Bloodbath"),
    ("FIRST_IN_ALLIANCE_DEATH", "First in Alliance to Die"),
    ("HIGHEST_TRAINING_SCORE", "Highest Training Score"),
    ("LOWEST_TRAINING_SCORE", "Lowest Training Score"),
    ("KILLS_OU", "Kills Over/Under"),
    ("PLACEMENT_OU", "Placement Over/Under"),
    ("MAKES_FINAL_8", "Makes Final 8"),
    ("MISSES_FINAL_8", "Eliminated Before Final 8"),
    ("MAKES_FINAL_5", "Makes Final 5"),
    ("MISSES_FINAL_5", "Eliminated Before Final 5"),
    ("ARENA_TYPE", "Arena Type"),
    ("EXACT_TRAINING_SCORE", "Exact Training Score"),
    ("TRAINING_SCORE_OU", "Training Score Over/Under"),
    ("BLOODBATH_KILLS_OU", "Bloodbath Kills Over/Under"),
    ("BLOODBATH_DEATHS_OU", "Bloodbath Deaths Over/Under"),
    ("EXACT_BLOODBATH_DEATHS", "Exact Bloodbath Deaths"),
    ("BLOODBATH_NO_DEATHS", "Bloodbath Contains No Deaths"),
    ("GAMES_DURATION", "Games Duration (Days)"),
    ("GAMES_FEAST", "Games — Features a Feast"),
    ("GAMES_BETRAYAL", "Games — Features a Betrayal"),
    ("DISTRICT_VICTOR", "District Futures"),
    ("DISTRICT_KILLS_OU", "District Total Kills Over/Under"),
    ("DISTRICT_BOTH_BLOODBATH", "District Both Survive Bloodbath"),
    ("DISTRICT_BOTH_FINAL_8", "District Both Make Final 8"),
    ("DISTRICT_ONE_FINAL_8", "District At Least One Makes Final 8"),
    ("DISTRICT_BOTH_FINAL_5", "District Both Make Final 5"),
    ("DISTRICT_ONE_FINAL_5", "District At Least One Makes Final 5"),
    ("ALLIANCE_VICTOR", "Alliance Victor"),
    ("ALLIANCE_KILLS_OU", "Alliance Total Kills Over/Under"),
    ("ALLIANCE_ALL_FINAL_8", "Alliance All Make Final 8"),
    ("ALLIANCE_ONE_FINAL_8", "Alliance At Least One Makes Final 8"),
    ("PARTNER_PLACE_HIGHER", "Places Higher Than District Partner"),
    ("PARTNER_PLACE_LOWER", "Places Lower Than District Partner"),
    ("PARTNER_SCORE_HIGHER", "Scores Higher Than District Partner"),
    ("PARTNER_SCORE_LOWER", "Scores Lower Than District Partner"),
]


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def dashboard(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        alive = (await db.execute(select(func.count(Tribute.id)).where(Tribute.status == "ALIVE"))).scalar() or 0
        dead = (await db.execute(select(func.count(Tribute.id)).where(Tribute.status == "DEAD"))).scalar() or 0
        open_mkts = (await db.execute(select(func.count(Market.id)).where(Market.status == "OPEN"))).scalar() or 0
        total_bets = (await db.execute(select(func.count(Bet.id)))).scalar() or 0
        pending_bets = (await db.execute(select(func.count(Bet.id)).where(Bet.status == "PENDING", Bet.parlay_id.is_(None)))).scalar() or 0
        total_users = (await db.execute(select(func.count(User.discord_id)).where(User.guild_id == _GUILD_ID()))).scalar() or 0
        phases = (await db.execute(select(BettingPhase).order_by(BettingPhase.sort_order))).scalars().all()
        phase_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='current_phase_id'"))).fetchone()
        active_phase_id = json.loads(phase_row[0]) if phase_row else None
        economy = await economy_totals(db, _GUILD_ID())

    return request.app.state.templates.TemplateResponse("admin/index.html", {
        "request": request, "user": user,
        "alive": alive, "dead": dead, "open_mkts": open_mkts,
        "total_bets": total_bets, "pending_bets": pending_bets, "total_users": total_users,
        "phases": phases, "active_phase_id": active_phase_id, "economy": economy,
        "success": success, "error": error,
    })


# ── Phases ─────────────────────────────────────────────────────────────────────

@router.get("/phases")
async def phases(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        phases_list = (await db.execute(select(BettingPhase).order_by(BettingPhase.sort_order))).scalars().all()
        phase_row = (await db.execute(text("SELECT value FROM game_settings WHERE key='current_phase_id'"))).fetchone()
        active_phase_id = json.loads(phase_row[0]) if phase_row else None

    return request.app.state.templates.TemplateResponse("admin/phases.html", {
        "request": request, "user": user,
        "phases": phases_list, "active_phase_id": active_phase_id,
        "success": success, "error": error,
    })


@router.post("/phases/new")
async def phase_create(
    user: SessionUser = Depends(require_admin),
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
):
    if not name.strip():
        return _redirect("/admin/phases", error="Phase+name+is+required.")
    async with get_db() as db:
        phase = BettingPhase(name=name.strip(), description=description.strip() or None, sort_order=sort_order)
        db.add(phase)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Phase created", {"name": name.strip(), "sort_order": str(sort_order)}))
    return _redirect("/admin/phases", msg="Phase+created.")


@router.post("/phases/{phase_id}/activate")
async def phase_activate(phase_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        phase = await db.get(BettingPhase, phase_id)
        if not phase:
            return _redirect("/admin/phases", error="Phase+not+found.")
        await db.execute(text("INSERT OR REPLACE INTO game_settings (key, value) VALUES ('current_phase_id', :v)"), {"v": str(phase_id)})
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Phase activated", {"phase": phase.name}))
    return _redirect("/admin/phases", msg=f"Phase+'{phase.name}'+activated.")


@router.post("/phases/{phase_id}/delete")
async def phase_delete(phase_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        phase = await db.get(BettingPhase, phase_id)
        if not phase:
            return _redirect("/admin/phases", error="Phase+not+found.")
        await db.delete(phase)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Phase deleted", {"phase": phase.name}))
    return _redirect("/admin/phases", msg="Phase+deleted.")


# ── Tributes ───────────────────────────────────────────────────────────────────

@router.get("/tributes")
async def tribute_list(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        tributes = (await db.execute(
            select(Tribute).order_by(Tribute.district, Tribute.gender)
        )).scalars().all()
        alliances = (await db.execute(select(Alliance))).scalars().all()
        alliance_map = {a.id: a.name for a in alliances}

    return request.app.state.templates.TemplateResponse("admin/tributes.html", {
        "request": request, "user": user,
        "tributes": tributes, "alliance_map": alliance_map,
        "success": success, "error": error,
    })


@router.get("/tributes/new")
async def tribute_new_form(request: Request, user: SessionUser = Depends(require_admin), error: str = ""):
    async with get_db() as db:
        alliances = (await db.execute(select(Alliance))).scalars().all()
    return request.app.state.templates.TemplateResponse("admin/tribute_form.html", {
        "request": request, "user": user,
        "tribute": None, "alliances": alliances, "error": error,
    })


@router.post("/tributes/new")
async def tribute_create(
    user: SessionUser = Depends(require_admin),
    name: Annotated[str, Form()] = "",
    district: Annotated[int, Form()] = 1,
    gender: Annotated[str, Form()] = "M",
    age: Annotated[str, Form()] = "",
    training_score: Annotated[str, Form()] = "",
    face_claim: Annotated[str, Form()] = "",
    times_played: Annotated[int, Form()] = 0,
    highest_placement: Annotated[str, Form()] = "",
    alliance_id: Annotated[str, Form()] = "",
    discord_user_id: Annotated[str, Form()] = "",
    non_binary: Annotated[str, Form()] = "",
    sade_participant: Annotated[str, Form()] = "",
    sade_champion: Annotated[str, Form()] = "",
):
    if not name.strip():
        return _redirect("/admin/tributes/new", error="Name+is+required.")
    async with get_db() as db:
        t = Tribute(
            name=name.strip(),
            district=district,
            gender=gender,
            age=int(age) if age.strip() else None,
            training_score=int(training_score) if training_score.strip() else None,
            face_claim=face_claim.strip() or None,
            times_played=times_played,
            highest_placement=int(highest_placement) if highest_placement.strip() else None,
            alliance_id=int(alliance_id) if alliance_id.strip() else None,
            discord_user_id=int(discord_user_id) if discord_user_id.strip() else None,
            non_binary=non_binary == "on",
            sade_participant=sade_participant == "on",
            sade_champion=sade_champion == "on",
            status="ALIVE",
        )
        db.add(t)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Tribute created", {"name": name.strip(), "district": str(district)}))
    return _redirect("/admin/tributes", msg="Tribute+created.")


@router.get("/tributes/{tribute_id}/edit")
async def tribute_edit_form(request: Request, tribute_id: int, user: SessionUser = Depends(require_admin), error: str = ""):
    async with get_db() as db:
        tribute = await db.get(Tribute, tribute_id)
        if not tribute:
            return _redirect("/admin/tributes", error="Tribute+not+found.")
        alliances = (await db.execute(select(Alliance))).scalars().all()
    return request.app.state.templates.TemplateResponse("admin/tribute_form.html", {
        "request": request, "user": user,
        "tribute": tribute, "alliances": alliances, "error": error,
    })


@router.post("/tributes/{tribute_id}/edit")
async def tribute_edit(
    tribute_id: int,
    user: SessionUser = Depends(require_admin),
    name: Annotated[str, Form()] = "",
    district: Annotated[int, Form()] = 1,
    gender: Annotated[str, Form()] = "M",
    age: Annotated[str, Form()] = "",
    training_score: Annotated[str, Form()] = "",
    face_claim: Annotated[str, Form()] = "",
    times_played: Annotated[int, Form()] = 0,
    highest_placement: Annotated[str, Form()] = "",
    alliance_id: Annotated[str, Form()] = "",
    discord_user_id: Annotated[str, Form()] = "",
    non_binary: Annotated[str, Form()] = "",
    sade_participant: Annotated[str, Form()] = "",
    sade_champion: Annotated[str, Form()] = "",
):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t:
            return _redirect("/admin/tributes", error="Tribute+not+found.")
        t.name = name.strip() or t.name
        t.district = district
        t.gender = gender
        t.age = int(age) if age.strip() else None
        t.training_score = int(training_score) if training_score.strip() else None
        t.face_claim = face_claim.strip() or None
        t.times_played = times_played
        t.highest_placement = int(highest_placement) if highest_placement.strip() else None
        t.alliance_id = int(alliance_id) if alliance_id.strip() else None
        t.discord_user_id = int(discord_user_id) if discord_user_id.strip() else None
        t.non_binary = non_binary == "on"
        t.sade_participant = sade_participant == "on"
        t.sade_champion = sade_champion == "on"
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Tribute updated", {"tribute": t.name, "district": str(t.district)}))
    return _redirect("/admin/tributes", msg="Tribute+updated.")


@router.post("/tributes/{tribute_id}/kill")
async def tribute_kill(
    tribute_id: int,
    user: SessionUser = Depends(require_admin),
    death_cause: Annotated[str, Form()] = "Another Tribute",
    killed_by_id: Annotated[str, Form()] = "",
    placement: Annotated[int, Form()] = 0,
):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t or t.status != "ALIVE":
            return _redirect("/admin/tributes", error="Tribute+not+found+or+already+dead.")
        t.status = "DEAD"
        t.death_cause = death_cause
        t.placement = placement if placement > 0 else None
        if killed_by_id.strip():
            killer_id = int(killed_by_id)
            t.killed_by_id = killer_id
            killer = await db.get(Tribute, killer_id)
            if killer:
                killer.kills = (killer.kills or 0) + 1
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Tribute eliminated", {"tribute": t.name, "cause": death_cause, "placement": str(placement) if placement > 0 else "auto"}))
    return _redirect("/admin/tributes", msg=f"{t.name}+has+been+eliminated.")


@router.post("/tributes/{tribute_id}/unkill")
async def tribute_unkill(tribute_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t or t.status == "ALIVE":
            return _redirect("/admin/tributes", error="Tribute+not+found+or+already+alive.")
        if t.killed_by_id:
            killer = await db.get(Tribute, t.killed_by_id)
            if killer:
                killer.kills = max(0, (killer.kills or 1) - 1)
        t.status = "ALIVE"
        t.death_cause = None
        t.placement = None
        t.killed_by_id = None
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Tribute revived", {"tribute": t.name}))
    return _redirect("/admin/tributes", msg=f"{t.name}+revived.")


@router.post("/tributes/{tribute_id}/victor")
async def tribute_victor(tribute_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t:
            return _redirect("/admin/tributes", error="Tribute+not+found.")
        t.status = "VICTOR"
        t.placement = 1
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Victor crowned", {"tribute": t.name}))
    return _redirect("/admin/tributes", msg=f"{t.name}+crowned+Victor!")


# ── Markets ────────────────────────────────────────────────────────────────────

@router.get("/markets")
async def market_list(
    request: Request,
    user: SessionUser = Depends(require_admin),
    status: str = "all",
    success: str = "",
    error: str = "",
):
    async with get_db() as db:
        q = select(Market).order_by(Market.created_at.desc())
        if status == "open":
            q = q.where(Market.status == "OPEN")
        elif status == "closed":
            q = q.where(Market.status == "CLOSED")
        elif status == "resolved":
            q = q.where(Market.status == "RESOLVED")
        markets_list = (await db.execute(q)).scalars().all()

        tribute_ids = {m.tribute_a_id for m in markets_list if m.tribute_a_id} | \
                      {m.tribute_b_id for m in markets_list if m.tribute_b_id}
        tributes_map: dict = {}
        if tribute_ids:
            rows = (await db.execute(select(Tribute).where(Tribute.id.in_(tribute_ids)))).scalars().all()
            tributes_map = {t.id: t for t in rows}

        bet_counts = dict((await db.execute(
            select(Bet.market_id, func.count(Bet.id)).where(Bet.status == "PENDING").group_by(Bet.market_id)
        )).all())

    return request.app.state.templates.TemplateResponse("admin/markets.html", {
        "request": request, "user": user,
        "markets": markets_list, "tributes_map": tributes_map,
        "bet_counts": bet_counts, "status": status,
        "success": success, "error": error,
    })


@router.get("/markets/new")
async def market_new_form(request: Request, user: SessionUser = Depends(require_admin), error: str = ""):
    async with get_db() as db:
        tributes = (await db.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(Tribute.district)
        )).scalars().all()
        phases = (await db.execute(select(BettingPhase).order_by(BettingPhase.sort_order))).scalars().all()
        alliances = (await db.execute(select(Alliance))).scalars().all()

    return request.app.state.templates.TemplateResponse("admin/market_form.html", {
        "request": request, "user": user,
        "market": None, "tributes": tributes, "phases": phases,
        "alliances": alliances, "market_types": MARKET_TYPES, "error": error,
    })


@router.post("/markets/new")
async def market_create(
    user: SessionUser = Depends(require_admin),
    market_type: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    tribute_a_id: Annotated[str, Form()] = "",
    tribute_b_id: Annotated[str, Form()] = "",
    placement_num: Annotated[str, Form()] = "",
    top_n: Annotated[str, Form()] = "",
    ou_line: Annotated[str, Form()] = "",
    ou_side: Annotated[str, Form()] = "",
    odds: Annotated[int, Form()] = -110,
    phase_id: Annotated[str, Form()] = "",
    cashout_allowed: Annotated[str, Form()] = "",
    cashout_rate: Annotated[str, Form()] = "",
    open_immediately: Annotated[str, Form()] = "",
):
    if not market_type or not label.strip():
        return _redirect("/admin/markets/new", error="Market+type+and+label+are+required.")
    async with get_db() as db:
        m = Market(
            type=market_type,
            label=label.strip(),
            tribute_a_id=int(tribute_a_id) if tribute_a_id.strip() else None,
            tribute_b_id=int(tribute_b_id) if tribute_b_id.strip() else None,
            placement_num=int(placement_num) if placement_num.strip() else None,
            top_n=int(top_n) if top_n.strip() else None,
            ou_line=float(ou_line) if ou_line.strip() else None,
            ou_side=ou_side.strip() or None,
            odds=odds,
            phase_id=int(phase_id) if phase_id.strip() else None,
            cashout_allowed=cashout_allowed == "on" or None,
            cashout_rate=float(cashout_rate) if cashout_rate.strip() else None,
            status="OPEN" if open_immediately == "on" else "CLOSED",
        )
        db.add(m)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market created", {"label": label.strip(), "type": market_type, "status": "OPEN" if open_immediately == "on" else "CLOSED"}))
    return _redirect("/admin/markets", msg="Market+created.")


@router.post("/markets/recalc")
async def market_recalc(user: SessionUser = Depends(require_admin)):
    from bot.cogs.admin import _recalculate_markets
    async with get_db() as db:
        await _recalculate_markets(db)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Odds recalculated"))
    return _redirect("/admin/markets", msg="Odds+recalculated.")


@router.post("/markets/bulk-close")
async def market_bulk_close(user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        markets = (await db.execute(select(Market).where(Market.status == "OPEN"))).scalars().all()
        for m in markets:
            m.status = "CLOSED"
        await db.commit()
        count = len(markets)
    asyncio.create_task(post_admin_action(user, "Bulk market close", {"markets closed": str(count)}))
    return _redirect("/admin/markets", msg=f"Closed+{count}+open+markets.")


@router.post("/markets/{market_id}/open")
async def market_open(market_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")
        m.status = "OPEN"
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market opened", {"market": m.label}))
    return _redirect("/admin/markets", msg="Market+opened.")


@router.post("/markets/{market_id}/close")
async def market_close(market_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")
        m.status = "CLOSED"
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market closed", {"market": m.label}))
    return _redirect("/admin/markets", msg="Market+closed.")


@router.post("/markets/{market_id}/reopen")
async def market_reopen(market_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")
        m.status = "CLOSED"
        m.result = None
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market reopened", {"market": m.label}))
    return _redirect("/admin/markets", msg="Market+reopened+as+Closed.")


@router.post("/markets/{market_id}/set-odds")
async def market_set_odds(
    market_id: int,
    user: SessionUser = Depends(require_admin),
    odds: Annotated[int, Form()] = -110,
):
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")
        m.odds = odds
        m.odds_override = True
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market odds set", {"market": m.label, "odds": f"{odds:+d}"}))
    return _redirect("/admin/markets", msg=f"Odds+set+to+{odds:+d}.")


@router.post("/markets/{market_id}/clear-override")
async def market_clear_override(market_id: int, user: SessionUser = Depends(require_admin)):
    from bot.cogs.admin import _recalculate_markets
    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")
        m.odds_override = False
        await _recalculate_markets(db)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Market override cleared", {"market": m.label}))
    return _redirect("/admin/markets", msg="Override+cleared+and+odds+recalculated.")


@router.post("/markets/{market_id}/resolve")
async def market_resolve(
    market_id: int,
    user: SessionUser = Depends(require_admin),
    result: Annotated[str, Form()] = "",
):
    # result: "true" | "false" | "void"
    bool_result: bool | None = None if result == "void" else (result == "true")

    async with get_db() as db:
        m = await db.get(Market, market_id)
        if not m:
            return _redirect("/admin/markets", error="Market+not+found.")

        m.status = "RESOLVED"
        m.result = bool_result

        bets = (await db.execute(
            select(Bet).where(Bet.market_id == market_id, Bet.status == "PENDING")
        )).scalars().all()

        for bet in bets:
            if bool_result is None:
                bet.status = "VOIDED"
                db_user = (await db.execute(select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id))).scalar_one_or_none()
                if db_user:
                    db_user.chips += bet.wager
            elif bool_result and bet.parlay_id is None:
                bet.status = "WON"
                db_user = (await db.execute(select(User).where(User.guild_id == bet.guild_id, User.discord_id == bet.user_id))).scalar_one_or_none()
                if db_user:
                    db_user.chips += bet.payout_if_win
                    db_user.total_won += bet.payout_if_win
            elif not bool_result and bet.parlay_id is None:
                bet.status = "LOST"
            elif bet.parlay_id is not None:
                bet.status = "WON" if bool_result else ("VOIDED" if bool_result is None else "LOST")
                await _settle_parlay(db, bet.parlay_id)

        await db.commit()

    label = "WON" if bool_result else ("VOIDED" if bool_result is None else "LOST")
    asyncio.create_task(post_admin_action(user, "Market resolved", {"market": m.label, "result": label, "bets settled": str(len(bets))}))
    return _redirect("/admin/markets", msg=f"Market+resolved+as+{label}.+{len(bets)}+bets+settled.")


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
            db_user = (await db.execute(select(User).where(User.guild_id == parlay.guild_id, User.discord_id == parlay.user_id))).scalar_one_or_none()
            if db_user:
                db_user.chips += parlay.total_payout
                db_user.total_won += parlay.total_payout
        elif all(l.status == "VOIDED" for l in legs):
            parlay.status = "WON"
            db_user = (await db.execute(select(User).where(User.guild_id == parlay.guild_id, User.discord_id == parlay.user_id))).scalar_one_or_none()
            if db_user:
                db_user.chips += parlay.total_wager


# ── Alliances ──────────────────────────────────────────────────────────────────

@router.get("/alliances")
async def alliances(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        alliances_list = (await db.execute(select(Alliance))).scalars().all()
        tributes_list = (await db.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(Tribute.district)
        )).scalars().all()
        all_tributes = (await db.execute(select(Tribute).order_by(Tribute.district))).scalars().all()

    return request.app.state.templates.TemplateResponse("admin/alliances.html", {
        "request": request, "user": user,
        "alliances": alliances_list, "tributes": tributes_list, "all_tributes": all_tributes,
        "success": success, "error": error,
    })


@router.post("/alliances/new")
async def alliance_create(
    user: SessionUser = Depends(require_admin),
    name: Annotated[str, Form()] = "",
):
    if not name.strip():
        return _redirect("/admin/alliances", error="Alliance+name+is+required.")
    async with get_db() as db:
        a = Alliance(name=name.strip())
        db.add(a)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Alliance created", {"name": name.strip()}))
    return _redirect("/admin/alliances", msg="Alliance+created.")


@router.post("/alliances/{alliance_id}/add-member")
async def alliance_add_member(
    alliance_id: int,
    user: SessionUser = Depends(require_admin),
    tribute_id: Annotated[int, Form()] = 0,
):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if not t:
            return _redirect("/admin/alliances", error="Tribute+not+found.")
        t.alliance_id = alliance_id
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Alliance member added", {"tribute": t.name}))
    return _redirect("/admin/alliances", msg="Member+added.")


@router.post("/alliances/{alliance_id}/remove-member/{tribute_id}")
async def alliance_remove_member(
    alliance_id: int,
    tribute_id: int,
    user: SessionUser = Depends(require_admin),
):
    async with get_db() as db:
        t = await db.get(Tribute, tribute_id)
        if t and t.alliance_id == alliance_id:
            t.alliance_id = None
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Alliance member removed", {"tribute": t.name if t else str(tribute_id)}))
    return _redirect("/admin/alliances", msg="Member+removed.")


@router.post("/alliances/{alliance_id}/delete")
async def alliance_delete(alliance_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        a = await db.get(Alliance, alliance_id)
        if a:
            await db.delete(a)
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Alliance deleted", {"alliance": a.name if a else str(alliance_id)}))
    return _redirect("/admin/alliances", msg="Alliance+deleted.")


# ── Chips ──────────────────────────────────────────────────────────────────────

@router.get("/chips")
async def chips(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        users = (await db.execute(select(User).where(User.guild_id == _GUILD_ID()).order_by(User.chips.desc()))).scalars().all()
    return request.app.state.templates.TemplateResponse("admin/chips.html", {
        "request": request, "user": user, "users": users,
        "success": success, "error": error,
    })


@router.post("/chips/give")
async def chips_give(
    user: SessionUser = Depends(require_admin),
    discord_id: Annotated[str, Form()] = "",
    amount: Annotated[int, Form()] = 0,
    reason: Annotated[str, Form()] = "",
):
    if amount <= 0:
        return _redirect("/admin/chips", error="Amount+must+be+positive.")
    async with get_db() as db:
        try:
            uid = int(discord_id.strip())
        except ValueError:
            return _redirect("/admin/chips", error="Invalid+Discord+ID.")
        db_user = (await db.execute(select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == uid))).scalar_one_or_none()
        if not db_user:
            return _redirect("/admin/chips", error="User+not+found+in+database.")
        db_user.chips += amount
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Chips given", {"user": db_user.username, "amount": f"{amount:,}", "reason": reason or "none"}))
    return _redirect("/admin/chips", msg=f"Gave+{amount:,}+chips+to+{db_user.username}.")


@router.post("/chips/take")
async def chips_take(
    user: SessionUser = Depends(require_admin),
    discord_id: Annotated[str, Form()] = "",
    amount: Annotated[int, Form()] = 0,
):
    if amount <= 0:
        return _redirect("/admin/chips", error="Amount+must+be+positive.")
    async with get_db() as db:
        try:
            uid = int(discord_id.strip())
        except ValueError:
            return _redirect("/admin/chips", error="Invalid+Discord+ID.")
        db_user = (await db.execute(select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == uid))).scalar_one_or_none()
        if not db_user:
            return _redirect("/admin/chips", error="User+not+found.")
        db_user.chips = max(0, db_user.chips - amount)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Chips taken", {"user": db_user.username, "amount": f"{amount:,}"}))
    return _redirect("/admin/chips", msg=f"Took+{amount:,}+chips+from+{db_user.username}.")


@router.post("/chips/give-all")
async def chips_give_all(
    user: SessionUser = Depends(require_admin),
    amount: Annotated[int, Form()] = 0,
):
    if amount <= 0:
        return _redirect("/admin/chips", error="Amount+must+be+positive.")
    if amount > _MAX_GIVE_ALL:
        return _redirect("/admin/chips", error=f"Amount+exceeds+the+{_MAX_GIVE_ALL:,}+chip+per-grant+cap.")
    async with get_db() as db:
        users = (await db.execute(select(User).where(User.guild_id == _GUILD_ID()))).scalars().all()
        for u in users:
            u.chips += amount
        await db.commit()
        count = len(users)
    asyncio.create_task(post_admin_action(user, "Chips given to all", {"amount": f"{amount:,}", "players": str(count)}))
    return _redirect("/admin/chips", msg=f"Gave+{amount:,}+chips+to+{count}+players.")


@router.post("/chips/set")
async def chips_set(
    user: SessionUser = Depends(require_admin),
    discord_id: Annotated[str, Form()] = "",
    amount: Annotated[int, Form()] = 0,
):
    if amount < 0:
        return _redirect("/admin/chips", error="Amount+cannot+be+negative.")
    async with get_db() as db:
        try:
            uid = int(discord_id.strip())
        except ValueError:
            return _redirect("/admin/chips", error="Invalid+Discord+ID.")
        db_user = (await db.execute(select(User).where(User.guild_id == _GUILD_ID(), User.discord_id == uid))).scalar_one_or_none()
        if not db_user:
            return _redirect("/admin/chips", error="User+not+found.")
        db_user.chips = amount
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Chips set", {"user": db_user.username, "amount": f"{amount:,}"}))
    return _redirect("/admin/chips", msg=f"Set+{db_user.username}+balance+to+{amount:,}.")


# ── Settings ───────────────────────────────────────────────────────────────────

@router.get("/settings")
async def settings(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        rows = (await db.execute(text("SELECT key, value FROM game_settings"))).all()
        settings_map = {r[0]: r[1] for r in rows}
        theme = settings_map.get("active_theme", "dark")
        announcement = settings_map.get("capitol_announcement", "")
        cashout_allowed = settings_map.get("cashout_allowed", "false")
        cashout_rate = settings_map.get("cashout_rate", "0.65")
        default_chips_setting = settings_map.get("default_chips", str(1000))
        deposit_rate = settings_map.get("deposit_rate", "1.0")
        withdraw_rate = settings_map.get("withdraw_rate", "1.0")

    return request.app.state.templates.TemplateResponse("admin/settings.html", {
        "request": request, "user": user,
        "theme": theme,
        "announcement": announcement,
        "cashout_allowed": cashout_allowed,
        "cashout_rate": cashout_rate,
        "default_chips": default_chips_setting,
        "deposit_rate": deposit_rate,
        "withdraw_rate": withdraw_rate,
        "success": success, "error": error,
    })


@router.post("/settings")
async def settings_save(
    user: SessionUser = Depends(require_admin),
    active_theme: Annotated[str, Form()] = "dark",
    cashout_allowed: Annotated[str, Form()] = "",
    cashout_rate: Annotated[str, Form()] = "0.65",
    default_chips: Annotated[str, Form()] = "1000",
    deposit_rate: Annotated[str, Form()] = "1.0",
    withdraw_rate: Annotated[str, Form()] = "1.0",
    capitol_announcement: Annotated[str, Form()] = "",
):
    async with get_db() as db:
        async def upsert(key: str, value: str) -> None:
            await db.execute(text(f"INSERT OR REPLACE INTO game_settings (key, value) VALUES (:k, :v)"), {"k": key, "v": value})

        await upsert("active_theme", active_theme)
        await upsert("cashout_allowed", "true" if cashout_allowed == "on" else "false")
        await upsert("cashout_rate", cashout_rate)
        await upsert("default_chips", default_chips)
        await upsert("deposit_rate", deposit_rate)
        await upsert("withdraw_rate", withdraw_rate)
        if capitol_announcement:
            import json
            await upsert("capitol_announcement", json.dumps(capitol_announcement))
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Settings saved", {"theme": active_theme, "cashout": "on" if cashout_allowed == "on" else "off"}))
    return _redirect("/admin/settings", msg="Settings+saved.")


# ── Restrictions (rate overrides + public-parlay blocks) ───────────────────────

@router.get("/restrictions")
async def restrictions(request: Request, user: SessionUser = Depends(require_admin), success: str = "", error: str = ""):
    async with get_db() as db:
        overrides = await list_overrides(db, _GUILD_ID())
        blocks = await list_public_blocks(db, _GUILD_ID())

    return request.app.state.templates.TemplateResponse("admin/restrictions.html", {
        "request": request, "user": user,
        "overrides": overrides, "blocks": blocks,
        "success": success, "error": error,
    })


@router.post("/restrictions/rates/set")
async def restrictions_rate_set(
    user: SessionUser = Depends(require_admin),
    scope: Annotated[str, Form()] = "USER",
    target_id: Annotated[str, Form()] = "",
    direction: Annotated[str, Form()] = "DEPOSIT",
    rate: Annotated[float, Form()] = 1.0,
):
    if scope not in ("ROLE", "USER") or direction not in ("DEPOSIT", "WITHDRAW"):
        return _redirect("/admin/restrictions", error="Invalid+scope+or+direction.")
    try:
        tid = int(target_id)
    except ValueError:
        return _redirect("/admin/restrictions", error="Target+ID+must+be+a+number.")
    if rate <= 0:
        return _redirect("/admin/restrictions", error="Rate+must+be+positive.")
    async with get_db() as db:
        await set_override(db, _GUILD_ID(), scope, tid, direction, rate)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Exchange rate override set", {
        "scope": scope, "target_id": str(tid), "direction": direction, "rate": str(rate),
    }))
    return _redirect("/admin/restrictions", msg="Rate+override+saved.")


@router.post("/restrictions/rates/{override_id}/delete")
async def restrictions_rate_delete(override_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        row = await db.get(ExchangeRateOverride, override_id)
        if row:
            await db.delete(row)
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Exchange rate override removed", {"id": str(override_id)}))
    return _redirect("/admin/restrictions", msg="Rate+override+removed.")


@router.post("/restrictions/public-block/set")
async def restrictions_public_block_set(
    user: SessionUser = Depends(require_admin),
    scope: Annotated[str, Form()] = "USER",
    target_id: Annotated[str, Form()] = "",
):
    if scope not in ("ROLE", "USER"):
        return _redirect("/admin/restrictions", error="Invalid+scope.")
    try:
        tid = int(target_id)
    except ValueError:
        return _redirect("/admin/restrictions", error="Target+ID+must+be+a+number.")
    async with get_db() as db:
        await set_public_block(db, _GUILD_ID(), scope, tid, True)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Public-parlay block added", {"scope": scope, "target_id": str(tid)}))
    return _redirect("/admin/restrictions", msg="Public-parlay+block+added.")


@router.post("/restrictions/public-block/{block_id}/delete")
async def restrictions_public_block_delete(block_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        row = await db.get(PublicBetRestriction, block_id)
        if row:
            await db.delete(row)
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Public-parlay block removed", {"id": str(block_id)}))
    return _redirect("/admin/restrictions", msg="Public-parlay+block+removed.")


# ── Parlay Templates ───────────────────────────────────────────────────────────

@router.get("/parlays")
async def parlay_templates(
    request: Request,
    user: SessionUser = Depends(require_admin),
    success: str = "",
    error: str = "",
):
    async with get_db() as db:
        templates_raw = (await db.execute(
            select(ParlayTemplate).order_by(ParlayTemplate.created_at.desc())
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

    return request.app.state.templates.TemplateResponse("admin/parlays.html", {
        "request": request, "user": user,
        "templates": templates_raw, "tpl_legs": tpl_legs, "tpl_markets": tpl_markets,
        "success": success, "error": error,
    })


@router.post("/parlays/new")
async def parlay_template_create(
    user: SessionUser = Depends(require_admin),
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    active: Annotated[str, Form()] = "",
):
    if not name.strip():
        return _redirect("/admin/parlays", error="Name+is+required.")
    async with get_db() as db:
        tpl = ParlayTemplate(
            name=name.strip(),
            description=description.strip() or None,
            source="ADMIN",
            active=active == "on",
        )
        db.add(tpl)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Parlay template created", {"name": name.strip()}))
    return _redirect("/admin/parlays", msg="Template+created.")


@router.post("/parlays/{tpl_id}/add-leg")
async def parlay_template_add_leg(
    tpl_id: int,
    user: SessionUser = Depends(require_admin),
    market_id: Annotated[int, Form()] = 0,
):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if not tpl:
            return _redirect("/admin/parlays", error="Template+not+found.")
        leg = ParlayTemplateLeg(template_id=tpl_id, market_id=market_id, sort_order=0)
        db.add(leg)
        await db.commit()
    asyncio.create_task(post_admin_action(user, "Parlay template leg added", {"template": tpl.name, "market_id": str(market_id)}))
    return _redirect("/admin/parlays", msg="Leg+added.")


@router.post("/parlays/{tpl_id}/toggle")
async def parlay_template_toggle(tpl_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if tpl:
            tpl.active = not tpl.active
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Parlay template toggled", {"template": tpl.name if tpl else str(tpl_id), "active": str(tpl.active) if tpl else "unknown"}))
    return _redirect("/admin/parlays")


@router.post("/parlays/{tpl_id}/delete")
async def parlay_template_delete(tpl_id: int, user: SessionUser = Depends(require_admin)):
    async with get_db() as db:
        tpl = await db.get(ParlayTemplate, tpl_id)
        if tpl:
            await db.delete(tpl)
            await db.commit()
    asyncio.create_task(post_admin_action(user, "Parlay template deleted", {"template": tpl.name if tpl else str(tpl_id)}))
    return _redirect("/admin/parlays", msg="Template+deleted.")

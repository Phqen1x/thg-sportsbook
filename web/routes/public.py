from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from typing import Annotated
from sqlalchemy import func, select, text

from bot.odds.calculator import combined_american

from bot.cogs.display import LEADERBOARD_CATEGORIES, _leaderboard_rows
from bot.database.models import Alliance, Bet, BettingPhase, DistrictRecord, Market, Parlay, ParlayTemplate, ParlayTemplateLeg, Tribute, User
from web import discord_api as _discord_api
from web.database import available_guilds, get_db, get_request_guild
from web.deps import optional_user, require_user
from web.session import SessionUser, set_session
router = APIRouter(tags=["public"])


def _parlay_flavor(
    markets: list,
    tributes_map: dict,
    alliance_names: dict,
    district_records: dict,
    fallback_name: str,
    fallback_desc: str | None,
) -> tuple[str, str | None]:
    """Generate a fun, data-driven title + description for a featured parlay."""
    tid_set: set[int] = set()
    for m in markets:
        if m.tribute_a_id:
            tid_set.add(m.tribute_a_id)
        if m.tribute_b_id:
            tid_set.add(m.tribute_b_id)

    tributes = [tributes_map[tid] for tid in tid_set if tid in tributes_map]
    if not tributes:
        return (fallback_name, fallback_desc)

    def first(t) -> str:
        return t.name.split()[0]

    districts = sorted({t.district for t in tributes})
    drecs = {d: district_records[d] for d in districts if d in district_records}

    # Detect a central tribute: one tribute that appears in the majority of legs.
    # This fires for tribute-centric parlays where one competitor anchors every market.
    tribute_freq: dict[int, int] = {}
    for m in markets:
        if m.tribute_a_id:
            tribute_freq[m.tribute_a_id] = tribute_freq.get(m.tribute_a_id, 0) + 1
        if m.tribute_b_id:
            tribute_freq[m.tribute_b_id] = tribute_freq.get(m.tribute_b_id, 0) + 1
    central_tid: int | None = None
    if tribute_freq:
        max_freq = max(tribute_freq.values())
        dominant_tids = [tid for tid, cnt in tribute_freq.items() if cnt == max_freq]
        if (
            len(dominant_tids) == 1
            and max_freq >= max(2, len(markets) // 2)
            and dominant_tids[0] in tributes_map
        ):
            central_tid = dominant_tids[0]

    if central_tid is not None:
        t = tributes_map[central_tid]
        fname = first(t)
        dr = drecs.get(t.district)
        parts: list[str] = []
        if t.sade_champion:
            parts.append(
                f"{fname} has already worn the victor's crown — SADE experience the odds can't capture"
            )
        if t.times_played and t.times_played >= 2:
            parts.append(f"{fname} has {t.times_played} prior games of experience")
        elif t.times_played == 1:
            parts.append(f"{fname} isn't new to this — one prior game under their belt")
        if t.training_score and t.training_score >= 9:
            parts.append(f"a training score of {t.training_score} puts them in elite territory")
        elif t.training_score:
            parts.append(f"training score: {t.training_score}")
        if t.kills and t.kills >= 3:
            parts.append(
                f"{t.kills} kills already makes {fname} one of the most dangerous in the arena"
            )
        elif t.kills:
            parts.append(f"{t.kills} {'kill' if t.kills == 1 else 'kills'} this game")
        if t.alliance_id and t.alliance_id in alliance_names:
            parts.append(f"running with {alliance_names[t.alliance_id]}")
        if dr and dr.wins:
            parts.append(
                f"District {t.district} has {dr.wins} all-time "
                f"{'victory' if dr.wins == 1 else 'victories'}"
            )
        desc = ". ".join(parts).capitalize() + "." if parts else f"Everything riding on {fname}."
        if t.sade_champion:
            name = f"{fname}: Double Victor"
        elif t.times_played and t.times_played >= 2:
            name = f"{fname}: Veteran's Slate"
        elif t.kills and t.kills >= 3:
            name = f"{fname}: Blood Trail"
        elif t.training_score and t.training_score >= 9:
            name = f"{fname}: Top of the Class"
        else:
            name = f"All In on {fname}"
        return (name, desc)

    same_district = len(districts) == 1
    aid_set = {t.alliance_id for t in tributes if t.alliance_id}
    allied = len(aid_set) == 1 and len(tributes) >= 2 and all(t.alliance_id == next(iter(aid_set)) for t in tributes)
    veterans = [t for t in tributes if t.times_played and t.times_played > 0]
    rookies = [t for t in tributes if not t.times_played]
    killers = [t for t in tributes if t.kills and t.kills > 0]
    sade_champs = [t for t in tributes if t.sade_champion]
    high_scorers = [t for t in tributes if t.training_score and t.training_score >= 9]
    dominant = [d for d, dr in drecs.items() if (dr.wins or 0) >= 3]

    if allied:
        aname = alliance_names.get(next(iter(aid_set)), "The Alliance")
        names_str = " & ".join(first(t) for t in tributes[:3])
        return (
            f"{aname} Solidarity",
            f"{names_str} march under the same banner. When an alliance sticks together in the arena, the odds have a way of falling right.",
        )

    if same_district:
        d = districts[0]
        dr = drecs.get(d)
        wins_clause = f" {dr.wins} all-time {'victory' if dr.wins == 1 else 'victories'} and" if dr and dr.wins else ""
        return (
            f"District {d} Double Down",
            f"District {d} has{wins_clause} something to prove. Every leg in this parlay rides on District {d} — trust the district.",
        )

    if len(killers) == len(tributes) and killers:
        kill_total = sum(t.kills for t in tributes)
        return (
            "Blood on Their Hands",
            f"Every tribute in this parlay has drawn blood — {kill_total} {'kill' if kill_total == 1 else 'kills'} between them. The dangerous ones tend to stick around.",
        )

    if sade_champs:
        champ_names = " & ".join(first(t) for t in sade_champs)
        plural_verb = "has" if len(sade_champs) == 1 else "have"
        return (
            "Champions Don't Quit",
            f"{champ_names} {plural_verb} already worn the victor's crown. SADE experience is the kind of edge that doesn't show up in the odds — but should.",
        )

    if len(veterans) >= 2 and not rookies:
        vet_parts = [f"{first(t)} ({t.times_played}×)" for t in veterans[:2]]
        return (
            "Seasoned Slate",
            f"Experience is everything: {', '.join(vet_parts)}. These tributes know the arena better than most — and they've got the scars to prove it.",
        )

    if len(high_scorers) == len(tributes) and high_scorers:
        avg = sum(t.training_score for t in tributes) // len(tributes)
        return (
            "Top of the Class",
            f"Average training score: {avg}. These tributes walked into the arena already impressive. High scores don't guarantee wins — but they're not nothing.",
        )

    if dominant:
        d_win_parts = [f"District {d} ({drecs[d].wins or 0} wins)" for d in dominant[:2]]
        return (
            "Proven Blood",
            f"{' and '.join(d_win_parts)} in the history books. Count on these districts to get it done!",
        )

    if len(veterans) > 0 and len(rookies) > 0:
        vet = veterans[0]
        rook = rookies[0]
        return (
            "Old Guard, New Blood",
            f"{first(vet)} brings {vet.times_played} prior {'game' if vet.times_played == 1 else 'games'} of experience while {first(rook)} is making their debut. Old instincts, fresh hunger — a compelling combination.",
        )

    if len(districts) >= 3:
        d_str = " · ".join(f"D{d}" for d in districts[:4])
        return (
            "Cross-District Sweep",
            f"A wide net: {d_str}. Multiple districts in one slip means more ways to win — and more ways to feel the tension.",
        )

    if len(districts) == 2:
        d1, d2 = districts
        dr1, dr2 = drecs.get(d1), drecs.get(d2)
        w1 = dr1.wins or 0 if dr1 else 0
        w2 = dr2.wins or 0 if dr2 else 0
        if w1 != w2:
            better, worse = (d1, d2) if w1 > w2 else (d2, d1)
            line = f"District {better} leads the historical win count, but District {worse} is hungry for theirs."
        elif w1 > 0:
            line = f"Districts {d1} and {d2} each have {w1} all-time {'win' if w1 == 1 else 'wins'} — evenly matched on paper."
        else:
            line = f"Districts {d1} and {d2} in one slip."
        return (
            f"District {d1} × District {d2}",
            f"{line} Back them both and hold on.",
        )

    t = tributes[0]
    dr = drecs.get(t.district)
    details = []
    if t.times_played:
        details.append(f"{first(t)} has {t.times_played} prior {'game' if t.times_played == 1 else 'games'} of experience")
    if t.training_score:
        details.append(f"a training score of {t.training_score}")
    if dr and dr.wins:
        details.append(f"District {t.district} has {dr.wins} all-time {'victory' if dr.wins == 1 else 'victories'}")
    desc = ". ".join(details) + "." if details else f"Everything riding on {first(t)}."
    return (f"All In on {first(t)}", desc)


async def _phase_name(db) -> str | None:
    row = (await db.execute(text("SELECT value FROM game_settings WHERE key='current_phase_id'"))).fetchone()
    if not row:
        return None
    phase_id = json.loads(row[0]) if row[0] else None
    if phase_id is None:
        return None
    phase = await db.get(BettingPhase, phase_id)
    return phase.name if phase else None


@router.get("/")
async def home(request: Request, user: SessionUser = Depends(require_user)):
    async with get_db() as db:
        tributes = (await db.execute(
            select(Tribute).where(Tribute.status == "ALIVE").order_by(Tribute.district)
        )).scalars().all()

        win_markets_raw = (await db.execute(
            select(Market).where(Market.type == "TRIBUTE_WINS", Market.status == "OPEN")
        )).scalars().all()
        win_markets = {m.tribute_a_id: m for m in win_markets_raw}

        open_markets = (await db.execute(
            select(Market).where(Market.status == "OPEN").order_by(Market.created_at.desc())
        )).scalars().all()

        bet_counts_raw = (await db.execute(
            select(Bet.market_id, func.count(Bet.id)).group_by(Bet.market_id)
        )).all()
        bet_counts = {row[0]: row[1] for row in bet_counts_raw}

        tribute_ids = {m.tribute_a_id for m in open_markets if m.tribute_a_id} | \
                      {m.tribute_b_id for m in open_markets if m.tribute_b_id}
        tributes_map: dict = {}
        if tribute_ids:
            t_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(tribute_ids))
            )).scalars().all()
            tributes_map = {t.id: t for t in t_rows}

        leaderboard = (await db.execute(
            select(User)
            .where(User.guild_id == get_request_guild())
            .order_by(User.chips.desc())
            .limit(10)
        )).scalars().all()

        open_count = len(open_markets)
        alive_count = (await db.execute(
            select(func.count(Tribute.id)).where(Tribute.status == "ALIVE")
        )).scalar() or 0

        phase_name = await _phase_name(db)

        # Banners
        banner_row = (await db.execute(
            text("SELECT value FROM game_settings WHERE key='activity_banners'")
        )).fetchone()
        banners = json.loads(banner_row[0]) if banner_row else []

        # Featured parlay templates
        tpl_raw = (await db.execute(
            select(ParlayTemplate).where(ParlayTemplate.active == True)
            .order_by(ParlayTemplate.created_at.desc()).limit(6)
        )).scalars().all()
        featured_parlays = []
        for tpl in tpl_raw:
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
            odds_list = [m.odds for m in leg_markets if m.status == "OPEN"]
            combined = combined_american(odds_list) if len(odds_list) >= 2 else None
            featured_parlays.append({
                "id": tpl.id, "name": tpl.name, "description": tpl.description,
                "difficulty": tpl.difficulty, "combined_odds": combined,
                "legs": leg_markets,
            })

        # Load tribute + district history for dynamic parlay flavor text
        parlay_tid_set: set[int] = set()
        for fp in featured_parlays:
            for mkt in fp["legs"]:
                if mkt.tribute_a_id:
                    parlay_tid_set.add(mkt.tribute_a_id)
                if mkt.tribute_b_id:
                    parlay_tid_set.add(mkt.tribute_b_id)

        parlay_tributes_map: dict = {}
        parlay_alliance_names: dict = {}
        parlay_district_records: dict = {}
        if parlay_tid_set:
            pt_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(parlay_tid_set))
            )).scalars().all()
            parlay_tributes_map = {t.id: t for t in pt_rows}

            aid_set = {t.alliance_id for t in pt_rows if t.alliance_id}
            if aid_set:
                a_rows = (await db.execute(
                    select(Alliance).where(Alliance.id.in_(aid_set))
                )).scalars().all()
                parlay_alliance_names = {a.id: a.name for a in a_rows}

            parlay_districts = {t.district for t in pt_rows}
            dr_rows = (await db.execute(
                select(DistrictRecord).where(DistrictRecord.district.in_(parlay_districts))
            )).scalars().all()
            parlay_district_records = {dr.district: dr for dr in dr_rows}

        for fp in featured_parlays:
            name, desc = _parlay_flavor(
                fp["legs"], parlay_tributes_map, parlay_alliance_names,
                parlay_district_records, fp["name"], fp["description"],
            )
            fp["name"] = name
            fp["description"] = desc

    def fmt_odds(n):
        return f"+{n}" if n >= 0 else str(n)

    return request.app.state.templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "tributes": tributes,
        "win_markets": win_markets,
        "open_markets": open_markets,
        "bet_counts": bet_counts,
        "tributes_map": tributes_map,
        "leaderboard": leaderboard,
        "open_count": open_count,
        "alive_count": alive_count,
        "phase_name": phase_name,
        "banners": banners,
        "featured_parlays": featured_parlays,
        "fmt_odds": fmt_odds,
    })


@router.get("/tributes")
async def tributes(request: Request, user: SessionUser = Depends(require_user)):
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
    user: SessionUser = Depends(require_user),
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

        # Featured parlay templates (pinned above market list)
        tpl_raw = (await db.execute(
            select(ParlayTemplate).where(ParlayTemplate.active == True)
            .order_by(ParlayTemplate.created_at.desc()).limit(6)
        )).scalars().all()
        featured_parlays = []
        for tpl in tpl_raw:
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
            odds_list = [m.odds for m in leg_markets if m.status == "OPEN"]
            combined = combined_american(odds_list) if len(odds_list) >= 2 else None
            featured_parlays.append({
                "id": tpl.id, "name": tpl.name, "description": tpl.description,
                "difficulty": tpl.difficulty, "combined_odds": combined,
                "legs": leg_markets,
            })

        fp_tid_set: set[int] = set()
        for fp in featured_parlays:
            for mkt in fp["legs"]:
                if mkt.tribute_a_id: fp_tid_set.add(mkt.tribute_a_id)
                if mkt.tribute_b_id: fp_tid_set.add(mkt.tribute_b_id)
        fp_tributes_map: dict = {}
        fp_alliance_names: dict = {}
        fp_district_records: dict = {}
        if fp_tid_set:
            pt_rows = (await db.execute(
                select(Tribute).where(Tribute.id.in_(fp_tid_set))
            )).scalars().all()
            fp_tributes_map = {t.id: t for t in pt_rows}
            aid_set = {t.alliance_id for t in pt_rows if t.alliance_id}
            if aid_set:
                a_rows = (await db.execute(
                    select(Alliance).where(Alliance.id.in_(aid_set))
                )).scalars().all()
                fp_alliance_names = {a.id: a.name for a in a_rows}
            fp_districts = {t.district for t in pt_rows}
            dr_rows = (await db.execute(
                select(DistrictRecord).where(DistrictRecord.district.in_(fp_districts))
            )).scalars().all()
            fp_district_records = {dr.district: dr for dr in dr_rows}
        for fp in featured_parlays:
            name, desc = _parlay_flavor(
                fp["legs"], fp_tributes_map, fp_alliance_names,
                fp_district_records, fp["name"], fp["description"],
            )
            fp["name"] = name
            fp["description"] = desc

    return request.app.state.templates.TemplateResponse("markets.html", {
        "request": request,
        "user": user,
        "markets": markets_list,
        "tributes_map": tributes_map,
        "bet_counts": bet_counts,
        "status": status,
        "type_filter": type_filter,
        "phase_name": phase_name,
        "featured_parlays": featured_parlays,
    })


@router.get("/leaderboard")
async def leaderboard(
    request: Request,
    user: SessionUser = Depends(require_user),
    category: str = "CHIPS",
):
    if category not in dict(LEADERBOARD_CATEGORIES):
        category = "CHIPS"
    gid = get_request_guild()

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

    rows_out = [
        {"rank": i + 1, "username": usernames.get(uid, "Member"), "value": value}
        for i, (uid, value) in enumerate(rows)
    ]

    return request.app.state.templates.TemplateResponse("leaderboard.html", {
        "request": request,
        "user": user,
        "rows": rows_out,
        "category": category,
        "title": title,
        "value_kind": kind,
        "categories": LEADERBOARD_CATEGORIES,
    })


@router.get("/select-server")
async def select_server(request: Request, user: SessionUser | None = Depends(optional_user)):
    if user is None:
        return RedirectResponse("/auth/login")
    guild_ids = available_guilds()
    if len(guild_ids) == 1:
        # Only one guild — auto-select and redirect.
        user.guild_id = guild_ids[0]
        resp = RedirectResponse("/")
        set_session(resp, user)
        return resp
    # Fetch guild names in parallel.
    import asyncio
    names = await asyncio.gather(*[_discord_api.get_guild_name(gid) for gid in guild_ids])
    guilds = [{"id": gid, "name": name or str(gid)} for gid, name in zip(guild_ids, names)]
    return request.app.state.templates.TemplateResponse("select_server.html", {
        "request": request,
        "user": user,
        "guilds": guilds,
    })


@router.post("/select-server")
async def select_server_post(
    request: Request,
    guild_id: Annotated[int, Form()],
    user: SessionUser | None = Depends(optional_user),
):
    if user is None:
        return RedirectResponse("/auth/login")
    if guild_id not in available_guilds():
        return RedirectResponse("/select-server?error=Invalid+server+selection.", status_code=303)
    user.guild_id = guild_id
    resp = RedirectResponse("/", status_code=303)
    set_session(resp, user)
    return resp

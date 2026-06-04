#!/usr/bin/env python3
"""Build the THG Sportsbook Update & Reference Guide PDF."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

GOLD = colors.HexColor("#C9A84C")
DARK = colors.HexColor("#1A1A1A")
DARKER = colors.HexColor("#111111")
LIGHT_GRAY = colors.HexColor("#CCCCCC")
MID_GRAY = colors.HexColor("#888888")
DIM_GRAY = colors.HexColor("#555555")
WHITE = colors.white
GREEN = colors.HexColor("#4CAF50")
RED = colors.HexColor("#E53935")


def styles():
    title = ParagraphStyle(
        "DocTitle",
        fontName="Helvetica-Bold",
        fontSize=28,
        leading=34,
        textColor=GOLD,
        spaceAfter=4,
        alignment=TA_CENTER,
    )
    subtitle = ParagraphStyle(
        "DocSubtitle",
        fontName="Helvetica-Oblique",
        fontSize=12,
        leading=16,
        textColor=LIGHT_GRAY,
        spaceAfter=6,
        alignment=TA_CENTER,
    )
    version = ParagraphStyle(
        "Version",
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=MID_GRAY,
        spaceAfter=20,
        alignment=TA_CENTER,
    )
    h2 = ParagraphStyle(
        "H2",
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=GOLD,
        spaceBefore=20,
        spaceAfter=4,
    )
    h3 = ParagraphStyle(
        "H3",
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=WHITE,
        spaceBefore=12,
        spaceAfter=3,
    )
    h4 = ParagraphStyle(
        "H4",
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=GOLD,
        spaceBefore=8,
        spaceAfter=2,
    )
    body = ParagraphStyle(
        "Body",
        fontName="Helvetica",
        fontSize=10,
        leading=15,
        textColor=LIGHT_GRAY,
        spaceAfter=5,
    )
    bullet = ParagraphStyle(
        "Bullet",
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=LIGHT_GRAY,
        leftIndent=0,
        spaceAfter=2,
    )
    note = ParagraphStyle(
        "Note",
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=13,
        textColor=MID_GRAY,
        spaceAfter=4,
    )
    code = ParagraphStyle(
        "Code",
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=13,
        textColor=GOLD,
        spaceAfter=2,
    )
    return {
        "title": title, "subtitle": subtitle, "version": version,
        "h2": h2, "h3": h3, "h4": h4,
        "body": body, "bullet": bullet, "note": note, "code": code,
    }


S = styles()

W = LETTER[0] - 1.7 * inch  # usable text width


def hr(color=GOLD, thickness=0.5, space_before=4, space_after=4):
    return HRFlowable(
        width="100%", thickness=thickness, color=color,
        spaceBefore=space_before, spaceAfter=space_after,
    )


def gap(h=6):
    return Spacer(1, h)


def h2(text):
    return [Paragraph(text, S["h2"]), hr()]


def h3(text):
    return [Paragraph(text, S["h3"])]


def h4(text):
    return [Paragraph(text, S["h4"])]


def body(text):
    return Paragraph(text, S["body"])


def note(text):
    return Paragraph(text, S["note"])


def bullets(items):
    li = [
        ListItem(
            Paragraph(b, S["bullet"]),
            bulletColor=GOLD,
            bulletFontName="Helvetica-Bold",
        )
        for b in items
    ]
    return ListFlowable(li, bulletType="bullet", leftIndent=16, bulletFontSize=7, spaceAfter=4)


def cmd_table(rows, col_widths=None):
    """Two-column table for command / description pairs."""
    if col_widths is None:
        col_widths = [W * 0.38, W * 0.62]
    data = [
        [
            Paragraph(f"<font name='Helvetica-Bold' color='#C9A84C'>{cmd}</font>", S["bullet"]),
            Paragraph(desc, S["bullet"]),
        ]
        for cmd, desc in rows
    ]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DARKER),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [DARKER, colors.HexColor("#161616")]),
        ("GRID", (0, 0), (-1, -1), 0.25, DIM_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def factor_table(rows):
    """Three-column table for odds factor explanations."""
    col_widths = [W * 0.22, W * 0.22, W * 0.56]
    header = [
        Paragraph("<b>Factor</b>", S["bullet"]),
        Paragraph("<b>Strength</b>", S["bullet"]),
        Paragraph("<b>What it does</b>", S["bullet"]),
    ]
    data = [header] + [
        [Paragraph(f"<font name='Helvetica-Bold' color='#C9A84C'>{a}</font>", S["bullet"]),
         Paragraph(b, S["bullet"]),
         Paragraph(c, S["bullet"])]
        for a, b, c in rows
    ]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A2A2A")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARKER, colors.HexColor("#161616")]),
        ("GRID", (0, 0), (-1, -1), 0.25, DIM_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
    ]))
    return t


# ── Document content ──────────────────────────────────────────────────────────

def build_story():
    story = []

    # ── Cover ──────────────────────────────────────────────────────────────────
    story += [
        gap(30),
        Paragraph("THG SPORTSBOOK", S["title"]),
        Paragraph("Bot Update &amp; Reference Guide", S["subtitle"]),
        Paragraph("Covers all changes since initial launch · June 2026", S["version"]),
        hr(thickness=1.0, space_before=8, space_after=8),
        gap(6),
        body(
            "This document is the authoritative reference for the THG Sportsbook Discord bot. "
            "It covers every command available to players and administrators, explains how the "
            "odds engine works in plain language, and details every change made since the initial "
            "commit. Whether you are a bettor trying to understand the board or an admin managing "
            "a game, this guide has you covered."
        ),
        gap(10),
    ]

    # ── Table of Contents ──────────────────────────────────────────────────────
    story += h2("Table of Contents")
    story.append(bullets([
        "Section 1 — What Changed Since Launch",
        "Section 2 — Player Commands",
        "Section 3 — Admin Commands",
        "Section 4 — How Odds Are Calculated",
        "Section 5 — Market Types Reference",
    ]))
    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — WHAT CHANGED
    # ══════════════════════════════════════════════════════════════════════════
    story += h2("Section 1 — What Changed Since Launch")
    story.append(body(
        "The bot shipped with a solid foundation: tributes, markets, straight bets, parlays, "
        "and an image-based odds board. Three subsequent updates expanded that foundation "
        "substantially. Here is a plain-English summary of every addition and improvement."
    ))

    # 1.1 New market types
    story += h3("1.1  Eleven New Market Types")
    story.append(body(
        "The original bot supported 11 market types. The roster has grown to 22, adding a full "
        "set of milestone markets, Pre-Games prop markets, and arena-type betting."
    ))
    story.append(cmd_table([
        ("Makes Final 8 / Misses Final 8",    "Will this tribute be alive when the field reaches 8? Auto-resolves when the Final 8 phase begins."),
        ("Makes Final 5 / Misses Final 5",    "Same concept for the Final 5 phase."),
        ("Makes the Finale / Misses Finale",  "Same concept for the Finale (top 3) phase."),
        ("Arena Type",                         "Pre-Games prop: will the arena be natural or man-made? Opens before the Games start."),
        ("Exact Training Score",               "Pre-Games prop: guess a tribute's exact score (1–12). Resolves the moment scores are revealed."),
        ("Combined District Score",            "Pre-Games prop: guess the sum of both tributes' scores for a district. Resolves when both scores are known."),
        ("Training Score Over/Under",          "Pre-Games prop: will a tribute score over or under 6.5? Resolves when the score is revealed."),
    ]))
    story.append(gap(6))
    story.append(note(
        "Milestone markets auto-resolve when the phase transition is made by an admin. "
        "Pre-Games prop markets auto-resolve when /admin tribute set_score is used."
    ))

    # 1.2 Tribute attributes
    story += h3("1.2  Richer Tribute Profiles")
    story.append(body(
        "Tributes now carry more information, all of which feeds into odds calculations or "
        "display formatting."
    ))
    story.append(bullets([
        "<b>Training score is now optional at add time.</b> A tribute can be added to the roster before scores are announced, with markets already open and priced on district averages.",
        "<b>Non-binary display flag.</b> A tribute can be marked non-binary — they display as 'NB' everywhere but odds calculations still use the underlying gender for historical stats.",
        "<b>Discord member link.</b> A tribute can be linked to a server member, which pulls in their join date for the seniority bonus.",
        "<b>Seniority date override.</b> Admins can manually set a join date for tributes whose member data isn't available.",
        "<b>SADE participant / champion flags.</b> Track which tributes are SADE participants and enforce that only one SADE Champion can exist at a time.",
        "<b>Kill boost accumulator.</b> Every time a tribute kills someone, their odds improve by an amount proportional to how strong that victim was. This boost persists and compounds.",
    ]))

    # 1.3 New data models
    story += h3("1.3  New Data Models")
    story.append(bullets([
        "<b>MarketTemplate</b> — Admins can define custom market types that persist across games. Custom types appear alongside built-in types in the /admin market add autocomplete.",
        "<b>DistrictRecord</b> — Stores aggregate historical statistics per district: wins, placements, kills, kill records, training score averages, and a manually-set reputation score.",
        "<b>Modifier / ModifierAssignment</b> — A modifier is a named weight (e.g. ×1.3 for 'injury recovery', ×0.7 for 'rivalry penalty'). An assignment applies it to a specific tribute or an entire district. Multiple modifiers stack multiplicatively.",
    ]))

    # 1.4 Improved odds engine
    story += h3("1.4  Improved Odds Engine")
    story.append(body(
        "The core probability formulas were rewritten to be more mathematically consistent and "
        "to incorporate new data sources. Key improvements:"
    ))
    story.append(bullets([
        "<b>Training score is now optional.</b> If a score hasn't been set, the engine uses the district's historical average (or 6 as a neutral default) so markets price sensibly before Reaping.",
        "<b>Unified strength model.</b> A tribute's 'strength' is their training score as a share of the total field score. Every market derives from this single number, so Win ⊆ Top-3 ⊆ Top-5 ⊆ Top-8 is guaranteed — a tribute can never be more likely to win than to make the Finale.",
        "<b>Poisson kill model.</b> Kills Over/Under markets now use a Poisson distribution based on the tribute's expected kill share, producing more realistic pricing than the previous linear approximation.",
        "<b>Bloodbath model.</b> Bloodbath Survivor odds now correctly reflect that ~40% of the field is eliminated in the opening and that weaker tributes are more likely to fall.",
        "<b>Kill Event model.</b> A specific kill's probability is now computed as P(victim dies) × P(death is tribute-on-tribute) × killer's share of the killing field — instead of an ad-hoc formula.",
        "<b>Pre-Games props.</b> Training score markets use a discrete Gaussian distribution centred on the district's historical average. Combined district score uses a triangular distribution for the sum of two scores.",
        "<b>Direction-aware group influence.</b> For 'bad-for-strong-tribute' markets (Misses Finale, OVER placement), the group influence now correctly pushes the line in the right direction instead of accidentally helping the tribute.",
    ]))

    # 1.5 New odds factors
    story += h3("1.5  New Odds Factors")
    story.append(body(
        "Five new factors now feed into every market recalculation. All factors are multiplicative "
        "and are applied on top of the base probability."
    ))
    story.append(factor_table([
        ("District History",   "Up to ±30%",  "Wins, placements, kill rates, and recent performance across all past games all nudge a district's tributes. High historical kill rates are a penalty for win/survival markets (fighters die young) but a bonus for kill markets."),
        ("District Reputation", "Up to ±10%", "A manually-set score (1 = best, 5 = worst, 3 = neutral). Each step away from neutral shifts probability by 5%."),
        ("Server Seniority",  "0.5× – 1.5×", "Members who joined less than a month ago get a 50% reduction. Members with 1+ years get a 10% boost per year, capped at 1.5×."),
        ("Modifiers",         "Any weight",   "Admin-applied multipliers that stack multiplicatively. A tribute can have multiple modifiers and also receive a 20% bleed from their alliance members' average modifier."),
        ("Kill Boost",        "Compounding",  "Each kill permanently boosts the killer's odds. The boost is proportional to how strong the victim was: killing the field favourite is worth up to 3× a normal kill; killing a long-shot might be worth only 0.5×."),
    ]))

    # 1.6 Parlay validation
    story += h3("1.6  Parlay Conflict Validation")
    story.append(body(
        "The system now detects logically impossible parlay combinations and rejects them before "
        "a bet is placed."
    ))
    story.append(bullets([
        "<b>Placement conflicts.</b> Two placement bets on the same tribute always conflict (victor, exact placement, top-N, and placement O/U all describe where a tribute finishes — they can't all be true). The only allowed exception is an opposite over/under pair on the same tribute (e.g. 'finishes top 3' AND 'finishes outside top 12'), which together describe a valid finishing window.",
        "<b>Position conflicts.</b> Two tributes can't both finish exactly 1st (or any other exact spot), so parlaying two victor bets or two exact-placement bets for the same position is blocked.",
        "<b>Milestone conflicts.</b> You can't combine 'Makes Final 8' and 'Misses Final 8' for the same tribute, and you can't stack two 'makes milestone' bets for the same tribute.",
    ]))

    # 1.7 Display improvements
    story += h3("1.7  Display and UX Improvements")
    story.append(bullets([
        "<b>/odds tribute view.</b> Pass a tribute name to /odds for a full detail card: their stats, win odds, and up to 16 non-win markets — all in a styled image.",
        "<b>Smarter featured market selection.</b> The hot odds board now picks markets with both variety (one from each type) and a balanced mix of positive and negative odds, weighted by how many bets each market has attracted.",
        "<b>Non-binary gender display.</b> All embeds, commands, and cards display 'NB' for non-binary tributes instead of the underlying M/F value.",
        "<b>Custom market type labels.</b> The /markets browser now shows custom market type names (instead of raw type codes) for player-facing display.",
        "<b>Read-optimised sessions.</b> Autocomplete and display queries now use a read-only database session for better performance under concurrent load.",
    ]))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — PLAYER COMMANDS
    # ══════════════════════════════════════════════════════════════════════════
    story += h2("Section 2 — Player Commands")
    story.append(body(
        "These commands are available to all server members. No special role is required."
    ))

    story += h3("2.1  Viewing Information")
    story.append(cmd_table([
        ("/odds",          "Displays the Hot Odds board: a styled image card showing every tribute's win odds plus a curated selection of featured non-win markets. Add a tribute name to see a full detail card for that tribute."),
        ("/tributes",      "Lists every tribute grouped by status (Alive, Victor, Fallen) with training scores, kill counts, and placement info."),
        ("/markets",       "Opens a paginated browser of every open betting market, sorted by type and district, with odds and implied win probability shown for each."),
        ("/balance",       "Shows your current chip balance, total chips won, total bets placed, and your win rate."),
        ("/leaderboard",   "Ranks all server members by current chip balance. Top chip holders are displayed."),
    ]))

    story += h3("2.2  Placing Bets")
    story.append(cmd_table([
        ("/bet",                "Place a straight single bet on any open market. Provide the market ID and your wager amount. Your payout is calculated at the current odds and locked in immediately."),
        ("/parlay add",         "Add a market to your pending parlay slip. You can add up to 10 legs. The bot validates each leg for logical conflicts as you add it."),
        ("/parlay view",        "Preview your current parlay slip: shows each leg, the combined odds, and the projected payout for any wager amount."),
        ("/parlay submit",      "Lock in your parlay with a wager. All legs must win for the parlay to pay out. Voided legs are removed and the payout adjusts across the remaining legs."),
        ("/parlay remove",      "Remove a specific leg from your slip by its position number (see /parlay view for numbering)."),
        ("/parlay clear",       "Discard your entire pending parlay slip without placing a bet."),
        ("/cashout",            "Cash out a pending straight bet or parlay early. Returns your original wager plus a share of the profit — less than a full win, but immediate and guaranteed. Not all markets support cashout."),
    ]))

    story += h3("2.3  Bet History")
    story.append(cmd_table([
        ("/mybets",   "Displays your personal bet history as a styled image card. Filter by All, Pending, Won, or Lost. Shows both straight bets and parlays with odds, wagers, and payouts."),
    ]))

    story.append(gap(8))
    story.append(note(
        "All bet amounts are in Chips — the server's fictional currency. "
        "Every member starts with a default chip balance when they first interact with the bot. "
        "Chips earned from winning bets accumulate permanently."
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — ADMIN COMMANDS
    # ══════════════════════════════════════════════════════════════════════════
    story += h2("Section 3 — Admin Commands")
    story.append(body(
        "Admin commands live under the /admin group. Access requires the configured Admin role. "
        "Most commands that modify game state (kills, phase transitions, odds overrides) "
        "trigger an automatic odds recalculation across all open and closed markets."
    ))

    # 3.1 Tribute
    story += h3("3.1  /admin tribute — Managing Tributes")
    story.append(cmd_table([
        ("/admin tribute add",       "Add a tribute to the Games. Requires name, district (1–12), and gender (M/F). Training score is optional — set it later. Supports face claim images (URL or file upload), server member linking for seniority, SADE flags, and non-binary display. Auto-creates all standard markets on add."),
        ("/admin tribute edit",      "Update a tribute's name, district, score, face claim, or linked member. Also supports a manual seniority date override. Triggers a full odds recalculation."),
        ("/admin tribute set_score", "Set a tribute's training score. Immediately resolves all Exact Training Score and Training Score O/U markets for that tribute, and resolves Combined District Score markets if the district partner's score is also known."),
        ("/admin tribute kill",      "Mark a tribute as dead. Provide the cause of death and optionally the killer. Automatically: sets placement, resolves all open markets for the dead tribute, awards a kill-quality boost to the killer, resolves First Blood markets if applicable, and recalculates odds for the remaining field."),
        ("/admin tribute remove",    "Remove a tribute entirely. All open markets for that tribute are voided and wagers refunded."),
        ("/admin tribute list",      "List all tributes with status, score, kills, and alliance membership."),
    ]))

    # 3.2 Market
    story += h3("3.2  /admin market — Managing Markets")
    story.append(cmd_table([
        ("/admin market add",          "Manually create a new betting market of any built-in or custom type. Most markets are created automatically when tributes are added, but this command lets you add extras."),
        ("/admin market set_phase",    "Assign or clear the betting phase for a market. Phase-restricted markets only open during the specified phase."),
        ("/admin market odds",         "Override the odds for a specific market. The override flag prevents the odds engine from automatically repricing this market during recalculations."),
        ("/admin market close",        "Close a single market to new bets. Existing bets remain pending."),
        ("/admin market reopen",       "Reopen a previously closed market."),
        ("/admin market resolve",      "Resolve a market as Won, Lost, or Voided. Won: bets on this market pay out. Lost: bets lose. Voided: wagers are refunded. Parlays with a voided leg recalculate across remaining legs."),
        ("/admin market bulk_close",   "Close all open markets immediately. Useful when transitioning phases."),
        ("/admin market bulk_open",    "Open all closed markets, optionally filtered to those belonging to a specific betting phase."),
        ("/admin market list",         "Paginated admin view of all markets (open, closed, and resolved) with full detail."),
    ]))

    # 3.3 Market types
    story += h3("3.3  /admin market_type — Custom Market Types")
    story.append(body(
        "Custom market types let you define new categories of bet that persist across games. "
        "Once created, a custom type appears in the /admin market add autocomplete."
    ))
    story.append(cmd_table([
        ("/admin market_type create", "Define a new custom market type with a name, description, difficulty (sets default odds), and optional label template."),
        ("/admin market_type list",   "List all custom market types with their difficulty settings and active/inactive status."),
        ("/admin market_type edit",   "Update a custom type's name, description, difficulty, or label template."),
        ("/admin market_type delete", "Delete a custom type. Blocked if any markets of that type currently exist."),
    ]))
    story.append(gap(4))
    story.append(note(
        "Difficulty presets map to starting odds: Easy ≈ −200, Moderate ≈ +100, "
        "Hard ≈ +300, Very Hard ≈ +700, Longshot ≈ +1500."
    ))

    # 3.4 Game control
    story += h3("3.4  /admin game — Game Control")
    story.append(cmd_table([
        ("/admin game start",       "Start the Games. Opens all Pre-Games phase markets and sets the game into motion. Tributes should be added before starting."),
        ("/admin game arena",       "Set the arena type (Natural or Artificial). This immediately adjusts all death-cause market odds: artificial arenas favour Gamemaker deaths; natural arenas favour mutt and environmental deaths."),
        ("/admin game set_phase",   "Transition to a named betting phase. Closes all current markets, opens markets for the new phase, and auto-resolves any milestone markets that trigger on phase entry (e.g. entering Final 8 resolves all 'Makes Final 8' and 'Misses Final 8' markets)."),
        ("/admin game end",         "End the Games by declaring a victor. Resolves all remaining open markets, marks the victor's Tribute Wins market as won, and closes everything else."),
        ("/admin game reset_confirm", "DANGER: Wipes all tributes, bets, parlays, and markets. Historical records and modifiers are preserved. Requires typing 'yes' to confirm."),
    ]))

    # 3.5 Phases
    story += h3("3.5  /admin phase — Betting Phases")
    story.append(cmd_table([
        ("/admin phase add",    "Create a new betting phase (e.g. Pre-Games, Bloodbath, Arena, Final 8). Phases control which markets are open at any point in the game."),
        ("/admin phase list",   "List all phases with their sort order and the number of markets assigned to each."),
        ("/admin phase delete", "Delete a phase. Blocked if any markets are currently assigned to it."),
    ]))

    # 3.6 Alliances
    story += h3("3.6  /admin alliance — Tribute Alliances")
    story.append(body(
        "Alliances link tributes together for odds calculations. Members of an alliance "
        "influence each other's odds — strong allies lift weaker members; weak allies can drag "
        "down stronger ones."
    ))
    story.append(cmd_table([
        ("/admin alliance create",       "Create a named alliance."),
        ("/admin alliance add_tribute",  "Add a tribute to an alliance. Triggers a full odds recalculation."),
        ("/admin alliance remove_tribute", "Remove a tribute from their alliance. Triggers a full odds recalculation."),
        ("/admin alliance list",         "List all alliances with their current members."),
        ("/admin alliance delete",       "Delete an alliance (removes all member assignments)."),
    ]))

    # 3.7 Modifiers
    story += h3("3.7  /admin modifier — Odds Modifiers")
    story.append(body(
        "Modifiers are named multipliers that adjust a tribute's probability for all their markets. "
        "Common uses: injury penalties, training advantages, political favour, Capitol sponsorship. "
        "Modifiers stack multiplicatively and can target a single tribute or an entire district."
    ))
    story.append(cmd_table([
        ("/admin modifier create",   "Create a reusable modifier with a label and a weight (e.g. ×1.25 for a 25% probability boost, ×0.8 for a 20% reduction)."),
        ("/admin modifier assign",   "Apply a modifier to a specific tribute or to all tributes in a district. Triggers a full recalculation."),
        ("/admin modifier unassign", "Remove a modifier assignment. Triggers a full recalculation."),
        ("/admin modifier list",     "List all modifiers with their current assignments."),
        ("/admin modifier delete",   "Delete a modifier and all its assignments permanently."),
    ]))

    # 3.8 History
    story += h3("3.8  /admin history — District Historical Records")
    story.append(body(
        "Historical records give the odds engine a memory of past games. These numbers come "
        "from your server's Hunger Games history and are entered manually."
    ))
    story.append(cmd_table([
        ("/admin history games",  "Set the total number of past Hunger Games. This number is the denominator for all per-game rate calculations."),
        ("/admin history arena",  "Record how many past games used a natural vs. man-made arena. Used in arena-type market odds and in the historical display."),
        ("/admin history set",    "Update aggregate stats for a single district: wins, runner-up finishes, placements, kills, bloodbath kills, kill record, training score averages, and reputation (1–5)."),
        ("/admin history list",   "Paginated district-by-district view of all recorded stats with computed odds factors shown."),
        ("/admin history reset",  "Clear all historical data for a district (sets every field to null)."),
    ]))

    # 3.9 Settings
    story += h3("3.9  /admin settings — Bot Settings")
    story.append(cmd_table([
        ("/admin settings cashout",         "Configure global cashout availability and the cashout rate (what fraction of potential profit is paid out on early exit)."),
        ("/admin settings market_cashout",  "Override cashout settings for a specific market independently of the global setting."),
        ("/admin settings chips_give",      "Add chips to a specific user's balance."),
        ("/admin settings chips_take",      "Remove chips from a user's balance."),
        ("/admin settings chips_set",       "Set a user's balance to a specific amount."),
        ("/admin settings chips_reset",     "Reset ALL users to the default starting balance."),
        ("/admin settings default_chips",   "Set the chip amount that new users receive when they first interact with the bot."),
        ("/admin settings announce",        "Post a Capitol announcement embed to the designated announcement channel."),
        ("/admin settings theme",           "Set the visual theme (colour palette) used for all image cards."),
    ]))

    # 3.10 Restrictions
    story += h3("3.10  /restrict — Betting Restrictions")
    story.append(body(
        "The /restrict group is available at the top level (not nested under /admin). "
        "It controls which users are allowed to bet and what they can bet on."
    ))
    story.append(cmd_table([
        ("/restrict ban",              "Block a user from placing any bet on the bot."),
        ("/restrict unban",            "Lift a full betting ban."),
        ("/restrict block_district",   "Prevent a user from betting on any market that involves a specific district (useful for conflicts of interest)."),
        ("/restrict unblock_district", "Remove a district-level restriction."),
        ("/restrict block_tribute",    "Prevent a user from betting on any market involving a specific tribute."),
        ("/restrict unblock_tribute",  "Remove a tribute-level restriction."),
        ("/restrict view",             "Show all active restrictions for a user."),
    ]))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 4 — HOW ODDS ARE CALCULATED
    # ══════════════════════════════════════════════════════════════════════════
    story += h2("Section 4 — How Odds Are Calculated")
    story.append(body(
        "This section explains the odds engine in plain English. No maths degree required."
    ))

    # 4.1 American odds primer
    story += h3("4.1  Reading American Odds")
    story.append(body(
        "All odds on the bot are displayed in American format — the same format used by major "
        "sportsbooks. Here is how to read them:"
    ))
    story.append(bullets([
        "<b>Negative odds (e.g. −150)</b> mean the tribute is a favourite. You would need to bet 150 chips to win 100. The bigger the negative number, the bigger the favourite.",
        "<b>Positive odds (e.g. +200)</b> mean the tribute is an underdog. A 100-chip bet wins 200. The bigger the positive number, the bigger the long-shot.",
        "<b>Even money (+100 / −100)</b> means the bet is roughly a coin flip — you win exactly what you wager.",
    ]))
    story.append(body(
        "Every set of odds implies a probability. −150 implies a ~60% chance; +200 implies "
        "a ~33% chance. The bot computes probabilities first, then converts them to American odds."
    ))

    # 4.2 The core: training score
    story += h3("4.2  The Core Ingredient: Training Score")
    story.append(body(
        "Almost every market starts with a simple idea: a tribute's odds are proportional to "
        "their training score as a share of the total field score."
    ))
    story.append(body(
        "Example: if five tributes have scores 10, 8, 7, 6, and 5 (total = 36), the tribute "
        "with score 10 has a 10/36 ≈ 28% chance to win. That converts to roughly −38 odds. "
        "The tribute with score 5 has a 5/36 ≈ 14% chance, or about +614 odds."
    ))
    story.append(body(
        "If a score hasn't been revealed yet, the bot uses the district's historical average "
        "score (or a neutral value of 6 if no history exists), so markets price sensibly "
        "during the Pre-Games period."
    ))

    # 4.3 Five layers on top
    story += h3("4.3  Five Layers Applied on Top")
    story.append(body(
        "The base probability from training scores is then adjusted by up to five additional "
        "factors. Each factor multiplies the base probability — they stack together."
    ))

    story += h4("Layer 1: Group Influence (District &amp; Alliance)")
    story.append(body(
        "Strong district-mates and strong allies pull a tribute's odds toward their average. "
        "A weak tribute in a district full of powerhouses benefits slightly from that proximity "
        "(Capitol favourites often come from the same districts). A strong tribute in a weak "
        "alliance is slightly dragged down. The district effect is 10%; the alliance effect is 20%."
    ))

    story += h4("Layer 2: District Historical Performance")
    story.append(body(
        "Districts with a strong track record across past games get a modest probability boost "
        "(up to ±30%). The engine looks at wins, top-8 and top-5 finishes, average placement, "
        "runner-up rate, and kill record. Importantly, high historical kill rates are treated "
        "as a penalty for win/survival markets — fighters get into more fights and die younger — "
        "but as a bonus for kill-market odds."
    ))

    story += h4("Layer 3: District Reputation")
    story.append(body(
        "Admins can assign each district a reputation score from 1 (best) to 5 (worst), with "
        "3 as neutral. Each step away from neutral moves probability by 5%. A reputation-1 "
        "district gets +10%; a reputation-5 district gets −10%. This is a small, manual override "
        "for storyline reasons that sits on top of the historical data."
    ))

    story += h4("Layer 4: Server Seniority")
    story.append(body(
        "If a tribute is linked to a Discord member, how long that member has been in the server "
        "counts for something. Members with less than a month of seniority receive a 50% odds "
        "penalty (their tribute is an inexperienced underdog). Members with one or more years get "
        "a 10% boost per year, up to a maximum of 1.5× (50% boost). Members between one month "
        "and one year are neutral."
    ))

    story += h4("Layer 5: Modifiers")
    story.append(body(
        "Admins can apply named multipliers to any tribute or district at any time. A ×1.3 "
        "modifier means a 30% probability boost; a ×0.7 modifier means a 30% reduction. "
        "Multiple modifiers on the same tribute multiply together. Additionally, 20% of each "
        "tribute's modifier bleeds into their alliance members' odds — so a heavily-boosted "
        "tribute lifts their allies slightly."
    ))

    # 4.4 Kill boost
    story += h3("4.4  Kill Quality Boost")
    story.append(body(
        "Every time a tribute kills another tribute, their odds permanently improve. But not "
        "all kills are equal:"
    ))
    story.append(bullets([
        "Killing the <b>field favourite</b> (a tribute with high win odds) is worth the most — up to 3× the standard boost. Taking out a strong contender is a meaningful achievement.",
        "Killing an <b>average</b> tribute gives a 1× boost — a normal improvement.",
        "Killing a <b>long-shot</b> (someone with very low win odds) gives less than 1× — finishing off a tribute who was already barely a factor barely moves the needle.",
    ]))
    story.append(body(
        "The boost compounds: each kill multiplies the tribute's existing kill-boost factor. "
        "A tribute who chains kills against strong opponents can see a significant cumulative "
        "improvement to all their markets."
    ))

    # 4.5 Arena type adjustment
    story += h3("4.5  Arena Type Adjustment (Death Cause Markets Only)")
    story.append(body(
        "When the admin sets the arena type, death-cause market odds adjust to reflect the "
        "environment:"
    ))
    story.append(bullets([
        "<b>Natural arenas</b> (wilderness, forests, oceans): Natural Causes probability ×1.5, Mutt probability ×1.25, Gamemaker probability ×0.6.",
        "<b>Artificial arenas</b> (traps, technology, industrial): Natural Causes probability ×0.6, Mutt probability ×0.8, Gamemaker probability ×1.5.",
        "Tribute-on-tribute kill probability is unaffected by arena type.",
    ]))

    # 4.6 Recalculation
    story += h3("4.6  When Odds Recalculate")
    story.append(body(
        "The odds engine runs a full recalculation automatically whenever any of the following "
        "happens:"
    ))
    story.append(bullets([
        "A tribute is added or edited",
        "A tribute's training score is set",
        "A tribute dies (the field shrinks and everyone's odds shift)",
        "A tribute is added to or removed from an alliance",
        "A modifier is assigned or removed",
        "A phase transition is made",
    ]))
    story.append(body(
        "Markets with a manually-overridden odds value are excluded from recalculation — they "
        "stay at whatever the admin set."
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 5 — MARKET TYPES REFERENCE
    # ══════════════════════════════════════════════════════════════════════════
    story += h2("Section 5 — Market Types Reference")
    story.append(body(
        "Every market type the bot supports, with a plain-English description of what the bet "
        "covers, how it resolves, and the key input that drives its starting odds."
    ))

    mt_rows = [
        ("Tribute Wins (Victor)",          "Will this tribute win the entire Games?",                              "Training score share of total field. Betting on the favourite to win costs more; long-shots pay big."),
        ("Tribute Placement (Exact)",      "Will this tribute finish in exactly the stated position?",            "Probability of finishing at that exact spot decreases quickly for positions beyond 2nd."),
        ("Tribute Top-N Finish",           "Will this tribute finish in the top N?",                             "Uses the same top-k model as wins, so Top-5 is always more likely than Top-1 for the same tribute."),
        ("Top Killer",                     "Will this tribute have the most kills when the Games end?",          "Uses a squared-score model — stronger tributes are disproportionately likely to lead in kills."),
        ("Kill Event (A kills B)",         "Will tribute A specifically kill tribute B?",                        "Combines probability that B dies, that the death is by another tribute, and A's share of the field."),
        ("Death Cause",                    "If this tribute dies, what will be the cause?",                     "Defaults to equal probability across the four causes; adjusts based on arena type once set."),
        ("First Blood",                    "Will this tribute land the first kill of the Games?",               "Squared-score model favouring strong, aggressive tributes."),
        ("Bloodbath Survivor",             "Will this tribute survive the opening bloodbath?",                  "~40% of the field is assumed to die in the bloodbath; weaker tributes are more likely to fall."),
        ("Kills Over/Under",               "Will this tribute's kill total go over or under the stated line?",  "Poisson distribution based on the tribute's expected kill share. Lines of 0.5 and 1.5 are most common."),
        ("Placement Over/Under",           "Will this tribute finish better (Under) or worse (Over) than the line?", "Top-k model: 'under 12.5' means finishing in the top 12."),
        ("Makes Final 8",                  "Will this tribute be alive when the field reaches 8?",             "Top-8 probability from the unified strength model. Auto-resolves on phase transition."),
        ("Misses Final 8",                 "Will this tribute be eliminated before the final 8?",               "Complement of Makes Final 8."),
        ("Makes Final 5",                  "Will this tribute be alive when the field reaches 5?",             "Top-5 probability. Auto-resolves on phase transition."),
        ("Misses Final 5",                 "Will this tribute be eliminated before the final 5?",               "Complement of Makes Final 5."),
        ("Makes the Finale",               "Will this tribute reach the Finale (top 3)?",                      "Top-3 probability. Auto-resolves on phase transition."),
        ("Eliminated Before Finale",       "Will this tribute be out before the Finale?",                       "Complement of Makes the Finale."),
        ("Arena Type",                     "Will the arena be Natural or Artificial?",                          "Starts at even money. Admins set this; the market resolves once the arena is revealed."),
        ("Exact Training Score",           "Will this tribute score exactly N in training?",                   "Discrete Gaussian distribution centred on the district's historical average score."),
        ("Combined District Score",        "What will the sum of both district tributes' scores be?",          "Triangular distribution for the sum of two independent scores. Resolves when both scores are known."),
        ("Training Score Over/Under",      "Will this tribute score over or under 6.5 in training?",          "Gaussian model using historical average. Resolves when the score is set."),
        ("Sponsor Event (Custom)",         "Admin-defined events tied to Capitol decisions or sponsor outcomes.", "Default odds set by the difficulty level chosen when creating the market."),
    ]

    col_w = [W * 0.26, W * 0.35, W * 0.39]
    header = [
        Paragraph("<b>Market Type</b>", S["bullet"]),
        Paragraph("<b>What you're betting on</b>", S["bullet"]),
        Paragraph("<b>How odds are set</b>", S["bullet"]),
    ]
    data = [header] + [
        [Paragraph(f"<font name='Helvetica-Bold' color='#C9A84C'>{a}</font>", S["bullet"]),
         Paragraph(b, S["bullet"]),
         Paragraph(c, S["bullet"])]
        for a, b, c in mt_rows
    ]
    t = Table(data, colWidths=col_w)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A2A2A")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARKER, colors.HexColor("#161616")]),
        ("GRID", (0, 0), (-1, -1), 0.25, DIM_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
    ]))
    story.append(t)

    # Closing note
    story.append(gap(16))
    story.append(hr(color=GOLD, thickness=1.0))
    story.append(gap(8))
    story.append(Paragraph("Capitol Wagering Bureau · THG Sportsbook · All odds are bot-calculated", S["version"]))

    return story


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    out = Path(__file__).parent.parent / "BOT_UPDATE_GUIDE.pdf"

    doc = SimpleDocTemplate(
        str(out),
        pagesize=LETTER,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title="THG Sportsbook — Bot Update & Reference Guide",
        author="Capitol Wagering Bureau",
    )

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(DARK)
        canvas.rect(0, 0, LETTER[0], LETTER[1], fill=1, stroke=0)
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.5)
        margin = 0.4 * inch
        canvas.rect(margin, margin, LETTER[0] - 2 * margin, LETTER[1] - 2 * margin, fill=0, stroke=1)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GRAY)
        canvas.drawCentredString(LETTER[0] / 2, 0.25 * inch, f"THG Sportsbook — Bot Update & Reference Guide  ·  Page {doc.page}")
        canvas.restoreState()

    story = build_story()
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"PDF written to: {out}")


if __name__ == "__main__":
    main()

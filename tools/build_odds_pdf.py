"""Generates ODDS_GUIDE.pdf — a non-technical guide to how tribute odds work."""

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Palette ───────────────────────────────────────────────────────────────────
NAVY       = colors.HexColor("#1C1C2E")
GOLD       = colors.HexColor("#C9A84C")
GOLD_LIGHT = colors.HexColor("#EDD47A")
CRIMSON    = colors.HexColor("#8B1A1A")
OFF_WHITE  = colors.HexColor("#F7F4EE")
WARM_GRAY  = colors.HexColor("#E5E0D8")
MID_GRAY   = colors.HexColor("#A09880")
DARK_TEXT  = colors.HexColor("#1C1C2E")
PALE_GOLD  = colors.HexColor("#FAF3E0")

# ── Styles ────────────────────────────────────────────────────────────────────
base = getSampleStyleSheet()

def style(name, **kw):
    return ParagraphStyle(name, **kw)

COVER_TITLE = style("CoverTitle",
    fontName="Helvetica-Bold", fontSize=34, leading=42,
    textColor=GOLD, alignment=TA_CENTER, spaceAfter=8)

COVER_SUB = style("CoverSub",
    fontName="Helvetica", fontSize=14, leading=20,
    textColor=OFF_WHITE, alignment=TA_CENTER, spaceAfter=4)

COVER_BYLINE = style("CoverByline",
    fontName="Helvetica-Oblique", fontSize=10, leading=14,
    textColor=MID_GRAY, alignment=TA_CENTER)

SECTION_HEADER = style("SectionHeader",
    fontName="Helvetica-Bold", fontSize=16, leading=22,
    textColor=GOLD, spaceBefore=24, spaceAfter=6)

SUBHEADER = style("SubHeader",
    fontName="Helvetica-Bold", fontSize=11, leading=15,
    textColor=CRIMSON, spaceBefore=14, spaceAfter=4)

BODY = style("Body",
    fontName="Helvetica", fontSize=10, leading=15,
    textColor=DARK_TEXT, spaceAfter=6)

BODY_BOLD = style("BodyBold",
    fontName="Helvetica-Bold", fontSize=10, leading=15,
    textColor=DARK_TEXT, spaceAfter=4)

BULLET = style("Bullet",
    fontName="Helvetica", fontSize=10, leading=15,
    textColor=DARK_TEXT, leftIndent=16, spaceAfter=3,
    bulletIndent=6, bulletFontName="Helvetica", bulletFontSize=10)

NOTE = style("Note",
    fontName="Helvetica-Oblique", fontSize=9, leading=13,
    textColor=MID_GRAY, spaceAfter=4)

PIPELINE_LABEL = style("PipelineLabel",
    fontName="Helvetica-Bold", fontSize=11, leading=14,
    textColor=NAVY, alignment=TA_CENTER)

PIPELINE_DESC = style("PipelineDesc",
    fontName="Helvetica", fontSize=8, leading=11,
    textColor=MID_GRAY, alignment=TA_CENTER)

TH = style("TH",
    fontName="Helvetica-Bold", fontSize=9, leading=12,
    textColor=OFF_WHITE, alignment=TA_CENTER)

TD = style("TD",
    fontName="Helvetica", fontSize=9, leading=12,
    textColor=DARK_TEXT, alignment=TA_LEFT)

TD_CENTER = style("TDCenter",
    fontName="Helvetica", fontSize=9, leading=12,
    textColor=DARK_TEXT, alignment=TA_CENTER)

TD_BOLD = style("TDBold",
    fontName="Helvetica-Bold", fontSize=9, leading=12,
    textColor=DARK_TEXT, alignment=TA_LEFT)


# ── Table helpers ─────────────────────────────────────────────────────────────

def base_table_style(col_widths=None):
    return TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [OFF_WHITE, WARM_GRAY]),
        ("GRID",         (0, 0), (-1, -1), 0.5, WARM_GRAY),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ])

def th(text): return Paragraph(text, TH)
def td(text): return Paragraph(text, TD)
def tdc(text): return Paragraph(text, TD_CENTER)
def tdb(text): return Paragraph(text, TD_BOLD)


def divider():
    return HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=6, spaceBefore=6)


def section_rule():
    return HRFlowable(width="100%", thickness=0.5, color=WARM_GRAY, spaceAfter=2, spaceBefore=2)


# ── Page template ─────────────────────────────────────────────────────────────

def on_page(canvas, doc):
    """Footer on every page except the cover."""
    if doc.page == 1:
        return
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, LETTER[0], 28, fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MID_GRAY)
    canvas.drawString(0.75 * inch, 10, "THE PANEM SPORTSBOOK  ·  ODDS GUIDE")
    canvas.drawRightString(LETTER[0] - 0.75 * inch, 10, f"Page {doc.page - 1}")
    canvas.restoreState()


# ── Cover page ────────────────────────────────────────────────────────────────

def build_cover():
    story = []

    story.append(Spacer(1, 1.6 * inch))
    story.append(Paragraph("THE PANEM SPORTSBOOK", COVER_TITLE))
    story.append(Spacer(1, 0.1 * inch))
    story.append(HRFlowable(width="60%", thickness=2, color=GOLD,
                             hAlign="CENTER", spaceAfter=14))
    story.append(Paragraph("How Tribute Odds Work", style("CT2",
        fontName="Helvetica", fontSize=22, leading=28,
        textColor=OFF_WHITE, alignment=TA_CENTER, spaceAfter=10)))
    story.append(Paragraph("A Plain-Language Guide for Bookmakers & Spectators", COVER_BYLINE))
    story.append(Spacer(1, 0.5 * inch))
    story.append(HRFlowable(width="40%", thickness=1, color=MID_GRAY,
                             hAlign="CENTER", spaceAfter=20))
    story.append(Paragraph(
        "Every number you see next to a tribute's name tells a story. "
        "This guide explains exactly what shapes those numbers — from training "
        "scores and alliances to server history and the arena itself.",
        style("CoverBlurb",
            fontName="Helvetica-Oblique", fontSize=11, leading=17,
            textColor=WARM_GRAY, alignment=TA_CENTER,
            leftIndent=0.8*inch, rightIndent=0.8*inch)
    ))

    story.append(PageBreak())
    return story


# ── Pipeline overview ─────────────────────────────────────────────────────────

def build_pipeline():
    story = []
    story.append(Paragraph("How the Odds Pipeline Works", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "Every time a tribute enters or leaves the arena, all open markets are "
        "recalculated automatically. The odds travel through four stages — each "
        "one layering additional context on top of the last.",
        BODY))
    story.append(Spacer(1, 0.15 * inch))

    stages = [
        ("1", "Training Score", "The starting point.\nBased purely on how strong\nthe tribute is vs. the field."),
        ("2", "Peer Influence", "Districtmates and alliance\nmembers pull odds up\nor down slightly."),
        ("3", "Modifiers", "Custom bonuses/penalties,\nserver seniority, and\ndistrict history."),
        ("4", "Arena Type", "Only for Death Cause bets.\nThe environment shifts which\ncauses are likely."),
    ]

    arrows = [
        Paragraph("①", PIPELINE_LABEL), Paragraph("→", style("Arr",
            fontName="Helvetica-Bold", fontSize=18, textColor=GOLD, alignment=TA_CENTER)),
        Paragraph("②", PIPELINE_LABEL), Paragraph("→", style("Arr2",
            fontName="Helvetica-Bold", fontSize=18, textColor=GOLD, alignment=TA_CENTER)),
        Paragraph("③", PIPELINE_LABEL), Paragraph("→", style("Arr3",
            fontName="Helvetica-Bold", fontSize=18, textColor=GOLD, alignment=TA_CENTER)),
        Paragraph("④", PIPELINE_LABEL),
    ]

    box_data = []
    box_row_num = []
    box_row_label = []
    box_row_desc = []
    for num, label, desc in stages:
        box_row_num.append(Paragraph(f"STAGE {num}", style(f"SN{num}",
            fontName="Helvetica-Bold", fontSize=8, leading=10,
            textColor=GOLD, alignment=TA_CENTER)))
        box_row_label.append(Paragraph(label, PIPELINE_LABEL))
        box_row_desc.append(Paragraph(desc, PIPELINE_DESC))

    # Build a simple pipeline table
    col_w = [1.3 * inch, 0.3 * inch, 1.3 * inch, 0.3 * inch, 1.3 * inch, 0.3 * inch, 1.3 * inch]
    row_num   = [box_row_num[0],   Paragraph("", BODY), box_row_num[1],   Paragraph("", BODY), box_row_num[2],   Paragraph("", BODY), box_row_num[3]]
    row_label = [box_row_label[0], Paragraph("→", style("Arr_", fontName="Helvetica-Bold", fontSize=16, textColor=GOLD, alignment=TA_CENTER)), box_row_label[1], Paragraph("→", style("Arr__", fontName="Helvetica-Bold", fontSize=16, textColor=GOLD, alignment=TA_CENTER)), box_row_label[2], Paragraph("→", style("Arr___", fontName="Helvetica-Bold", fontSize=16, textColor=GOLD, alignment=TA_CENTER)), box_row_label[3]]
    row_desc  = [box_row_desc[0],  Paragraph("", BODY), box_row_desc[1],  Paragraph("", BODY), box_row_desc[2],  Paragraph("", BODY), box_row_desc[3]]

    pipeline_table = Table([row_num, row_label, row_desc], colWidths=col_w)
    pipeline_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, -1), NAVY),
        ("BACKGROUND",    (2, 0), (2, -1), NAVY),
        ("BACKGROUND",    (4, 0), (4, -1), NAVY),
        ("BACKGROUND",    (6, 0), (6, -1), NAVY),
        ("BACKGROUND",    (1, 0), (1, -1), colors.white),
        ("BACKGROUND",    (3, 0), (3, -1), colors.white),
        ("BACKGROUND",    (5, 0), (5, -1), colors.white),
        ("BOX",           (0, 0), (0, -1), 1, GOLD),
        ("BOX",           (2, 0), (2, -1), 1, GOLD),
        ("BOX",           (4, 0), (4, -1), 1, GOLD),
        ("BOX",           (6, 0), (6, -1), 1, GOLD),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))

    story.append(pipeline_table)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "Markets with a manual odds override are frozen — they skip all four "
        "stages and never change unless you update them by hand.",
        NOTE))
    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Stage 1 ───────────────────────────────────────────────────────────────────

def build_stage1():
    story = []
    story.append(Paragraph("Stage 1 — Training Score (The Starting Point)", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "Every tribute's odds begin with one question: <b>how does their training "
        "score compare to everyone still alive?</b> A tribute with a score of 10 "
        "in a field where the total is 100 starts with a 10% chance of winning. "
        "The math below shows how that percentage is adjusted per market type.",
        BODY))
    story.append(Spacer(1, 0.1 * inch))

    # Market type breakdown table
    rows = [
        [th("Market"), th("What Drives the Odds"), th("Key Detail")],
        [tdb("Victor"), td("Tribute's score as a % of all alive scores"), td("1 point = 1% more likely to win in a 100-point field")],
        [tdb("Exact Placement"), td("Win % × a placement multiplier"), td("2nd place is ×1.6; 5th is ×3.5; the further back, the bigger the boost")],
        [tdb("Top-N Finish"), td("Win % × the N value × 0.7"), td("\"Top 4\" gives a ×2.8 boost before dampening")],
        [tdb("Top Killer"), td("Score squared vs. field's scores squared"), td("Score gaps are amplified — a 10 vs. 8s is a much bigger edge here than in Victor")],
        [tdb("Kill Event (A→B)"), td("Both tributes' strengths × field size"), td("Bigger fields = more chances for them to meet")],
        [tdb("Bloodbath Survivor"), td("Inverted — weaker tributes start as favorites"), td("Top scorers attract attention; weaker tributes slip past unnoticed")],
        [tdb("First Blood"), td("Same as Victor market"), td("Proportional score share")],
        [tdb("Kills Over/Under"), td("Expected kill rate from score share"), td("Higher score = higher expected kills; line adjusts threshold")],
        [tdb("Placement Over/Under"), td("Score share × the line value"), td("Under = finishes better than line; Over = finishes worse")],
        [tdb("Death Cause"), td("Starts flat at +200"), td("Adjusted only by the Arena Factor (Stage 4)")],
        [tdb("Custom Markets"), td("Difficulty setting or manual odds"), td("Easy ≈ −200 · Moderate ≈ +100 · Hard ≈ +300 · Very Hard ≈ +700 · Longshot ≈ +1500")],
    ]
    col_w = [1.5 * inch, 2.4 * inch, 2.6 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)

    story.append(Spacer(1, 0.15 * inch))

    # Placement multipliers sub-table
    story.append(Paragraph("Exact Placement Multipliers", SUBHEADER))
    story.append(Paragraph(
        "The further back the placement, the larger the multiplier applied to "
        "the tribute's win percentage — reflecting how much easier it is to "
        "finish 8th than 1st.",
        BODY))

    pm_rows = [
        [th("Placement"), th("Multiplier"), th("Example — tribute with 10% win odds")],
        [tdc("2nd"), tdc("×1.6"), td("16% chance → about −95 odds")],
        [tdc("3rd"), tdc("×2.2"), td("22% chance → about +355 odds... wait, that's the same direction?")],
        [tdc("3rd"), tdc("×2.2"), td("22% chance → about −39 odds")],
        [tdc("4th"), tdc("×2.8"), td("28% chance → about −39 odds")],
        [tdc("5th"), tdc("×3.5"), td("35% chance → about −54 odds")],
        [tdc("6th"), tdc("×4.0"), td("40% chance → about −67 odds")],
        [tdc("7th"), tdc("×4.5"), td("45% chance → about −82 odds")],
        [tdc("8th"), tdc("×5.0"), td("50% chance → −100 (even money)")],
        [tdc("9th+"), tdc("×(place × 0.8)"), td("Scales linearly beyond 8th")],
    ]
    # fix the duplicate row issue
    pm_rows = [
        [th("Placement"), th("Multiplier"), th("Example — tribute at 10% win odds")],
        [tdc("2nd"), tdc("×1.6"), td("16% → approx. −95")],
        [tdc("3rd"), tdc("×2.2"), td("22% → approx. −38")],
        [tdc("4th"), tdc("×2.8"), td("28% → approx. −39")],
        [tdc("5th"), tdc("×3.5"), td("35% → approx. −54")],
        [tdc("6th"), tdc("×4.0"), td("40% → approx. −67")],
        [tdc("7th"), tdc("×4.5"), td("45% → approx. −82")],
        [tdc("8th"), tdc("×5.0"), td("50% → −100 (even money)")],
        [tdc("9th+"), tdc("×(place × 0.8)"), td("Scales linearly beyond 8th")],
    ]
    col_w2 = [1.2 * inch, 1.2 * inch, 4.1 * inch]
    t2 = Table(pm_rows, colWidths=col_w2, repeatRows=1)
    t2.setStyle(base_table_style())
    story.append(t2)

    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Stage 2 ───────────────────────────────────────────────────────────────────

def build_stage2():
    story = []
    story.append(Paragraph("Stage 2 — Peer Influence (District & Alliance)", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "After the base odds are set, the system looks at who else is still alive "
        "from the same district and the same alliance. Tributes are subtly pulled "
        "toward their peers — if your partner is a favourite, you become a slightly "
        "bigger favourite too. If they're an underdog, you're nudged downward.",
        BODY))

    story.append(Spacer(1, 0.1 * inch))

    rows = [
        [th("Peer Group"), th("Influence Strength"), th("Who Counts")],
        [tdb("District"), td("10% pull toward the peer average"), td("Living tributes from the same district only")],
        [tdb("Alliance"), td("20% pull toward the peer average"), td("Living tributes in the same named alliance only")],
    ]
    col_w = [1.4 * inch, 2.5 * inch, 2.6 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("What This Means in Practice", SUBHEADER))
    bullets = [
        ("•", "A weak tribute allied with strong tributes will see their odds slightly improve."),
        ("•", "A strong tribute in a weak district will have their odds very slightly dragged down."),
        ("•", "Alliance influence is twice as strong as district influence."),
        ("•", "Only alive tributes contribute — dead peers no longer affect anyone."),
        ("•", "This stage has no effect on Kill Event, Death Cause, or custom market types."),
    ]
    for bullet, text in bullets:
        story.append(Paragraph(f"{bullet}  {text}", BULLET))

    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Stage 3 ───────────────────────────────────────────────────────────────────

def build_stage3():
    story = []
    story.append(Paragraph("Stage 3 — The Modifier Factor", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "This is where admin-controlled context gets layered in. Four things combine "
        "into a single multiplier that nudges the probability up or down before the "
        "final odds are calculated.",
        BODY))

    # ── Layer A
    story.append(Paragraph("Layer A — Custom Odds Modifiers", SUBHEADER))
    story.append(Paragraph(
        "These are reusable multipliers you create via <b>/admin modifier</b>. "
        "You assign them to a specific tribute or to an entire district. "
        "Multiple modifiers on the same tribute multiply together.",
        BODY))

    rows = [
        [th("Example Modifier"), th("Weight"), th("Effect on Odds")],
        [td("Career Training"),   tdc("×1.5"), td("Tribute is 50% more likely than their score alone suggests")],
        [td("Injured"),           tdc("×0.6"), td("Tribute is 40% less likely")],
        [td("Fan Favourite"),     tdc("×1.2"), td("Slight 20% boost — sponsor advantages")],
        [td("District 2 boost"),  tdc("×1.3"), td("Applies to every tribute in District 2")],
    ]
    col_w = [2.0 * inch, 1.1 * inch, 3.4 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "Note: district modifiers affect everyone in that district. If you want to "
        "target one tribute specifically, assign to the tribute, not the district.",
        NOTE))

    # ── Layer B
    story.append(Paragraph("Layer B — Alliance Modifier Bleed", SUBHEADER))
    story.append(Paragraph(
        "20% of the average modifier weight of a tribute's alive allies bleeds "
        "into their own modifier. A tribute with no modifier assigned, but allied "
        "with someone carrying a ×1.5 boost, will receive a small automatic lift.",
        BODY))
    story.append(Paragraph(
        "Example: Tribute A has no modifier. Their two allies have ×1.4 and ×1.2. "
        "Ally average = ×1.3. Tribute A ends up with ×1.06 (20% of the ×0.3 gap).",
        NOTE))

    # ── Layer C
    story.append(Paragraph("Layer C — Server Seniority", SUBHEADER))
    story.append(Paragraph(
        "If a tribute is linked to a Discord server member, their membership "
        "length influences their odds. Long-standing members are rewarded; "
        "brand-new members take a penalty.",
        BODY))

    rows = [
        [th("Time in Server"), th("Multiplier"), th("Notes")],
        [td("Less than 30 days"),  tdc("×0.5"),  td("New members start as bigger underdogs")],
        [td("30 days – 1 year"),   tdc("×1.0"),  td("Neutral — no effect")],
        [td("1–2 years"),          tdc("×1.1"),  td("+10% boost")],
        [td("2–3 years"),          tdc("×1.2"),  td("+20% boost")],
        [td("3–4 years"),          tdc("×1.3"),  td("+30% boost")],
        [td("Each additional year"), tdc("+×0.1"), td("Continues to scale upward")],
    ]
    col_w = [2.0 * inch, 1.2 * inch, 3.3 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "You can override a tribute's join date using /admin tribute edit → seniority_date. "
        "Useful for members who rejoined after leaving.",
        NOTE))

    # ── Layer D
    story.append(Paragraph("Layer D — District Historical Performance", SUBHEADER))
    story.append(Paragraph(
        "If your server has historical Games data entered via <b>/admin history</b>, "
        "each district's past performance subtly shifts their current tributes' odds. "
        "This influence is <b>capped at 15%</b> — history matters, but it can't "
        "override a strong current tribute.",
        BODY))

    rows = [
        [th("Historical Stat"), th("Effect if Above Average")],
        [td("Kills per Game"),           td("Slight boost — this district produces fighters")],
        [td("Average Placement"),        td("Boost if low (low = better finish). Inverted stat.")],
        [td("Total Wins (Victors)"),      td("Boost — proven winner history")],
        [td("Top-8 Finishes"),           td("Boost — consistently deep runs")],
        [td("Top-5 Finishes"),           td("Boost — elite performer history")],
        [td("Kill Record"),              td("Boost — highest single-game kill total")],
        [td("Runner-Up Finishes/Game"),  td("Boost — comes close to winning often")],
        [td("Avg Placement (Last 5)"),   td("Boost if low. More weight on recent form.")],
        [td("Bloodbath Kills/Game"),     td("Boost — aggressive early-game district")],
    ]
    col_w = [2.6 * inch, 3.9 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "Only stats that have been set contribute. A district with no history set "
        "gets a neutral ×1.0 from this layer. Stats are compared to the average "
        "across all districts that have data — not an absolute scale.",
        NOTE))

    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Stage 4 ───────────────────────────────────────────────────────────────────

def build_stage4():
    story = []
    story.append(Paragraph("Stage 4 — Arena Type (Death Cause Only)", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "This stage only affects <b>Death Cause</b> markets. Set the arena type "
        "via <b>/admin game arena</b> to shift the probability of each cause of death. "
        "If no arena type is set, all causes are treated equally.",
        BODY))

    story.append(Spacer(1, 0.1 * inch))

    rows = [
        [th("Death Cause"),    th("Artificial Arena"), th("Natural Arena"),    th("Explanation")],
        [td("Natural Causes"), tdc("×0.60 (less likely)"), tdc("×1.50 (more likely)"), td("Exposure, starvation, and hazards are common in wilderness arenas")],
        [td("Mutt"),           tdc("×0.80 (less likely)"), tdc("×1.25 (more likely)"), td("Natural arenas have wildlife and creature threats")],
        [td("Another Tribute"), tdc("×1.00 (unchanged)"), tdc("×1.00 (unchanged)"),   td("Tribute-on-tribute combat is arena-neutral")],
        [td("Gamemakers"),     tdc("×1.50 (more likely)"), tdc("×0.60 (less likely)"), td("Tech traps and constructed hazards dominate artificial arenas")],
    ]
    col_w = [1.4 * inch, 1.4 * inch, 1.4 * inch, 2.3 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)

    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Quick-reference ───────────────────────────────────────────────────────────

def build_quickref():
    story = []
    story.append(Paragraph("Quick Reference — Admin Controls", SECTION_HEADER))
    story.append(divider())
    story.append(Paragraph(
        "Everything below can be changed without touching code. "
        "Odds recalculate automatically on the next trigger after any change.",
        BODY))

    story.append(Paragraph("Influence Strength Knobs", SUBHEADER))
    rows = [
        [th("Setting"), th("Current Value"), th("Where to Change"), th("What It Does")],
        [td("District peer influence"),   tdc("10%"), td("bot/odds/defaults.py → _DISTRICT_ALPHA"),  td("How much alive districtmates shift your odds")],
        [td("Alliance peer influence"),   tdc("20%"), td("bot/odds/defaults.py → _ALLIANCE_ALPHA"),  td("How much allies' training strength (vs. field avg) shifts your odds; weak allies penalise")],
        [td("Alliance factor bleed"),     tdc("20%"), td("bot/odds/defaults.py → ALLIANCE_FACTOR_ALPHA"), td("How much allies' history/bloodbath/modifiers (vs. field avg) shift your odds; weak allies penalise")],
        [td("District history impact"),   tdc("15%"), td("bot/odds/defaults.py → HIST_ALPHA"),       td("Max effect of district historical stats")],
    ]
    col_w = [1.7 * inch, 1.1 * inch, 2.2 * inch, 1.5 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)

    story.append(Paragraph("In-Bot Admin Commands", SUBHEADER))
    rows = [
        [th("Command"),                          th("What It Controls")],
        [td("/admin modifier create/assign"),     td("Create and attach custom multipliers to tributes or districts")],
        [td("/admin tribute add/edit"),           td("Link a Discord member for seniority; set seniority_date override")],
        [td("/admin game arena"),                 td("Set arena type (Artificial / Natural) to shift Death Cause odds")],
        [td("/admin history set"),                td("Enter district historical stats that feed into Layer D")],
        [td("/admin market odds"),                td("Override odds on a specific market — freezes it from recalculation")],
        [td("/admin game start"),                 td("Opens Pre-Games markets and triggers a full odds recalculation")],
    ]
    col_w = [2.4 * inch, 4.1 * inch]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(base_table_style())
    story.append(t)

    story.append(Spacer(1, 0.2 * inch))
    return story


# ── Build ─────────────────────────────────────────────────────────────────────

def build():
    out = "ODDS_GUIDE.pdf"
    doc = SimpleDocTemplate(
        out,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="Panem Sportsbook — Odds Guide",
        author="The Capitol",
    )

    story = []
    story += build_cover()
    story += build_pipeline()
    story += build_stage1()
    story += build_stage2()
    story += build_stage3()
    story += build_stage4()
    story += build_quickref()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"Written: {out}")


if __name__ == "__main__":
    build()

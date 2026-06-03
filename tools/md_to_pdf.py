#!/usr/bin/env python3
"""Convert SPORTSBOOK_GUIDE.md to PDF using reportlab."""

import re
import sys
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, ListFlowable, ListItem
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

GOLD = colors.HexColor("#C9A84C")
DARK = colors.HexColor("#1A1A1A")
LIGHT_GRAY = colors.HexColor("#CCCCCC")
MID_GRAY = colors.HexColor("#888888")
WHITE = colors.white


def build_styles():
    base = getSampleStyleSheet()

    title = ParagraphStyle(
        "DocTitle",
        fontName="Helvetica-Bold",
        fontSize=26,
        leading=32,
        textColor=GOLD,
        spaceAfter=6,
        alignment=TA_CENTER,
    )
    subtitle = ParagraphStyle(
        "DocSubtitle",
        fontName="Helvetica-Oblique",
        fontSize=13,
        leading=18,
        textColor=LIGHT_GRAY,
        spaceAfter=18,
        alignment=TA_CENTER,
    )
    h2 = ParagraphStyle(
        "H2",
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=20,
        textColor=GOLD,
        spaceBefore=18,
        spaceAfter=6,
    )
    h3 = ParagraphStyle(
        "H3",
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=WHITE,
        spaceBefore=10,
        spaceAfter=4,
    )
    body = ParagraphStyle(
        "Body",
        fontName="Helvetica",
        fontSize=10,
        leading=15,
        textColor=LIGHT_GRAY,
        spaceAfter=6,
    )
    bullet_item = ParagraphStyle(
        "BulletItem",
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=LIGHT_GRAY,
        leftIndent=0,
        spaceAfter=2,
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "h2": h2,
        "h3": h3,
        "body": body,
        "bullet": bullet_item,
    }


def inline_format(text: str) -> str:
    """Convert **bold** and `code` inline markdown to reportlab XML."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<font name='Helvetica-Bold' color='#C9A84C'>\1</font>", text)
    return text


def parse_md(md_text: str, styles: dict) -> list:
    story = []
    lines = md_text.splitlines()
    i = 0
    bullet_buffer = []

    def flush_bullets():
        if not bullet_buffer:
            return
        items = [
            ListItem(
                Paragraph(inline_format(b), styles["bullet"]),
                bulletColor=GOLD,
                bulletFontName="Helvetica-Bold",
            )
            for b in bullet_buffer
        ]
        story.append(
            ListFlowable(
                items,
                bulletType="bullet",
                leftIndent=16,
                bulletFontSize=8,
                spaceAfter=6,
            )
        )
        bullet_buffer.clear()

    while i < len(lines):
        line = lines[i]

        # Title (h1)
        if line.startswith("# "):
            flush_bullets()
            parts = line[2:].split(" — ", 1)
            story.append(Paragraph(parts[0], styles["title"]))
            if len(parts) > 1:
                story.append(Paragraph(parts[1], styles["subtitle"]))
            i += 1
            continue

        # H2
        if line.startswith("## "):
            flush_bullets()
            story.append(Paragraph(inline_format(line[3:]), styles["h2"]))
            story.append(HRFlowable(width="100%", thickness=0.5, color=GOLD, spaceAfter=4))
            i += 1
            continue

        # H3
        if line.startswith("### "):
            flush_bullets()
            story.append(Paragraph(inline_format(line[4:]), styles["h3"]))
            i += 1
            continue

        # HR
        if line.strip() == "---":
            flush_bullets()
            story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceBefore=6, spaceAfter=6))
            i += 1
            continue

        # Bullet
        if line.startswith("- "):
            bullet_buffer.append(line[2:].strip())
            i += 1
            continue

        # Blank line
        if line.strip() == "":
            flush_bullets()
            i += 1
            continue

        # Normal paragraph
        flush_bullets()
        story.append(Paragraph(inline_format(line.strip()), styles["body"]))
        i += 1

    flush_bullets()
    return story


def main():
    repo_root = Path(__file__).parent.parent
    src = repo_root / "SPORTSBOOK_GUIDE.md"
    out = repo_root / "SPORTSBOOK_GUIDE.pdf"

    md_text = src.read_text(encoding="utf-8")
    styles = build_styles()

    doc = SimpleDocTemplate(
        str(out),
        pagesize=LETTER,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title="THG Sportsbook — Server Guide",
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
        canvas.drawCentredString(LETTER[0] / 2, 0.25 * inch, f"Page {doc.page}")
        canvas.restoreState()

    story = parse_md(md_text, styles)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"PDF written to: {out}")


if __name__ == "__main__":
    main()

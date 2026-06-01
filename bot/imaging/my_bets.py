from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, cinzel, cinzel_regular, rajdhani, rajdhani_bold,
    draw_rounded_rect, draw_text_centered, draw_text_right, odds_color, status_color,
)
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_pct
from bot.odds.calculator import implied_probability

WIDTH = 960
PAD = 20
HEADER_H = 72
ROW_H = 44
PARLAY_HEADER_H = 40
LEG_ROW_H = 36
PARLAY_FOOTER_H = 38
SECTION_LABEL_H = 32
FOOTER_H = 52
COL_WIDTHS = [320, 110, 90, 120, 110]  # Market | Wager | Odds | Payout | Status


@dataclass
class BetRowData:
    bet_id: int
    market_label: str
    wager: int
    odds: int
    payout: int
    status: str


@dataclass
class ParlayData:
    parlay_id: int
    total_wager: int
    total_payout: int
    combined_odds: int
    status: str
    legs: list[BetRowData] = field(default_factory=list)


def _col_x(col: int) -> int:
    x = PAD
    for i in range(col):
        x += COL_WIDTHS[i]
    return x


def _draw_header(draw: ImageDraw.ImageDraw, username: str, chips: int) -> None:
    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=COLORS["header_dark"])
    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=COLORS["card_border"])

    title_font = cinzel(20)
    sub_font = rajdhani(13)
    draw.text((PAD, 14), "MY BETS", font=title_font, fill=COLORS["header_gold"])
    draw.text((PAD, 44), f"@{username}", font=sub_font, fill=COLORS["text_dim"])

    bal_val = cinzel(18)
    bal_lbl = rajdhani(12)
    draw_text_right(draw, "BALANCE", bal_lbl, COLORS["text_dim"], WIDTH - PAD, 16)
    draw_text_right(draw, fmt_chips(chips), bal_val, COLORS["header_gold"], WIDTH - PAD, 36)


def _draw_table_header(draw: ImageDraw.ImageDraw, y: int) -> None:
    draw.rectangle((0, y, WIDTH, y + ROW_H - 4), fill=COLORS["bg_mid"])
    draw.rectangle((0, y + ROW_H - 4, WIDTH, y + ROW_H), fill=COLORS["divider"])

    headers = ["MARKET", "WAGER", "ODDS", "PAYOUT", "STATUS"]
    col_font = rajdhani_bold(13)
    for i, h in enumerate(headers):
        x = _col_x(i) + 6
        draw.text((x, y + 12), h, font=col_font, fill=COLORS["text_dim"])


def _draw_bet_row(draw: ImageDraw.ImageDraw, y: int, bet: BetRowData, alt: bool) -> None:
    row_fill = COLORS["row_alt"] if alt else COLORS["card_bg"]
    draw.rectangle((0, y, WIDTH, y + ROW_H - 1), fill=row_fill)
    draw.rectangle((0, y + ROW_H - 1, WIDTH, y + ROW_H), fill=COLORS["divider"])

    label_font = rajdhani(15)
    val_font = rajdhani_bold(15)
    odds_font = rajdhani_bold(15)
    status_font = rajdhani_bold(14)

    # Market label
    label = bet.market_label if len(bet.market_label) <= 38 else bet.market_label[:36] + "…"
    draw.text((_col_x(0) + 6, y + (ROW_H - 18) // 2), label, font=label_font, fill=COLORS["text_white"])

    # Wager
    draw.text((_col_x(1) + 6, y + (ROW_H - 18) // 2), fmt_chips(bet.wager).replace(" chips", ""), font=val_font, fill=COLORS["text_white"])

    # Odds
    oc = odds_color(bet.odds)
    draw.text((_col_x(2) + 6, y + (ROW_H - 18) // 2), fmt_odds(bet.odds), font=odds_font, fill=oc)

    # Payout
    draw.text((_col_x(3) + 6, y + (ROW_H - 18) // 2), fmt_chips(bet.payout).replace(" chips", ""), font=val_font, fill=COLORS["text_white"])

    # Status pill
    sc = status_color(bet.status)
    px = _col_x(4) + 4
    pill_w = COL_WIDTHS[4] - 10
    draw_rounded_rect(draw, (px, y + 8, px + pill_w, y + ROW_H - 8), 8,
                      fill=(*sc[:3], 160), outline=sc, outline_width=1)
    draw_text_centered(draw, bet.status, status_font, (255, 255, 255, 255), px + pill_w // 2, y + 13)


def _draw_parlay_block(draw: ImageDraw.ImageDraw, y: int, p: ParlayData) -> int:
    sc = status_color(p.status)

    # Parlay header bar
    draw_rounded_rect(draw, (PAD, y, WIDTH - PAD, y + PARLAY_HEADER_H), 8,
                      fill=COLORS["header_dark"],
                      outline=COLORS["card_border"],
                      outline_width=2)

    hf = rajdhani_bold(15)
    sf = rajdhani_bold(13)
    draw.text((PAD + 10, y + 10), f"PARLAY #{p.parlay_id}", font=hf, fill=COLORS["header_gold"])
    legs_label = f"{len(p.legs)}-LEG PARLAY"
    draw_text_centered(draw, legs_label, sf, COLORS["text_dim"], WIDTH // 2, y + 12)
    draw_rounded_rect(draw, (WIDTH - PAD - 100, y + 8, WIDTH - PAD - 4, y + PARLAY_HEADER_H - 8), 6,
                      fill=(*sc[:3], 160), outline=sc, outline_width=1)
    draw_text_centered(draw, p.status, sf, (255, 255, 255, 255), WIDTH - PAD - 52, y + 12)
    y += PARLAY_HEADER_H + 2

    # Leg rows
    leg_font = rajdhani(14)
    odds_font = rajdhani_bold(14)
    for i, leg in enumerate(p.legs):
        row_fill = COLORS["row_alt"] if i % 2 == 0 else COLORS["card_bg"]
        draw.rectangle((PAD, y, WIDTH - PAD, y + LEG_ROW_H - 1), fill=row_fill)
        draw.text((PAD + 24, y + (LEG_ROW_H - 16) // 2), f"  └  {leg.market_label}", font=leg_font, fill=COLORS["text_dim"])
        oc = odds_color(leg.odds)
        draw_text_right(draw, fmt_odds(leg.odds), odds_font, oc, WIDTH - PAD - 10, y + (LEG_ROW_H - 18) // 2)
        y += LEG_ROW_H

    # Parlay summary footer
    draw.rectangle((PAD, y, WIDTH - PAD, y + PARLAY_FOOTER_H), fill=COLORS["bg_mid"])
    draw.rectangle((PAD, y, WIDTH - PAD, y + 2), fill=COLORS["card_border"])
    draw.rectangle((PAD, y + PARLAY_FOOTER_H - 2, WIDTH - PAD, y + PARLAY_FOOTER_H), fill=COLORS["divider"])

    pf = rajdhani_bold(14)
    draw.text((PAD + 10, y + 10), f"WAGER  {fmt_chips(p.total_wager)}", font=pf, fill=COLORS["text_white"])
    combo_str = f"COMBINED  {fmt_odds(p.combined_odds)}"
    oc = odds_color(p.combined_odds)
    draw_text_centered(draw, combo_str, pf, oc, WIDTH // 2, y + 10)
    draw_text_right(draw, f"PAYOUT  {fmt_chips(p.total_payout)}", pf, COLORS["header_gold"], WIDTH - PAD - 10, y + 10)

    y += PARLAY_FOOTER_H + PAD
    return y


def _draw_section_label(draw: ImageDraw.ImageDraw, y: int, text: str) -> None:
    draw.rectangle((0, y, WIDTH, y + SECTION_LABEL_H), fill=COLORS["bg_mid"])
    draw.rectangle((0, y + SECTION_LABEL_H - 2, WIDTH, y + SECTION_LABEL_H), fill=COLORS["divider"])
    font = cinzel_regular(12)
    draw.text((PAD, y + 8), text, font=font, fill=COLORS["text_gold"])


def _draw_footer(draw: ImageDraw.ImageDraw, y: int, total_wagered: int, total_potential: int, chips: int) -> None:
    draw.rectangle((0, y, WIDTH, y + FOOTER_H), fill=COLORS["header_dark"])
    draw.rectangle((0, y, WIDTH, y + 3), fill=COLORS["card_border"])

    f = rajdhani_bold(14)
    lf = rajdhani(13)
    draw.text((PAD, y + 8), "TOTAL WAGERED", font=lf, fill=COLORS["text_dim"])
    draw.text((PAD, y + 28), fmt_chips(total_wagered), font=f, fill=COLORS["text_white"])

    draw_text_centered(draw, "TOTAL POTENTIAL", lf, COLORS["text_dim"], WIDTH // 2, y + 8)
    draw_text_centered(draw, fmt_chips(total_potential), f, COLORS["header_gold"], WIDTH // 2, y + 28)

    draw_text_right(draw, "CURRENT BALANCE", lf, COLORS["text_dim"], WIDTH - PAD, y + 8)
    draw_text_right(draw, fmt_chips(chips), f, COLORS["header_gold"], WIDTH - PAD, y + 28)


def render_my_bets(
    username: str,
    chips: int,
    straight_bets: list[BetRowData],
    parlays: list[ParlayData],
    filter_status: str = "ALL",
) -> io.BytesIO:
    # Calculate dynamic height
    h = HEADER_H
    if straight_bets:
        h += SECTION_LABEL_H + ROW_H + len(straight_bets) * ROW_H
    if parlays:
        h += SECTION_LABEL_H
        for p in parlays:
            h += PARLAY_HEADER_H + 2 + len(p.legs) * LEG_ROW_H + PARLAY_FOOTER_H + PAD
    h += FOOTER_H + PAD * 2

    total_wagered = sum(b.wager for b in straight_bets)
    total_potential = sum(b.payout for b in straight_bets if b.status == "PENDING")
    for p in parlays:
        total_wagered += p.total_wager
        if p.status == "PENDING":
            total_potential += p.total_payout

    img = Image.new("RGBA", (WIDTH, h), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    for gx in range(0, WIDTH, 40):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    _draw_header(draw, username, chips)
    cur_y = HEADER_H

    if straight_bets:
        _draw_section_label(draw, cur_y, "STRAIGHT BETS")
        cur_y += SECTION_LABEL_H
        _draw_table_header(draw, cur_y)
        cur_y += ROW_H
        for i, bet in enumerate(straight_bets):
            _draw_bet_row(draw, cur_y, bet, i % 2 == 0)
            cur_y += ROW_H

    if parlays:
        _draw_section_label(draw, cur_y, "PARLAYS")
        cur_y += SECTION_LABEL_H + PAD // 2
        for p in parlays:
            cur_y = _draw_parlay_block(draw, cur_y, p)

    if not straight_bets and not parlays:
        no_bets_font = rajdhani(18)
        draw_text_centered(draw, "No bets found.", no_bets_font, COLORS["text_dim"], WIDTH // 2, HEADER_H + 60)
        cur_y = HEADER_H + 120

    _draw_footer(draw, max(cur_y, h - FOOTER_H), total_wagered, total_potential, chips)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

from __future__ import annotations

import io
import math
import textwrap
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, Theme, get_active_theme,
    cinzel, cinzel_regular, rajdhani, rajdhani_bold,
    draw_rounded_rect, draw_text_centered, draw_text_right, paste_logo, odds_color, status_color,
)
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_pct, fmt_odds_with_mult
from bot.odds.calculator import implied_probability, parlay_payout, combined_american

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


def _draw_header(draw: ImageDraw.ImageDraw, img: Image.Image, username: str, chips: int, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=c["header_dark"])
    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=c["card_border"])

    logo_size = 44
    paste_logo(img, 8, (HEADER_H - logo_size) // 2, logo_size)

    title_font = cinzel(20)
    sub_font = rajdhani(13)
    text_x = 8 + logo_size + 8
    draw.text((text_x, 14), "MY BETS", font=title_font, fill=c["header_gold"])
    draw.text((text_x, 44), f"@{username}", font=sub_font, fill=c["text_dim"])

    bal_val = cinzel(18)
    bal_lbl = rajdhani(12)
    draw_text_right(draw, "BALANCE", bal_lbl, c["text_dim"], WIDTH - PAD, 16)
    draw_text_right(draw, fmt_chips(chips), bal_val, c["header_gold"], WIDTH - PAD, 36)


def _draw_table_header(draw: ImageDraw.ImageDraw, y: int, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, y, WIDTH, y + ROW_H - 4), fill=c["bg_mid"])
    draw.rectangle((0, y + ROW_H - 4, WIDTH, y + ROW_H), fill=c["divider"])

    headers = ["MARKET", "WAGER", "ODDS", "PAYOUT", "STATUS"]
    col_font = rajdhani_bold(13)
    for i, h in enumerate(headers):
        x = _col_x(i) + 6
        draw.text((x, y + 12), h, font=col_font, fill=c["text_dim"])


def _draw_bet_row(draw: ImageDraw.ImageDraw, y: int, bet: BetRowData, alt: bool, *, colors: dict) -> None:
    c = colors
    row_fill = c["row_alt"] if alt else c["card_bg"]
    draw.rectangle((0, y, WIDTH, y + ROW_H - 1), fill=row_fill)
    draw.rectangle((0, y + ROW_H - 1, WIDTH, y + ROW_H), fill=c["divider"])

    label_font = rajdhani(15)
    val_font = rajdhani_bold(15)
    odds_font = rajdhani_bold(15)
    status_font = rajdhani_bold(14)

    label = bet.market_label if len(bet.market_label) <= 38 else bet.market_label[:36] + "…"
    draw.text((_col_x(0) + 6, y + (ROW_H - 18) // 2), label, font=label_font, fill=c["text_white"])

    draw.text((_col_x(1) + 6, y + (ROW_H - 18) // 2), fmt_chips(bet.wager).replace(" chips", ""), font=val_font, fill=c["text_white"])

    oc = odds_color(bet.odds, c)
    draw.text((_col_x(2) + 6, y + (ROW_H - 18) // 2), fmt_odds(bet.odds), font=odds_font, fill=oc)

    draw.text((_col_x(3) + 6, y + (ROW_H - 18) // 2), fmt_chips(bet.payout).replace(" chips", ""), font=val_font, fill=c["text_white"])

    sc = status_color(bet.status, c)
    px = _col_x(4) + 4
    pill_w = COL_WIDTHS[4] - 10
    draw_rounded_rect(draw, (px, y + 8, px + pill_w, y + ROW_H - 8), 8,
                      fill=(*sc[:3], 160), outline=sc, outline_width=1)
    draw_text_centered(draw, bet.status, status_font, (255, 255, 255, 255), px + pill_w // 2, y + 13)


def _draw_parlay_block(draw: ImageDraw.ImageDraw, y: int, p: ParlayData, *, colors: dict) -> int:
    c = colors
    sc = status_color(p.status, c)

    draw_rounded_rect(draw, (PAD, y, WIDTH - PAD, y + PARLAY_HEADER_H), 8,
                      fill=c["header_dark"],
                      outline=c["card_border"],
                      outline_width=2)

    hf = rajdhani_bold(15)
    sf = rajdhani_bold(13)
    draw.text((PAD + 10, y + 10), f"PARLAY #{p.parlay_id}", font=hf, fill=c["header_gold"])
    legs_label = f"{len(p.legs)}-LEG PARLAY"
    draw_text_centered(draw, legs_label, sf, c["text_dim"], WIDTH // 2, y + 12)
    draw_rounded_rect(draw, (WIDTH - PAD - 100, y + 8, WIDTH - PAD - 4, y + PARLAY_HEADER_H - 8), 6,
                      fill=(*sc[:3], 160), outline=sc, outline_width=1)
    draw_text_centered(draw, p.status, sf, (255, 255, 255, 255), WIDTH - PAD - 52, y + 12)
    y += PARLAY_HEADER_H + 2

    leg_font = rajdhani(14)
    odds_font = rajdhani_bold(14)
    for i, leg in enumerate(p.legs):
        row_fill = c["row_alt"] if i % 2 == 0 else c["card_bg"]
        draw.rectangle((PAD, y, WIDTH - PAD, y + LEG_ROW_H - 1), fill=row_fill)
        draw.text((PAD + 24, y + (LEG_ROW_H - 16) // 2), f"  └  {leg.market_label}", font=leg_font, fill=c["text_dim"])
        oc = odds_color(leg.odds, c)
        draw_text_right(draw, fmt_odds(leg.odds), odds_font, oc, WIDTH - PAD - 10, y + (LEG_ROW_H - 18) // 2)
        y += LEG_ROW_H

    draw.rectangle((PAD, y, WIDTH - PAD, y + PARLAY_FOOTER_H), fill=c["bg_mid"])
    draw.rectangle((PAD, y, WIDTH - PAD, y + 2), fill=c["card_border"])
    draw.rectangle((PAD, y + PARLAY_FOOTER_H - 2, WIDTH - PAD, y + PARLAY_FOOTER_H), fill=c["divider"])

    pf = rajdhani_bold(14)
    draw.text((PAD + 10, y + 10), f"WAGER  {fmt_chips(p.total_wager)}", font=pf, fill=c["text_white"])
    combo_str = f"COMBINED  {fmt_odds(p.combined_odds)}"
    oc = odds_color(p.combined_odds, c)
    draw_text_centered(draw, combo_str, pf, oc, WIDTH // 2, y + 10)
    draw_text_right(draw, f"PAYOUT  {fmt_chips(p.total_payout)}", pf, c["header_gold"], WIDTH - PAD - 10, y + 10)

    y += PARLAY_FOOTER_H + PAD
    return y


def _draw_section_label(draw: ImageDraw.ImageDraw, y: int, text: str, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, y, WIDTH, y + SECTION_LABEL_H), fill=c["bg_mid"])
    draw.rectangle((0, y + SECTION_LABEL_H - 2, WIDTH, y + SECTION_LABEL_H), fill=c["divider"])
    font = cinzel_regular(12)
    draw.text((PAD, y + 8), text, font=font, fill=c["text_gold"])


def _draw_footer(draw: ImageDraw.ImageDraw, y: int, total_wagered: int, total_potential: int, chips: int, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, y, WIDTH, y + FOOTER_H), fill=c["header_dark"])
    draw.rectangle((0, y, WIDTH, y + 3), fill=c["card_border"])

    f = rajdhani_bold(14)
    lf = rajdhani(13)
    draw.text((PAD, y + 8), "TOTAL WAGERED", font=lf, fill=c["text_dim"])
    draw.text((PAD, y + 28), fmt_chips(total_wagered), font=f, fill=c["text_white"])

    draw_text_centered(draw, "TOTAL POTENTIAL", lf, c["text_dim"], WIDTH // 2, y + 8)
    draw_text_centered(draw, fmt_chips(total_potential), f, c["header_gold"], WIDTH // 2, y + 28)

    draw_text_right(draw, "CURRENT BALANCE", lf, c["text_dim"], WIDTH - PAD, y + 8)
    draw_text_right(draw, fmt_chips(chips), f, c["header_gold"], WIDTH - PAD, y + 28)


def render_my_bets(
    username: str,
    chips: int,
    straight_bets: list[BetRowData],
    parlays: list[ParlayData],
    filter_status: str = "ALL",
) -> io.BytesIO:
    theme = get_active_theme()
    c = theme.colors

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

    img = Image.new("RGBA", (WIDTH, h), c["bg"])
    theme.draw_bg(img, WIDTH, h)
    draw = ImageDraw.Draw(img)

    for gx in range(0, WIDTH, 40):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    _draw_header(draw, img, username, chips, colors=c)
    cur_y = HEADER_H

    if straight_bets:
        _draw_section_label(draw, cur_y, "STRAIGHT BETS", colors=c)
        cur_y += SECTION_LABEL_H
        _draw_table_header(draw, cur_y, colors=c)
        cur_y += ROW_H
        for i, bet in enumerate(straight_bets):
            _draw_bet_row(draw, cur_y, bet, i % 2 == 0, colors=c)
            cur_y += ROW_H

    if parlays:
        _draw_section_label(draw, cur_y, "PARLAYS", colors=c)
        cur_y += SECTION_LABEL_H + PAD // 2
        for p in parlays:
            cur_y = _draw_parlay_block(draw, cur_y, p, colors=c)

    if not straight_bets and not parlays:
        no_bets_font = rajdhani(18)
        draw_text_centered(draw, "No bets found.", no_bets_font, c["text_dim"], WIDTH // 2, HEADER_H + 60)
        cur_y = HEADER_H + 120

    _draw_footer(draw, max(cur_y, h - FOOTER_H), total_wagered, total_potential, chips, colors=c)

    theme.draw_fg(img, WIDTH, h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def render_tail_board(
    featured: list[dict],
    member: list[dict],
) -> io.BytesIO:
    """Render a preview board of all available parlays to tail."""
    theme = get_active_theme()
    c = theme.colors

    # Convert tail dictionaries to ParlayData for rendering
    parlay_data: list[ParlayData] = []
    
    # Combined list for indexing
    all_entries = featured + member
    
    for i, e in enumerate(all_entries):
        legs = [
            BetRowData(0, lbl, 0, odds, 0, "OPEN")
            for lbl, odds in zip(e["labels"], e["odds_list"])
        ]
        p_data = ParlayData(
            parlay_id=i + 1,
            total_wager=100,
            total_payout=parlay_payout(100, e["odds_list"]),
            combined_odds=combined_american(e["odds_list"]),
            status=e["tag"],
            legs=legs
        )
        parlay_data.append(p_data)

    SUB_ROW_H = 26

    h = HEADER_H + SECTION_LABEL_H + PAD
    for idx, p in enumerate(parlay_data):
        sub = all_entries[idx]["sub"]
        h += PARLAY_HEADER_H + 2 + (SUB_ROW_H if sub else 0) + len(p.legs) * LEG_ROW_H + PARLAY_FOOTER_H + PAD
    h += FOOTER_H + PAD

    img = Image.new("RGBA", (WIDTH, h), c["bg"])
    theme.draw_bg(img, WIDTH, h)
    draw = ImageDraw.Draw(img)

    for gx in range(0, WIDTH, 40):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=c["header_dark"])
    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=c["card_border"])
    paste_logo(img, 8, (HEADER_H - 44) // 2, 44)
    title_font = cinzel(20)
    sub_font = rajdhani(13)
    draw_text_centered(draw, "TAILABLE PARLAYS", title_font, c["header_gold"], WIDTH // 2, 20)
    draw_text_centered(draw, "COPY FEATURED SLIPS AT LIVE ODDS", sub_font, c["text_dim"], WIDTH // 2, 48)

    cur_y = HEADER_H
    _draw_section_label(draw, cur_y, "FEATURED & MEMBER SLIPS", colors=c)
    cur_y += SECTION_LABEL_H + PAD

    for idx, p in enumerate(parlay_data):
        sc = status_color("WON", c) if p.status in ("SAFE", "BALANCED", "LONGSHOT") else c["text_gold"]
        display_name = all_entries[idx]["name"]
        sub = all_entries[idx]["sub"]

        draw_rounded_rect(draw, (PAD, cur_y, WIDTH - PAD, cur_y + PARLAY_HEADER_H), 8,
                          fill=c["header_dark"],
                          outline=c["card_border"],
                          outline_width=2)

        hf = rajdhani_bold(15)
        sf = rajdhani_bold(13)
        draw.text((PAD + 10, cur_y + 10), display_name.upper(), font=hf, fill=c["header_gold"])

        tag_label = p.status
        draw_text_centered(draw, tag_label, sf, c["text_dim"], WIDTH // 2, cur_y + 12)

        # Combined odds visible in header right side
        draw_text_right(draw, fmt_odds_with_mult(p.combined_odds), sf,
                        odds_color(p.combined_odds, c), WIDTH - PAD - 10, cur_y + 12)

        y_leg = cur_y + PARLAY_HEADER_H + 2

        if sub:
            draw.rectangle((PAD, y_leg, WIDTH - PAD, y_leg + SUB_ROW_H - 1), fill=c["bg_mid"])
            draw.text((PAD + 14, y_leg + 5), sub[:110], font=rajdhani(13), fill=c["text_dim"])
            y_leg += SUB_ROW_H
        leg_font = rajdhani(14)
        odds_font = rajdhani_bold(14)
        for i, leg in enumerate(p.legs):
            row_fill = c["row_alt"] if i % 2 == 0 else c["card_bg"]
            draw.rectangle((PAD, y_leg, WIDTH - PAD, y_leg + LEG_ROW_H - 1), fill=row_fill)
            draw.text((PAD + 24, y_leg + (LEG_ROW_H - 16) // 2), f"  └  {leg.market_label}", font=leg_font, fill=c["text_dim"])
            oc = odds_color(leg.odds, c)
            draw_text_right(draw, fmt_odds(leg.odds), odds_font, oc, WIDTH - PAD - 10, y_leg + (LEG_ROW_H - 18) // 2)
            y_leg += LEG_ROW_H

        draw.rectangle((PAD, y_leg, WIDTH - PAD, y_leg + PARLAY_FOOTER_H), fill=c["bg_mid"])
        draw.rectangle((PAD, y_leg, WIDTH - PAD, y_leg + 2), fill=c["card_border"])
        draw.rectangle((PAD, y_leg + PARLAY_FOOTER_H - 2, WIDTH - PAD, y_leg + PARLAY_FOOTER_H), fill=c["divider"])

        pf = rajdhani_bold(14)
        draw.text((PAD + 10, y_leg + 10), f"100 CHIPS", font=pf, fill=c["text_white"])
        combo_str = f"TOTAL ODDS: {fmt_odds_with_mult(p.combined_odds)}"
        oc = odds_color(p.combined_odds, c)
        draw_text_centered(draw, combo_str, pf, oc, WIDTH // 2, y_leg + 10)
        draw_text_right(draw, f"PAYS {fmt_chips(p.total_payout)}", pf, c["header_gold"], WIDTH - PAD - 10, y_leg + 10)

        cur_y = y_leg + PARLAY_FOOTER_H + PAD

    # Footer
    draw.rectangle((0, h - FOOTER_H, WIDTH, h), fill=c["header_dark"])
    draw.rectangle((0, h - FOOTER_H, WIDTH, h - FOOTER_H + 3), fill=c["card_border"])
    draw_text_centered(draw, "USE /parlay tail TO BROWSE AND BET", rajdhani_bold(14), c["text_dim"], WIDTH // 2, h - FOOTER_H + 18)

    theme.draw_fg(img, WIDTH, h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def render_tail_detail(
    entry: dict,
) -> io.BytesIO:
    """Render a high-quality visual slip for a single tailed parlay."""
    theme = get_active_theme()
    c = theme.colors

    # Prep data
    odds_list = entry["odds_list"]
    labels = entry["labels"]
    combined = combined_american(odds_list)
    payout = parlay_payout(100, odds_list)
    tag = entry["tag"]
    name = entry["name"]
    sub = entry["sub"]

    sub_lines = textwrap.wrap(sub, width=88) if sub else []
    SUB_LINE_H = 20
    SUB_PAD = 14

    n_legs = len(odds_list)
    h = HEADER_H + 20 + n_legs * LEG_ROW_H + PARLAY_FOOTER_H + FOOTER_H + 40
    if sub_lines:
        h += len(sub_lines) * SUB_LINE_H + SUB_PAD

    img = Image.new("RGBA", (WIDTH, h), c["bg"])
    theme.draw_bg(img, WIDTH, h)
    draw = ImageDraw.Draw(img)

    for gx in range(0, WIDTH, 40):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=c["header_dark"])
    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=c["card_border"])
    paste_logo(img, 8, (HEADER_H - 44) // 2, 44)
    title_font = cinzel(20)
    sub_font = rajdhani(13)
    draw_text_centered(draw, "PARLAY PREVIEW", title_font, c["header_gold"], WIDTH // 2, 20)
    draw_text_centered(draw, "LIVE ODDS AT THE TIME OF VIEWING", sub_font, c["text_dim"], WIDTH // 2, 48)

    cur_y = HEADER_H + 20
    
    # Slip Block
    draw_rounded_rect(draw, (PAD, cur_y, WIDTH - PAD, cur_y + PARLAY_HEADER_H), 8,
                      fill=c["header_dark"], outline=c["card_border"], outline_width=2)
    
    hf = rajdhani_bold(17)
    sf = rajdhani_bold(14)
    draw.text((PAD + 14, cur_y + 10), name.upper(), font=hf, fill=c["header_gold"])
    draw_text_right(draw, tag, sf, c["text_dim"], WIDTH - PAD - 14, cur_y + 12)
    
    cur_y += PARLAY_HEADER_H + 2
    
    if sub_lines:
        sub_block_h = len(sub_lines) * SUB_LINE_H + SUB_PAD
        draw.rectangle((PAD, cur_y, WIDTH - PAD, cur_y + sub_block_h), fill=c["bg_mid"])
        for li, line in enumerate(sub_lines):
            draw.text((PAD + 24, cur_y + 7 + li * SUB_LINE_H), line, font=rajdhani(14), fill=c["text_white"])
        cur_y += sub_block_h + 2

    leg_font = rajdhani(16)
    odds_font = rajdhani_bold(16)
    for i, (lbl, odds) in enumerate(zip(labels, odds_list)):
        row_fill = c["row_alt"] if i % 2 == 0 else c["card_bg"]
        draw.rectangle((PAD, cur_y, WIDTH - PAD, cur_y + LEG_ROW_H - 1), fill=row_fill)
        draw.text((PAD + 24, cur_y + (LEG_ROW_H - 20) // 2), f"  └  {lbl}", font=leg_font, fill=c["text_white"])
        oc = odds_color(odds, c)
        draw_text_right(draw, fmt_odds(odds), odds_font, oc, WIDTH - PAD - 10, cur_y + (LEG_ROW_H - 20) // 2)
        cur_y += LEG_ROW_H

    draw.rectangle((PAD, cur_y, WIDTH - PAD, cur_y + PARLAY_FOOTER_H + 10), fill=c["bg_mid"])
    draw.rectangle((PAD, cur_y, WIDTH - PAD, cur_y + 2), fill=c["card_border"])
    
    pf = rajdhani_bold(16)
    draw.text((PAD + 14, cur_y + 14), "100 CHIPS", font=pf, fill=c["text_white"])
    combo_str = f"TOTAL ODDS: {fmt_odds_with_mult(combined)}"
    oc = odds_color(combined, c)
    draw_text_centered(draw, combo_str, pf, oc, WIDTH // 2, cur_y + 14)
    draw_text_right(draw, f"PAYS {fmt_chips(payout)}", pf, c["header_gold"], WIDTH - PAD - 14, cur_y + 14)
    
    cur_y += PARLAY_FOOTER_H + 30

    # Footer
    draw.rectangle((0, h - FOOTER_H, WIDTH, h), fill=c["header_dark"])
    draw.rectangle((0, h - FOOTER_H, WIDTH, h - FOOTER_H + 3), fill=c["card_border"])
    draw_text_centered(draw, "USE TAIL & BET TO WAGER NOW", rajdhani_bold(14), c["text_dim"], WIDTH // 2, h - FOOTER_H + 18)

    theme.draw_fg(img, WIDTH, h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, cinzel, cinzel_regular, rajdhani, rajdhani_bold,
    draw_rounded_rect, draw_text_centered, draw_text_right, odds_color,
)
from bot.odds.calculator import combined_american
from bot.utils.formatters import fmt_odds, fmt_chips

WIDTH = 800
PAD = 32
HEADER_H = 100
LEG_H = 48
SUMMARY_H = 140
FOOTER_H = 50


@dataclass
class ParlayLegData:
    leg_num: int
    market_label: str
    odds: int


def _draw_ticket_border(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    draw_rounded_rect(draw, (4, 4, w - 4, h - 4), 16,
                      fill=COLORS["card_bg"],
                      outline=COLORS["card_border"],
                      outline_width=3)
    # Inner shadow line
    draw_rounded_rect(draw, (10, 10, w - 10, h - 10), 12,
                      fill=None,
                      outline=(*COLORS["card_border"][:3], 60),
                      outline_width=1)


def _draw_header(draw: ImageDraw.ImageDraw, w: int, submitted: bool) -> None:
    # Gold header band
    draw_rounded_rect(draw, (4, 4, w - 4, HEADER_H), 14,
                      fill=COLORS["header_dark"],
                      outline=None)
    draw.rectangle((4, HEADER_H - 4, w - 4, HEADER_H), fill=COLORS["header_dark"])
    draw.rectangle((4, HEADER_H, w - 4, HEADER_H + 3), fill=COLORS["card_border"])

    title_font = cinzel(24)
    sub_font = rajdhani(14)
    draw_text_centered(draw, "CAPITOL SPORTSBOOK", title_font, COLORS["header_gold"], w // 2, 20)
    draw_text_centered(draw, "PARLAY BET SLIP", sub_font, COLORS["text_dim"], w // 2, 58)


def _draw_legs(draw: ImageDraw.ImageDraw, legs: list[ParlayLegData], y_start: int, w: int) -> int:
    leg_font = rajdhani(16)
    num_font = rajdhani_bold(14)
    odds_font = rajdhani_bold(16)
    sep_font = rajdhani(12)

    cur_y = y_start
    for i, leg in enumerate(legs):
        row_fill = COLORS["row_alt"] if i % 2 == 0 else COLORS["card_bg"]
        draw.rectangle((PAD, cur_y, w - PAD, cur_y + LEG_H - 2), fill=row_fill)

        # Leg number badge
        draw_rounded_rect(draw, (PAD + 4, cur_y + 10, PAD + 28, cur_y + LEG_H - 10), 6,
                          fill=COLORS["header_dark"],
                          outline=COLORS["card_border"],
                          outline_width=1)
        draw_text_centered(draw, str(leg.leg_num), num_font, COLORS["header_gold"], PAD + 16, cur_y + 14)

        # Market label
        label = leg.market_label if len(leg.market_label) <= 44 else leg.market_label[:42] + "…"
        draw.text((PAD + 36, cur_y + (LEG_H - 20) // 2), label, font=leg_font, fill=COLORS["text_white"])

        # Odds right-aligned
        oc = odds_color(leg.odds)
        odds_str = fmt_odds(leg.odds)
        draw_text_right(draw, odds_str, odds_font, oc, w - PAD - 8, cur_y + (LEG_H - 20) // 2)

        cur_y += LEG_H

    # Dashed gold divider
    dash_y = cur_y + 8
    for dx in range(PAD, w - PAD, 14):
        draw.rectangle((dx, dash_y, dx + 8, dash_y + 2), fill=COLORS["card_border"])
    return dash_y + 16


def _draw_summary(
    draw: ImageDraw.ImageDraw,
    legs: list[ParlayLegData],
    wager: int,
    payout: int,
    w: int,
    y_start: int,
) -> None:
    all_odds = [l.odds for l in legs]
    combo = combined_american(all_odds) if all_odds else 0

    label_font = rajdhani(15)
    val_font = rajdhani_bold(17)
    big_font = cinzel(22)
    big_sub = rajdhani(13)

    row_y = y_start

    # Combined odds
    draw.text((PAD, row_y), "COMBINED ODDS", font=label_font, fill=COLORS["text_dim"])
    combo_str = fmt_odds(combo)
    oc = odds_color(combo)
    draw_text_right(draw, combo_str, val_font, oc, w - PAD, row_y)
    row_y += 28

    # Wager
    draw.text((PAD, row_y), "WAGER", font=label_font, fill=COLORS["text_dim"])
    draw_text_right(draw, fmt_chips(wager), val_font, COLORS["text_white"], w - PAD, row_y)
    row_y += 28

    # Divider
    draw.rectangle((PAD, row_y, w - PAD, row_y + 1), fill=COLORS["divider"])
    row_y += 12

    # Payout — large gold
    draw.text((PAD, row_y), "POTENTIAL PAYOUT", font=label_font, fill=COLORS["text_dim"])
    draw_text_right(draw, fmt_chips(payout), big_font, COLORS["header_gold"], w - PAD, row_y - 4)


def _draw_status_watermark(
    draw: ImageDraw.ImageDraw,
    w: int,
    h: int,
    submitted: bool,
) -> None:
    status_text = "SUBMITTED" if submitted else "PENDING SUBMISSION"
    color = (*COLORS["won"][:3], 35) if submitted else (*COLORS["header_gold"][:3], 25)

    wm_font = cinzel(36)
    bbox = draw.textbbox((0, 0), status_text, font=wm_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # Create watermark on separate layer and rotate
    wm = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    wm_draw = ImageDraw.Draw(wm)
    wm_draw.text(((w - tw) // 2, (h - th) // 2 + 40), status_text, font=wm_font, fill=color)
    rotated = wm.rotate(-25, expand=False)

    from PIL import Image as PImage
    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    base.paste(rotated, (0, 0), mask=rotated)

    return base


def _draw_barcode(draw: ImageDraw.ImageDraw, w: int, y: int) -> None:
    bar_x = PAD + 20
    bar_y = y
    widths = [2, 1, 3, 1, 2, 1, 1, 3, 2, 1, 3, 1, 2, 2, 1, 3, 1, 2, 1, 3, 2, 1, 1, 2, 3, 1]
    for bw in widths:
        draw.rectangle((bar_x, bar_y, bar_x + bw, bar_y + 28), fill=COLORS["text_dim"])
        bar_x += bw + 2
    barcode_font = rajdhani(10)
    draw_text_centered(draw, "C A P I T O L  S P O R T S B O O K", barcode_font,
                       COLORS["text_dim"], w // 2, bar_y + 32)


def render_parlay_slip(
    legs: list[ParlayLegData],
    wager: int,
    payout: int,
    submitted: bool = False,
) -> io.BytesIO:
    n_legs = max(len(legs), 1)
    content_h = (
        HEADER_H + 12
        + n_legs * LEG_H + 20
        + SUMMARY_H
        + FOOTER_H + 20
    )
    h = max(content_h, 500)

    img = Image.new("RGBA", (WIDTH, h), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    # Subtle bg texture
    for gx in range(0, WIDTH, 30):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    _draw_ticket_border(draw, WIDTH, h)
    _draw_header(draw, WIDTH, submitted)

    cur_y = HEADER_H + 12
    cur_y = _draw_legs(draw, legs, cur_y, WIDTH)
    _draw_summary(draw, legs, wager, payout, WIDTH, cur_y)
    _draw_barcode(draw, WIDTH, h - FOOTER_H - 10)

    # Watermark overlay
    if True:
        wm_layer = _draw_status_watermark(draw, WIDTH, h, submitted)
        if wm_layer is not None:
            img = Image.alpha_composite(img, wm_layer)

    # Status bar at bottom
    status_color = COLORS["won"] if submitted else COLORS["pending"]
    draw_final = ImageDraw.Draw(img)
    draw_rounded_rect(draw_final, (4, h - 36, WIDTH - 4, h - 4), 10,
                      fill=(*status_color[:3], 40),
                      outline=status_color,
                      outline_width=1)
    status_text = "BET SUBMITTED — GOOD LUCK, TRIBUTE!" if submitted else "⚡  PENDING — USE /parlay submit TO LOCK IN"
    sf = rajdhani_bold(14)
    draw_text_centered(draw_final, status_text, sf, status_color, WIDTH // 2, h - 27)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

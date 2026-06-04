from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, Theme, get_active_theme,
    cinzel, cinzel_regular, rajdhani, rajdhani_bold,
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


def _draw_ticket_border(draw: ImageDraw.ImageDraw, w: int, h: int, *, colors: dict) -> None:
    c = colors
    draw_rounded_rect(draw, (4, 4, w - 4, h - 4), 16,
                      fill=c["card_bg"],
                      outline=c["card_border"],
                      outline_width=3)
    draw_rounded_rect(draw, (10, 10, w - 10, h - 10), 12,
                      fill=None,
                      outline=(*c["card_border"][:3], 60),
                      outline_width=1)


def _draw_header(draw: ImageDraw.ImageDraw, w: int, submitted: bool, *, colors: dict) -> None:
    c = colors
    draw_rounded_rect(draw, (4, 4, w - 4, HEADER_H), 14,
                      fill=c["header_dark"],
                      outline=None)
    draw.rectangle((4, HEADER_H - 4, w - 4, HEADER_H), fill=c["header_dark"])
    draw.rectangle((4, HEADER_H, w - 4, HEADER_H + 3), fill=c["card_border"])

    title_font = cinzel(24)
    sub_font = rajdhani(14)
    draw_text_centered(draw, "CAPITOL SPORTSBOOK", title_font, c["header_gold"], w // 2, 20)
    draw_text_centered(draw, "PARLAY BET SLIP", sub_font, c["text_dim"], w // 2, 58)


def _draw_legs(
    draw: ImageDraw.ImageDraw,
    legs: list[ParlayLegData],
    y_start: int,
    w: int,
    *,
    colors: dict,
) -> int:
    c = colors
    leg_font = rajdhani(16)
    num_font = rajdhani_bold(14)
    odds_font = rajdhani_bold(16)

    cur_y = y_start
    for i, leg in enumerate(legs):
        row_fill = c["row_alt"] if i % 2 == 0 else c["card_bg"]
        draw.rectangle((PAD, cur_y, w - PAD, cur_y + LEG_H - 2), fill=row_fill)

        draw_rounded_rect(draw, (PAD + 4, cur_y + 10, PAD + 28, cur_y + LEG_H - 10), 6,
                          fill=c["header_dark"],
                          outline=c["card_border"],
                          outline_width=1)
        draw_text_centered(draw, str(leg.leg_num), num_font, c["header_gold"], PAD + 16, cur_y + 14)

        label = leg.market_label if len(leg.market_label) <= 44 else leg.market_label[:42] + "…"
        draw.text((PAD + 36, cur_y + (LEG_H - 20) // 2), label, font=leg_font, fill=c["text_white"])

        oc = odds_color(leg.odds, c)
        odds_str = fmt_odds(leg.odds)
        draw_text_right(draw, odds_str, odds_font, oc, w - PAD - 8, cur_y + (LEG_H - 20) // 2)

        cur_y += LEG_H

    dash_y = cur_y + 8
    for dx in range(PAD, w - PAD, 14):
        draw.rectangle((dx, dash_y, dx + 8, dash_y + 2), fill=c["card_border"])
    return dash_y + 16


def _draw_summary(
    draw: ImageDraw.ImageDraw,
    legs: list[ParlayLegData],
    wager: int,
    payout: int,
    w: int,
    y_start: int,
    *,
    colors: dict,
) -> None:
    c = colors
    all_odds = [leg.odds for leg in legs]
    combo = combined_american(all_odds) if all_odds else 0

    label_font = rajdhani(15)
    val_font = rajdhani_bold(17)
    big_font = cinzel(22)

    row_y = y_start

    draw.text((PAD, row_y), "COMBINED ODDS", font=label_font, fill=c["text_dim"])
    combo_str = fmt_odds(combo)
    oc = odds_color(combo, c)
    draw_text_right(draw, combo_str, val_font, oc, w - PAD, row_y)
    row_y += 28

    draw.text((PAD, row_y), "WAGER", font=label_font, fill=c["text_dim"])
    draw_text_right(draw, fmt_chips(wager), val_font, c["text_white"], w - PAD, row_y)
    row_y += 28

    draw.rectangle((PAD, row_y, w - PAD, row_y + 1), fill=c["divider"])
    row_y += 12

    draw.text((PAD, row_y), "POTENTIAL PAYOUT", font=label_font, fill=c["text_dim"])
    draw_text_right(draw, fmt_chips(payout), big_font, c["header_gold"], w - PAD, row_y - 4)


def _draw_status_watermark(
    draw: ImageDraw.ImageDraw,
    w: int,
    h: int,
    submitted: bool,
    *,
    colors: dict,
) -> Image.Image | None:
    c = colors
    status_text = "SUBMITTED" if submitted else "PENDING SUBMISSION"
    wm_color = (*c["won"][:3], 35) if submitted else (*c["header_gold"][:3], 25)

    wm_font = cinzel(36)
    bbox = draw.textbbox((0, 0), status_text, font=wm_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    wm = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    wm_draw = ImageDraw.Draw(wm)
    wm_draw.text(((w - tw) // 2, (h - th) // 2 + 40), status_text, font=wm_font, fill=wm_color)
    rotated = wm.rotate(-25, expand=False)

    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    base.paste(rotated, (0, 0), mask=rotated)
    return base


def _draw_barcode(draw: ImageDraw.ImageDraw, w: int, y: int, *, colors: dict) -> None:
    c = colors
    bar_x = PAD + 20
    bar_y = y
    widths = [2, 1, 3, 1, 2, 1, 1, 3, 2, 1, 3, 1, 2, 2, 1, 3, 1, 2, 1, 3, 2, 1, 1, 2, 3, 1]
    for bw in widths:
        draw.rectangle((bar_x, bar_y, bar_x + bw, bar_y + 28), fill=c["text_dim"])
        bar_x += bw + 2
    barcode_font = rajdhani(10)
    draw_text_centered(draw, "C A P I T O L  S P O R T S B O O K", barcode_font,
                       c["text_dim"], w // 2, bar_y + 32)


def render_parlay_slip(
    legs: list[ParlayLegData],
    wager: int,
    payout: int,
    submitted: bool = False,
) -> io.BytesIO:
    theme = get_active_theme()
    c = theme.colors

    n_legs = max(len(legs), 1)
    content_h = (
        HEADER_H + 12
        + n_legs * LEG_H + 20
        + SUMMARY_H
        + FOOTER_H + 20
    )
    h = max(content_h, 500)

    img = Image.new("RGBA", (WIDTH, h), c["bg"])
    theme.draw_bg(img, WIDTH, h)
    draw = ImageDraw.Draw(img)

    for gx in range(0, WIDTH, 30):
        draw.line((gx, 0, gx, h), fill=(255, 255, 255, 3))

    _draw_ticket_border(draw, WIDTH, h, colors=c)
    _draw_header(draw, WIDTH, submitted, colors=c)

    cur_y = HEADER_H + 12
    cur_y = _draw_legs(draw, legs, cur_y, WIDTH, colors=c)
    _draw_summary(draw, legs, wager, payout, WIDTH, cur_y, colors=c)
    _draw_barcode(draw, WIDTH, h - FOOTER_H - 10, colors=c)

    wm_layer = _draw_status_watermark(draw, WIDTH, h, submitted, colors=c)
    if wm_layer is not None:
        img = Image.alpha_composite(img, wm_layer)

    # Status bar needs a fresh draw handle after alpha_composite
    slip_status_color = c["won"] if submitted else c["pending"]
    draw_final = ImageDraw.Draw(img)
    draw_rounded_rect(draw_final, (4, h - 36, WIDTH - 4, h - 4), 10,
                      fill=(*slip_status_color[:3], 40),
                      outline=slip_status_color,
                      outline_width=1)
    status_text = "BET SUBMITTED — GOOD LUCK, TRIBUTE!" if submitted else "⚡  PENDING — USE /parlay submit TO LOCK IN"
    sf = rajdhani_bold(14)
    draw_text_centered(draw_final, status_text, sf, slip_status_color, WIDTH // 2, h - 27)

    theme.draw_fg(img, WIDTH, h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

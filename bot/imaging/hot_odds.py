from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, cinzel, cinzel_regular, rajdhani, rajdhani_bold,
    draw_rounded_rect, draw_text_centered, draw_text_right,
    image_from_bytes, paste_image, odds_color,
)
from bot.odds.calculator import implied_probability
from bot.utils.formatters import fmt_odds, fmt_pct, fmt_chips

if TYPE_CHECKING:
    from bot.database.models import Tribute, Market

WIDTH = 1200
CARD_W = 190
CARD_H = 270
CARD_RADIUS = 12
HEADER_H = 85
SUBTITLE_H = 36
SECTION_H = 42
FOOTER_H = 38
PAD = 16
MARKET_ROW_H = 42


@dataclass
class TributeCardData:
    tribute_id: int
    name: str
    district: int
    gender: str
    training_score: int
    status: str
    win_odds: int | None
    face_bytes: bytes | None


@dataclass
class FeaturedMarket:
    label: str
    odds: int
    market_type: str


def _draw_header(draw: ImageDraw.ImageDraw, img: Image.Image, chips: int) -> None:
    draw_rounded_rect(draw, (0, 0, WIDTH - 1, HEADER_H - 1), 0,
                      fill=COLORS["header_dark"])

    # Decorative gold border on bottom of header
    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=COLORS["card_border"])

    # Crest — simple geometric Capitol symbol drawn with Pillow
    cx, cy = 52, HEADER_H // 2
    draw.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), outline=COLORS["header_gold"], width=2)
    draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=COLORS["header_gold"], width=1)
    for angle_deg in range(0, 360, 45):
        angle = math.radians(angle_deg)
        x1 = cx + int(14 * math.cos(angle))
        y1 = cy + int(14 * math.sin(angle))
        x2 = cx + int(22 * math.cos(angle))
        y2 = cy + int(22 * math.sin(angle))
        draw.line((x1, y1, x2, y2), fill=COLORS["header_gold"], width=1)

    # Title
    title_font = cinzel(22)
    draw.text((82, 14), "CAPITOL SPORTSBOOK", font=title_font, fill=COLORS["header_gold"])
    sub_font = rajdhani(13)
    draw.text((82, 44), "MAY THE ODDS BE EVER IN YOUR FAVOR", font=sub_font, fill=COLORS["text_dim"])

    # Chip balance
    bal_label = rajdhani(12)
    bal_val = cinzel(18)
    draw_text_right(draw, "ACCOUNT BALANCE", bal_label, COLORS["text_dim"], WIDTH - PAD, 16)
    draw_text_right(draw, fmt_chips(chips), bal_val, COLORS["header_gold"], WIDTH - PAD, 34)


def _draw_section_label(draw: ImageDraw.ImageDraw, y: int, text: str) -> None:
    draw.rectangle((0, y, WIDTH, y + SECTION_H), fill=COLORS["bg_mid"])
    draw.rectangle((0, y + SECTION_H - 2, WIDTH, y + SECTION_H), fill=COLORS["divider"])
    font = cinzel_regular(14)
    draw_text_centered(draw, text, font, COLORS["header_gold"], WIDTH // 2, y + 12)


def _tribute_status_color(status: str) -> tuple:
    return {
        "ALIVE":  COLORS["alive"],
        "DEAD":   COLORS["dead"],
        "VICTOR": COLORS["victor"],
    }.get(status, COLORS["text_dim"])


def _draw_tribute_card(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    card: TributeCardData,
) -> None:
    # Card background
    draw_rounded_rect(
        draw, (x, y, x + CARD_W, y + CARD_H), CARD_RADIUS,
        fill=COLORS["card_bg"],
        outline=COLORS["card_border"],
        outline_width=2,
    )

    inner_x = x + PAD // 2
    cur_y = y + 10

    # Face claim image
    img_size = 110
    img_x = x + (CARD_W - img_size) // 2
    if card.face_bytes:
        face_img = image_from_bytes(card.face_bytes)
        if face_img:
            paste_image(img, face_img, (img_x, cur_y), (img_size, img_size), circular=True)
        else:
            _draw_placeholder_face(draw, img_x, cur_y, img_size, card.gender)
    else:
        _draw_placeholder_face(draw, img_x, cur_y, img_size, card.gender)

    # District badge overlay on face image
    badge_font = rajdhani_bold(11)
    badge_text = f"D{card.district}"
    draw_rounded_rect(draw, (img_x, cur_y, img_x + 32, cur_y + 20), 4,
                      fill=COLORS["header_dark"], outline=COLORS["card_border"], outline_width=1)
    draw_text_centered(draw, badge_text, badge_font, COLORS["header_gold"], img_x + 16, cur_y + 4)

    cur_y += img_size + 8

    # Name
    name_font = cinzel_regular(11)
    name = card.name if len(card.name) <= 14 else card.name[:12] + "…"
    draw_text_centered(draw, name.upper(), name_font, COLORS["text_white"], x + CARD_W // 2, cur_y)
    cur_y += 18

    # Gender + Score row
    meta_font = rajdhani(13)
    gender_label = "MALE" if card.gender == "M" else "FEMALE"
    meta_text = f"{gender_label}  ·  SCORE {card.training_score}"
    draw_text_centered(draw, meta_text, meta_font, COLORS["text_dim"], x + CARD_W // 2, cur_y)
    cur_y += 20

    # Divider
    draw.rectangle((x + 12, cur_y, x + CARD_W - 12, cur_y + 1), fill=COLORS["divider"])
    cur_y += 8

    # Win odds
    if card.win_odds is not None:
        odds_font = cinzel(20)
        odds_str = fmt_odds(card.win_odds)
        oc = odds_color(card.win_odds)
        draw_text_centered(draw, odds_str, odds_font, oc, x + CARD_W // 2, cur_y)
        cur_y += 28

        # Implied probability
        prob_font = rajdhani(13)
        prob = implied_probability(card.win_odds)
        draw_text_centered(draw, fmt_pct(prob), prob_font, COLORS["text_dim"], x + CARD_W // 2, cur_y)
        cur_y += 20
    else:
        cur_y += 48

    # Status pill
    sc = _tribute_status_color(card.status)
    pill_y = cur_y
    draw_rounded_rect(draw, (x + 30, pill_y, x + CARD_W - 30, pill_y + 22), 11,
                      fill=(*sc[:3], 160), outline=sc, outline_width=1)
    status_font = rajdhani_bold(12)
    draw_text_centered(draw, card.status, status_font, (255, 255, 255, 255), x + CARD_W // 2, pill_y + 5)


def _draw_placeholder_face(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, size: int, gender: str,
) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=COLORS["bg_mid"], outline=COLORS["divider"], width=1)
    font = cinzel(20)
    symbol = "M" if gender == "M" else "F"
    draw_text_centered(draw, symbol, font, COLORS["text_dim"], x + size // 2, y + size // 2 - 14)


def _draw_featured_markets(
    draw: ImageDraw.ImageDraw,
    markets: list[FeaturedMarket],
    y_start: int,
) -> int:
    if not markets:
        return y_start

    label_font = rajdhani(14)
    odds_font = rajdhani_bold(15)
    col_w = (WIDTH - PAD * 3) // 2
    col_h = MARKET_ROW_H + 4

    for i, mkt in enumerate(markets[:8]):
        col = i % 2
        row = i // 2
        mx = PAD + col * (col_w + PAD)
        my = y_start + row * (col_h + 4)

        draw_rounded_rect(draw, (mx, my, mx + col_w, my + col_h), 8,
                          fill=COLORS["card_bg"],
                          outline=COLORS["divider"],
                          outline_width=1)

        # Market label
        label = mkt.label if len(mkt.label) <= 36 else mkt.label[:34] + "…"
        draw.text((mx + 10, my + (col_h - 18) // 2), label, font=label_font, fill=COLORS["text_white"])

        # Odds chip on the right — opaque fill, white text
        odds_str = fmt_odds(mkt.odds)
        oc = odds_color(mkt.odds)
        bbox = draw.textbbox((0, 0), odds_str, font=odds_font)
        ow = bbox[2] - bbox[0] + 20
        chip_x = mx + col_w - ow - 8
        chip_y1 = my + 7
        chip_y2 = my + col_h - 7
        draw_rounded_rect(draw, (chip_x, chip_y1, chip_x + ow, chip_y2), 6,
                          fill=(*oc[:3], 200), outline=oc, outline_width=1)
        draw_text_centered(draw, odds_str, odds_font, (255, 255, 255, 255),
                           chip_x + ow // 2, chip_y1 + (chip_y2 - chip_y1 - 18) // 2)

    rows = math.ceil(len(markets[:8]) / 2)
    return y_start + rows * (col_h + 4)


def _draw_footer(draw: ImageDraw.ImageDraw, img_h: int) -> None:
    draw.rectangle((0, img_h - FOOTER_H, WIDTH, img_h), fill=COLORS["header_dark"])
    draw.rectangle((0, img_h - FOOTER_H, WIDTH, img_h - FOOTER_H + 2), fill=COLORS["card_border"])
    font = rajdhani(13)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    draw.text((PAD, img_h - FOOTER_H + 11), "CAPITOL ANNOUNCEMENT", font=font, fill=COLORS["text_gold"])
    draw_text_right(draw, ts, font, COLORS["text_dim"], WIDTH - PAD, img_h - FOOTER_H + 11)
    msg_font = rajdhani(12)
    draw_text_centered(
        draw,
        "TODAY WE HONOR TOMORROW'S VICTORS. PLACE YOUR BETS WISELY.",
        msg_font, COLORS["text_dim"], WIDTH // 2, img_h - FOOTER_H + 11,
    )


def render_hot_odds(
    cards: list[TributeCardData],
    featured: list[FeaturedMarket],
    user_chips: int,
) -> io.BytesIO:
    alive_cards = [c for c in cards if c.status == "ALIVE"]
    other_cards = [c for c in cards if c.status != "ALIVE"]
    sorted_cards = sorted(alive_cards, key=lambda c: -(c.training_score or 0)) + other_cards

    n_cols = min(len(sorted_cards), 6) if sorted_cards else 1
    cards_section_w = n_cols * CARD_W + (n_cols + 1) * PAD
    cards_x_start = (WIDTH - cards_section_w) // 2 + PAD

    n_market_rows = math.ceil(min(len(featured), 8) / 2)
    market_section_h = n_market_rows * (MARKET_ROW_H + 8) + 8 if featured else 0

    img_h = (
        HEADER_H
        + SECTION_H
        + CARD_H + PAD * 2
        + (SECTION_H if featured else 0)
        + market_section_h
        + FOOTER_H
        + PAD
    )

    img = Image.new("RGBA", (WIDTH, img_h), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    # Subtle grid texture
    for gx in range(0, WIDTH, 40):
        draw.line((gx, 0, gx, img_h), fill=(255, 255, 255, 4))
    for gy in range(0, img_h, 40):
        draw.line((0, gy, WIDTH, gy), fill=(255, 255, 255, 4))

    _draw_header(draw, img, user_chips)

    cur_y = HEADER_H
    _draw_section_label(draw, cur_y, "HUNGER GAMES  ·  TRIBUTE ODDS")
    cur_y += SECTION_H + PAD

    for i, card in enumerate(sorted_cards[:6]):
        cx = cards_x_start + i * (CARD_W + PAD)
        _draw_tribute_card(img, draw, cx, cur_y, card)

    cur_y += CARD_H + PAD

    if featured:
        _draw_section_label(draw, cur_y, "FEATURED MARKETS")
        cur_y += SECTION_H + 8
        _draw_featured_markets(draw, featured, cur_y)
        cur_y += market_section_h

    _draw_footer(draw, img_h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

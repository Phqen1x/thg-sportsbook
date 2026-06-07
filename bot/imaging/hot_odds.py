from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from bot.imaging.base import (
    COLORS, Theme, get_active_theme,
    cinzel, cinzel_regular, rajdhani, rajdhani_bold,
    draw_rounded_rect, draw_text_centered, draw_text_right,
    image_from_bytes, paste_image, odds_color,
)
from bot.odds.calculator import implied_probability
from bot.utils.formatters import fmt_odds, fmt_pct, fmt_chips

if TYPE_CHECKING:
    from bot.database.models import Tribute, Market

WIDTH = 1200
HEADER_H = 85
SECTION_H = 42
FOOTER_H = 38
PAD = 16
MARKET_ROW_H = 42
TRIBUTE_ROW_H = 32
_TRIBUTE_ROW_GAP = 2


@dataclass
class BoardCardData:
    """One moneyline row on the Hot Odds board. Generic across the three board
    modes — a tribute, a district, or an alliance — so the same renderer draws
    all of them. ``badge`` is the small left-hand pill (e.g. "D5M", "D7", "CP"),
    ``odds`` the headline moneyline, and ``sort_key`` controls row order."""
    badge: str
    name: str
    odds: int | None
    status: str
    sort_key: tuple = ()


@dataclass
class FeaturedMarket:
    label: str
    odds: int
    market_type: str


@dataclass
class TributeDetailData:
    name: str
    district: int
    gender: str
    training_score: int | None
    status: str
    kills: int
    placement: int | None
    death_cause: str | None
    win_odds: int | None
    face_bytes: bytes | None
    markets: list[FeaturedMarket]


def _tribute_status_color(status: str, colors: dict) -> tuple:
    return {
        "ALIVE":  colors["alive"],
        "DEAD":   colors["dead"],
        "VICTOR": colors["victor"],
    }.get(status, colors["text_dim"])


def _draw_header(draw: ImageDraw.ImageDraw, img: Image.Image, chips: int, *, colors: dict) -> None:
    c = colors
    draw_rounded_rect(draw, (0, 0, WIDTH - 1, HEADER_H - 1), 0,
                      fill=c["header_dark"])

    draw.rectangle((0, HEADER_H - 3, WIDTH, HEADER_H), fill=c["card_border"])

    cx, cy = 52, HEADER_H // 2
    draw.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), outline=c["header_gold"], width=2)
    draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=c["header_gold"], width=1)
    for angle_deg in range(0, 360, 45):
        angle = math.radians(angle_deg)
        x1 = cx + int(14 * math.cos(angle))
        y1 = cy + int(14 * math.sin(angle))
        x2 = cx + int(22 * math.cos(angle))
        y2 = cy + int(22 * math.sin(angle))
        draw.line((x1, y1, x2, y2), fill=c["header_gold"], width=1)

    title_font = cinzel(22)
    draw.text((82, 14), "PANEM SPORTSBOOK", font=title_font, fill=c["header_gold"])
    sub_font = rajdhani(13)
    draw.text((82, 44), "MAY THE ODDS BE EVER IN YOUR FAVOR", font=sub_font, fill=c["text_dim"])

    bal_label = rajdhani(12)
    bal_val = cinzel(18)
    draw_text_right(draw, "ACCOUNT BALANCE", bal_label, c["text_dim"], WIDTH - PAD, 16)
    draw_text_right(draw, fmt_chips(chips), bal_val, c["header_gold"], WIDTH - PAD, 34)


def _draw_section_label(draw: ImageDraw.ImageDraw, y: int, text: str, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, y, WIDTH, y + SECTION_H), fill=c["bg_mid"])
    draw.rectangle((0, y + SECTION_H - 2, WIDTH, y + SECTION_H), fill=c["divider"])
    font = cinzel_regular(14)
    draw_text_centered(draw, text, font, c["header_gold"], WIDTH // 2, y + 12)


def _draw_board_rows(
    draw: ImageDraw.ImageDraw,
    cards: list[BoardCardData],
    y_start: int,
    *,
    colors: dict,
) -> None:
    if not cards:
        return

    c = colors
    n_cols = 2
    col_w = (WIDTH - PAD * (n_cols + 1)) // n_cols
    badge_font = rajdhani_bold(11)
    name_font = rajdhani(15)
    odds_font = rajdhani_bold(13)
    status_font = rajdhani_bold(11)

    for i, card in enumerate(cards):
        col = i % n_cols
        row_idx = i // n_cols
        cx = PAD + col * (col_w + PAD)
        cy = y_start + row_idx * (TRIBUTE_ROW_H + _TRIBUTE_ROW_GAP)

        is_dead = card.status in ("DEAD", "ELIMINATED")
        row_fill = c["row_alt"] if row_idx % 2 == 0 else c["card_bg"]
        draw_rounded_rect(draw, (cx, cy, cx + col_w, cy + TRIBUTE_ROW_H), 5,
                          fill=row_fill, outline=c["divider"], outline_width=1)

        pill_y1 = cy + 5
        pill_y2 = cy + TRIBUTE_ROW_H - 5

        badge_text = card.badge
        badge_tw = draw.textbbox((0, 0), badge_text, font=badge_font)[2]
        badge_w = badge_tw + 10
        badge_x = cx + 5
        draw_rounded_rect(draw, (badge_x, pill_y1, badge_x + badge_w, pill_y2), 4,
                          fill=c["header_dark"], outline=c["card_border"], outline_width=1)
        draw_text_centered(draw, badge_text, badge_font, c["header_gold"],
                           badge_x + badge_w // 2, pill_y1 + 3)

        sc = _tribute_status_color(card.status, c)
        status_text = card.status
        st_tw = draw.textbbox((0, 0), status_text, font=status_font)[2]
        st_w = st_tw + 10
        st_x = cx + col_w - 5 - st_w
        draw_rounded_rect(draw, (st_x, pill_y1, st_x + st_w, pill_y2), 4,
                          fill=(*sc[:3], 45), outline=sc, outline_width=1)
        draw_text_centered(draw, status_text, status_font, sc,
                           st_x + st_w // 2, pill_y1 + 3)

        right_edge = st_x - 6
        if card.odds is not None:
            odds_str = fmt_odds(card.odds)
            oc = odds_color(card.odds, c)
            ow = draw.textbbox((0, 0), odds_str, font=odds_font)[2] + 14
            chip_x = right_edge - ow
            draw_rounded_rect(draw, (chip_x, pill_y1, chip_x + ow, pill_y2), 4,
                              fill=(*oc[:3], 160), outline=oc, outline_width=1)
            draw_text_centered(draw, odds_str, odds_font, (255, 255, 255, 255),
                               chip_x + ow // 2, pill_y1 + 3)
            right_edge = chip_x - 6

        name_x = badge_x + badge_w + 8
        max_name_w = right_edge - name_x - 4
        name = card.name
        while draw.textbbox((0, 0), name, font=name_font)[2] > max_name_w and len(name) > 1:
            name = name[:-1]
        if name != card.name:
            name = name.rstrip() + "…"

        name_color = c["text_dim"] if is_dead else c["text_white"]
        text_y = cy + (TRIBUTE_ROW_H - 15) // 2
        draw.text((name_x, text_y), name, font=name_font, fill=name_color)


def _draw_placeholder_face(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, size: int, gender: str,
    *,
    colors: dict,
) -> None:
    c = colors
    draw.ellipse((x, y, x + size, y + size), fill=c["bg_mid"], outline=c["divider"], width=1)
    font = cinzel(20)
    symbol = {"M": "M", "F": "F", "NB": "NB"}.get(gender, "?")
    draw_text_centered(draw, symbol, font, c["text_dim"], x + size // 2, y + size // 2 - 14)


_TRIBUTE_NAME_RE = re.compile(
    r'^(D\d+(?:M|F|NB))\s+.+?\s+'
    r'(?=(?:Wins|Finishes|Top|Gets|Kills|Dies|Survives|Makes|Eliminated|Training|Placement|First|Bloodbath)\b)'
)
_ABBREVIATIONS = (
    ("Eliminated Before Final 8",  "Out - F8"),
    ("Eliminated Before Final 5",  "Out - F5"),
    ("Survives the Bloodbath",      "BB Survivor"),
    ("Gets Most Kills",             "Most Kills"),
    ("Gets First Kill",             "First Kill"),
    ("Wins the Games",              "Wins"),
    ("Training Score",              "Tr. Score"),
    ("Final 8",                     "F8"),
    ("Final 5",                     "F5"),
    ("Finishes",                    "Fin."),
    (" — Over ",                    " O "),
    (" — Under ",                   " U "),
    ("Over",                        "O"),
    ("Under",                       "U"),
)


def _smart_shorten_label(label: str, draw, font, max_width: int) -> str:
    """Return label shortened to fit within max_width pixels, preserving meaning."""
    def fits(s: str) -> bool:
        return draw.textbbox((0, 0), s, font=font)[2] <= max_width

    if fits(label):
        return label

    m = _TRIBUTE_NAME_RE.match(label)
    if m:
        shortened = label[: m.end(1)] + " " + label[m.end():]
        if fits(shortened):
            return shortened
        label = shortened

    for old, new in _ABBREVIATIONS:
        if old in label:
            test = label.replace(old, new)
            if fits(test):
                return test
            label = test

    for end in range(len(label) - 1, 0, -1):
        test = label[:end].rstrip() + "…"
        if fits(test):
            return test
    return "…"


def _draw_featured_markets(
    draw: ImageDraw.ImageDraw,
    markets: list[FeaturedMarket],
    y_start: int,
    *,
    colors: dict,
) -> int:
    if not markets:
        return y_start

    c = colors
    label_font = rajdhani(14)
    odds_font = rajdhani_bold(15)
    col_w = (WIDTH - PAD * 3) // 2
    col_h = MARKET_ROW_H + 4

    for i, mkt in enumerate(markets):
        col = i % 2
        row = i // 2
        mx = PAD + col * (col_w + PAD)
        my = y_start + row * (col_h + 4)

        draw_rounded_rect(draw, (mx, my, mx + col_w, my + col_h), 8,
                          fill=c["card_bg"],
                          outline=c["divider"],
                          outline_width=1)

        odds_str = fmt_odds(mkt.odds)
        oc = odds_color(mkt.odds, c)
        odds_bbox = draw.textbbox((0, 0), odds_str, font=odds_font)
        ow = odds_bbox[2] - odds_bbox[0] + 20
        chip_x = mx + col_w - ow - 8
        chip_y1 = my + 7
        chip_y2 = my + col_h - 7

        max_label_w = chip_x - (mx + 10) - 8
        label = _smart_shorten_label(mkt.label, draw, label_font, max_label_w)
        draw.text((mx + 10, my + (col_h - 18) // 2), label, font=label_font, fill=c["text_white"])

        draw_rounded_rect(draw, (chip_x, chip_y1, chip_x + ow, chip_y2), 6,
                          fill=(*oc[:3], 200), outline=oc, outline_width=1)
        draw_text_centered(draw, odds_str, odds_font, (255, 255, 255, 255),
                           chip_x + ow // 2, chip_y1 + (chip_y2 - chip_y1 - 18) // 2)

    rows = math.ceil(len(markets) / 2)
    return y_start + rows * (col_h + 4)


def _draw_footer(draw: ImageDraw.ImageDraw, img_h: int, *, colors: dict) -> None:
    c = colors
    draw.rectangle((0, img_h - FOOTER_H, WIDTH, img_h), fill=c["header_dark"])
    draw.rectangle((0, img_h - FOOTER_H, WIDTH, img_h - FOOTER_H + 2), fill=c["card_border"])
    font = rajdhani(13)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    draw.text((PAD, img_h - FOOTER_H + 11), "CAPITOL ANNOUNCEMENT", font=font, fill=c["text_gold"])
    draw_text_right(draw, ts, font, c["text_dim"], WIDTH - PAD, img_h - FOOTER_H + 11)
    msg_font = rajdhani(12)
    draw_text_centered(
        draw,
        "TODAY WE HONOR TOMORROW'S VICTORS. PLACE YOUR BETS WISELY.",
        msg_font, c["text_dim"], WIDTH // 2, img_h - FOOTER_H + 11,
    )


def render_hot_odds(
    cards: list[BoardCardData],
    featured: list[FeaturedMarket],
    user_chips: int,
    board_title: str = "TRIBUTE MONEYLINES  ·  TRIBUTE TO WIN THE GAMES",
    featured_title: str = "FEATURED MARKETS",
) -> io.BytesIO:
    theme = get_active_theme()
    c = theme.colors

    sorted_cards = sorted(cards, key=lambda card: card.sort_key)

    n_tribute_rows = math.ceil(len(sorted_cards) / 2) if sorted_cards else 1
    tribute_section_h = n_tribute_rows * (TRIBUTE_ROW_H + _TRIBUTE_ROW_GAP) - _TRIBUTE_ROW_GAP + PAD

    featured_8 = featured[:8]
    n_market_rows = math.ceil(len(featured_8) / 2)
    market_section_h = n_market_rows * (MARKET_ROW_H + 8) + 8 if featured_8 else 0

    img_h = (
        HEADER_H
        + SECTION_H
        + tribute_section_h
        + (SECTION_H if featured_8 else 0)
        + market_section_h
        + FOOTER_H
        + PAD
    )

    img = Image.new("RGBA", (WIDTH, img_h), c["bg"])
    theme.draw_bg(img, WIDTH, img_h)
    draw = ImageDraw.Draw(img)

    _draw_header(draw, img, user_chips, colors=c)

    cur_y = HEADER_H
    _draw_section_label(draw, cur_y, board_title, colors=c)
    cur_y += SECTION_H + PAD

    _draw_board_rows(draw, sorted_cards, cur_y, colors=c)
    cur_y += tribute_section_h

    if featured_8:
        _draw_section_label(draw, cur_y, featured_title, colors=c)
        cur_y += SECTION_H + 8
        _draw_featured_markets(draw, featured_8, cur_y, colors=c)

    _draw_footer(draw, img_h, colors=c)

    theme.draw_fg(img, WIDTH, img_h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def render_tribute_detail(
    detail: TributeDetailData,
    user_chips: int,
) -> io.BytesIO:
    theme = get_active_theme()
    c = theme.colors

    FACE_SIZE = 180
    stats_row_h = FACE_SIZE + PAD * 2

    featured_markets = detail.markets[:16]
    n_market_rows = math.ceil(len(featured_markets) / 2)
    market_section_h = n_market_rows * (MARKET_ROW_H + 8) + 8 if featured_markets else 0

    img_h = (
        HEADER_H
        + SECTION_H
        + stats_row_h
        + (SECTION_H + market_section_h if featured_markets else 0)
        + FOOTER_H
        + PAD
    )

    img = Image.new("RGBA", (WIDTH, img_h), c["bg"])
    theme.draw_bg(img, WIDTH, img_h)
    draw = ImageDraw.Draw(img)

    _draw_header(draw, img, user_chips, colors=c)

    cur_y = HEADER_H
    section_label = f"D{detail.district}{detail.gender}  ·  {detail.name.upper()}"
    _draw_section_label(draw, cur_y, section_label, colors=c)
    cur_y += SECTION_H + PAD

    face_x, face_y = PAD, cur_y
    if detail.face_bytes:
        face_img = image_from_bytes(detail.face_bytes)
        if face_img:
            paste_image(img, face_img, (face_x, face_y), (FACE_SIZE, FACE_SIZE), circular=True)
        else:
            _draw_placeholder_face(draw, face_x, face_y, FACE_SIZE, detail.gender, colors=c)
    else:
        _draw_placeholder_face(draw, face_x, face_y, FACE_SIZE, detail.gender, colors=c)

    badge_font = rajdhani_bold(13)
    badge_text = f"D{detail.district}{detail.gender}"
    badge_tw = draw.textbbox((0, 0), badge_text, font=badge_font)[2]
    badge_w = badge_tw + 12
    draw_rounded_rect(draw, (face_x, face_y, face_x + badge_w, face_y + 22), 4,
                      fill=c["header_dark"], outline=c["card_border"], outline_width=1)
    draw_text_centered(draw, badge_text, badge_font, c["header_gold"],
                       face_x + badge_w // 2, face_y + 4)

    stats_x = face_x + FACE_SIZE + PAD * 2
    sy = face_y + 4

    name_font = cinzel(26)
    draw.text((stats_x, sy), detail.name.upper(), font=name_font, fill=c["text_white"])
    sy += 38

    meta_font = rajdhani(16)
    gender_label = {"M": "MALE", "F": "FEMALE", "NB": "NON-BINARY"}.get(detail.gender, detail.gender)
    score_str = str(detail.training_score) if detail.training_score is not None else "?"
    draw.text((stats_x, sy), f"{gender_label}  ·  TRAINING SCORE {score_str}",
              font=meta_font, fill=c["text_dim"])
    sy += 26

    sc = _tribute_status_color(detail.status, c)
    label_w = draw.textbbox((0, 0), "STATUS  ", font=meta_font)[2]
    draw.text((stats_x, sy), "STATUS  ", font=meta_font, fill=c["text_dim"])
    draw.text((stats_x + label_w, sy), detail.status, font=meta_font, fill=sc)
    sy += 26

    draw.text((stats_x, sy), f"KILLS  {detail.kills}", font=meta_font, fill=c["text_dim"])
    sy += 26

    if detail.placement is not None:
        draw.text((stats_x, sy), f"PLACEMENT  {detail.placement}", font=meta_font, fill=c["text_dim"])
        sy += 26

    if detail.death_cause:
        draw.text((stats_x, sy), f"CAUSE OF DEATH  {detail.death_cause}",
                  font=meta_font, fill=c["text_dim"])

    if detail.win_odds is not None:
        lbl_font = rajdhani(13)
        odds_font_lg = cinzel(38)
        prob_font = rajdhani(14)

        draw_text_right(draw, "TO WIN THE GAMES", lbl_font, c["text_dim"], WIDTH - PAD, face_y + 6)

        odds_str = fmt_odds(detail.win_odds)
        oc = odds_color(detail.win_odds, c)
        draw_text_right(draw, odds_str, odds_font_lg, oc, WIDTH - PAD, face_y + 24)

        prob = implied_probability(detail.win_odds)
        draw_text_right(draw, f"{fmt_pct(prob)} IMPLIED PROBABILITY",
                        prob_font, c["text_dim"], WIDTH - PAD, face_y + 74)

    cur_y += stats_row_h

    if featured_markets:
        _draw_section_label(draw, cur_y, "TRIBUTE MARKETS", colors=c)
        cur_y += SECTION_H + 8
        _draw_featured_markets(draw, featured_markets, cur_y, colors=c)

    _draw_footer(draw, img_h, colors=c)

    theme.draw_fg(img, WIDTH, img_h)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

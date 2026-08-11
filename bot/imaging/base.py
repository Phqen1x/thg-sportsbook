from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import math
import random
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from bot import config

log = logging.getLogger("capitol.imaging")

COLORS = {
    "bg":           (10,  10,  10,  255),
    "bg_mid":       (18,  18,  18,  255),
    "card_bg":      (20,  20,  20,  255),
    "card_border":  (139, 105,  20,  255),
    "header_gold":  (201, 162,  39,  255),
    "header_dark":  (15,  13,   8,  255),
    "text_white":   (242, 242, 242, 255),
    "text_dim":     (176, 176, 176, 255),
    "text_gold":    (214, 176,  60, 255),
    "odds_pos":     ( 76, 175,  80, 255),
    "odds_neg":     (207, 102, 121, 255),
    "alive":        ( 76, 175,  80, 255),
    "dead":         ( 85,  85,  85, 255),
    "victor":       (255, 215,   0, 255),
    "pending":      (240, 192,  64, 255),
    "won":          ( 76, 175,  80, 255),
    "lost":         (207,  68,  68, 255),
    "cashed_out":   ( 91, 155, 213, 255),
    "voided":       (136, 136, 136, 255),
    "divider":      ( 50,  45,  30, 255),
    "row_alt":      ( 24,  22,  15, 255),
}

FOREST_COLORS = {
    "bg":           (  4,  13,   4, 255),
    "bg_mid":       (  8,  20,   8, 255),
    "card_bg":      ( 10,  24,  10, 255),
    "card_border":  ( 48,  88,  24, 255),
    "header_gold":  (155, 210,  62, 255),
    "header_dark":  (  3,  10,   3, 255),
    "text_white":   (210, 232, 198, 255),
    "text_dim":     (125, 152, 106, 255),
    "text_gold":    (145, 200,  55, 255),
    "odds_pos":     ( 68, 188,  68, 255),
    "odds_neg":     (195,  65,  85, 255),
    "alive":        ( 68, 188,  68, 255),
    "dead":         ( 62,  62,  62, 255),
    "victor":       (195, 222,  44, 255),
    "pending":      (188, 168,  48, 255),
    "won":          ( 68, 188,  68, 255),
    "lost":         (188,  58,  58, 255),
    "cashed_out":   ( 68, 148, 198, 255),
    "voided":       (118, 118, 118, 255),
    "divider":      ( 24,  52,  14, 255),
    "row_alt":      ( 12,  26,  10, 255),
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font_path(name: str) -> Path:
    return config.FONTS_DIR / name


def load_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    key = (name, size)
    if key not in _font_cache:
        path = _font_path(name)
        if path.exists():
            try:
                _font_cache[key] = ImageFont.truetype(str(path), size)
            except Exception:
                log.warning(f"Failed to load font {name}, using default")
                _font_cache[key] = ImageFont.load_default()
        else:
            log.warning(f"Font file not found: {path}, using default")
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def cinzel(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("BarlowSemiCondensed-ExtraBold.ttf", size)


def cinzel_regular(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("BarlowSemiCondensed-SemiBold.ttf", size)


def rajdhani(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("BarlowSemiCondensed-Medium.ttf", size)


def rajdhani_bold(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("BarlowSemiCondensed-Bold.ttf", size)


def hex_to_rgba(h: str) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r, g, b, 255


def draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple,
    outline: tuple | None = None,
    outline_width: int = 2,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=outline_width)


def draw_text_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    color: tuple,
    cx: int,
    y: int,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w // 2, y), text, font=font, fill=color)


def draw_text_right(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    color: tuple,
    rx: int,
    y: int,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((rx - w, y), text, font=font, fill=color)


def make_circular_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


def make_rounded_mask(w: int, h: int, radius: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    return mask


def paste_image(
    base: Image.Image,
    overlay: Image.Image,
    xy: tuple[int, int],
    size: tuple[int, int],
    circular: bool = False,
    radius: int = 12,
) -> None:
    resized = overlay.convert("RGBA").resize(size, Image.LANCZOS)
    if circular:
        mask = make_circular_mask(size[0])
    else:
        mask = make_rounded_mask(size[0], size[1], radius)
    base.paste(resized, xy, mask=mask)


def image_from_bytes(data: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


_logo_cache: Image.Image | None = None


def get_logo() -> Image.Image | None:
    global _logo_cache
    if _logo_cache is None:
        path = config.LOGO_PATH
        if path.exists():
            try:
                _logo_cache = Image.open(path).convert("RGBA")
            except Exception:
                log.warning(f"Failed to load logo from {path}")
    return _logo_cache


def paste_logo(img: Image.Image, x: int, y: int, size: int) -> None:
    logo = get_logo()
    if logo is None:
        return
    resized = logo.resize((size, size), Image.LANCZOS)
    img.paste(resized, (x, y), mask=resized)


T = TypeVar("T")


async def render_async(sync_fn: Callable[..., T], *args) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, sync_fn, *args)


async def _is_safe_image_url(url: str) -> bool:
    """Return True only if url is HTTPS and resolves exclusively to public IP addresses."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = parsed.hostname
        if not host:
            return False
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM),
        )
        if not infos:
            return False
        for _, _, _, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or not ip.is_global:
                return False
        return True
    except Exception:
        return False


async def fetch_image_bytes(url: str) -> bytes | None:
    if not url:
        return None
    if url.startswith("/uploads/"):
        # Locally-persisted upload (see AdminCog._persist_asset) — read straight off
        # disk instead of round-tripping through HTTP, since it's not a real URL.
        path = config.UPLOADS_DIR / Path(url).name
        try:
            return await asyncio.get_running_loop().run_in_executor(None, path.read_bytes)
        except OSError as e:
            log.warning(f"Failed to read local upload {path}: {e}")
            return None
    if not await _is_safe_image_url(url):
        log.warning(f"Blocked face claim fetch to non-HTTPS or private-network URL: {url}")
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PanemSportsbook/1.0)",
        "Accept": "image/*,*/*",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    log.debug(f"Fetched face claim image {len(data)} bytes from {url}")
                    return data
                log.warning(f"Face claim fetch returned HTTP {resp.status} for {url}")
    except Exception as e:
        log.warning(f"Failed to fetch face claim image {url}: {e}")
    return None


def buf_to_discord_file(buf: io.BytesIO, filename: str):
    import discord
    buf.seek(0)
    return discord.File(buf, filename=filename)


def odds_color(odds: int, colors: dict | None = None) -> tuple:
    c = colors or COLORS
    return c["odds_pos"] if odds >= 0 else c["odds_neg"]


def status_color(status: str, colors: dict | None = None) -> tuple:
    c = colors or COLORS
    return c.get(status.lower(), c["text_dim"])


# ── Theme system ─────────────────────────────────────────────────────────────

def _noop_hook(img: Image.Image, w: int, h: int) -> None:
    pass


def _draw_vine_side(
    draw: ImageDraw.ImageDraw, w: int, h: int, side: str, rng: random.Random
) -> None:
    """Draw one organic vine along the given edge of the image."""
    x_base = 0 if side == "left" else w
    sign = 1 if side == "left" else -1

    vine_col = (30, 72, 12, 200)
    leaf_cols = [(22, 62, 9, 185), (42, 98, 18, 165), (18, 48, 7, 195)]
    tendril_col = (35, 82, 15, 155)
    berry_col = (95, 20, 28, 190)

    freq1, freq2 = 0.022, 0.047
    amp1, amp2 = 12, 5
    off1 = rng.uniform(0, 6.28)
    off2 = rng.uniform(0, 6.28)

    stem: list[tuple[int, int]] = []
    y = 0
    while y <= h:
        x_off = amp1 * math.sin(y * freq1 + off1) + amp2 * math.sin(y * freq2 + off2) + 10
        stem.append((int(x_base + sign * x_off), y))
        y += 2

    for i in range(len(stem) - 1):
        draw.line([stem[i], stem[i + 1]], fill=vine_col, width=2 if (i // 15) % 2 == 0 else 1)

    interval = max(1, len(stem) // 20)
    for i in range(0, len(stem), interval):
        px, py = stem[i]

        # Leaf
        lw, lh = rng.randint(12, 26), rng.randint(5, 12)
        lx = px + sign * rng.randint(8, 30)
        ly = py + rng.randint(-10, 10)
        draw.ellipse((lx - lw // 2, ly - lh // 2, lx + lw // 2, ly + lh // 2),
                     fill=rng.choice(leaf_cols))

        if rng.random() < 0.55:
            slw, slh = rng.randint(6, 16), rng.randint(4, 8)
            slx = px + sign * rng.randint(4, 22)
            sly = py + rng.randint(-18, 18)
            draw.ellipse((slx - slw // 2, sly - slh // 2, slx + slw // 2, sly + slh // 2),
                         fill=rng.choice(leaf_cols))

        # Curling tendril via quadratic bezier approximation
        tx_end = px + sign * rng.randint(16, 38)
        ty_end = py + rng.randint(-22, 22)
        mx_ctrl = (px + tx_end) // 2 + sign * rng.randint(3, 12)
        my_ctrl = (py + ty_end) // 2 + rng.randint(-10, 10)
        t_pts: list[tuple[int, int]] = []
        for s in range(6):
            t = s / 5
            bx = int((1 - t) ** 2 * px + 2 * (1 - t) * t * mx_ctrl + t ** 2 * tx_end)
            by = int((1 - t) ** 2 * py + 2 * (1 - t) * t * my_ctrl + t ** 2 * ty_end)
            t_pts.append((bx, by))
        draw.line(t_pts, fill=tendril_col, width=1)

        if rng.random() < 0.28:
            for _ in range(rng.randint(2, 4)):
                bx = tx_end + rng.randint(-5, 5)
                by_b = ty_end + rng.randint(-5, 5)
                br = rng.randint(2, 4)
                draw.ellipse((bx - br, by_b - br, bx + br, by_b + br), fill=berry_col)


def _forest_bg(img: Image.Image, w: int, h: int) -> None:
    draw = ImageDraw.Draw(img)
    _draw_vine_side(draw, w, h, "left", random.Random(7331))
    _draw_vine_side(draw, w, h, "right", random.Random(8442))


def _forest_fg(img: Image.Image, w: int, h: int) -> None:
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    rng = random.Random(1337)
    for _ in range(20):
        fx = rng.randint(20, w - 20)
        fy = rng.randint(0, h)
        fr = rng.randint(1, 2)
        alpha = rng.randint(55, 130)
        gr = fr + 3
        draw.ellipse((fx - gr, fy - gr, fx + gr, fy + gr), fill=(170, 220, 60, alpha // 3))
        draw.ellipse((fx - fr, fy - fr, fx + fr, fy + fr), fill=(210, 240, 100, alpha))
    img.alpha_composite(overlay)


@dataclass
class Theme:
    name: str
    colors: dict
    draw_bg: Callable[[Image.Image, int, int], None] = field(default=_noop_hook)
    draw_fg: Callable[[Image.Image, int, int], None] = field(default=_noop_hook)


THEME_DARK = Theme("dark", COLORS)
THEME_FOREST = Theme("forest", FOREST_COLORS, _forest_bg, _forest_fg)

_THEMES: dict[str, Theme] = {"dark": THEME_DARK, "forest": THEME_FOREST}
_active_theme: Theme = THEME_DARK


def set_active_theme(theme: Theme) -> None:
    global _active_theme
    _active_theme = theme


def get_active_theme() -> Theme:
    return _active_theme


def get_theme_by_name(name: str) -> Theme | None:
    return _THEMES.get(name.lower())


def list_theme_names() -> list[str]:
    return list(_THEMES.keys())

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Callable, TypeVar

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
    "text_white":   (232, 232, 232, 255),
    "text_dim":     (136, 136, 136, 255),
    "text_gold":    (184, 148,  40, 255),
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
    return load_font("Cinzel-Bold.ttf", size)


def cinzel_regular(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("Cinzel-Regular.ttf", size)


def rajdhani(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("Rajdhani-SemiBold.ttf", size)


def rajdhani_bold(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font("Rajdhani-Bold.ttf", size)


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


T = TypeVar("T")


async def render_async(sync_fn: Callable[..., T], *args) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, sync_fn, *args)


async def fetch_image_bytes(url: str) -> bytes | None:
    if not url:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CapitolSportsbook/1.0)",
        "Accept": "image/*,*/*",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
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


def odds_color(odds: int) -> tuple:
    return COLORS["odds_pos"] if odds >= 0 else COLORS["odds_neg"]


def status_color(status: str) -> tuple:
    return COLORS.get(status.lower(), COLORS["text_dim"])

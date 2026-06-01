"""
Downloads Cinzel and Rajdhani fonts from Google Fonts CDN into assets/fonts/.
Run once before starting the bot: python scripts/download_fonts.py
"""
import sys
import urllib.request
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

FONT_URLS = {
    "Cinzel-Regular.ttf":    "https://fonts.gstatic.com/s/cinzel/v26/8vIU7ww63mVu7gtR-kwKxNvkNOjw-tbnTYo.ttf",
    "Cinzel-Bold.ttf":       "https://fonts.gstatic.com/s/cinzel/v26/8vIU7ww63mVu7gtR-kwKxNvkNOjw-jHgTYo.ttf",
    "Rajdhani-Regular.ttf":  "https://fonts.gstatic.com/s/rajdhani/v17/LDIxapCSOBg7S-QT7q4A.ttf",
    "Rajdhani-SemiBold.ttf": "https://fonts.gstatic.com/s/rajdhani/v17/LDI2apCSOBg7S-QT7pbYF8Os.ttf",
    "Rajdhani-Bold.ttf":     "https://fonts.gstatic.com/s/rajdhani/v17/LDI2apCSOBg7S-QT7pa8FsOs.ttf",
}


def download_fonts() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in FONT_URLS.items():
        dest = FONTS_DIR / filename
        if dest.exists():
            print(f"  [skip] {filename} already exists")
            continue
        print(f"  [download] {filename}...", end=" ", flush=True)
        try:
            urllib.request.urlretrieve(url, dest)
            print("OK")
        except Exception as e:
            print(f"FAILED ({e})")
            print(f"    Manual download: {url}")


if __name__ == "__main__":
    print("Downloading fonts to assets/fonts/...")
    download_fonts()
    print("Done.")

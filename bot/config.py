import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.environ.get("DOTENV_PATH"))

BASE_DIR = Path(__file__).resolve().parent.parent

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ROLE_ID: int | None = int(os.environ["ADMIN_ROLE_ID"]) if os.environ.get("ADMIN_ROLE_ID") else None
ANNOUNCEMENT_CHANNEL_ID: int | None = int(os.environ["ANNOUNCEMENT_CHANNEL_ID"]) if os.environ.get("ANNOUNCEMENT_CHANNEL_ID") else None
# Channel where chip withdraw/deposit requests are posted for admins to action.
WITHDRAW_CHANNEL_ID: int | None = int(os.environ["WITHDRAW_CHANNEL_ID"]) if os.environ.get("WITHDRAW_CHANNEL_ID") else None
DEV_GUILD_ID: int | None = int(os.environ["DEV_GUILD_ID"]) if os.environ.get("DEV_GUILD_ID") else None

DEFAULT_CHIPS: int = int(os.environ.get("DEFAULT_CHIPS", "1000"))
CASHOUT_ALLOWED: bool = os.environ.get("CASHOUT_ALLOWED", "false").lower() == "true"
CASHOUT_RATE: float = float(os.environ.get("CASHOUT_RATE", "0.65"))

# Public base URL of the web app (used by /play to link the standalone Activity).
WEB_BASE_URL: str | None = (os.environ.get("WEB_BASE_URL") or "").rstrip("/") or None

DB_PATH: str = os.environ.get("DB_PATH") or str(BASE_DIR / "data" / "sportsbook.db")
FONTS_DIR: Path = Path(os.environ.get("FONTS_DIR") or str(BASE_DIR / "assets" / "fonts"))
LOGO_PATH: Path = Path(os.environ.get("LOGO_PATH") or str(BASE_DIR / "panem.png"))

# Lemonade local-AI server (https://github.com/lemonade-sdk/lemonade)
LEMONADE_BASE_URL: str = os.environ.get("LEMONADE_BASE_URL", "http://localhost:13305")
LEMONADE_MODEL: str = os.environ.get("LEMONADE_MODEL", "auto")
# Context window override passed as num_ctx to llama-server; 0 = use server default.
LEMONADE_CTX_SIZE: int = int(os.environ.get("LEMONADE_CTX_SIZE", "32768"))

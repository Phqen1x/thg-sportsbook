import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

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

DB_PATH: str = str(BASE_DIR / "data" / "sportsbook.db")
FONTS_DIR: Path = BASE_DIR / "assets" / "fonts"

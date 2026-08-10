from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.environ.get("DOTENV_PATH"), override=True)

BASE_DIR = Path(__file__).resolve().parent.parent

# Reuse bot configuration
from bot import config as _bot

BOT_TOKEN: str = _bot.BOT_TOKEN
ADMIN_ROLE_ID: int | None = _bot.ADMIN_ROLE_ID
DB_PATH: str = _bot.DB_PATH
DEFAULT_CHIPS: int = _bot.DEFAULT_CHIPS
UPLOADS_DIR = _bot.UPLOADS_DIR
SINGLE_PAYOUT_CAP: int = _bot.SINGLE_PAYOUT_CAP
PARLAY_PAYOUT_CAP: int = _bot.PARLAY_PAYOUT_CAP

# Web-specific
DISCORD_CLIENT_ID: str = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET: str = os.environ.get("DISCORD_CLIENT_SECRET", "")
WEB_BASE_URL: str = os.environ.get("WEB_BASE_URL", "http://localhost:8000").rstrip("/")
WEB_SECRET_KEY: str = os.environ.get("WEB_SECRET_KEY", "dev-secret-change-me")
GUILD_ID: int | None = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
WEB_HOST: str = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT: int = int(os.environ.get("WEB_PORT", "8000"))
WEB_SSL_CERTFILE: str | None = os.environ.get("WEB_SSL_CERTFILE") or None
WEB_SSL_KEYFILE: str | None = os.environ.get("WEB_SSL_KEYFILE") or None

DISCORD_REDIRECT_URI: str = f"{WEB_BASE_URL}/auth/callback"

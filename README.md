# THG Sportsbook

A Hunger Games–themed Discord sportsbook bot with a web admin dashboard and an embedded Discord Activity. Server members wager chips on tributes and game events; admins manage markets, resolve bets, and run live-game operations from a browser panel or in-Discord UI.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Discord Server                                         │
│  ┌──────────────┐   slash commands    ┌───────────────┐ │
│  │  Members     │ ──────────────────► │  discord.py   │ │
│  │              │                     │  Bot (bot/)   │ │
│  │  [Activity]  │ ◄── embedded SPA ── │               │ │
│  └──────────────┘                     └───────┬───────┘ │
└──────────────────────────────────────────────┼──────────┘
                                               │ shared SQLite DB
                                   ┌───────────▼───────────┐
                                   │  FastAPI Web App       │
                                   │  (web/)                │
                                   │                        │
                                   │  /          dashboard  │
                                   │  /admin/*   admin UI   │
                                   │  /activity  SPA shell  │
                                   │  /api/activity/*  JSON │
                                   └────────────────────────┘
```

- **`bot/`** — discord.py bot: slash commands, odds engine, bet settlement, image generation
- **`web/`** — FastAPI app: browser admin dashboard + Discord Activity SPA + JSON API
- **`data/sportsbook.db`** — single SQLite database shared by both processes

---

## Prerequisites

- Python 3.11+
- A Discord application with a bot token ([discord.com/developers](https://discord.com/developers/applications))
- For the web dashboard: OAuth2 credentials (Client ID + Secret) from the same application

---

## Quick Start

**1. Clone and install**

```bash
git clone https://github.com/phqen1x/thg-sportsbook.git
cd thg-sportsbook
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

**2. Configure environment**

```bash
cp .env.example .env
# Edit .env — at minimum set BOT_TOKEN
```

**3. Run the bot**

```bash
python3 -m bot.main
```

**4. Run the web dashboard** *(optional)*

```bash
python3 -m web.main
# Open http://localhost:8000
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values. Required fields are marked.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | **Yes** | Discord bot token |
| `GUILD_ID` | **Yes** (web) | Discord server (guild) ID |
| `DISCORD_CLIENT_ID` | **Yes** (web) | OAuth2 client ID |
| `DISCORD_CLIENT_SECRET` | **Yes** (web) | OAuth2 client secret |
| `WEB_SECRET_KEY` | **Yes** (web) | Secret for signing session cookies — generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_ROLE_ID` | No | Role ID that grants admin access. Falls back to server Administrator permission if unset. |
| `ANNOUNCEMENT_CHANNEL_ID` | No | Channel where `/admin announce` posts Capitol announcements |
| `WITHDRAW_CHANNEL_ID` | No | Channel where chip withdrawal requests are posted |
| `DEV_GUILD_ID` | No | Guild ID for instant slash command sync during development |
| `DEFAULT_CHIPS` | No | Starting chip balance for new users (default: `1000`) |
| `CASHOUT_ALLOWED` | No | Enable early cashout globally (default: `false`) |
| `CASHOUT_RATE` | No | Fraction of profit paid on cashout (default: `0.65`) |
| `WEB_BASE_URL` | No | Public URL of the web app, no trailing slash (default: `http://localhost:8000`) |
| `WEB_HOST` | No | Bind address (default: `0.0.0.0`) |
| `WEB_PORT` | No | Port (default: `8000`) |
| `WEB_SSL_CERTFILE` | No | Path to TLS certificate for HTTPS |
| `WEB_SSL_KEYFILE` | No | Path to TLS private key for HTTPS |
| `DB_PATH` | No | Path to SQLite database (default: `data/sportsbook.db`) |

> **Security:** Never commit `.env` to git. Generate a strong `WEB_SECRET_KEY` before deploying — the app will refuse to start if it detects the placeholder value.

---

## Web Dashboard

The web dashboard is a browser-based admin panel secured via Discord OAuth2. Only users who are members of your Discord server and hold the configured admin role can access `/admin`.

**Discord OAuth2 setup:**
1. In your [Discord application](https://discord.com/developers/applications), go to **OAuth2 → Redirects**
2. Add `<WEB_BASE_URL>/auth/callback` (e.g. `https://sportsbook.example.com/auth/callback`)
3. Set `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and `GUILD_ID` in `.env`

**Admin panel features:**
- Manage tributes (create, edit, kill, revive, crown victor)
- Create, open, close, and resolve betting markets
- Set or override market odds; clear overrides to restore calculated odds
- Manage alliances
- Give, take, or set player chip balances
- Configure betting phases, cashout rules, and Capitol announcements
- Create and manage parlay templates

---

## Discord Activity

The embedded in-Discord Activity allows members to browse markets, place bets, and view the leaderboard without leaving Discord. It is served by the same `web/` process — no extra service needed.

See **[ACTIVITY_SETUP.md](ACTIVITY_SETUP.md)** for full setup instructions including the Discord Developer Portal configuration.

---

## Docker

A `Dockerfile` and `docker-compose.yml` are provided for running the bot in a container.

```bash
# Copy and edit your environment file first
cp .env.example .env

docker compose up -d
```

The bot mounts `./data` and `./assets` as volumes so the database and fonts persist across restarts.

To also run the web dashboard, add a second service to `docker-compose.yml`:

```yaml
  web:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./assets:/app/assets
    ports:
      - "8000:8000"
    command: ["python3", "-m", "web.main"]
```

---

## Generating PDFs

The bot ships pre-built PDF guides. To regenerate them from source:

```bash
python3 tools/build_odds_pdf.py       # ODDS_GUIDE.pdf
python3 tools/build_update_guide.py   # BOT_UPDATE_GUIDE.pdf
```

---

## Project Layout

```
bot/
  cogs/        slash command handlers
  database/    SQLAlchemy models and migrations
  odds/        odds calculator and modifier engine
  imaging/     bet slip and leaderboard image generation
  utils/       shared formatters and helpers
web/
  routes/      FastAPI route modules (auth, admin, member, activity)
  templates/   Jinja2 HTML templates
  static/      CSS, JS for the browser dashboard
  activity/    Discord Activity SPA (vanilla JS, no build step)
tools/         standalone scripts (PDF generation, data backfill)
scripts/       utility shell scripts
assets/fonts/  bundled fonts for image generation
data/          runtime database (gitignored)
```

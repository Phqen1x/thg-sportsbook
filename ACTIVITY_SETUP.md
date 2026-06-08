# Capitol Sportsbook — Discord Activity Setup

The bot ships an **embedded Discord Activity**: an in-Discord UI for browsing
markets/odds/leaderboard, placing bets and parlays, tailing public slips, and (for
admins) running live-game operations. It is served by the existing FastAPI web app
(`web/`) and reuses the bot's database and betting logic.

It does **not** need a separate process — the same `python3 -m web.main` server hosts
both the browser dashboard and the Activity.

---

## 1. How it works (architecture)

- **Frontend:** a no-build vanilla-JS SPA in `web/activity/` (the Embedded App SDK is
  vendored at `web/activity/static/discord-sdk.js`, so no npm/bundler is required).
- **API:** JSON endpoints under `/api/activity/*` (see `web/routes/activity.py`).
- **Serving:** Discord loads the Activity at the iframe root with a `?frame_id=…`
  query; `web/app.py` detects that and serves the SPA, while normal browser visits to
  `/` still get the dashboard. Direct browser testing works at `/activity`.
- **Auth (server-verified):**
  1. The SPA does the SDK `authorize()` handshake → one-time OAuth `code`.
  2. `POST /api/activity/token` exchanges the code **server-side** (client secret),
     reads the authoritative Discord identity, and checks admin role with the **bot
     token** against `GUILD_ID` + `ADMIN_ROLE_ID`.
  3. The server mints a short-lived **signed bearer token** (`is_admin` baked in) that
     the SPA sends on every call. Nothing the client claims is trusted; admin
     endpoints require `is_admin` from the verified signature.

---

## 2. Environment

The Activity reuses the variables the web dashboard already needs — no new required
vars (see the "Web Dashboard" section of `.env`):

| Variable | Used for |
| --- | --- |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | OAuth code exchange (server-side) |
| `BOT_TOKEN` | Server-side guild role lookup |
| `GUILD_ID` + `ADMIN_ROLE_ID` | Determines who is an admin in the Activity |
| `WEB_SECRET_KEY` | Signs the activity bearer token (set a strong value!) |
| `WEB_BASE_URL` | Public HTTPS URL of the server; used by `/play`'s browser link |

---

## 3. Discord Developer Portal

In <https://discord.com/developers/applications> → your app:

1. **OAuth2:** confirm the client ID/secret match your `.env`. (No redirect URI is
   needed for the embedded flow — only for the browser dashboard login.)
2. **Activities → Settings → Enable Activities.**
3. **Activities → URL Mappings:** add a single root mapping:
   - **Prefix:** `/`
   - **Target:** your server's host, e.g. `sportsbook.example.com`
     (the host behind `WEB_BASE_URL`, **without** scheme or path).
   This makes Discord proxy `https://<client_id>.discordsays.com/…` →
   `https://<your-host>/…`. The SPA's assets and API calls all ride this one mapping
   via the `/.proxy/` prefix the client adds automatically inside Discord.
4. (Optional) Set the Activity's name/icon/supported platforms.

> Discord requires the target to be reachable over **HTTPS**.

---

## 4. Running

Production / staging (HTTPS already terminated in front of the app):

```bash
python3 -m web.main      # serves dashboard + Activity on WEB_HOST:WEB_PORT
```

Local development (Discord needs a public HTTPS URL):

```bash
# terminal 1 — the app
python3 -m web.main      # e.g. http://localhost:8000

# terminal 2 — expose it over HTTPS
cloudflared tunnel --url http://localhost:8000
# → https://something.trycloudflare.com
```

Then set the Dev Portal **URL Mapping target** to that tunnel host
(`something.trycloudflare.com`) and `WEB_BASE_URL` to the full `https://…` URL.

---

## 5. Launching & testing

- **In Discord:** join a voice channel → **Activities** (rocket) button →
  **Capitol Sportsbook**. Or run `/play` for instructions + a browser link.
- **Standalone (no Discord):** open `https://<your-host>/activity`. The SDK handshake
  only runs inside Discord; for quick UI/API testing in a plain tab you can append a
  pre-minted token, e.g. `…/activity?token=<bearer>` (mint one in a Python shell with
  `web.activity_auth.mint_token`). This is for development only.

### Verifying

1. Auth completes and your username/balance appear in the top bar.
2. Place a straight bet and a parlay — chips deduct, and the same bets show up in the
   bot's `/mybets` (shared database).
3. The **Admin** tab appears only for users with `ADMIN_ROLE_ID`; open/close/resolve a
   market and confirm bets settle.
4. A non-admin never sees the Admin tab, and admin API calls return `403`.

---

## 6. Extending the admin panel

The Activity intentionally exposes only **live-game** admin ops (markets open/close/
resolve, chips give/take, tribute kill/victor). Full setup (tributes, phases,
alliances, settings) stays in the browser dashboard. To add more admin actions later:
add a `bearer_admin`-guarded endpoint in `web/routes/activity.py` and surface it from
the matching section in `web/activity/static/app.js` (`viewAdmin` / `admin*`
functions) — the patterns mirror the existing live-ops actions.

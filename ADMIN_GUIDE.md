# THG Sportsbook — Admin Guide

This guide includes first-time setup, then everything you'll actually touch day-to-day once the Games are live. Notably, the built-in Discord Activity and the [website](Phoenix will handle this for the time being) contain admin commands for most if not all things you will need including viewing stats, managing tributes, chips, phases, alliances, parlays, and settings.

---

## Part 1 — First-Time Setup

You only do this once per deployment. Phoenix will handle it for the foreseeable future, but for posterity this is good info to have.

### Point a few channels and settings

Once the bot is running, these are one-time (or rarely-touched) configuration commands, all under `/admin settings`:

- `/admin settings admin_role role:` — set which role can use admin commands
- `/admin settings announce_channel channel:` — where `/admin settings announce` posts go
- `/admin settings withdraw_channel channel:` — where member `/withdraw` and `/deposit` requests land
- `/admin settings log_channel channel:` — where an audit log of admin actions gets posted
- `/admin settings default_chips amount:` — starting balance for newly-seen users
- `/admin settings theme name:` — visual theme for generated odds/leaderboard images

Everything above can be re-run at any time if you change your mind.

---

## Part 2 — Running a Game, Day to Day

This is the normal cycle: build a roster, start the Games, progress through phases, resolve events as they happen, end the Games, repeat next season.

### 2.1 Build the roster (Phoenix will handle this for the time being)

- `/admin tribute add name: district: gender: age: ...` — add one tribute. Also takes `member:` (links a Discord user for a seniority odds bonus), `sade_participant`/`sade_champion`, `times_played`/`highest_placement` (prior-Games history factors into odds).
- For a full cast at once: `/admin tribute import_template` downloads a JSON template, fill it in, then `/admin tribute mass_import file:`. 
- `/admin tribute set_score` — set (or update) a tribute's training score; this also resolves any score-dependent markets that were waiting on it.
- `/admin tribute list` — see the full roster.
- `/admin tribute edit` / `/admin tribute remove` — fix mistakes or pull a tribute entirely.

Adding tributes and markets automatically triggers a re-price of the odds board, so you don't need to manually recalculate after routine roster changes.

### 2.2 Set up phases (if not already seeded)

The Games move through named phases (Pre-Games, Bloodbath, Arena, etc.) that gate which markets are open:

- `/admin phase add name: description: sort_order:` — add a phase
- `/admin phase list` / `/admin phase delete` — manage them

### 2.3 Start the Games

`/admin game start` — activates the game and opens Pre-Reaping markets. If a game is already running, you'll be told to `/admin game end` it first.

### 2.4 Progress the game

- `/admin game set_phase phase_id: confirm:` — move to the next betting phase. If some tributes are missing training scores it'll ask you to pass `confirm:yes` to proceed anyway.
- `/admin game arena arena_type:` — set Natural / Artificial / Neutral; this reprices death-cause markets accordingly.
- `/admin tribute kill` — mark a tribute dead (records cause, killer, etc. and resolves the markets that hinge on it).
- `/admin tribute unkill tribute_id:` — undo a kill and restore whatever markets it resolved.
- `/admin tribute debilitate` — apply a temporary odds penalty (Debilitated / Moderately / Severely / clear) for an injured tribute.
- `/admin alliance create` / `add_tribute` / `remove_tribute` / `delete` / `list` — manage alliances as they form and break in-story.

### 2.5 Markets

Most markets auto-generate per tribute/alliance, but you'll manage them directly too:

- `/admin market add market_type: ...` — one-off market (kill events, custom props, etc.) — parameters vary by type (tribute/district/alliance target, cause, placement number, top-N, O/U line).
- `/admin market list` — browse everything with pagination.
- `/admin market close` / `reopen` — stop or resume new bets on a specific market.
- `/admin market odds market_id: odds:` — manually override a market's odds.
- `/admin market clear-override market_id:` — remove one override (or omit `market_id` to clear *all* overrides) and let the model reprice it.
- `/admin market recalc` — force a full re-price of every non-overridden market against the current roster.
- `/admin market bulk_close` / `bulk_open` — close/open in bulk, optionally filtered by type or phase.
- `/admin market backfill` — create any auto-markets that are missing for existing tributes/alliances (useful after an import or a bug fix).
- `/admin market_type create/list/edit/delete` — define custom market types that persist across games, for prop bets outside the built-in categories.

### 2.6 Resolving outcomes

Individual markets settle via `/admin market resolve market_id: result:<WIN|LOSS|VOID>`. For bulk/structural settlement there's a dedicated `/admin resolve` group:

- `/admin resolve placements` — settle all placement markets (exact spot, top-N, O/U, runner-up) at once
- `/admin resolve top_killer` — settle "Most Kills" markets
- `/admin resolve duration days:` — settle Games-duration O/U markets
- `/admin resolve feast` / `betrayal` — settle "Games features a Feast/Betrayal" markets
- `/admin resolve bb_double_kill` — manual override for bloodbath double-kill markets
- `/admin resolve trap_deaths` / `env_deaths` — settle arena trap/environmental death-count markets
- `/admin resolve type market_type:` — nuclear option: resolve *every* open/closed market of one type to the same outcome

### 2.7 End the Games

`/admin game end victor_id:` — crowns the victor, sets their placement, and settles win markets.

### 2.8 Curate parlays for members

`/admin parlay` manages the pre-built parlays that show up under members' `/featured` and tail board:

- `/admin parlay create` — start an empty template, then `/admin parlay addleg` / `removeleg` to build it
- `/admin parlay save_slip` — turn your own current `/parlay` slip into a reusable template
- `/admin parlay toggle` — show/hide a template on the tailing board
- `/admin parlay set-description` — edit the blurb shown for a parlay (works on auto-generated ones too)
- `/admin parlay generate` — regenerate the auto-featured parlays from currently open markets
- `/admin parlay ai-lore` (upload a PDF of district lore) + `/admin parlay ai-generate` — use the local Lemonade AI integration to generate smarter, lore-aware featured parlays
- `/admin parlay list` / `delete` — manage existing templates

### 2.9 Watch the numbers

`/admin stats overview` / `kills` / `districts` / `alliances` — live running snapshots: survivors, eliminations, kill leaders, per-district and per-alliance breakdowns.

### 2.10 Cross-game legacy stats

`/admin history games` / `arena` / `set` / `list` / `reset` — maintain the aggregate historical stats (past Games count, arena-type history, per-district track record) that feed the odds model's "District Legacy" factor season over season.

---

## Part 3 — Managing the Economy

- `/admin settings chips_give user: amount:` / `chips_take` / `chips_set` — adjust one user's balance
- `/admin settings chips_give_all amount: role:` — give chips to everyone (optionally filtered by role)
- `/admin settings chips_reset` — reset **everyone** to the default balance (use with care)
- `/admin settings default_chips amount:` — change the starting balance for new users going forward
- **Withdraw/deposit requests:** when a member runs `/withdraw` or `/deposit`, a request is posted to your configured `withdraw_channel`. It's on you to manually verify the Panars side and then credit/debit their chips with `/admin settings chips_give` / `chips_take` — the bot doesn't touch the external economy itself.
- **Cashout rules:** `/admin settings cashout` (global on/off + rate), `market_cashout` (override for one market), `market_type_cashout` (override for a whole market type), `parlay_cashout` (override for one public parlay)

---

## Part 4 — Moderation & Restrictions

`/admin restrict`:

- `ban` / `unban` — block/unblock a user from placing *any* bet
- `block_district` / `unblock_district` — restrict betting on a specific district
- `block_tribute` / `unblock_tribute` — restrict betting on a specific tribute
- `view` — see all active restrictions for a user
- `lock_tribute member: tribute_id:` — mark a Discord user as *playing* a specific tribute, which locks them out of the bot, the web dashboard, and the Activity entirely (prevents someone betting on their own outcome). `unlock_tribute` reverses it.

---

## Part 5 — Odds Modifiers (advanced)

`/admin modifier` lets you nudge odds for story reasons (training accidents, political favor, injuries) without hand-setting a specific market:

- `create` — define a reusable modifier
- `assign` / `bulk_assign` — apply it to a tribute, district, or alliance (one at a time or in bulk)
- `unassign` — remove an assignment
- `list` — see all modifiers and what they're currently applied to
- `delete` — remove a modifier and all its assignments

Modifiers are applied field-relative and tempered automatically so they don't pile everything up against the odds cap — you don't need to compensate manually.

---

## Part 6 — Pausing Betting

`/admin game pause_betting state:<Pause betting | Resume betting>` — pause all betting. While paused, members can't place straight bets or submit/tail parlays (their existing pending bets are untouched). Use this during coverage when you want to lock down betting, then resume when you're ready.

---

## Part 7 — Danger Zone

`/admin game reset_confirm` — **deletes all tributes, bets, parlays, and markets.** This is for starting a brand-new season from scratch, not for undoing a mistake mid-game. There's no undo — double-check before you run it.

---

## Quick Reference

`/admin tribute` | Roster: add, edit, kill, unkill, remove, debilitate, scores, imports |
`/admin market` | Individual markets: add, list, close, reopen, odds, overrides, recalc, backfill |
`/admin market_type` | Custom market type definitions |
`/admin resolve` | Bulk/structural market settlement |
`/admin game` | Start, end, phase transitions, arena type, pause betting, reset |
`/admin phase` | Betting-phase definitions |
`/admin alliance` | Alliance management |
`/admin parlay` | Curated/pre-built parlay templates, incl. AI generation |
`/admin stats` | Live running-game snapshots |
`/admin history` | Cross-game legacy stats |
`/admin modifier` | Odds modifiers for tributes/districts/alliances |
`/admin settings` | Chips, cashout rules, channels, admin role, theme |
`/admin restrict` | Bans, district/tribute betting blocks, tribute-player locks |

Everything above accepts Discord's autocomplete — start typing an ID field and the bot will suggest valid options, so you rarely need to look up a raw tribute or market ID by hand.

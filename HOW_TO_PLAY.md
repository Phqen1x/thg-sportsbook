# How To Play — Panem Sportsbook

Welcome to the official wagering service for the Interactive Discord Hunger Games. Every member starts with a 1000 chip balance, you can gain more through depositing Panars and winning bets.

---

## 1. Your Balance

Run **`/balance`** any time to see your current chip count, overall stats, and win/loss record.

Chips are the currency you actually bet with. Chips can be converted to Panars, and vice versa:

- **`/deposit amount:`** — convert Panars into chips (an admin will verify and credit you; minimum 5,000).
- **`/withdraw amount:`** — convert chips back into Panars (sends a payout request to the admins; minimum 5,000).

---

## 2. Finding Something to Bet On

Before you bet, make sure to check the current odds, what tributes are playing, and what markets you can bet on:

**`/odds`** | The Odds board — top tributes by training score with live win odds, plus a few featured non-win markets. Add the `tribute:` argument to zoom in on one tribute's full market spread. 
**`/tributes`** | The full roster grouped by status (Alive / Dead / Victor) with training scores and running kill counts. 
**`/markets`** | Every open market, paginated and grouped by category, with live odds displayed. 

Odds update live as the game plays out — a death, a new alliance, or a phase change can shift the whole board, so re-check `/odds` before locking in a bet you've been sitting on.

---

## 3. Placing a Straight Bet

**`/bet`** is your basic single-market wager.

```
/bet market_id:<market> amount:<chips>
```

If you don't already know the market's ID, don't worry — start typing and use the narrowing options before it:

- `subject_type` — narrow by category (tribute / district / alliance), optional
- `subject` — the specific tribute, district, or alliance, optional
- `market_type` — narrow by market category, optional
- `market_id` — the actual market to bet on (autocompletes as you type; you can also just start typing a tribute or market name directly here and skip the narrowing fields entirely)
- `amount` — how many chips to wager (1–500,000)

Your payout is calculated and shown the moment you place the bet, and it's locked in at those odds even if the market's odds shift afterward. Once a market **closes**, no new bets are accepted on it.

---

## 4. Building a Parlay

A parlay chains multiple markets into one ticket — every leg has to hit for the parlay to pay, but the combined payout climbs fast with each leg you add (up to 10 legs).

```
/parlay add market_id:<market>        → add a leg to your slip (repeat per leg)
/parlay view                          → preview your slip and potential payout
/parlay remove leg_number:<n>         → drop a specific leg (see /parlay view for numbers)
/parlay clear                         → wipe the whole slip and start over
/parlay submit wager:<chips>          → lock it in
```

`/parlay submit` also takes two optional arguments:
- `public:` — list your parlay on the public tail board for others to copy (opted in by default)
- `name:` — a custom title for your public parlay on the tail board 

If a leg gets voided (if the market is cancelled for any reason), that leg drops from the parlay and your payout recalculates across whatever legs remain — it doesn't cancel or ruin the whole parlay.

Changed your mind about a public parlay? **`/parlay unlist parlay_id:`** pulls it off the tail board without affecting the bet itself.

### Riding Someone Else's Slip

Not sure what to build yourself? Browse what's already out there:

- **`/featured`** — Gamemaker-curated parlays, ready to tail at current live odds.
- **`/parlay tail`** — browse *all* parlays on thet ail board (featured and other members' public slips) and copy one with a click.

Tailing copies the legs at today's odds into a fresh slip for you to submit — it doesn't touch the original bettor's ticket.

---

## 5. Cashing Out Early

Don't want to sweat it to the final result? **`/cashout`** lets you exit a pending bet or parlay before it settles. Cashing out is disabled by default, but can be enabled by Gamemakers on specific markets and parlays.

```
/cashout cashout_type:<Single Bet | Parlay> cashout_id:<bet or parlay>
```

Cashout pays your wager back plus a cut of your potential profit — less than a full win, but guaranteed and immediate. 

---

## 6. Keeping Track

**`/mybets`** | Your personal bet history — filter by All, Pending, Won, or Lost. Shows straight bets and parlays.
**`/leaderboard`** | See a variety of different leaderboards of server members -- Most Chips, Most Chips Bet, Most Bets Won, Most Parlays Won, Most Parlays Tailed.
**`/balance`** | View your current chip count and other related stats.

---

## 7. Don't want to type out all these commands? Use the built in Discord Activity or visit the website!

Run **`/play`** to view instructions on how to play the Panem Sportsbook **Discord Activity** and view the link to the [website](https://panem-sportsbook.phqen1x.com/). 

The activity is a full in-Discord app for browsing markets, checking odds, and placing bets/parlays without typing a single slash command. Everything you can do with `/bet` and `/parlay` is available there too, in a point-and-click UI. 

The [website](https://panem-sportsbook.phqen1x.com/) is similar to the activity, an easy to use dashboard with all the same capabilities of the discord commands and activity. 

---

## 8. What You Can Bet On

Markets are grouped into categories. Most are auto-generated the moment a tribute enters the Games; special one-off markets can be added by Gamemakers as the game develops.

- **Tribute Win** — will this tribute be crowned victor?
- **Placement** — exact finishing position, top-N cutoffs, runner-up
- **Kills** — most kills in the Games, specific kill events, over/under kill totals
- **Bloodbath & Early Game** — bloodbath survival, first kill of the Games
- **Death Cause** — tribute-caused, mutt, trap/event, or environmental
- **District Futures & Alliance** — which district produces the victor, alliance performance, partner-vs-partner outcomes
- **Special & Sponsor Events** — one-off markets tied to specific game moments, added by Gamemakers as they happen

---

## 9. A Few Things to Know

- **Betting can be paused sportswide.** If Gamemakers pause betting (e.g. mid-event), `/bet` and `/parlay submit`/tailing will be temporarily blocked until they resume it — this isn't a bug.
- **Betting phases matter.** The Games move through phases (Pre-Games, Bloodbath, Arena, etc.), and some markets only open during specific phases. If a market you want isn't showing up, it may not be live yet.
- Odds are entirely algorithmically-calculated by the bot using training scores, full district history, alliances, server seniority, and any active Capitol modifiers — no player or Gamemaker hand-sets an individual tribute's number.
- Bets are final once submitted; cashout (where available) is your only way out early.

May the odds be ever in your favor.

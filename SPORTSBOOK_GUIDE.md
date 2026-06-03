# THG Sportsbook — Server Guide

Welcome to the **THG Sportsbook**, the Capitol's official wagering service for the Hunger Games. Use your chips to bet on tributes, build parlays, and climb the leaderboard. May the odds be ever in your favor.

---

## Getting Started

Every server member starts with a chip balance when they first interact with the bot. Chips are earned by winning bets and lost by losing them. Check your current balance, betting history, and stats at any time with `/balance`.

---

## Placing Bets

### Straight Bets

Browse all available markets with `/markets`. Each market has an ID, a label describing what you're betting on, and live odds displayed in American format. When you find a market you like, place a wager with `/bet`.

- Markets are only open during active betting windows — once a market closes, no new bets are accepted.
- Odds are live and may shift as game conditions change.
- Your potential payout is calculated and shown at the time you place your bet, and locked in at those odds.

### Parlays

A parlay lets you chain multiple markets into a single bet with a combined payout. The more legs you add, the higher the potential return — and the higher the risk.

- Use `/parlay add` to build your slip one market at a time.
- Preview your slip and its potential payout with `/parlay view`.
- When you're satisfied, lock it in with `/parlay submit`.
- All legs must win for the parlay to pay out. If any leg loses, the entire parlay loses. If a leg is voided (market cancelled), that leg is removed and the payout recalculates across the remaining legs.
- You can hold up to ten legs on a single parlay.

### Early Cashout

If you want to take your winnings before a market settles, you may be able to cash out early with `/cashout`. Cashout returns your original wager plus a portion of your potential profit — less than a full win, but guaranteed. Not all markets allow cashout; check availability when browsing.

---

## What You Can Bet On

Markets are organized into categories. Most categories auto-generate for every tribute when they enter the Games, with additional one-off and event markets added by admins as the game progresses.

### Tribute Win Markets
Will a specific tribute win the entire Games and be crowned victor?

### Placement Markets
Will a tribute finish at a specific placement, or finish inside/outside a top-N cutoff?

### Kill Markets
Will a tribute lead the Games in total kills? Will a specific tribute kill another specific tribute? Will a tribute go over or under a projected kill total?

### Bloodbath & Early Game
Will a tribute survive the bloodbath? Will a tribute land the first kill of the Games?

### Death Cause Markets
If a tribute dies, what will be the cause — another tribute, a mutt, the Gamemakers, or natural/environmental causes?

### Special & Sponsor Events
Admin-created markets tied to game events, Capitol decisions, or sponsor outcomes. These can appear at any point during the Games.

---

## How Odds Are Determined

Odds are not set manually — they are calculated dynamically by the bot and update as game conditions change. Several factors feed into each tribute's odds:

**Training Score**
A tribute's training score is the primary driver of their odds across most markets. A higher score generally means better odds on favorable outcomes and worse odds on longshots.

**District Legacy**
Historical performance of each district across past Hunger Games influences current odds. Districts with strong track records receive a subtle edge; districts with poor histories face a slight disadvantage.

**Alliances**
Tributes in alliances share an odds influence with their allies. A strong alliance can lift the odds of a weaker member; a weak alliance can drag down a stronger one.

**Server Seniority**
The length of time a tribute's linked Discord member has been in the server factors into their odds. Long-tenured members represent experienced players, and their tributes reflect that.

**Active Modifiers**
The Capitol may apply modifiers to specific tributes or entire districts at any time — reflecting training advantages, injuries, political favor, or other in-game developments. These modifiers shift odds up or down accordingly.

**Arena Type**
For death cause markets specifically, the type of arena matters. Artificial arenas favor Gamemaker-driven deaths; natural arenas favor environmental and mutt-related causes.

All of these factors are layered on top of each other and recalculate automatically whenever the game state changes — a tribute's death, a new alliance, or a phase transition can all shift the odds board in real time.

---

## Keeping Track

**`/odds`** — View the Hot Odds board: a styled card showing the top tributes by training score with their current win odds, plus a selection of featured non-win markets.

**`/tributes`** — Full roster of all tributes grouped by status (Alive, Dead, Victor) with training scores and kill counts.

**`/markets`** — Paginated browser of every open market, organized by category, with odds and implied probability.

**`/mybets`** — Your personal bet history. Filter by All, Pending, Won, or Lost. Displays straight bets and parlays in a styled card layout.

**`/leaderboard`** — See who's leading the server in total chips.

---

## Betting Phases

The Games progress through distinct phases — Pre-Games, the Bloodbath, the Arena, and beyond. Markets may be tied to specific phases and will open or close automatically as the game advances. Keep an eye on phase transitions; they can create short windows where certain markets briefly become available.

---

## Fair Play

- All odds are bot-calculated and publicly visible. No market is manually set by players.
- Capitol announcements from admins will be posted to the announcements channel for major game events.
- Bets are final once submitted. Cashout is the only way to exit a position early, and only when the market allows it.

Good luck, and may your tributes carry you to the top of the leaderboard.

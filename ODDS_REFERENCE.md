# Odds Reference — How Everything Affects a Tribute's Odds

All odds on non-overridden open markets are recalculated automatically whenever a tribute is added or killed. The pipeline has four stages applied in order:

```
Base Odds  →  Group Influence  →  Modifier Factor  →  Arena Factor (DEATH_CAUSE only)
```

Probabilities are clamped to [0.01, 0.99] at every stage. Final odds are rounded to the nearest 5 (American format).

---

## Stage 1 — Base Odds (Training Score)

The foundation. Each formula produces a raw probability from the tribute's training score relative to all alive tributes.

**Notation:** `score` = this tribute's training score, `total` = sum of all alive scores, `n` = number of alive tributes.

### TRIBUTE_WINS
```
prob = score / total
```
Pure proportional share of the alive field's training scores.

### TRIBUTE_PLACEMENT (Exact)
```
prob = min(0.95,  (score / total) × placement_multiplier)
```
| Placement | Multiplier |
|-----------|------------|
| 2nd       | ×1.6       |
| 3rd       | ×2.2       |
| 4th       | ×2.8       |
| 5th       | ×3.5       |
| 6th       | ×4.0       |
| 7th       | ×4.5       |
| 8th       | ×5.0       |
| 9th+      | ×(place × 0.8) |

Lower placements (closer to winning) push harder tributes to heavy favorites.

### TRIBUTE_TOP_N
```
prob = min(0.95,  (score / total) × k × 0.7)
```
`k` = the N value. The 0.7 dampener keeps very large fields from producing near-certain probabilities.

### TRIBUTE_KILLS (Top Killer)
```
prob = score² / sum(all alive scores²)
```
Squaring amplifies score differences — a tribute with 10 vs a field of 8s is a much bigger favorite here than in the win market.

### KILL_EVENT (A kills B)
```
prob = (score_a / total) × (score_b / total) × n × 0.4
```
Capped at 0.90. Depends on both tributes' strength; scaling by `n` accounts for more opportunities in larger fields.

### BLOODBATH_SURVIVOR
```
prob = 1.0 - (score / total) × 0.5
```
Clamped to [0.10, 0.90]. **Inverted logic:** weaker tributes are more likely to survive the bloodbath (they are ignored; top scorers draw attention). A tribute at exactly half the total weight has about 75% survival odds.

### FIRST_BLOOD
```
prob = score / total
```
Identical to TRIBUTE_WINS — proportional share of the field.

### KILLS_OU
Expected kill rate first: `kill_rate = (score / total) × max(1, n-1) × 0.5`

| Side  | Threshold   | Formula                                     | Clamp         |
|-------|-------------|---------------------------------------------|---------------|
| OVER  | ≤ 1 kill    | `kill_rate / (kill_rate + 0.5)`             | [0.05, 0.90]  |
| OVER  | > 1 kill    | `kill_rate / (threshold + kill_rate × 0.5)` | [0.05, 0.85]  |
| UNDER | ≤ 1 kill    | `0.5 / (kill_rate + 0.5)`                  | [0.10, 0.90]  |
| UNDER | > 1 kill    | `threshold / (threshold + kill_rate)`       | [0.10, 0.90]  |

### PLACEMENT_OU
```
under_prob = min(0.92, max(0.05,  (score / total) × line))
UNDER odds = under_prob
OVER  odds = 1.0 - under_prob
```
The line defaults to `n / 2.0` (median placement) if not set.

### DEATH_CAUSE
No training-score formula. Falls through to `+200` as the starting point before the Arena Factor (Stage 4) adjusts it.

### Custom Market Types
Fall through to `DEFAULT_FALLBACK_ODDS = +200`, or use the template's `default_odds` override if set, or the difficulty ladder:

| Difficulty | Default Odds |
|------------|-------------|
| Easy       | -200        |
| Moderate   | +100        |
| Hard       | +300        |
| Very Hard  | +700        |
| Longshot   | +1500       |

---

## Stage 2 — Group Influence (District & Alliance Peers)

**Applies to:** TRIBUTE_WINS, TRIBUTE_PLACEMENT, TRIBUTE_TOP_N, TRIBUTE_KILLS, FIRST_BLOOD, BLOODBATH_SURVIVOR, KILLS_OU, PLACEMENT_OU.  
Does **not** apply to: KILL_EVENT, DEATH_CAUSE, PLACEMENT_OU OVER (inverted — see below), custom types.

The tribute's raw probability is nudged toward the average probability of their peers:

```
base_prob   = score / total

district_adj = DISTRICT_ALPHA  × (avg_districtmate_prob − base_prob)   # α = 0.10
alliance_adj = ALLIANCE_ALPHA  × (avg_ally_prob        − base_prob)    # α = 0.20

adjusted_prob = base_prob + district_adj + alliance_adj
factor        = adjusted_prob / base_prob
```

Then `stage1_probability × factor` is the new probability.

**Effect summary:**
- Strong districtmate/ally → your odds get slightly better (pulled up).
- Weak districtmate/ally → your odds get slightly worse (pulled down).
- District influence is half the strength of alliance influence.
- Only **alive** peers count.

**PLACEMENT_OU OVER special case:** the group factor is applied to the implied UNDER probability, then inverted, so a stronger field still points correctly toward a worse placement.

---

## Stage 3 — Modifier Factor

A single compound multiplier applied to the probability from Stage 2. Built in four layers:

### Layer A — Direct & District Modifiers
Explicit odds modifiers created via `/admin modifier`. Applied multiplicatively:

```
raw_factor = 1.0
  × weight_of_each_direct_tribute_modifier
  × weight_of_each_district_modifier_for_tribute's_district
```

Multiple modifiers on the same tribute stack multiplicatively (e.g., ×1.5 × ×0.8 = ×1.2).

### Layer B — Alliance Bleed
```
own = own + 0.20 × (avg_ally_modifier − own)
```
20% of the average modifier weight of alive alliance members bleeds into this tribute's factor. A tribute with no modifier assigned but allied to heavily-boosted tributes will receive a small boost.

### Layer C — Seniority (Discord Membership Length)

| Server tenure         | Factor |
|-----------------------|--------|
| < 30 days             | ×0.5   |
| 30 days – 1 year      | ×1.0   |
| 1–2 years             | ×1.1   |
| 2–3 years             | ×1.2   |
| 3–4 years             | ×1.3   |
| N years (N ≥ 1)       | ×(1.0 + 0.1 × floor(N)) |

Only applies if the tribute has a linked server member. Use `seniority_date` on `/admin tribute edit` to override the join date (e.g., for returning members).

### Layer D — District Historical Factor

Damping constant: **HIST_ALPHA = 0.15** — meaning historical stats can shift odds by at most ±15% of the gap from neutral.

The district's aggregate stats are each compared to the global average across all districts that have data:

| Stat                          | Direction  | Notes                                    |
|-------------------------------|------------|------------------------------------------|
| Total kills per game          | Higher = better | Normalized by `num_games`          |
| Average placement             | **Lower = better** (inverted) |                       |
| Total wins                    | Higher = better |                                    |
| Top-8 finishes                | Higher = better |                                    |
| Top-5 finishes                | Higher = better |                                    |
| Kill record                   | Higher = better |                                    |
| Runner-up finishes per game   | Higher = better | Normalized by `num_games`          |
| Avg placement (last 5 games)  | **Lower = better** (inverted) |                       |
| Bloodbath kills per game      | Higher = better | Normalized by `num_games`          |

Only stats that are set (non-null) contribute. All contributing ratios are averaged, then dampened:
```
raw_factor = average(district_stat / global_avg_stat)   ← each non-null stat
hist_factor = 1.0 + (raw_factor − 1.0) × 0.15
```

### Applying the Compound Factor

After layers A–D are combined:
```
combined_factor = raw_factor_after_alliance_bleed × seniority_factor × hist_factor

adj_prob = base_prob × combined_factor   (clamped to [0.01, 0.99])
```

Same PLACEMENT_OU OVER inversion applies as in Stage 2.

---

## Stage 4 — Arena Death Factor (DEATH_CAUSE only)

Applied after all other stages. Adjusts the probability of each death cause based on the current arena type (set via `/admin game arena`).

| Death Cause      | Artificial Arena | Natural Arena |
|------------------|:-:|:-:|
| Natural Causes   | ×0.60 | ×1.50 |
| Mutt             | ×0.80 | ×1.25 |
| Another Tribute  | ×1.00 | ×1.00 |
| Gamemakers       | ×1.50 | ×0.60 |

No arena type set → all factors are ×1.0.

---

## Odds Override

Setting `odds_override = True` on a market (via `/admin market odds`) pins the odds permanently. That market is **excluded from all recalculation** until the override is removed.

---

## Quick Reference — Constants You Can Tune

All tunable constants live in `bot/odds/defaults.py`:

| Constant                  | Current Value | What it controls                                           |
|---------------------------|:-------------:|------------------------------------------------------------|
| `_DISTRICT_ALPHA`         | 0.10          | How much alive districtmates shift your odds               |
| `_ALLIANCE_ALPHA`         | 0.20          | How much alive allies shift your odds (2× district)        |
| `MODIFIER_ALLIANCE_ALPHA` | 0.20          | How much of allies' modifier weight bleeds into yours      |
| `HIST_ALPHA`              | 0.15          | Max district-history influence on odds (dampening factor)  |
| `DEFAULT_FALLBACK_ODDS`   | +200          | Starting odds for DEATH_CAUSE and unrecognized types       |
| `PLACEMENT_MULTIPLIER`    | see table     | Per-placement odds boost for exact placement markets       |
| `ARENA_DEATH_CAUSE_FACTORS` | see table   | Per-cause probability multiplier by arena type             |

Seniority thresholds and factors live in `_seniority_factor()` in `bot/cogs/admin.py`.  
Difficulty-to-odds mapping lives in `DIFFICULTY_ODDS` in `bot/cogs/admin.py`.

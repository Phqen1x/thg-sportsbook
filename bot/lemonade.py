"""
Lemonade local-AI client.

Wraps the Lemonade Server's OpenAI-compatible HTTP API so the rest of the
codebase never needs to know which model or backend is running.  Keep this
module generic — other use-cases beyond parlay generation can call
`LemonadeClient.chat_complete()` directly.

Lemonade Server: https://github.com/lemonade-sdk/lemonade
Default base URL: http://localhost:13305
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("capitol.lemonade")

# ---------------------------------------------------------------------------
# Generic client
# ---------------------------------------------------------------------------

_TEXT_LABELS = {"tool-calling", "vision"}
_EXCLUDE_LABELS = {"image", "embeddings"}


class LemonadeClient:
    """Async HTTP client for a running Lemonade Server instance."""

    def __init__(self, base_url: str = "http://localhost:13305", model: str = "auto") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._resolved_model: str | None = None

    async def _resolve_model(self, http: httpx.AsyncClient) -> str:
        """Return the model ID to use, auto-detecting a text model when needed."""
        if self.model != "auto":
            return self.model
        if self._resolved_model:
            return self._resolved_model
        resp = await http.get(f"{self.base_url}/v1/models")
        resp.raise_for_status()
        models = resp.json().get("data", [])
        # Prefer hot (currently-loaded) text models to avoid cold-start 400s.
        hot: str | None = None
        fallback: str | None = None
        for m in models:
            labels = set(m.get("labels", []))
            if labels & _TEXT_LABELS and not (labels & _EXCLUDE_LABELS):
                if fallback is None:
                    fallback = m["id"]
                if "hot" in labels and hot is None:
                    hot = m["id"]
        chosen = hot or fallback
        if not chosen:
            raise RuntimeError("No suitable text model found on Lemonade server")
        self._resolved_model = chosen
        log.info("Lemonade auto-selected model: %s (hot=%s)", chosen, hot is not None)
        return self._resolved_model

    async def chat_complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        num_ctx: int = 0,
        json_mode: bool = False,
        timeout: float = 600.0,
        http: httpx.AsyncClient | None = None,
        _retries: int = 2,
    ) -> str:
        """Send a chat completion request; return the assistant's reply text.

        num_ctx: override the model's context window (llama-server extension).
                 0 means use the server default.
        http:    reuse a caller-owned AsyncClient (e.g. when fanning out many
                 requests in parallel). When None, a client is created and
                 closed for this single call.
        """
        if http is not None:
            return await self._chat_complete(
                http, messages,
                temperature=temperature, max_tokens=max_tokens,
                num_ctx=num_ctx, json_mode=json_mode, _retries=_retries,
            )
        async with httpx.AsyncClient(timeout=timeout) as owned_http:
            return await self._chat_complete(
                owned_http, messages,
                temperature=temperature, max_tokens=max_tokens,
                num_ctx=num_ctx, json_mode=json_mode, _retries=_retries,
            )

    async def _chat_complete(
        self,
        http: httpx.AsyncClient,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        num_ctx: int,
        json_mode: bool,
        _retries: int,
    ) -> str:
        model = await self._resolve_model(http)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if num_ctx:
            payload["num_ctx"] = num_ctx
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(_retries + 1):
            resp = await http.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
            )
            if resp.status_code in (400, 503) and attempt < _retries:
                log.warning(
                    "Lemonade %s (attempt %d/%d): %s — retrying in 5s",
                    resp.status_code, attempt + 1, _retries + 1, resp.text[:200],
                )
                # Clear cached model so next attempt re-checks hot status
                self._resolved_model = None
                await asyncio.sleep(5)
                model = await self._resolve_model(http)
                payload["model"] = model
                continue
            if not resp.is_success:
                log.error("Lemonade %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            break

        content = resp.json()["choices"][0]["message"]["content"] or ""
        log.debug("Lemonade raw response: %s", content[:500])
        return content

    async def list_models(self) -> list[str]:
        """Return model IDs available on this Lemonade instance."""
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(f"{self.base_url}/v1/models")
            resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", [])]

    async def health(self) -> bool:
        """Return True if the Lemonade server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(f"{self.base_url}/v1/models")
                return resp.status_code < 500
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Parlay-generation helper
# ---------------------------------------------------------------------------

_PARLAY_SYSTEM = """\
You are a Panem Sportsbook analyst for the Hunger Games universe.
You receive district lore, historical performance stats, and live tribute data,
then suggest thematic parlay bets that tell a coherent narrative story grounded
in each district's identity, proven track record, and current momentum.

For each parlay, write a 2–3 sentence pitch that makes a bettor WANT to tail
it. Use lore, historical stats, and tribute data as EVIDENCE for a persuasive
argument — do not just list the data. The structure should be: hook (why this
angle is compelling right now) → evidence (one or two specific facts that
support it) → closer (the punchy reason to pull the trigger). Write like a
sharp analyst selling a pick, not a Wikipedia summary. Vary your sentence
rhythm; avoid starting consecutive sentences with "District X has...".

CRITICAL — description accuracy: the description must only claim you are
"backing" or "riding" a tribute/district if every leg in the parlay bets IN
THEIR FAVOUR. If any leg pits two tributes against each other (e.g. "D4F
places higher than D4M"), the description must reflect that — you are backing
one tribute OVER the other, not both. Never write a description that
contradicts the actual direction of the legs.

Always respond with valid JSON only — no markdown fences, no prose outside the JSON.
"""

_PARLAY_USER_TMPL = """\
=== DISTRICT LORE ===
{lore}

=== DISTRICT HISTORICAL STATS ===
Format: D# | rep=1-5(1=best) | funding | wins | avg_place | last5_place | top8 | top5 | kills | bb_kills | kill_rec | avg_ts
{district_records}

=== ALIVE TRIBUTES ===
Format: D# | name | gender | age | kills | training_score | times_played | debilitation
{tributes}

=== OPEN MARKETS (ID: label | odds) ===
{markets}

=== TASK ===
Generate exactly {count} parlay suggestions covering tiers: {tiers}.
Use the district lore AND historical stats together to build narrative-driven,
intelligently differentiated picks. Tiers are defined by the parlay's COMBINED
American odds (legs multiply together, so combined odds grow fast):
  - SAFE     : combined odds below +500 (favor historically strong districts and
               low-odds markets; typically fewer or shorter-priced legs)
  - BALANCED : combined odds between +500 and +3000
  - LONGSHOT : combined odds above +3000 (take risks grounded in underdog lore or
               surging kill leaders; longer-priced legs)
Choose legs so each parlay's combined odds land in its tier's range.

STRICT THEME RULE — follow these steps for EVERY parlay, in order:
  1. Before choosing any subject, scan the OPEN MARKETS list and tally how many
     market IDs exist for each district/tribute. Only consider subjects that
     have AT LEAST 3 matching market IDs. If a district or tribute has fewer
     than 3 open markets, skip it — you cannot build a valid parlay from it.
  2. From the eligible subjects (3+ markets), choose one: one district, one
     tribute, or one named alliance. IMPORTANT: treat D#M and D#F as the same
     district as D# — e.g. D6, D6M, and D6F all refer to District 6. If any
     parlay in this batch already uses District 6 (in any form), you MUST
     choose a different district. Each parlay must focus on a DISTINCT district
     or tribute; no two parlays may share the same district number.
  3. Collect ONLY the market IDs whose label explicitly mentions that district,
     tribute, or alliance. Do NOT include markets about any other district or
     tribute, even indirectly.
  4. From those filtered IDs, pick 3–6 legs that hit your target tier.
  5. For EACH chosen leg, write out in plain English who you are betting FOR and
     who (if anyone) you are betting AGAINST. Example: "D4F places higher than
     D4M" means you are FOR D4F and AGAINST D4M — you are NOT backing both D4
     tributes. A leg like "D4F top-8 finish" means you are FOR D4F only.
  6. Write the name and description using ONLY the directions you identified in
     step 5. The description must not claim you are "backing" a tribute or
     district unless every leg in the parlay actually bets in their favour.
     If the legs pit two tributes against each other, say so explicitly —
     e.g. "ride D4F over her district partner". Never say "back both tributes"
     if any leg has one tribute beating the other.
  7. Before finalising: re-read each market_id label and confirm it matches
     the subject in your name/description. Remove any leg that does not match.
  8. Confirm no other parlay in the batch uses the same district number. If
     there is a conflict, change this parlay's subject to a different district.
A parlay that mixes subjects, duplicates a district, or has a description that
contradicts the direction of its legs is invalid — reject it and start over.

Respond as a JSON array (no other text):
[
  {{
    "name": "short evocative title (max 60 chars)",
    "description": "2–3 sentences, max 280 chars. Lead with a hook that frames the narrative angle, use one or two specific stats or lore facts as EVIDENCE, and close with the punchy reason to bet it. Do NOT just recite data — argue from it. The reader should feel convinced, not informed.",
    "tier": "SAFE" | "BALANCED" | "LONGSHOT",
    "market_ids": [<integer IDs only, from the list above>]
  }}
]
"""


_MAX_MARKETS = 35
_MAX_LORE_CHARS = 2000

# Scoped per-subject path: keep each prompt small so it fits a modest server
# context window and runs fast.  One subject needs only a handful of legs, so a
# tight market cap and shorter lore slice cost nothing in quality.
_SUBJECT_MAX_MARKETS = 20
_SUBJECT_MAX_LORE_CHARS = 1000


def _select_markets(
    markets: list[dict[str, Any]], limit: int = _MAX_MARKETS
) -> list[dict[str, Any]]:
    """Return up to ``limit`` markets, preferring competitive (near-zero) odds."""
    if len(markets) <= limit:
        return markets
    # Sort by closeness to 0 (most competitive odds first) to give the model
    # the most decision-relevant markets when we have to truncate.
    return sorted(markets, key=lambda m: abs(m["odds"]))[:limit]


def _build_market_lines(markets: list[dict[str, Any]]) -> str:
    lines = [
        f"{m['id']}: {m['label']} | {m['odds']:+d}"
        for m in markets
    ]
    return "\n".join(lines) or "(no open markets)"


def _build_tribute_lines(tributes: list[dict[str, Any]]) -> str:
    lines = []
    for t in tributes:
        deb = t.get("debilitation_level") or "healthy"
        played = t.get("times_played", 0)
        vet = f"vet(x{played})" if played else "rookie"
        lines.append(
            f"D{t['district']} | {t['name']} | {t.get('gender','?')} | "
            f"age={t.get('age') or '?'} | kills={t['kills']} | "
            f"ts={t.get('training_score') or '?'} | {vet} | {deb}"
        )
    return "\n".join(lines) or "(no alive tributes)"


def _build_district_record_lines(records: list[dict[str, Any]]) -> str:
    lines = []
    for r in sorted(records, key=lambda x: x["district"]):
        d = r["district"]
        rep = r.get("reputation") or "?"
        funding = r.get("funding_level") or "unknown"
        wins = r.get("wins") or 0
        avg_p = r.get("avg_placement") or "?"
        last5 = r.get("avg_placement_last5") or "?"
        top8 = r.get("top8_finishes") or 0
        top5 = r.get("top5_finishes") or 0
        kills = r.get("total_kills") or 0
        bb_kills = r.get("bloodbath_kills") or 0
        kill_rec = r.get("kill_record") or 0
        avg_ts = r.get("avg_training_score") or "?"
        lines.append(
            f"D{d} | rep={rep} | {funding} | wins={wins} | avg_place={avg_p} | "
            f"last5_place={last5} | top8={top8} | top5={top5} | kills={kills} | "
            f"bb_kills={bb_kills} | kill_rec={kill_rec} | avg_ts={avg_ts}"
        )
    return "\n".join(lines) or "(no district records)"


def _repair_truncated_array(text: str) -> Any:
    """Try to salvage complete JSON objects from a truncated array.

    When the model hits max_tokens mid-output the JSON array is left open.
    We find the last complete object boundary ('}') and close the array there
    so we can return whatever parlays were fully generated.
    """
    last_close = text.rfind("}")
    if last_close == -1:
        raise ValueError("no complete JSON objects in truncated output")
    repaired = text[: last_close + 1].rstrip().rstrip(",") + "\n]"
    return json.loads(repaired)


def _extract_json(raw: str) -> Any:
    """Parse JSON from a model response, tolerating markdown fences and thinking tags."""
    text = raw.strip()
    # Strip <think>...</think> blocks emitted by reasoning models (Gemma-4, Qwen3, etc.)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```$", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise ValueError("model returned empty content after stripping thinking tags")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Output may be a truncated array (model hit max_tokens mid-generation).
        # Attempt recovery before giving up.
        if text.lstrip().startswith("["):
            log.warning("JSON parse failed; attempting truncation recovery")
            return _repair_truncated_array(text)
        raise


async def generate_ai_parlays(
    client: LemonadeClient,
    *,
    lore: str,
    markets: list[dict[str, Any]],
    tributes: list[dict[str, Any]],
    district_records: list[dict[str, Any]] | None = None,
    count: int = 3,
    tiers: list[str] | None = None,
    num_ctx: int = 0,
    timeout: float = 600.0,
) -> list[dict[str, Any]]:
    """Ask Lemonade to generate ``count`` themed parlay suggestions.

    markets: list of {id, label, odds}
    tributes: list of {district, name, gender, age, kills, training_score,
                       times_played, debilitation_level}
    district_records: list of DistrictRecord-shaped dicts (all fields optional)
    num_ctx: context-window override forwarded to llama-server (0 = server default)

    Returns a list of dicts with keys: name, description, tier, market_ids.
    Raises ValueError if the model returns unparseable or invalid JSON.
    """
    if tiers is None:
        tiers = ["SAFE", "BALANCED", "LONGSHOT"]

    selected_markets = _select_markets(markets)
    truncated_lore = lore[:_MAX_LORE_CHARS]
    if len(lore) > _MAX_LORE_CHARS:
        truncated_lore = truncated_lore.rsplit(" ", 1)[0] + " [...]"
    user_msg = _PARLAY_USER_TMPL.format(
        lore=truncated_lore,
        district_records=_build_district_record_lines(district_records or []),
        markets=_build_market_lines(selected_markets),
        tributes=_build_tribute_lines(tributes),
        count=count,
        tiers=", ".join(tiers),
    )

    raw = await client.chat_complete(
        [
            {"role": "system", "content": _PARLAY_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.8,
        max_tokens=8192,
        num_ctx=num_ctx,
        timeout=timeout,
    )

    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Lemonade returned non-JSON: {raw[:300]}") from exc

    # Normalise: model might wrap the array in an object
    if isinstance(data, dict):
        for key in ("parlays", "suggestions", "results", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError(f"Unexpected JSON shape from Lemonade: {list(data.keys())}")

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got {type(data).__name__}")

    valid_ids = {m["id"] for m in markets}
    results: list[dict[str, Any]] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        tier = str(item.get("tier", "BALANCED")).upper()
        if tier not in {"SAFE", "BALANCED", "LONGSHOT"}:
            tier = "BALANCED"
        mids = [int(mid) for mid in item.get("market_ids", []) if int(mid) in valid_ids]
        if len(mids) < 3:
            log.warning("Lemonade parlay '%s' has too few valid legs (%d); skipping", item.get("name"), len(mids))
            continue
        results.append({
            "name": str(item.get("name", "AI Parlay"))[:100],
            "description": str(item.get("description", ""))[:500],
            "tier": tier,
            "market_ids": mids,
        })

    return results


# ---------------------------------------------------------------------------
# Scoped per-subject generation
# ---------------------------------------------------------------------------
#
# The batch helper above asks one model call to partition all markets, dedupe
# districts, count legs, and target odds tiers — reasoning small local models
# do unreliably.  The scoped path instead does the hard constraints in Python
# (the caller pre-bundles one subject's markets and picks distinct subjects),
# then asks the model only for what it is good at: choosing legs from a small
# bounded list and writing persuasive copy.  Coherence ("legs that go
# together") is guaranteed by construction because each call only ever sees one
# subject's markets.  Calls are independent, so they run in parallel.

_SUBJECT_SYSTEM = """\
You are a Panem Sportsbook analyst for the Hunger Games universe. You are given
ONE subject (a single district or alliance): its lore, its historical stats,
its tributes, and the LIST OF MARKETS that belong to it. Build exactly ONE
coherent parlay using ONLY those markets, and write a pitch that makes a bettor
want to tail it.

Rules:
- Use ONLY the market IDs from the provided list. Every leg is about this one
  subject — you cannot reference any other district or tribute.
- Pick 3 to 6 legs that tell a single, connected story.
- Direction accuracy: only say you are "backing", "riding", or "on" a tribute
  if EVERY leg bets in their favour. If a leg pits two tributes against each
  other (e.g. "D4F places higher than D4M"), you are backing ONE tribute OVER
  the other — never claim you back both. The description must match the real
  direction of every leg.
- Write like a sharp analyst selling a pick: a hook (why this angle is
  compelling), one or two specific stats or lore facts as EVIDENCE, then a
  punchy closer. Vary your sentence rhythm; do not just list the data.
- Aim the parlay's risk level at: {target_tier}. SAFE = favour proven
  favourites and shorter-priced legs. LONGSHOT = embrace underdog lore and
  longer-priced legs. BALANCED = a mix. This is guidance, not a hard rule.

Respond with a SINGLE JSON object only — no markdown fences, no prose outside
the JSON:
{{"name": "short evocative title, max 60 chars",
  "description": "2-3 sentences, max 280 chars: hook -> evidence -> closer",
  "market_ids": [integer IDs from the list above]}}
"""

_SUBJECT_USER_TMPL = """\
=== SUBJECT ===
{subject_label}

=== LORE ===
{lore}

=== HISTORICAL STATS ===
Format: D# | rep=1-5(1=best) | funding | wins | avg_place | last5_place | top8 | top5 | kills | bb_kills | kill_rec | avg_ts
{district_records}

=== TRIBUTES ===
Format: D# | name | gender | age | kills | training_score | vet/rookie | debilitation
{tributes}

=== MARKETS FOR THIS SUBJECT (ID: label | odds) ===
{markets}

=== TASK ===
Build one {target_tier}-leaning parlay for {subject_label} using 3-6 of the
markets above. Return the single JSON object described in the system message.
"""


def _parse_subject_parlay(
    raw: str, valid_ids: set[int], *, subject_label: str
) -> dict[str, Any] | None:
    """Parse one scoped parlay object; return None if it is unusable."""
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("Lemonade subject '%s' returned non-JSON: %s", subject_label, raw[:200])
        return None
    # json_mode yields an object; tolerate a stray one-element array too.
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict)), None)
    if not isinstance(data, dict):
        return None
    mids: list[int] = []
    for mid in data.get("market_ids", []):
        try:
            mid_i = int(mid)
        except (TypeError, ValueError):
            continue
        if mid_i in valid_ids and mid_i not in mids:
            mids.append(mid_i)
    if len(mids) < 3:
        log.warning(
            "Lemonade subject '%s' parlay has too few valid legs (%d); skipping",
            subject_label, len(mids),
        )
        return None
    return {
        "name": str(data.get("name", "AI Parlay"))[:100],
        "description": str(data.get("description", ""))[:500],
        "market_ids": mids[:6],
        "subject_label": subject_label,
    }


async def generate_ai_parlay_for_subject(
    client: LemonadeClient,
    *,
    subject_label: str,
    lore: str,
    markets: list[dict[str, Any]],
    tributes: list[dict[str, Any]],
    district_records: list[dict[str, Any]] | None = None,
    target_tier: str = "BALANCED",
    http: httpx.AsyncClient | None = None,
    num_ctx: int = 0,
    timeout: float = 600.0,
    _retries: int = 1,
) -> dict[str, Any] | None:
    """Generate one coherent parlay scoped to a single pre-bundled subject.

    ``markets`` must already be limited to this subject's markets — the model is
    told to pick only from them, so coherence is guaranteed.  Returns a dict with
    keys name, description, market_ids, subject_label, or None if the model
    returns nothing usable after ``_retries`` extra attempts.
    """
    truncated_lore = lore[:_SUBJECT_MAX_LORE_CHARS]
    if len(lore) > _SUBJECT_MAX_LORE_CHARS:
        truncated_lore = truncated_lore.rsplit(" ", 1)[0] + " [...]"
    # Cap the market list so one subject's prompt stays small (fits a modest
    # server context window, generates faster).  The model may only pick from
    # the markets it is shown, so valid_ids is built from the capped set.
    shown_markets = _select_markets(markets, _SUBJECT_MAX_MARKETS)
    system = _SUBJECT_SYSTEM.format(target_tier=target_tier)
    user_msg = _SUBJECT_USER_TMPL.format(
        subject_label=subject_label,
        lore=truncated_lore,
        district_records=_build_district_record_lines(district_records or []),
        tributes=_build_tribute_lines(tributes),
        markets=_build_market_lines(shown_markets),
        target_tier=target_tier,
    )
    valid_ids = {m["id"] for m in shown_markets}

    for attempt in range(_retries + 1):
        try:
            raw = await client.chat_complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.8,
                max_tokens=1024,
                num_ctx=num_ctx,
                json_mode=True,
                timeout=timeout,
                http=http,
            )
        except Exception:
            log.exception("Lemonade call failed for subject '%s'", subject_label)
            return None
        parlay = _parse_subject_parlay(raw, valid_ids, subject_label=subject_label)
        if parlay is not None:
            return parlay
        if attempt < _retries:
            log.info("Retrying subject '%s' (attempt %d)", subject_label, attempt + 2)
    return None


async def generate_ai_parlays_for_subjects(
    client: LemonadeClient,
    subjects: list[dict[str, Any]],
    *,
    limit: int | None = None,
    num_ctx: int = 0,
    timeout: float = 600.0,
    max_concurrency: int = 2,
) -> list[dict[str, Any]]:
    """Generate one parlay per subject in parallel over a shared HTTP client.

    Each entry in ``subjects`` is a dict with keys: subject_label, lore, markets,
    tributes, district_records, target_tier.  Failed subjects are dropped.  When
    ``limit`` is set, at most that many successful parlays are returned, in the
    order the subjects were supplied (so callers can over-provision subjects to
    absorb individual failures).

    ``max_concurrency`` caps in-flight requests so we don't exceed a local
    llama-server's parallel slots — excess subjects queue rather than error.
    """
    if not subjects:
        return []
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _run(s: dict[str, Any], http: httpx.AsyncClient) -> dict[str, Any] | None:
        async with sem:
            return await generate_ai_parlay_for_subject(
                client,
                subject_label=s["subject_label"],
                lore=s.get("lore", ""),
                markets=s["markets"],
                tributes=s.get("tributes", []),
                district_records=s.get("district_records", []),
                target_tier=s.get("target_tier", "BALANCED"),
                http=http,
                num_ctx=num_ctx,
                timeout=timeout,
            )

    async with httpx.AsyncClient(timeout=timeout) as http:
        results = await asyncio.gather(*(_run(s, http) for s in subjects))

    parlays = [p for p in results if p is not None]
    if limit is not None:
        parlays = parlays[:limit]
    return parlays

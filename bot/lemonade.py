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
        _retries: int = 2,
    ) -> str:
        """Send a chat completion request; return the assistant's reply text.

        num_ctx: override the model's context window (llama-server extension).
                 0 means use the server default.
        """
        async with httpx.AsyncClient(timeout=timeout) as http:
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

        return resp.json()["choices"][0]["message"]["content"]

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
You receive district lore and current betting-market data, then suggest
thematic parlay bets that tell a coherent narrative story grounded in each
district's identity, history, and current standing.
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
intelligently differentiated picks. SAFE parlays should favor historically
strong districts and low-odds markets; LONGSHOT parlays should take risks
grounded in underdog lore or surging kill leaders.
Each parlay must include 3–8 legs drawn from the market IDs above.

Respond as a JSON array (no other text):
[
  {{
    "name": "short evocative title (max 80 chars)",
    "description": "1–2 sentence narrative justifying the picks using lore and stats",
    "tier": "SAFE" | "BALANCED" | "LONGSHOT",
    "market_ids": [<integer IDs only, from the list above>]
  }}
]
"""


_MAX_MARKETS = 35


def _select_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return up to _MAX_MARKETS markets, preferring competitive (near-zero) odds."""
    if len(markets) <= _MAX_MARKETS:
        return markets
    # Sort by closeness to 0 (most competitive odds first) to give the model
    # the most decision-relevant markets when we have to truncate.
    return sorted(markets, key=lambda m: abs(m["odds"]))[:_MAX_MARKETS]


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


def _extract_json(raw: str) -> Any:
    """Parse JSON from a model response, tolerating markdown code fences."""
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```$", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


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
    user_msg = _PARLAY_USER_TMPL.format(
        lore=lore,
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
        max_tokens=1024,
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
        if len(mids) < 2:
            log.warning("Lemonade parlay '%s' has too few valid legs (%d); skipping", item.get("name"), len(mids))
            continue
        results.append({
            "name": str(item.get("name", "AI Parlay"))[:100],
            "description": str(item.get("description", ""))[:500],
            "tier": tier,
            "market_ids": mids,
        })

    return results

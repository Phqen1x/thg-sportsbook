"""One-shot: re-price every unresolved, non-overridden market against the current
full roster, fixing odds that were computed against a partial field while
tributes were still being added (e.g. the first tribute's -9900 "99% to win").

Run from repo root:  python3 -m tools.backfill_market_odds
"""
import asyncio

from bot.database.engine import get_session
from bot.cogs.admin import _recalculate_markets


async def main() -> None:
    async with get_session() as session:
        await _recalculate_markets(session)
    print("Re-priced all unresolved, non-overridden markets against the full roster.")


if __name__ == "__main__":
    asyncio.run(main())

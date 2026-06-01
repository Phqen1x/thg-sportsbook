from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.odds.calculator import implied_probability
from bot.utils.formatters import fmt_odds, fmt_pct

if TYPE_CHECKING:
    from bot.database.models import Market, Tribute

PAGE_SIZE = 12

# Canonical display order for market types
_TYPE_ORDER: dict[str, int] = {
    "TRIBUTE_WINS":       0,
    "TRIBUTE_PLACEMENT":  1,
    "TRIBUTE_TOP_N":      2,
    "TRIBUTE_KILLS":      3,
    "KILLS_OU":           4,
    "PLACEMENT_OU":       5,
    "KILL_EVENT":         6,
    "FIRST_BLOOD":        7,
    "BLOODBATH_SURVIVOR": 8,
    "DEATH_CAUSE":        9,
    "SPONSOR_EVENT":      10,
}

_TYPE_LABELS: dict[str, str] = {
    "TRIBUTE_WINS":       "Victor Markets",
    "TRIBUTE_PLACEMENT":  "Placement Markets",
    "TRIBUTE_TOP_N":      "Top-N Finish",
    "TRIBUTE_KILLS":      "Top Killer",
    "KILLS_OU":           "Kills Over/Under",
    "PLACEMENT_OU":       "Placement Over/Under",
    "KILL_EVENT":         "Kill Events",
    "FIRST_BLOOD":        "First Blood",
    "BLOODBATH_SURVIVOR": "Bloodbath Survivor",
    "DEATH_CAUSE":        "Death Cause",
    "SPONSOR_EVENT":      "Sponsor Events",
}


def sort_markets(markets: list["Market"], tribute_map: dict[int, "Tribute"]) -> list["Market"]:
    """Sort markets by type → district → gender (M first) → id."""
    def _key(m: "Market"):
        trib = tribute_map.get(m.tribute_a_id)
        district = trib.district if trib else 99
        gender_order = 0 if (trib and trib.gender == "M") else 1
        return (_TYPE_ORDER.get(m.type, 99), district, gender_order, m.id)
    return sorted(markets, key=_key)


class MarketPageView(discord.ui.View):
    """
    Paginated embed view for markets. Works for both the public /markets
    command and the admin /admin market list command.

    Layout:
      Row 0: ⏮ ◀ [page / total] ▶ ⏭
      Row 1: Jump-to-category select menu
    """

    def __init__(
        self,
        sorted_markets: list["Market"],
        tribute_map: dict[int, "Tribute"],
        phase_map: dict[int, str] | None = None,
        is_admin: bool = False,
        title: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.sorted_markets = sorted_markets
        self.tribute_map = tribute_map
        self.phase_map = phase_map or {}
        self.is_admin = is_admin
        self.title = title or ("📊 MARKETS — ADMIN" if is_admin else "📊 OPEN BETTING MARKETS")
        self.page = 0
        self.total_pages = max(1, (len(sorted_markets) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.message: discord.Message | None = None

        # Map each market type to the page where it first appears
        self._cat_first_page: dict[str, int] = {}
        for i, m in enumerate(sorted_markets):
            if m.type not in self._cat_first_page:
                self._cat_first_page[m.type] = i // PAGE_SIZE

        # ── Row 0: navigation buttons ─────────────────────────────────────────
        self.btn_first = discord.ui.Button(
            emoji="⏮", style=discord.ButtonStyle.secondary, row=0, disabled=True
        )
        self.btn_prev = discord.ui.Button(
            emoji="◀", style=discord.ButtonStyle.secondary, row=0, disabled=True
        )
        self.btn_page_label = discord.ui.Button(
            label=f"1 / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=True,
        )
        self.btn_next = discord.ui.Button(
            emoji="▶", style=discord.ButtonStyle.secondary, row=0,
            disabled=self.total_pages <= 1,
        )
        self.btn_last = discord.ui.Button(
            emoji="⏭", style=discord.ButtonStyle.secondary, row=0,
            disabled=self.total_pages <= 1,
        )

        self.btn_first.callback = self._on_first
        self.btn_prev.callback = self._on_prev
        self.btn_next.callback = self._on_next
        self.btn_last.callback = self._on_last

        for btn in (self.btn_first, self.btn_prev, self.btn_page_label,
                    self.btn_next, self.btn_last):
            self.add_item(btn)

        # ── Row 1: category jump select ───────────────────────────────────────
        cat_options = [
            discord.SelectOption(
                label=_TYPE_LABELS.get(t, t)[:100],
                value=t,
                description=f"Starts on page {pg + 1}",
            )
            for t, pg in self._cat_first_page.items()
        ]
        if cat_options:
            self.cat_select = discord.ui.Select(
                placeholder="Jump to category…",
                options=cat_options[:25],
                row=1,
            )
            self.cat_select.callback = self._on_category
            self.add_item(self.cat_select)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _sync_buttons(self) -> None:
        at_first = self.page == 0
        at_last = self.page >= self.total_pages - 1
        self.btn_first.disabled = at_first
        self.btn_prev.disabled = at_first
        self.btn_next.disabled = at_last
        self.btn_last.disabled = at_last
        self.btn_page_label.label = f"{self.page + 1} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        start = self.page * PAGE_SIZE
        page_markets = self.sorted_markets[start : start + PAGE_SIZE]

        embed = discord.Embed(title=self.title, color=0xC9A227)

        current_type: str | None = None
        for m in page_markets:
            # Section header when market type changes within this page
            if m.type != current_type:
                current_type = m.type
                section = _TYPE_LABELS.get(m.type, m.type).upper()
                embed.add_field(name=f"── {section} ──", value="​", inline=False)

            prob = implied_probability(m.odds)

            ou_str = ""
            if m.ou_line is not None and m.ou_side:
                side = "O" if m.ou_side == "OVER" else "U"
                ou_str = f" {side}{m.ou_line:g}"

            if self.is_admin:
                phase_str = (
                    f" [{self.phase_map[m.phase_id]}]"
                    if m.phase_id and m.phase_id in self.phase_map
                    else ""
                )
                field_name = (
                    f"`#{m.id}` [{m.status}]{phase_str}"
                    f" {fmt_odds(m.odds)}{ou_str} ({fmt_pct(prob)})"
                )
            else:
                field_name = f"`#{m.id}` {fmt_odds(m.odds)}{ou_str} ({fmt_pct(prob)})"

            embed.add_field(name=field_name[:256], value=m.label[:200], inline=False)

        total = len(self.sorted_markets)
        embed.set_footer(
            text=(
                f"Page {self.page + 1} of {self.total_pages}"
                f"  ·  {total} market{'s' if total != 1 else ''}"
            )
        )
        return embed

    async def _safe_edit(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.NotFound:
            pass

    # ── Button callbacks ──────────────────────────────────────────────────────

    async def _on_first(self, interaction: discord.Interaction) -> None:
        self.page = 0
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_last(self, interaction: discord.Interaction) -> None:
        self.page = self.total_pages - 1
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_category(self, interaction: discord.Interaction) -> None:
        type_key = self.cat_select.values[0]
        target = self._cat_first_page.get(type_key)
        if target is not None:
            self.page = target
            self._sync_buttons()
        await self._safe_edit(interaction)

    # ── Timeout ───────────────────────────────────────────────────────────────

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.odds.calculator import implied_probability
from bot.utils.formatters import fmt_odds, fmt_pct

if TYPE_CHECKING:
    from bot.database.models import Market, MarketTemplate, Tribute

PAGE_SIZE = 12

# Canonical display order for market types
_TYPE_ORDER: dict[str, int] = {
    "TRIBUTE_WINS":              0,
    "TRIBUTE_PLACEMENT":         1,
    "TRIBUTE_TOP_N":             2,
    "TRIBUTE_RUNNER_UP":         3,
    "FIRST_TRIBUTE_TO_DIE":      4,
    "TRIBUTE_KILLS":             5,
    "KILLS_OU":                  6,
    "PLACEMENT_OU":              7,
    "MAKES_FINAL_8":             8,
    "MISSES_FINAL_8":            9,
    "MAKES_FINAL_5":             10,
    "MISSES_FINAL_5":            11,
    "KILL_EVENT":                14,
    "FIRST_BLOOD":               15,
    "BLOODBATH_SURVIVOR":        16,
    "TRIBUTE_KILLED_BLOODBATH":  16,
    "FIRST_IN_ALLIANCE_DEATH":   17,
    "DEATH_CAUSE":               18,
    "HIGHEST_TRAINING_SCORE":    19,
    "LOWEST_TRAINING_SCORE":     20,
    "ARENA_TYPE":                21,
    "EXACT_TRAINING_SCORE":      22,
    "COMBINED_DISTRICT_SCORE":   23,
    "TRAINING_SCORE_OU":         24,
    # Game-level props
    "BLOODBATH_KILLS_OU":        30,
    "BLOODBATH_DEATHS_OU":       30,
    "EXACT_BLOODBATH_DEATHS":    31,
    "BLOODBATH_NO_DEATHS":       32,
    "ANY_BB_DOUBLE_KILL":        33,
    "ARENA_TRAP_DEATHS_OU":      34,
    "ARENA_ENV_DEATHS_OU":       35,
    "ARENA_IS_NATURAL":          36,
    "ARENA_IS_ARTIFICIAL":       37,
    "NUM_TENS_OU":               38,
    "SOLO_TRIBUTES_OU":          39,
    "GAMES_DURATION":            40,
    "GAMES_FEAST":               42,
    "GAMES_BETRAYAL":            43,
    "DISTRICT_PARTNER_KILL":     44,
    # District markets
    "DISTRICT_VICTOR":           45,
    "DISTRICT_KILLS_OU":         46,
    "DISTRICT_BOTH_BLOODBATH":   47,
    "DISTRICT_WIPED_BLOODBATH":  47,
    "DISTRICT_BOTH_FINAL_8":     47,
    "DISTRICT_ONE_FINAL_8":      47,
    "DISTRICT_BOTH_FINAL_5":     47,
    "DISTRICT_ONE_FINAL_5":      47,
    # District extras
    "DISTRICT_HIGHEST_SCORE":    48,
    "FIRST_DISTRICT_WIPE":       49,
    # Alliance markets
    "ALLIANCE_VICTOR":           54,
    "ALLIANCE_KILLS_OU":         55,
    "ALLIANCE_ALL_BLOODBATH":    56,
    "ALLIANCE_WIPED_BLOODBATH":  56,
    "ALLIANCE_ALL_FINAL_8":      57,
    "ALLIANCE_ONE_FINAL_8":      57,
    "ALLIANCE_ALL_FINAL_5":      57,
    "ALLIANCE_ONE_FINAL_5":      57,
    # Alliance extras
    "FIRST_ALLIANCE_WIPED":      50,
    "ALLIANCE_MOST_KILLS":       51,
    "EXACT_ALLIANCE_KILLS":      52,
    "ALLIANCE_RUNNER_UP":        53,
}

_TYPE_LABELS: dict[str, str] = {
    "TRIBUTE_WINS":              "Victor Markets",
    "TRIBUTE_PLACEMENT":         "Placement Markets",
    "TRIBUTE_TOP_N":             "Top-N Finish",
    "TRIBUTE_RUNNER_UP":         "Runner-Up",
    "FIRST_TRIBUTE_TO_DIE":      "First to Die",
    "TRIBUTE_KILLS":             "Top Killer",
    "KILLS_OU":                  "Kills Over/Under",
    "PLACEMENT_OU":              "Placement Over/Under",
    "MAKES_FINAL_8":             "Makes Final 8",
    "MISSES_FINAL_8":            "Eliminated Before Final 8",
    "MAKES_FINAL_5":             "Makes Final 5",
    "MISSES_FINAL_5":            "Eliminated Before Final 5",
    "KILL_EVENT":                "Kill Events",
    "FIRST_BLOOD":               "First Blood",
    "BLOODBATH_SURVIVOR":        "Bloodbath Survivor",
    "TRIBUTE_KILLED_BLOODBATH":  "Killed in Bloodbath",
    "FIRST_IN_ALLIANCE_DEATH":   "First in Alliance to Die",
    "DEATH_CAUSE":               "Death Cause",
    "HIGHEST_TRAINING_SCORE":    "Highest Training Score",
    "LOWEST_TRAINING_SCORE":     "Lowest Training Score",
    "ARENA_TYPE":                "Arena Type",
    "EXACT_TRAINING_SCORE":      "Exact Training Score",
    "COMBINED_DISTRICT_SCORE":   "Combined District Score",
    "TRAINING_SCORE_OU":         "Training Score Over/Under",
    # Game-level props
    "BLOODBATH_KILLS_OU":        "Bloodbath Kills Over/Under",
    "BLOODBATH_DEATHS_OU":       "Bloodbath Deaths Over/Under",
    "EXACT_BLOODBATH_DEATHS":    "Exact Bloodbath Deaths",
    "BLOODBATH_NO_DEATHS":       "Bloodbath Contains No Deaths",
    "ANY_BB_DOUBLE_KILL":        "Any Tribute Records 2+ BB Kills",
    "ARENA_TRAP_DEATHS_OU":      "Arena Trap Deaths Over/Under",
    "ARENA_ENV_DEATHS_OU":       "Arena Environmental Deaths Over/Under",
    "ARENA_IS_NATURAL":          "Arena Is Natural",
    "ARENA_IS_ARTIFICIAL":       "Arena Is Artificial",
    "NUM_TENS_OU":               "Number of 10+ Scores Over/Under",
    "SOLO_TRIBUTES_OU":          "Solo Tributes Over/Under",
    "GAMES_DURATION":            "Games Duration (Days)",
    "GAMES_FEAST":               "Games Features a Feast",
    "GAMES_BETRAYAL":            "Games Features a Betrayal",
    "DISTRICT_PARTNER_KILL":     "District Partner Kill",
    # District markets
    "DISTRICT_VICTOR":           "District Victor",
    "DISTRICT_KILLS_OU":         "District Kills Over/Under",
    "DISTRICT_BOTH_BLOODBATH":   "District Both Bloodbath",
    "DISTRICT_WIPED_BLOODBATH":  "District Wiped in Bloodbath",
    "DISTRICT_BOTH_FINAL_8":     "District Both Make Final 8",
    "DISTRICT_ONE_FINAL_8":      "District One Makes Final 8",
    "DISTRICT_BOTH_FINAL_5":     "District Both Make Final 5",
    "DISTRICT_ONE_FINAL_5":      "District One Makes Final 5",
    # District extras
    "DISTRICT_HIGHEST_SCORE":    "District Highest Combined Score",
    "FIRST_DISTRICT_WIPE":       "First District Wipe",
    # Alliance markets
    "ALLIANCE_VICTOR":           "Alliance Victor",
    "ALLIANCE_KILLS_OU":         "Alliance Kills Over/Under",
    "ALLIANCE_ALL_BLOODBATH":    "Alliance All Bloodbath",
    "ALLIANCE_WIPED_BLOODBATH":  "Alliance Wiped in Bloodbath",
    "ALLIANCE_ALL_FINAL_8":      "Alliance All Make Final 8",
    "ALLIANCE_ONE_FINAL_8":      "Alliance One Makes Final 8",
    "ALLIANCE_ALL_FINAL_5":      "Alliance All Make Final 5",
    "ALLIANCE_ONE_FINAL_5":      "Alliance One Makes Final 5",
    # Alliance extras
    "FIRST_ALLIANCE_WIPED":      "First Alliance Wiped",
    "ALLIANCE_MOST_KILLS":       "Alliance Most Kills",
    "EXACT_ALLIANCE_KILLS":      "Alliance Exact Kill Count",
    "ALLIANCE_RUNNER_UP":        "Alliance Produces Runner-Up",
}


_SECTION_LABELS: dict[str, str] = {
    "tribute":  "Tribute Markets",
    "props":    "Game Props",
    "district": "District Markets",
    "alliance": "Alliance Markets",
    "custom":   "Custom Markets",
}
_SECTION_ORDER = ["tribute", "props", "district", "alliance", "custom"]


def _type_section(t: str) -> str:
    if t.startswith("CUSTOM_"):
        return "custom"
    # DISTRICT_PARTNER_KILL is a game-level prop, not a district-level market
    if (t.startswith("DISTRICT_") and t != "DISTRICT_PARTNER_KILL") or t == "FIRST_DISTRICT_WIPE":
        return "district"
    if t.startswith("ALLIANCE_") or t in ("FIRST_ALLIANCE_WIPED", "EXACT_ALLIANCE_KILLS"):
        return "alliance"
    return "tribute" if _TYPE_ORDER.get(t, 99) <= 24 else "props"


def sort_markets(markets: list["Market"], tribute_map: dict[int, "Tribute"]) -> list["Market"]:
    """Sort markets by type → district → gender (M first) → id."""
    def _key(m: "Market"):
        trib = tribute_map.get(m.tribute_a_id) if m.tribute_a_id is not None else None
        district = trib.district if trib else 99
        gender_order = {"M": 0, "F": 1, "NB": 2}.get(trib.gender, 3) if trib else 3
        return (_TYPE_ORDER.get(m.type, 99), district, gender_order, m.id)
    return sorted(markets, key=_key)


class MarketPageView(discord.ui.View):
    """
    Paginated embed view for markets. Works for both the public /markets
    command and the admin /admin market list command.

    Layout:
      Row 0: ⏮ ◀ [page / total] ▶ ⏭
      Row 1: Section select (Tribute / Game Props / District / Alliance / Custom)
      Row 2: Type select — repopulated when a section is chosen
    """

    def __init__(
        self,
        sorted_markets: list["Market"],
        tribute_map: dict[int, "Tribute"],
        phase_map: dict[int, str] | None = None,
        is_admin: bool = False,
        title: str | None = None,
        extra_type_labels: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.sorted_markets = sorted_markets
        self.tribute_map = tribute_map
        self.phase_map = phase_map or {}
        self.is_admin = is_admin
        self.title = title or ("📊 MARKETS — ADMIN" if is_admin else "📊 OPEN BETTING MARKETS")
        self._type_labels = {**_TYPE_LABELS, **(extra_type_labels or {})}
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

        # ── Row 1: skip-5 buttons ─────────────────────────────────────────────
        self.btn_skip_back = discord.ui.Button(
            label="⏪5️⃣", style=discord.ButtonStyle.secondary, row=1, disabled=True
        )
        self.btn_skip_fwd = discord.ui.Button(
            label="5️⃣⏩", style=discord.ButtonStyle.secondary, row=1,
            disabled=self.total_pages <= 1,
        )
        self.btn_skip_back.callback = self._on_skip_back
        self.btn_skip_fwd.callback = self._on_skip_fwd
        self.add_item(self.btn_skip_back)
        self.add_item(self.btn_skip_fwd)

        # ── Rows 2–3: two-step section → type navigation ──────────────────────
        self._section_types: dict[str, list[str]] = {}
        for t in self._cat_first_page:
            self._section_types.setdefault(_type_section(t), []).append(t)
        for sec in self._section_types:
            self._section_types[sec].sort(key=lambda t: _TYPE_ORDER.get(t, 99))

        present_sections = [s for s in _SECTION_ORDER if s in self._section_types]
        self._current_section: str | None = present_sections[0] if present_sections else None
        self.section_select: discord.ui.Select | None = None
        self.type_select: discord.ui.Select | None = None

        if present_sections:
            self.section_select = discord.ui.Select(
                placeholder="Jump to section…",
                options=[
                    discord.SelectOption(label=_SECTION_LABELS[s], value=s)
                    for s in present_sections
                ],
                row=2,
            )
            self.section_select.callback = self._on_section
            self.add_item(self.section_select)
            self._build_type_select(self._current_section)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_type_select(self, section: str | None) -> None:
        if self.type_select is not None:
            self.remove_item(self.type_select)
            self.type_select = None
        if not section:
            return
        types = self._section_types.get(section, [])
        if not types:
            return
        self.type_select = discord.ui.Select(
            placeholder="Jump to type…",
            options=[
                discord.SelectOption(
                    label=self._type_labels.get(t, t)[:100],
                    value=t,
                    description=f"Starts on page {self._cat_first_page[t] + 1}",
                )
                for t in types[:25]
            ],
            row=3,
        )
        self.type_select.callback = self._on_category
        self.add_item(self.type_select)

    def _sync_buttons(self) -> None:
        at_first = self.page == 0
        at_last = self.page >= self.total_pages - 1
        self.btn_first.disabled = at_first
        self.btn_prev.disabled = at_first
        self.btn_skip_back.disabled = at_first
        self.btn_next.disabled = at_last
        self.btn_last.disabled = at_last
        self.btn_skip_fwd.disabled = at_last
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
                section = self._type_labels.get(m.type, m.type).upper()
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

    async def _on_skip_back(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 5)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_skip_fwd(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 5)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_section(self, interaction: discord.Interaction) -> None:
        self._current_section = self.section_select.values[0]
        self._build_type_select(self._current_section)
        await self._safe_edit(interaction)

    async def _on_category(self, interaction: discord.Interaction) -> None:
        type_key = self.type_select.values[0]
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


TEMPLATE_PAGE_SIZE = 10


class MarketTypePageView(discord.ui.View):
    """Paginated embed view for market type templates (admin /market-type list)."""

    def __init__(
        self,
        templates: list["MarketTemplate"],
        difficulty_odds: dict[str, int],
    ) -> None:
        super().__init__(timeout=300)
        self.templates = templates
        self.difficulty_odds = difficulty_odds
        self.page = 0
        self.total_pages = max(1, (len(templates) + TEMPLATE_PAGE_SIZE - 1) // TEMPLATE_PAGE_SIZE)
        self.message: discord.Message | None = None

        self.btn_first = discord.ui.Button(emoji="⏮", style=discord.ButtonStyle.secondary, row=0, disabled=True)
        self.btn_prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0, disabled=True)
        self.btn_page_label = discord.ui.Button(
            label=f"1 / {self.total_pages}", style=discord.ButtonStyle.secondary, row=0, disabled=True
        )
        self.btn_next = discord.ui.Button(
            emoji="▶", style=discord.ButtonStyle.secondary, row=0, disabled=self.total_pages <= 1
        )
        self.btn_last = discord.ui.Button(
            emoji="⏭", style=discord.ButtonStyle.secondary, row=0, disabled=self.total_pages <= 1
        )

        self.btn_first.callback = self._on_first
        self.btn_prev.callback = self._on_prev
        self.btn_next.callback = self._on_next
        self.btn_last.callback = self._on_last

        for btn in (self.btn_first, self.btn_prev, self.btn_page_label, self.btn_next, self.btn_last):
            self.add_item(btn)

        self.btn_skip_back = discord.ui.Button(
            label="⏪5️⃣", style=discord.ButtonStyle.secondary, row=1, disabled=True
        )
        self.btn_skip_fwd = discord.ui.Button(
            label="5️⃣⏩", style=discord.ButtonStyle.secondary, row=1,
            disabled=self.total_pages <= 1,
        )
        self.btn_skip_back.callback = self._on_skip_back
        self.btn_skip_fwd.callback = self._on_skip_fwd
        self.add_item(self.btn_skip_back)
        self.add_item(self.btn_skip_fwd)

    def _sync_buttons(self) -> None:
        at_first = self.page == 0
        at_last = self.page >= self.total_pages - 1
        self.btn_first.disabled = at_first
        self.btn_prev.disabled = at_first
        self.btn_skip_back.disabled = at_first
        self.btn_next.disabled = at_last
        self.btn_last.disabled = at_last
        self.btn_skip_fwd.disabled = at_last
        self.btn_page_label.label = f"{self.page + 1} / {self.total_pages}"

    def build_embed(self) -> discord.Embed:
        start = self.page * TEMPLATE_PAGE_SIZE
        page_templates = self.templates[start : start + TEMPLATE_PAGE_SIZE]

        embed = discord.Embed(title="Market Types", color=0xC9A227)
        for t in page_templates:
            kind = "Built-in" if t.is_builtin else "Custom"
            status = "ACTIVE" if t.active else "INACTIVE"
            if t.default_odds is not None:
                odds_str = fmt_odds(t.default_odds)
            elif t.is_builtin:
                odds_str = "Computed"
            else:
                odds_str = fmt_odds(self.difficulty_odds[t.difficulty])
            label_str = f"\nLabel: `{t.label_template}`" if t.label_template else ""
            embed.add_field(
                name=f"#{t.id} {t.name} [{kind} · {status}]",
                value=(
                    f"{t.description}\n"
                    f"Difficulty: **{t.difficulty}** | Odds: **{odds_str}**"
                    f"{label_str}"
                ),
                inline=False,
            )

        total = len(self.templates)
        embed.set_footer(
            text=f"Page {self.page + 1} of {self.total_pages}  ·  {total} type{'s' if total != 1 else ''}"
        )
        return embed

    async def _safe_edit(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except discord.NotFound:
            pass

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

    async def _on_skip_back(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 5)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def _on_skip_fwd(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 5)
        self._sync_buttons()
        await self._safe_edit(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

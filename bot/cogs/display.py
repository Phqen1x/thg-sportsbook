from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func, or_

from bot.database.engine import get_session, get_read_session, get_setting
from bot.database.models import Alliance, Bet, BettingPhase, Market, MarketTemplate, Tribute, User
from bot.imaging.hot_odds import (
    BoardCardData, FeaturedMarket, TributeDetailData,
    render_hot_odds, render_tribute_detail,
)
from bot.imaging.base import render_async, fetch_image_bytes, buf_to_discord_file
from bot.utils.formatters import fmt_chips, fmt_odds, fmt_pct, market_type_label, safe_defer
from bot.utils.market_view import MarketPageView, sort_markets, _type_section
from bot.odds.calculator import implied_probability

log = logging.getLogger("capitol.display")


def _add_field_chunks(embed: discord.Embed, name: str, lines: list[str], inline: bool = False) -> None:
    """Add lines as one or more embed fields, splitting at the 1024-char limit."""
    chunk, first = [], True
    for line in lines:
        candidate = "\n".join(chunk + [line])
        if len(candidate) > 1024:
            embed.add_field(name=name if first else "​", value="\n".join(chunk), inline=inline)
            first = False
            chunk = [line]
        else:
            chunk.append(line)
    if chunk:
        embed.add_field(name=name if first else "​", value="\n".join(chunk), inline=inline)


def _balance(m: Market) -> int:
    """How competitive a line is: |american odds|, where even money (±100) is
    smallest and lopsided rails (±9900) are largest. Lower = more interesting."""
    return abs(m.odds)


def _balance_polarities(ranked: list[Market], n: int = 8) -> list[Market]:
    """From a pre-ranked list pick n markets with a balanced mix of +/- odds,
    preserving rank order within each sign. Falls back to top-n if only one sign exists."""
    pos = [m for m in ranked if m.odds > 0]
    neg = [m for m in ranked if m.odds < 0]

    if not pos or not neg:
        result = ranked[:n]
    else:
        target_neg = min(len(neg), n // 2)
        target_pos = min(len(pos), n - target_neg)
        target_neg = n - target_pos
        result = pos[:target_pos] + neg[:target_neg]

    result.sort(key=lambda m: m.odds)
    return result


def _select_variety_markets(markets: list[Market]) -> list[Market]:
    """Pick up to 8 markets spanning different types with a mix of positive and
    negative odds, favouring the most competitive (near even-money) lines."""
    if len(markets) <= 8:
        return sorted(markets, key=lambda m: m.odds)

    by_type: dict[str, list[Market]] = {}
    for m in markets:
        by_type.setdefault(m.type, []).append(m)
    for lst in by_type.values():
        lst.sort(key=_balance)

    candidates: list[Market] = []
    seen_ids: set[int] = set()

    # Pass 1: most competitive line of each type for variety across market types.
    for lst in sorted(by_type.values(), key=lambda l: _balance(l[0])):
        m = lst[0]
        candidates.append(m)
        seen_ids.add(m.id)

    # Pass 2: remaining slots filled with next most competitive lines overall.
    for m in sorted(markets, key=_balance):
        if m.id not in seen_ids:
            candidates.append(m)
            seen_ids.add(m.id)

    return _balance_polarities(candidates)


async def _tribute_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    async with get_read_session() as session:
        result = await session.execute(
            select(Tribute).order_by(Tribute.district, Tribute.non_binary, Tribute.gender)
        )
        tributes = result.scalars().all()
    choices = []
    for t in tributes:
        label = f"D{t.district}{t.display_gender} {t.name} ({t.status})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(t.id)))
    return choices[:25]


async def _get_or_create_user(session, member: discord.Member) -> User:
    u = await session.get(User, member.id)
    if u is None:
        default_raw = await get_setting("default_chips")
        default = json.loads(default_raw) if default_raw else 1000
        u = User(discord_id=member.id, username=member.display_name, chips=default)
        session.add(u)
        await session.flush()
    else:
        u.username = member.display_name
    return u


# ── Hot Odds board modes (district / alliance / tribute) ──────────────────────

_GENDER_SORT = {"M": 0, "F": 1, "NB": 2}

_BOARD_TITLES = {
    "tribute":  "TRIBUTE MONEYLINES  ·  TRIBUTE TO WIN THE GAMES",
    "district": "DISTRICT MONEYLINES  ·  DISTRICT TO WIN THE GAMES",
    "alliance": "ALLIANCE MONEYLINES  ·  ALLIANCE TO WIN THE GAMES",
}
_FEATURED_TITLES = {
    "tribute":  "FEATURED MARKETS",
    "district": "FEATURED DISTRICT MARKETS",
    "alliance": "FEATURED ALLIANCE MARKETS",
}
_MODE_BUTTON_LABELS = {
    "district": "District Odds",
    "alliance": "Alliance Odds",
    "tribute":  "Tribute Odds",
}
# The headline moneyline shown as the board cards is excluded from each mode's
# featured strip so the two halves never duplicate.
_HEADLINE_TYPE = {
    "tribute":  "TRIBUTE_WINS",
    "district": "DISTRICT_VICTOR",
    "alliance": "ALLIANCE_VICTOR",
}


def _alliance_badge(name: str) -> str:
    """A short pill tag for an alliance card — initials for multi-word names,
    otherwise the first few letters."""
    parts = [p for p in name.split() if p]
    tag = "".join(p[0] for p in parts[:4]) if len(parts) >= 2 else name[:3]
    return (tag or "ALY").upper()


def _group_status(statuses: list[str]) -> str:
    """Collapse a group of member tribute statuses into one board status."""
    if "VICTOR" in statuses:
        return "VICTOR"
    if "ALIVE" in statuses:
        return "ALIVE"
    return "DEAD"


async def _current_phase_name() -> str | None:
    """Name of the active betting phase, or None if no game/phase is set."""
    raw = await get_setting("current_phase_id")
    if not raw:
        return None
    try:
        phase_id = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if phase_id is None:
        return None
    async with get_read_session() as session:
        phase = await session.get(BettingPhase, phase_id)
        return phase.name if phase else None


def _featured_for_mode(
    markets: list[Market], mode: str, bet_counts: dict[int, int]
) -> list[FeaturedMarket]:
    """Pick featured markets related to the board mode being viewed: tribute
    props on the tribute board, district markets on the district board, etc."""
    headline = _HEADLINE_TYPE[mode]
    if mode == "tribute":
        pool = [m for m in markets
                if _type_section(m.type) in ("tribute", "props") and m.type != headline]
    else:
        pool = [m for m in markets
                if _type_section(m.type) == mode and m.type != headline]

    if bet_counts:
        ranked = sorted(pool, key=lambda m: (-bet_counts.get(m.id, 0), abs(m.odds)))
        chosen = _balance_polarities(ranked)
    else:
        chosen = _select_variety_markets(pool)

    return [FeaturedMarket(label=m.label, odds=m.odds, market_type=m.type) for m in chosen]


def _build_board(
    mode: str,
    tributes: list[Tribute],
    alliances: list[Alliance],
    markets: list[Market],
    bet_counts: dict[int, int],
) -> tuple[list[BoardCardData], list[FeaturedMarket]]:
    """Build the moneyline cards + featured markets for one board mode."""
    cards: list[BoardCardData] = []

    if mode == "tribute":
        win_odds = {m.tribute_a_id: m.odds for m in markets if m.type == "TRIBUTE_WINS"}
        for t in tributes:
            cards.append(BoardCardData(
                badge=f"D{t.district}{t.display_gender}",
                name=t.name,
                odds=win_odds.get(t.id),
                status=t.status,
                sort_key=(t.district, _GENDER_SORT.get(t.display_gender, 3)),
            ))

    elif mode == "district":
        victor = {m.placement_num: m.odds for m in markets if m.type == "DISTRICT_VICTOR"}
        for d in sorted({t.district for t in tributes}):
            members = [t for t in tributes if t.district == d]
            cards.append(BoardCardData(
                badge=f"D{d}",
                name=f"District {d}",
                odds=victor.get(d),
                status=_group_status([t.status for t in members]),
                sort_key=(d,),
            ))

    else:  # alliance
        victor = {m.placement_num: m.odds for m in markets if m.type == "ALLIANCE_VICTOR"}
        for a in alliances:
            members = [t for t in tributes if t.alliance_id == a.id]
            cards.append(BoardCardData(
                badge=_alliance_badge(a.name),
                name=a.name,
                odds=victor.get(a.id),
                status=_group_status([t.status for t in members]),
                sort_key=(a.name.lower(),),
            ))

    return cards, _featured_for_mode(markets, mode, bet_counts)


class OddsBoardView(discord.ui.View):
    """Hot Odds board with District / Alliance / Tribute toggle buttons. The
    underlying data is fetched once and cached on the view; each button press
    just re-renders the board from that cache and swaps the attached image."""

    def __init__(
        self, *,
        tributes: list[Tribute],
        alliances: list[Alliance],
        markets: list[Market],
        bet_counts: dict[int, int],
        user_chips: int,
        mode: str,
        available: list[str],
    ) -> None:
        super().__init__(timeout=300)
        self.tributes = tributes
        self.alliances = alliances
        self.markets = markets
        self.bet_counts = bet_counts
        self.user_chips = user_chips
        self.mode = mode
        self.available = available
        self.message: discord.Message | None = None
        self._build_buttons()

    def _build_buttons(self) -> None:
        self.clear_items()
        for m in ("district", "alliance", "tribute"):
            if m not in self.available:
                continue
            btn = discord.ui.Button(
                label=_MODE_BUTTON_LABELS[m],
                style=discord.ButtonStyle.primary if m == self.mode
                      else discord.ButtonStyle.secondary,
                disabled=m == self.mode,
                row=0,
            )
            btn.callback = self._make_callback(m)
            self.add_item(btn)

    def _make_callback(self, mode: str):
        async def _cb(interaction: discord.Interaction) -> None:
            self.mode = mode
            self._build_buttons()
            buf = await self.render()
            f = buf_to_discord_file(buf, "hot_odds.png")
            try:
                await interaction.response.edit_message(attachments=[f], view=self)
            except discord.NotFound:
                pass
        return _cb

    async def render(self):
        cards, featured = _build_board(
            self.mode, self.tributes, self.alliances, self.markets, self.bet_counts
        )
        from bot.imaging.hot_odds import DEFAULT_ANNOUNCEMENT
        _ann_raw = await get_setting("capitol_announcement")
        announcement = json.loads(_ann_raw) if _ann_raw else None
        return await render_async(
            render_hot_odds, cards, featured, self.user_chips,
            _BOARD_TITLES[self.mode], _FEATURED_TITLES[self.mode],
            announcement or DEFAULT_ANNOUNCEMENT,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class DisplayCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, ValueError):
            log.warning(f"Display command input error: {original}")
            msg = "Invalid selection. Please pick an option from the autocomplete list."
        else:
            log.error(f"Display command error: {error}", exc_info=error)
            msg = "An error occurred. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass

    # ── /odds ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="odds", description="View the live Hot Odds board")
    @app_commands.describe(tribute="Show detailed odds for a specific tribute")
    @app_commands.autocomplete(tribute=_tribute_autocomplete)
    async def odds(self, interaction: discord.Interaction, tribute: str | None = None) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        # ── Single-tribute detail view ────────────────────────────────────────
        if tribute is not None:
            try:
                tribute_id = int(tribute)
            except (TypeError, ValueError):
                await interaction.followup.send(
                    "Invalid tribute chosen. Please pick a tribute from the autocomplete list.",
                    ephemeral=True,
                )
                return
            async with get_session() as session:
                user = await _get_or_create_user(session, interaction.user)
                user_chips = user.chips

                t = await session.get(Tribute, tribute_id)
                if t is None:
                    await interaction.followup.send("Tribute not found.", ephemeral=True)
                    return

                mkt_result = await session.execute(
                    select(Market)
                    .where(Market.status == "OPEN")
                    .where(or_(
                        Market.tribute_a_id == tribute_id,
                        Market.tribute_b_id == tribute_id,
                    ))
                    .order_by(Market.id)
                )
                tribute_markets = mkt_result.scalars().all()

            win_odds = next(
                (m.odds for m in tribute_markets if m.type == "TRIBUTE_WINS"), None
            )
            non_win = [m for m in tribute_markets if m.type != "TRIBUTE_WINS"]
            featured_mkts = _balance_polarities(sorted(non_win, key=lambda m: abs(m.odds)), n=16)

            face_bytes: bytes | None = None
            if t.face_claim:
                result = await fetch_image_bytes(t.face_claim)
                face_bytes = result if isinstance(result, bytes) else None

            detail = TributeDetailData(
                name=t.name,
                district=t.district,
                gender=t.display_gender,
                training_score=t.training_score,
                status=t.status,
                kills=t.kills,
                placement=t.placement,
                death_cause=t.death_cause,
                win_odds=win_odds,
                face_bytes=face_bytes,
                markets=[
                    FeaturedMarket(label=m.label, odds=m.odds, market_type=m.type)
                    for m in featured_mkts
                ],
            )
            from bot.imaging.hot_odds import DEFAULT_ANNOUNCEMENT
            _ann_raw = await get_setting("capitol_announcement")
            announcement = json.loads(_ann_raw) if _ann_raw else None
            buf = await render_async(render_tribute_detail, detail, user_chips, announcement or DEFAULT_ANNOUNCEMENT)
            fname = f"tribute_{t.name.lower().replace(' ', '_')}.png"
            f = buf_to_discord_file(buf, fname)
            await interaction.followup.send(file=f, ephemeral=True)
            return

        # ── Full board view (District / Alliance / Tribute toggle) ────────────
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            user_chips = user.chips

            trib_result = await session.execute(
                select(Tribute).order_by(Tribute.district)
            )
            tributes = list(trib_result.scalars().all())

            alli_result = await session.execute(
                select(Alliance).order_by(Alliance.name)
            )
            alliances = list(alli_result.scalars().all())

            mkt_result = await session.execute(
                select(Market).where(Market.status == "OPEN").order_by(Market.id)
            )
            markets = list(mkt_result.scalars().all())

            bet_count_rows = await session.execute(
                select(Bet.market_id, func.count(Bet.id).label("cnt"))
                .where(Bet.parlay_id.is_(None))
                .join(Market, Bet.market_id == Market.id)
                .where(Market.status == "OPEN")
                .where(Market.type != "TRIBUTE_WINS")
                .group_by(Bet.market_id)
            )
            bet_counts: dict[int, int] = {
                row.market_id: row.cnt for row in bet_count_rows.all()
            }

        phase_name = await _current_phase_name()

        # Which board modes have data to offer.
        available: list[str] = []
        if any(m.type == "DISTRICT_VICTOR" for m in markets):
            available.append("district")
        if alliances and any(m.type == "ALLIANCE_VICTOR" for m in markets):
            available.append("alliance")
        if tributes:
            available.append("tribute")
        if not available:
            available = ["tribute"]

        # Pregames default to district odds (individual tribute victor moneylines
        # aren't open yet); every other phase defaults to tribute odds.
        if phase_name == "Pre-Games" and "district" in available:
            mode = "district"
        else:
            mode = "tribute" if "tribute" in available else available[0]

        view = OddsBoardView(
            tributes=tributes, alliances=alliances, markets=markets,
            bet_counts=bet_counts, user_chips=user_chips,
            mode=mode, available=available,
        )
        buf = await view.render()
        f = buf_to_discord_file(buf, "hot_odds.png")
        if len(available) > 1:
            msg = await interaction.followup.send(file=f, view=view, ephemeral=True)
            view.message = msg
        else:
            await interaction.followup.send(file=f, ephemeral=True)

    # ── /tributes ─────────────────────────────────────────────────────────────

    @app_commands.command(name="tributes", description="View all tributes and their stats")
    async def tributes(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(select(Tribute).order_by(Tribute.district))
            tributes = result.scalars().all()

        if not tributes:
            await interaction.followup.send("No tributes in the arena yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏛️ HUNGER GAMES — TRIBUTE ROSTER",
            color=0xC9A227,
        )

        alive = [t for t in tributes if t.status == "ALIVE"]
        dead = [t for t in tributes if t.status == "DEAD"]
        victor = [t for t in tributes if t.status == "VICTOR"]

        if alive:
            alive_lines = []
            for t in alive:
                gender_icon = "♂" if t.display_gender == "M" else ("♀" if t.display_gender == "F" else "⚧")
                alive_lines.append(
                    f"**D{t.district}{t.display_gender} {t.name}** {gender_icon} — Score: `{t.training_score if t.training_score is not None else '?'}` | Kills: `{t.kills}`"
                )
            _add_field_chunks(embed, "🟢 ALIVE", alive_lines)

        if victor:
            for t in victor:
                embed.add_field(
                    name="👑 VICTOR",
                    value=f"**D{t.district}{t.display_gender} {t.name}** — Score: `{t.training_score if t.training_score is not None else '?'}` | Kills: `{t.kills}`",
                    inline=False,
                )

        if dead:
            dead_lines = []
            for t in dead:
                placement = f"#{t.placement}" if t.placement else "?"
                cause = t.death_cause or "Unknown"
                dead_lines.append(f"D{t.district}{t.display_gender} {t.name} — {placement} | {cause}")
            _add_field_chunks(embed, "💀 FALLEN", dead_lines)

        embed.set_footer(text=f"Total tributes: {len(tributes)} | Alive: {len(alive)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /markets ──────────────────────────────────────────────────────────────

    @app_commands.command(name="markets", description="Browse all open betting markets with pagination")
    async def markets(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            mkt_result = await session.execute(
                select(Market).where(Market.status == "OPEN")
            )
            all_markets = mkt_result.scalars().all()

            trib_result = await session.execute(select(Tribute))
            tribute_map = {t.id: t for t in trib_result.scalars().all()}

            t_result = await session.execute(select(MarketTemplate))
            custom_type_labels = {f"CUSTOM_{t.id}": t.name for t in t_result.scalars().all()}

        if not all_markets:
            await interaction.followup.send("No open markets at the moment.", ephemeral=True)
            return

        sorted_mkts = sort_markets(all_markets, tribute_map)
        view = MarketPageView(sorted_mkts, tribute_map, is_admin=False, extra_type_labels=custom_type_labels)
        msg = await interaction.followup.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = msg

    # ── /balance ──────────────────────────────────────────────────────────────

    @app_commands.command(name="balance", description="Check your chip balance and stats")
    async def balance(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            user = await _get_or_create_user(session, interaction.user)
            chips = user.chips
            wagered = user.total_wagered
            won = user.total_won

        embed = discord.Embed(
            title=f"⚡ {interaction.user.display_name}'s Balance",
            color=0xC9A227,
        )
        embed.add_field(name="Chips", value=fmt_chips(chips), inline=False)
        embed.add_field(name="Total Wagered", value=fmt_chips(wagered))
        embed.add_field(name="Total Won", value=fmt_chips(won))
        if wagered > 0:
            roi = ((won - wagered) / wagered) * 100
            embed.add_field(name="ROI", value=f"{roi:+.1f}%")
        embed.set_footer(text="May the odds be ever in your favor.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="View the top chip holders")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        async with get_session() as session:
            result = await session.execute(
                select(User).order_by(User.chips.desc()).limit(10)
            )
            users = result.scalars().all()

        if not users:
            await interaction.followup.send("No players yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 CAPITOL LEADERBOARD — TOP BETTORS",
            color=0xFFD700,
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, u in enumerate(users):
            medal = medals[i] if i < 3 else f"**{i + 1}.**"
            lines.append(f"{medal} **{u.username}** — {fmt_chips(u.chips)}")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Balances update in real time.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DisplayCog(bot))

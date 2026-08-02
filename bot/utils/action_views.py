from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot.utils.checks import is_admin_member
from bot.utils.formatters import fmt_chips
from bot.utils.restrictions import is_fully_restricted, set_full_restriction


def render_request_content(
    kind: str,
    user_id: int,
    amount: int,
    processed_by: discord.abc.User | None = None,
    processed_at: datetime | None = None,
) -> str:
    """Builds the withdraw/deposit request message body from scratch — used both
    for the initial post and to rebuild it after "Mark Done" is pressed, so the
    text is never parsed back out of a previous version of itself."""
    mention = f"<@{user_id}>"
    if kind == "WITHDRAW":
        header = (
            f"💸 **Withdrawal request** — {mention} is converting **{fmt_chips(amount)}** "
            "into Panars. Their chip balance has already been debited."
        )
        commands = (
            "Run this to pay them out:\n"
            f"`/admin1 award-deprive citizen:{mention} operation:Award resource:Panars amount:{amount}`"
        )
    else:
        header = (
            f"🏦 **Deposit request** — {mention} wants to convert **{amount:,} Panars** "
            "into chips."
        )
        commands = (
            "Take their Panars, then credit the chips:\n"
            f"`/admin1 award-deprive citizen:{mention} operation:Deprive resource:Panars amount:{amount}`\n"
            f"`/settings chips_give user:{mention} amount:{amount}`"
        )
    if processed_by is not None:
        ts = int((processed_at or datetime.now(timezone.utc)).timestamp())
        return f"{header}\n✅ Processed by {processed_by.mention} at <t:{ts}:f>."
    return f"{header}\n{commands}"


class BlockToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"blocktoggle:(?P<guild_id>[0-9]+):(?P<user_id>[0-9]+)",
):
    """Persistent Block/Unblock button — attached to withdraw/deposit requests
    and every bet/parlay/win log message. Blocking only prevents new
    bets/parlays; it never touches already-pending ones."""

    def __init__(self, guild_id: int, user_id: int, blocked: bool) -> None:
        super().__init__(
            discord.ui.Button(
                label="Unblock" if blocked else "Block",
                style=discord.ButtonStyle.success if blocked else discord.ButtonStyle.danger,
                custom_id=f"blocktoggle:{guild_id}:{user_id}",
            )
        )
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        guild_id = int(match["guild_id"])
        user_id = int(match["user_id"])
        from bot.database.engine import get_read_session

        async with get_read_session() as session:
            blocked = await is_fully_restricted(session, guild_id, user_id)
        return cls(guild_id, user_id, blocked)

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not await is_admin_member(member, self.guild_id):
            await interaction.response.send_message(
                "You don't have permission to do that.", ephemeral=True
            )
            return

        from bot.database.engine import get_read_session

        async with get_read_session() as session:
            currently_blocked = await is_fully_restricted(session, self.guild_id, self.user_id)
        will_block = not currently_blocked

        confirm_view = _BlockConfirmView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            will_block=will_block,
            origin_message=interaction.message,
        )
        verb = "block" if will_block else "unblock"
        await interaction.response.send_message(
            f"Are you sure you want to **{verb}** <@{self.user_id}> from placing new bets/parlays?",
            view=confirm_view,
            ephemeral=True,
        )


class RequestDoneButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"reqdone:(?P<request_id>[0-9]+)",
):
    """Persistent "Mark Done" button on a withdraw/deposit request — strips the
    copy-paste GM command(s) out of the message and marks it processed."""

    def __init__(self, request_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Mark Done",
                style=discord.ButtonStyle.primary,
                custom_id=f"reqdone:{request_id}",
            )
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["request_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "You don't have permission to do that.", ephemeral=True
            )
            return
        if not await is_admin_member(member, interaction.guild_id):
            await interaction.response.send_message(
                "You don't have permission to do that.", ephemeral=True
            )
            return

        from bot.database.engine import get_session
        from bot.database.models import ChipRequest

        async with get_session() as session:
            req = await session.get(ChipRequest, self.request_id)
            if req is None:
                await interaction.response.send_message("Request not found.", ephemeral=True)
                return
            if req.status == "DONE":
                await interaction.response.send_message(
                    "This request has already been marked done.", ephemeral=True
                )
                return
            req.status = "DONE"
            guild_id, user_id, kind, amount = req.guild_id, req.user_id, req.kind, req.amount
            blocked = await is_fully_restricted(session, guild_id, user_id)

        content = render_request_content(
            kind, user_id, amount, processed_by=member, processed_at=datetime.now(timezone.utc)
        )
        new_view = build_request_view(None, guild_id, user_id, blocked)
        try:
            await interaction.response.edit_message(content=content, view=new_view)
        except discord.NotFound:
            pass


class _BlockConfirmView(discord.ui.View):
    """Short-lived (non-persistent) confirmation step before a block toggle
    actually takes effect — doesn't need to survive a bot restart."""

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        will_block: bool,
        origin_message: discord.Message | None,
    ) -> None:
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.will_block = will_block
        self.origin_message = origin_message

        confirm = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.danger if will_block else discord.ButtonStyle.success,
        )
        confirm.callback = self._on_confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        from bot.database.engine import get_session
        from bot.database.models import ChipRequest

        async with get_session() as session:
            await set_full_restriction(session, self.guild_id, self.user_id, self.will_block)

            chip_request = None
            request_id = _find_request_id(self.origin_message)
            if request_id is not None:
                chip_request = await session.get(ChipRequest, request_id)

        verb = "blocked" if self.will_block else "unblocked"
        await interaction.response.edit_message(
            content=f"<@{self.user_id}> has been {verb}.", view=None
        )

        if self.origin_message is not None:
            new_view = build_request_view(chip_request, self.guild_id, self.user_id, self.will_block)
            try:
                await self.origin_message.edit(view=new_view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


def _find_request_id(message: discord.Message | None) -> int | None:
    if message is None:
        return None
    for row in message.components:
        for child in getattr(row, "children", []):
            custom_id = getattr(child, "custom_id", None)
            if custom_id and custom_id.startswith("reqdone:"):
                try:
                    return int(custom_id.split(":", 1)[1])
                except ValueError:
                    return None
    return None


def build_request_view(
    chip_request,
    guild_id: int,
    user_id: int,
    blocked: bool,
) -> discord.ui.View:
    """Builds the button row for a withdraw/deposit request (or a plain bet/
    parlay/win log, passing chip_request=None) — freshly constructed each time
    a message is sent or edited, so it always reflects current state."""
    view = discord.ui.View(timeout=None)
    if chip_request is not None and chip_request.status == "PENDING":
        view.add_item(RequestDoneButton(chip_request.id))
    view.add_item(BlockToggleButton(guild_id, user_id, blocked))
    return view

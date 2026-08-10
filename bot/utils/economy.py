from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ChipRequest, User


async def economy_totals(session: AsyncSession, guild_id: int) -> dict:
    """Aggregate chip/Panar economy figures shared by the bot, web dashboard,
    and Activity admin overviews — one definition so the three surfaces can't
    drift out of sync on what "chips spent" etc. actually means.

    - chips_spent: total chips ever wagered (User.total_wagered), win or lose
    - chips_circulating: chips currently held across all players
    - panars_converted: Panars a player paid an admin for chips (completed /deposit requests)
    - chips_withdrawn: chips converted back into Panars (completed /withdraw requests)
    - net_panars: panars_converted - chips_withdrawn
    """
    chips_spent = await session.scalar(
        select(func.coalesce(func.sum(User.total_wagered), 0)).where(
            User.guild_id == guild_id
        )
    )
    chips_circulating = await session.scalar(
        select(func.coalesce(func.sum(User.chips), 0)).where(User.guild_id == guild_id)
    )
    panars_converted = await session.scalar(
        select(func.coalesce(func.sum(ChipRequest.amount), 0)).where(
            ChipRequest.guild_id == guild_id,
            ChipRequest.kind == "DEPOSIT",
            ChipRequest.status == "DONE",
        )
    )
    chips_withdrawn = await session.scalar(
        select(func.coalesce(func.sum(ChipRequest.amount), 0)).where(
            ChipRequest.guild_id == guild_id,
            ChipRequest.kind == "WITHDRAW",
            ChipRequest.status == "DONE",
        )
    )

    return {
        "chips_spent": chips_spent or 0,
        "chips_circulating": chips_circulating or 0,
        "panars_converted": panars_converted or 0,
        "chips_withdrawn": chips_withdrawn or 0,
        "net_panars": (panars_converted or 0) - (chips_withdrawn or 0),
    }

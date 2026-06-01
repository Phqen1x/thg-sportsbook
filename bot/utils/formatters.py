from __future__ import annotations
import logging

import discord

log = logging.getLogger("capitol")

STATUS_COLORS = {
    "PENDING":    0xF0C040,
    "WON":        0x4CAF50,
    "LOST":       0xCF4444,
    "CASHED_OUT": 0x5B9BD5,
    "VOIDED":     0x888888,
}

STATUS_EMOJI = {
    "PENDING":    "🟡",
    "WON":        "🟢",
    "LOST":       "🔴",
    "CASHED_OUT": "🔵",
    "VOIDED":     "⚫",
}

TRIBUTE_STATUS_EMOJI = {
    "ALIVE":  "🟢",
    "DEAD":   "💀",
    "VICTOR": "👑",
}


def fmt_chips(n: int) -> str:
    return f"{n:,} chips"


def fmt_odds(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def fmt_pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def fmt_status(s: str) -> str:
    emoji = STATUS_EMOJI.get(s, "")
    return f"{emoji} {s}"


def fmt_tribute_status(s: str) -> str:
    emoji = TRIBUTE_STATUS_EMOJI.get(s, "")
    return f"{emoji} {s}"


def market_type_label(t: str) -> str:
    return {
        "TRIBUTE_WINS":       "Victor",
        "TRIBUTE_PLACEMENT":  "Placement",
        "TRIBUTE_TOP_N":      "Top N Finish",
        "TRIBUTE_KILLS":      "Top Killer",
        "KILL_EVENT":         "Kill Event",
        "DEATH_CAUSE":        "Death Cause",
        "FIRST_BLOOD":        "First Blood",
        "BLOODBATH_SURVIVOR": "Bloodbath Survivor",
        "SPONSOR_EVENT":      "Sponsor Event",
        "KILLS_OU":           "Kills Over/Under",
        "PLACEMENT_OU":       "Placement Over/Under",
    }.get(t, t)


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    """
    Defers the interaction response. Returns True on success, False if the
    interaction token has already expired. Discord allows 3 seconds from the
    time the interaction is sent to acknowledge it — if the event loop is
    saturated or there is network latency the window can be missed; the user
    should simply retry the command.
    """
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        cmd = interaction.command
        # qualified_name gives the full path, e.g. "admin market list" not just "list"
        name = cmd.qualified_name if cmd else "?"
        log.warning(
            f"Interaction expired before defer() — command '/{name}' by {interaction.user}. "
            "Discord's 3-second window was missed (event loop busy or network lag). "
            "User should retry."
        )
        return False
    except discord.InteractionResponded:
        # Already acknowledged (e.g. duplicate dispatch) — safe to continue
        return True
    except Exception as e:
        log.error(f"Unexpected error in safe_defer: {e}", exc_info=True)
        return False

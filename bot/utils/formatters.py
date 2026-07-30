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


def fmt_odds_with_mult(n: int) -> str:
    main = fmt_odds(n)
    if n > 0:
        mult = n / 100.0 + 1.0
    elif n < 0:
        mult = 100.0 / abs(n) + 1.0
    else:
        mult = 1.0
    return f"{main} ({mult:,.2f}x)"


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
        "BLOODBATH_SURVIVOR":        "Bloodbath Survivor",
        "TRIBUTE_KILLED_BLOODBATH":  "Bloodbath Kill",
        "KILLS_OU":                "Kills Over/Under",
        "PLACEMENT_OU":            "Placement Over/Under",
        "DISTRICT_VICTOR":         "District Futures",
        "DISTRICT_KILLS_OU":       "District Kills O/U",
        "DISTRICT_BOTH_BLOODBATH":   "District Both Bloodbath",
        "DISTRICT_WIPED_BLOODBATH":  "District BB Wipe",
        "DISTRICT_BOTH_FINAL_8":   "District Both Final 8",
        "DISTRICT_ONE_FINAL_8":    "District One Final 8",
        "DISTRICT_BOTH_FINAL_5":   "District Both Final 5",
        "DISTRICT_ONE_FINAL_5":    "District One Final 5",
        "ALLIANCE_VICTOR":         "Alliance Victor",
        "ALLIANCE_KILLS_OU":       "Alliance Kills O/U",
        "BLOODBATH_KILLS_OU":        "Bloodbath Kills O/U",
        "ALLIANCE_ALL_FINAL_8":    "Alliance All Final 8",
        "ALLIANCE_ONE_FINAL_8":    "Alliance One Final 8",
        "ALLIANCE_ALL_FINAL_5":    "Alliance All Final 5",
        "ALLIANCE_ONE_FINAL_5":    "Alliance One Final 5",
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

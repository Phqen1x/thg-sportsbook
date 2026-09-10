"""Shared logic for the promotions system — Bonus Chips, profit boosts, and
deposit match. Imported by both the Discord bot cogs and the FastAPI web/Activity
routes so all three betting surfaces (and all settlement surfaces) stay
consistent.

Every function takes an explicit ``session`` (an ``AsyncSession``) and never
commits — the caller owns the transaction, exactly like the rest of the codebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from bot.database.models import (
    BonusBetLot, BonusGrant, BoostShopItem, BoostShopPurchase,
    DepositMatchClaim, DepositMatchPromo,
    ProfitBoostGrant, ProfitBoostTemplate, ProfitBoostToken,
    PromoClaimDrop, PromoClaimRedemption,
)
from bot.odds.calculator import (
    american_to_decimal, decimal_to_american, parlay_payout, straight_payout,
)
from bot.utils.market_view import _type_section


def _now() -> datetime:
    return datetime.utcnow()


# ── Bonus Chips ───────────────────────────────────────────────────────────────


async def expire_stale(session, guild_id: int, user_id: int | None = None) -> None:
    """Flip ACTIVE bonus lots / profit tokens whose expiry has passed to EXPIRED.
    Cheap to call opportunistically before a balance read or at settlement."""
    now = _now()
    lot_q = select(BonusBetLot).where(
        BonusBetLot.guild_id == guild_id,
        BonusBetLot.status == "ACTIVE",
        BonusBetLot.expires_at.is_not(None),
        BonusBetLot.expires_at < now,
    )
    tok_q = select(ProfitBoostToken).where(
        ProfitBoostToken.guild_id == guild_id,
        ProfitBoostToken.status == "ACTIVE",
        ProfitBoostToken.expires_at.is_not(None),
        ProfitBoostToken.expires_at < now,
    )
    if user_id is not None:
        lot_q = lot_q.where(BonusBetLot.discord_user_id == user_id)
        tok_q = tok_q.where(ProfitBoostToken.discord_user_id == user_id)
    for lot in (await session.execute(lot_q)).scalars().all():
        lot.status = "EXPIRED"
    for tok in (await session.execute(tok_q)).scalars().all():
        tok.status = "EXPIRED"


async def active_bonus_lots(session, guild_id: int, user_id: int) -> list[BonusBetLot]:
    """ACTIVE, non-expired lots with credit left, soonest-expiry first (nulls
    last), then oldest first."""
    now = _now()
    rows = (await session.execute(
        select(BonusBetLot).where(
            BonusBetLot.guild_id == guild_id,
            BonusBetLot.discord_user_id == user_id,
            BonusBetLot.status == "ACTIVE",
            BonusBetLot.amount_remaining > 0,
        )
    )).scalars().all()
    rows = [r for r in rows if r.expires_at is None or r.expires_at > now]
    rows.sort(key=lambda r: (r.expires_at is None, r.expires_at or now, r.id))
    return rows


async def bonus_balance(session, guild_id: int, user_id: int) -> int:
    return sum(r.amount_remaining for r in await active_bonus_lots(session, guild_id, user_id))


async def next_bonus_expiry(session, guild_id: int, user_id: int) -> datetime | None:
    for r in await active_bonus_lots(session, guild_id, user_id):
        if r.expires_at is not None:
            return r.expires_at
    return None


async def spend_bonus(session, guild_id: int, user_id: int, amount: int) -> None:
    """Debit ``amount`` of bonus credit, soonest-expiry-first. Raises
    ``ValueError`` if the balance is short (callers should pre-check)."""
    if amount <= 0:
        return
    remaining = amount
    for lot in await active_bonus_lots(session, guild_id, user_id):
        if remaining <= 0:
            break
        take = min(lot.amount_remaining, remaining)
        lot.amount_remaining -= take
        remaining -= take
        if lot.amount_remaining <= 0:
            lot.status = "EXHAUSTED"
    if remaining > 0:
        raise ValueError(f"insufficient bonus balance (short {remaining})")


async def refund_bonus(
    session, guild_id: int, user_id: int, amount: int,
    expires_at: datetime | None = None,
) -> None:
    """Return bonus credit as a fresh lot — used when a bonus-funded wager is
    voided. Original lot expiry is not tracked, so the refund is permanent."""
    if amount <= 0:
        return
    session.add(BonusBetLot(
        guild_id=guild_id, discord_user_id=user_id,
        original_amount=amount, amount_remaining=amount,
        expires_at=expires_at, source="REFUND", status="ACTIVE",
    ))


async def revoke_refund_lot(session, guild_id: int, user_id: int, amount: int) -> None:
    """Undo the most recent REFUND lot for a user (used by _unresolve_market
    when reversing a previously-voided bonus wager)."""
    if amount <= 0:
        return
    lot = (await session.execute(
        select(BonusBetLot).where(
            BonusBetLot.guild_id == guild_id,
            BonusBetLot.discord_user_id == user_id,
            BonusBetLot.source == "REFUND",
            BonusBetLot.status.in_(("ACTIVE", "EXHAUSTED")),
        ).order_by(BonusBetLot.id.desc())
    )).scalars().first()
    if lot is None:
        return
    lot.amount_remaining = max(0, lot.amount_remaining - amount)
    lot.status = "REVOKED"


def _expiry_from_hours(hours: int | None) -> datetime | None:
    if hours is None or hours <= 0:
        return None
    return _now() + timedelta(hours=hours)


async def grant_bonus_to_users(
    session, guild_id: int, user_ids: list[int], amount: int,
    expiry_hours: int | None, source: str, grant_id: int | None = None,
) -> int:
    exp = _expiry_from_hours(expiry_hours)
    n = 0
    for uid in user_ids:
        session.add(BonusBetLot(
            guild_id=guild_id, discord_user_id=uid,
            original_amount=amount, amount_remaining=amount,
            expires_at=exp, source=source, grant_id=grant_id, status="ACTIVE",
        ))
        n += 1
    return n


async def deduct_bonus_from_users(
    session, guild_id: int, user_ids: list[int], amount: int,
) -> int:
    """Reduce each listed user's ACTIVE bonus balance by up to ``amount``
    (soonest-expiry-first). ``amount`` <= 0 means "wipe all". Returns the count
    of users touched."""
    n = 0
    for uid in user_ids:
        lots = await active_bonus_lots(session, guild_id, uid)
        if not lots:
            continue
        remaining = amount if amount > 0 else sum(l.amount_remaining for l in lots)
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.amount_remaining, remaining)
            lot.amount_remaining -= take
            remaining -= take
            if lot.amount_remaining <= 0:
                lot.status = "EXHAUSTED"
        n += 1
    return n


async def bonus_user_ids(session, guild_id: int) -> list[int]:
    """Distinct users who currently hold any ACTIVE bonus lot."""
    rows = (await session.execute(
        select(BonusBetLot.discord_user_id).where(
            BonusBetLot.guild_id == guild_id,
            BonusBetLot.status == "ACTIVE",
        ).distinct()
    )).scalars().all()
    return list(rows)


# ── Profit boosts ────────────────────────────────────────────────────────────


def boost_matches_market(scope_type: str, scope_id: int | None, market) -> bool:
    """Does a boost with this scope apply to ``market``?"""
    if scope_type == "ANY":
        return True
    section = _type_section(market.type)
    if scope_type == "DISTRICT":
        return section == "district" and market.placement_num == scope_id
    if scope_type == "ALLIANCE":
        return section == "alliance" and market.placement_num == scope_id
    return False


def boost_scope_text(scope_type: str, scope_id) -> str:
    """Human label for a boost's scope, e.g. ``District 2`` / ``Alliance #4`` /
    ``any bet``."""
    if scope_type == "DISTRICT":
        return f"District {scope_id}"
    if scope_type == "ALLIANCE":
        return f"Alliance #{scope_id}"
    return "any bet"


async def active_boost_tokens(session, guild_id: int, user_id: int) -> list[ProfitBoostToken]:
    now = _now()
    rows = (await session.execute(
        select(ProfitBoostToken).where(
            ProfitBoostToken.guild_id == guild_id,
            ProfitBoostToken.discord_user_id == user_id,
            ProfitBoostToken.status == "ACTIVE",
        )
    )).scalars().all()
    return [r for r in rows if r.expires_at is None or r.expires_at > now]


async def eligible_boost_tokens(
    session, guild_id: int, user_id: int, markets: list, wager: int | None = None,
) -> list[ProfitBoostToken]:
    """Tokens the user may apply to a wager over ``markets`` (one market = a
    straight bet; many = a parlay slip). For a parlay a scoped token is eligible
    when AT LEAST ONE leg matches — only the matching legs' odds get boosted."""
    out = []
    for tok in await active_boost_tokens(session, guild_id, user_id):
        if tok.max_wager is not None and wager is not None and wager > tok.max_wager:
            continue
        if any(boost_matches_market(tok.scope_type, tok.scope_id, m) for m in markets):
            out.append(tok)
    return out


def boosted_single_payout(raw_payout: int, wager: int, pct: float) -> int:
    """Boost profit (not stake) by ``pct`` percent."""
    if pct <= 0:
        return raw_payout
    return wager + round((raw_payout - wager) * (1 + pct / 100.0))


def boosted_parlay_payout(
    leg_odds: list[int], wager: int, pct: float,
    matching_mask: list[bool] | None = None,
) -> int:
    """Lift the American odds of the legs the boost applies to (profit ×(1+pct%))
    then re-price the whole parlay through the normal vig pipeline, so the house
    edge and multi-leg dampening still apply. ``matching_mask`` None => every leg
    (ANY-scoped token)."""
    if pct <= 0 or not leg_odds:
        return parlay_payout(wager, leg_odds)
    factor = 1 + pct / 100.0
    adjusted: list[int] = []
    for i, odds in enumerate(leg_odds):
        apply = True if matching_mask is None else matching_mask[i]
        if apply:
            d = 1 + (american_to_decimal(odds) - 1) * factor
            adjusted.append(decimal_to_american(d))
        else:
            adjusted.append(odds)
    return parlay_payout(wager, adjusted)


def parlay_match_mask(scope_type: str, scope_id: int | None, markets: list) -> list[bool]:
    if scope_type == "ANY":
        return [True] * len(markets)
    return [boost_matches_market(scope_type, scope_id, m) for m in markets]


async def consume_boost(
    session, token: ProfitBoostToken, *, bet_id: int | None = None,
    parlay_id: int | None = None,
) -> None:
    token.status = "USED"
    token.used_at = _now()
    token.used_bet_id = bet_id
    token.used_parlay_id = parlay_id


async def restore_boost_for_bet(session, guild_id: int, bet_id: int) -> None:
    tok = (await session.execute(
        select(ProfitBoostToken).where(
            ProfitBoostToken.guild_id == guild_id,
            ProfitBoostToken.used_bet_id == bet_id,
            ProfitBoostToken.status == "USED",
        )
    )).scalars().first()
    if tok:
        tok.status = "ACTIVE"
        tok.used_at = None
        tok.used_bet_id = None
        tok.used_parlay_id = None


async def restore_boost_for_parlay(session, guild_id: int, parlay_id: int) -> None:
    tok = (await session.execute(
        select(ProfitBoostToken).where(
            ProfitBoostToken.guild_id == guild_id,
            ProfitBoostToken.used_parlay_id == parlay_id,
            ProfitBoostToken.status == "USED",
        )
    )).scalars().first()
    if tok:
        tok.status = "ACTIVE"
        tok.used_at = None
        tok.used_bet_id = None
        tok.used_parlay_id = None


async def reconsume_boost(
    session, token_id, *, bet_id: int | None = None, parlay_id: int | None = None,
) -> None:
    """Re-mark a boost token USED — for _unresolve_market, when a previously
    VOIDED bonus/boost wager goes back to PENDING and the boost is live again."""
    if not token_id:
        return
    tok = await session.get(ProfitBoostToken, int(token_id))
    if tok is not None and tok.status == "ACTIVE":
        await consume_boost(session, tok, bet_id=bet_id, parlay_id=parlay_id)


async def grant_boost_to_users(
    session, guild_id: int, template: ProfitBoostTemplate, user_ids: list[int],
    expiry_hours: int | None, granted_by: int | None, grant_id: int | None = None,
) -> int:
    exp = _expiry_from_hours(expiry_hours)
    n = 0
    for uid in user_ids:
        session.add(ProfitBoostToken(
            guild_id=guild_id, discord_user_id=uid, template_id=template.id,
            boost_pct=template.boost_pct, scope_type=template.scope_type,
            scope_id=template.scope_id, max_wager=template.max_wager,
            status="ACTIVE", expires_at=exp, granted_by=granted_by, grant_id=grant_id,
        ))
        n += 1
    return n


# ── Claimable promo drops (channel message + "Claim" button) ─────────────────

_DROP_EMBED_COLOR = 0xC9A227  # Capitol gold, matches the Gamemaker announcement


def claim_drop_reward_label(
    reward_kind: str, *, bonus_amount: int | None = None,
    boost_pct: float | None = None, scope_type: str | None = None,
    scope_id=None,
) -> str:
    """Human "what you get" text for a claim drop, shared by every surface."""
    if (reward_kind or "").upper() == "BONUS":
        return f"{int(bonus_amount or 0):,} Bonus Chips"
    return (
        f"+{float(boost_pct or 0):g}% profit boost "
        f"({boost_scope_text(scope_type or 'ANY', scope_id)})"
    )


def build_claim_drop_embed(
    message_text: str, reward_label: str, *,
    reward_expiry_hours: int | None = None, max_claims: int | None = None,
) -> dict:
    """Raw Discord embed dict for a claim-drop announcement. The custom body goes
    in the description; a field at the bottom spells out what the drop contains.
    The bot converts this with ``discord.Embed.from_dict``; the web app posts it
    straight through the REST API."""
    contains = reward_label
    extras = []
    if reward_expiry_hours:
        if reward_expiry_hours % 24:
            extras.append(f"expires {reward_expiry_hours}h after you claim")
        else:
            extras.append(f"expires {reward_expiry_hours // 24}d after you claim")
    if max_claims:
        extras.append(f"first {max_claims:,} claimers only")
    if extras:
        contains += "\n" + " · ".join(extras)
    return {
        "title": "🎁 Promo Drop",
        "description": (message_text or "").strip()[:4000],
        "color": _DROP_EMBED_COLOR,
        "fields": [{"name": "This drop contains", "value": contains[:1024], "inline": False}],
        "footer": {"text": "Panem Sportsbook — press Claim below."},
    }


async def claim_drop_embed_for(
    session, drop, *, closed_reason: str | None = None,
) -> dict:
    """Rebuild the announcement embed for an existing ``PromoClaimDrop`` row —
    used by the bot when it needs to re-render the message (e.g. to append a
    closing line once the drop is fully claimed or expired)."""
    if (drop.reward_kind or "").upper() == "BONUS":
        reward_label = claim_drop_reward_label("BONUS", bonus_amount=drop.bonus_amount)
    else:
        tpl = await session.get(ProfitBoostTemplate, drop.boost_template_id or 0)
        if tpl is not None:
            reward_label = claim_drop_reward_label(
                "BOOST", boost_pct=tpl.boost_pct,
                scope_type=tpl.scope_type, scope_id=tpl.scope_id,
            )
        else:
            reward_label = "a profit boost"
    embed = build_claim_drop_embed(
        drop.message_text, reward_label,
        reward_expiry_hours=drop.reward_expiry_hours, max_claims=drop.max_claims,
    )
    if closed_reason == "expired":
        embed["description"] = (embed["description"] + "\n\n⏰ **This promo drop has expired.**")[:4000]
    elif closed_reason == "claimed":
        embed["description"] = (embed["description"] + "\n\n✅ **This promo drop has been fully claimed.**")[:4000]
    return embed


async def claim_drop_redeem(
    session, drop: PromoClaimDrop, user_id: int,
) -> tuple[dict | None, str | None]:
    """Redeem one press of a claim-drop button for ``user_id``. Runs the
    expiry / claim-limit / already-claimed checks then grants the configured
    bonus or boost. The caller is responsible for having already verified the
    user isn't blocked from betting.

    Returns ``({"message": str, "exhausted": bool}, None)`` on success, or
    ``(None, error_message)`` if the claim can't proceed. ``exhausted`` (and,
    on the error path, ``drop.active`` having been flipped to ``False``) tells
    the caller to disable the button on the message."""
    now = _now()
    if not drop.active:
        return None, "This drop is no longer available."
    if drop.expires_at is not None and drop.expires_at <= now:
        drop.active = False
        return None, "This drop has expired."

    already = (await session.execute(
        select(PromoClaimRedemption).where(
            PromoClaimRedemption.drop_id == drop.id,
            PromoClaimRedemption.discord_user_id == user_id,
        )
    )).scalars().first()
    if already is not None:
        return None, "You've already claimed this drop."

    taken = (await session.execute(
        select(func.count()).select_from(PromoClaimRedemption).where(
            PromoClaimRedemption.drop_id == drop.id
        )
    )).scalar_one()
    if drop.max_claims is not None and taken >= drop.max_claims:
        drop.active = False
        return None, "Every claim for this drop has already been taken."

    exp_hours = drop.reward_expiry_hours
    if drop.reward_kind == "BONUS":
        amount = int(drop.bonus_amount or 0)
        if amount <= 0:
            return None, "This drop isn't configured correctly."
        await grant_bonus_to_users(
            session, drop.guild_id, [user_id], amount, exp_hours,
            source="CLAIM", grant_id=drop.id,
        )
        reward_txt = f"{amount:,} Bonus Chips"
        amount_or_pct = float(amount)
    else:
        tpl = await session.get(ProfitBoostTemplate, drop.boost_template_id or 0)
        if tpl is None or tpl.guild_id != drop.guild_id:
            return None, "The profit boost for this drop no longer exists."
        await grant_boost_to_users(
            session, drop.guild_id, tpl, [user_id], exp_hours,
            granted_by=drop.created_by, grant_id=drop.id,
        )
        reward_txt = (
            f"a +{tpl.boost_pct:g}% profit boost ({boost_scope_text(tpl.scope_type, tpl.scope_id)})"
        )
        amount_or_pct = float(tpl.boost_pct)

    session.add(PromoClaimRedemption(
        guild_id=drop.guild_id, drop_id=drop.id, discord_user_id=user_id,
        reward_kind=drop.reward_kind, amount_or_pct=amount_or_pct,
    ))
    drop.claims_count = taken + 1
    exhausted = drop.max_claims is not None and drop.claims_count >= drop.max_claims
    if exhausted:
        drop.active = False
    exp_note = ""
    if exp_hours:
        exp_note = f" It expires in {exp_hours}h." if exp_hours % 24 else f" It expires in {exp_hours // 24}d."
    return {"message": f"🎁 You claimed {reward_txt}!{exp_note}", "exhausted": exhausted}, None


# ── Placement: validate + apply bonus/boost on a wager ───────────────────────


async def validate_wager_promos(
    session, guild_id: int, user_id: int, current_chips: int, *,
    wager, bonus_amount, boost_token_id, markets: list, is_parlay: bool,
    raw_payout: int | None = None,
) -> tuple[dict | None, str | None]:
    """Validate the optional bonus-credit and profit-boost on a wager. No
    mutation.

    ``wager`` is the real chips risked (may be 0). ``bonus_amount`` is bonus
    credit staked *on top* — the total stake for payout purposes is
    ``wager + bonus_amount``. A win pays the same as a normal wager of that
    size; the bonus stake itself is not returned (subtracted at settlement by
    ``split_won_credit``). ``raw_payout`` is ignored — the payout is computed
    here from ``markets`` and the total stake.

    On success returns (info, None) where info has:
      ``bonus_amount`` – clamped to the user's bonus balance
      ``real_part``    – real chips to debit / add to total_wagered ( == wager )
      ``total_stake``  – store as Bet.wager / Parlay.total_wager
      ``payout``       – GROSS payout on the total stake, profit-boosted; store
                         as payout_if_win / total_payout
      ``raw_payout``   – GROSS payout on the total stake with NO boost
      ``net_if_win``   – ``payout - bonus_amount`` (what the bettor receives on
                         a win before house cut) — for confirmations/previews
      ``boost_token``, ``boost_pct``
    On failure returns (None, message)."""
    try:
        wager = max(0, int(wager or 0))
    except (TypeError, ValueError):
        wager = 0
    try:
        bonus_amount = max(0, int(bonus_amount or 0))
    except (TypeError, ValueError):
        bonus_amount = 0
    if bonus_amount > 0:
        bal = await bonus_balance(session, guild_id, user_id)
        if bonus_amount > bal:
            return None, (
                f"You only have {bal:,} in Bonus Chips but tried to use {bonus_amount:,}."
            )
    total_stake = wager + bonus_amount
    if total_stake < 1:
        return None, "Enter a wager, apply Bonus Chips, or both."
    if current_chips < wager:
        return None, f"Insufficient chips. You have {current_chips:,} but need {wager:,}."

    odds = [m.odds for m in markets]
    if is_parlay:
        raw = parlay_payout(total_stake, odds)
    else:
        raw = straight_payout(total_stake, odds[0])

    boost_token = None
    boost_pct = 0.0
    payout = raw
    if boost_token_id:
        try:
            boost_token = await session.get(ProfitBoostToken, int(boost_token_id))
        except (TypeError, ValueError):
            boost_token = None
        if (boost_token is None or boost_token.guild_id != guild_id
                or boost_token.discord_user_id != user_id
                or boost_token.status != "ACTIVE"):
            return None, "That profit boost isn't available."
        if boost_token.expires_at is not None and boost_token.expires_at <= _now():
            return None, "That profit boost has expired."
        if boost_token.max_wager is not None and total_stake > boost_token.max_wager:
            return None, (
                f"That profit boost only works on wagers of {boost_token.max_wager:,} or less."
            )
        mask = parlay_match_mask(boost_token.scope_type, boost_token.scope_id, markets)
        if not any(mask):
            return None, "That profit boost doesn't apply to this bet."
        boost_pct = boost_token.boost_pct
        if is_parlay:
            payout = boosted_parlay_payout(odds, total_stake, boost_pct, mask)
        else:
            payout = boosted_single_payout(raw, total_stake, boost_pct)

    return {
        "bonus_amount": bonus_amount,
        "real_part": wager,
        "total_stake": total_stake,
        "payout": payout,
        "raw_payout": raw,
        "net_if_win": payout - bonus_amount,
        "boost_token": boost_token,
        "boost_pct": boost_pct,
    }, None


async def commit_wager_promos(
    session, guild_id: int, user, info: dict, *,
    bet_id: int | None = None, parlay_id: int | None = None,
) -> None:
    """Apply the validated bonus/boost effects: debit bonus lots, bump
    ``bonus_wagered``, consume the boost token. Caller still debits real chips
    and bumps ``total_wagered`` by ``info['real_part']``."""
    if info["bonus_amount"] > 0:
        await spend_bonus(session, guild_id, user.discord_id, info["bonus_amount"])
        user.bonus_wagered += info["bonus_amount"]
    if info["boost_token"] is not None:
        await consume_boost(session, info["boost_token"], bet_id=bet_id, parlay_id=parlay_id)


# ── First-touch grants (brand-new user rows) ─────────────────────────────────


async def apply_first_touch_grants(session, guild_id: int, user_id: int) -> None:
    """Seed a just-created user with the signup Bonus Chips plus anything from
    FIRST_TOUCH bonus grants and first-touch profit-boost templates."""
    from bot.database.engine import get_setting
    import json

    raw_amt = await get_setting("signup_bonus_bet_amount")
    signup_amt = json.loads(raw_amt) if raw_amt else 2500
    raw_exp = await get_setting("signup_bonus_bet_expiry_hours")
    signup_exp_hours = json.loads(raw_exp) if raw_exp else None
    if signup_amt and signup_amt > 0:
        session.add(BonusBetLot(
            guild_id=guild_id, discord_user_id=user_id,
            original_amount=signup_amt, amount_remaining=signup_amt,
            expires_at=_expiry_from_hours(signup_exp_hours),
            source="SIGNUP", status="ACTIVE",
        ))

    ft_bonus = (await session.execute(
        select(BonusGrant).where(
            BonusGrant.guild_id == guild_id,
            BonusGrant.scope == "FIRST_TOUCH",
            BonusGrant.active == True,  # noqa: E712
        )
    )).scalars().all()
    for g in ft_bonus:
        session.add(BonusBetLot(
            guild_id=guild_id, discord_user_id=user_id,
            original_amount=g.amount, amount_remaining=g.amount,
            expires_at=_expiry_from_hours(g.expiry_hours),
            source="GRANT_USER", grant_id=g.id, status="ACTIVE",
        ))

    ft_templates = (await session.execute(
        select(ProfitBoostTemplate).where(
            ProfitBoostTemplate.guild_id == guild_id,
            ProfitBoostTemplate.grant_on_first_touch == True,  # noqa: E712
            ProfitBoostTemplate.active == True,  # noqa: E712
        )
    )).scalars().all()
    ft_grant_hours: dict[int, int | None] = {}
    if ft_templates:
        for pg in (await session.execute(
            select(ProfitBoostGrant).where(
                ProfitBoostGrant.guild_id == guild_id,
                ProfitBoostGrant.scope == "FIRST_TOUCH",
                ProfitBoostGrant.active == True,  # noqa: E712
            )
        )).scalars().all():
            ft_grant_hours[pg.template_id] = pg.expiry_hours
    for tpl in ft_templates:
        session.add(ProfitBoostToken(
            guild_id=guild_id, discord_user_id=user_id, template_id=tpl.id,
            boost_pct=tpl.boost_pct, scope_type=tpl.scope_type,
            scope_id=tpl.scope_id, max_wager=tpl.max_wager, status="ACTIVE",
            expires_at=_expiry_from_hours(ft_grant_hours.get(tpl.id)),
        ))


# ── Settlement helpers ──────────────────────────────────────────────────────


def split_won_credit(wager: int, bonus_bet_amount: int, paid: int) -> tuple[int, int, int]:
    """Given ``paid`` (the ``net_payout`` result = stake + profit − house cut),
    return ``(chips_to_credit, real_won, bonus_won)``. The bonus stake is never
    returned, so chips credited = ``paid − bonus_bet_amount``; that credit is
    split into real vs bonus stats by the real fraction of the stake."""
    credited = paid - bonus_bet_amount
    if wager > 0:
        rf = (wager - bonus_bet_amount) / wager
    else:
        rf = 1.0
    real_won = round(credited * rf)
    bonus_won = credited - real_won
    return credited, real_won, bonus_won


async def reprice_parlay_after_void(session, parlay, active_markets: list) -> int:
    """Recompute a parlay's ``total_payout`` over its surviving (non-voided)
    legs, re-applying the frozen profit boost to the legs it covers."""
    odds = [m.odds for m in active_markets]
    if parlay.profit_boost_pct and parlay.profit_boost_pct > 0:
        mask = None
        if parlay.profit_boost_token_id:
            tok = await session.get(ProfitBoostToken, parlay.profit_boost_token_id)
            if tok is not None and tok.scope_type != "ANY":
                mask = parlay_match_mask(tok.scope_type, tok.scope_id, active_markets)
        return boosted_parlay_payout(odds, parlay.total_wager, parlay.profit_boost_pct, mask)
    return parlay_payout(parlay.total_wager, odds)


def void_chip_refund(wager: int, bonus_bet_amount: int) -> int:
    """Chips to return when a (partly) bonus-funded wager is voided — only the
    real-chip slice of the stake. The caller separately calls ``refund_bonus``
    for ``bonus_bet_amount``."""
    return wager - bonus_bet_amount


# ── Deposit match ────────────────────────────────────────────────────────────


async def active_deposit_promo(
    session, guild_id: int, at: datetime | None = None,
) -> DepositMatchPromo | None:
    at = at or _now()
    return (await session.execute(
        select(DepositMatchPromo).where(
            DepositMatchPromo.guild_id == guild_id,
            DepositMatchPromo.active == True,  # noqa: E712
            DepositMatchPromo.starts_at <= at,
            DepositMatchPromo.ends_at > at,
        ).order_by(DepositMatchPromo.id.desc())
    )).scalars().first()


async def apply_deposit_match(
    session, guild_id: int, user_id: int, deposit_chips: int,
    member_role_ids: set[int] | None = None,
) -> int:
    """Record ``deposit_chips`` against the live promo (if any), grant the
    matched amount to the member as a Bonus Chip lot, and return that matched
    amount (for the caller's confirmation message). The caller does NOT credit
    real chips — the match is Bonus Chips only."""
    if deposit_chips <= 0:
        return 0
    promo = await active_deposit_promo(session, guild_id)
    if promo is None:
        return 0
    if promo.role_id is not None:
        if member_role_ids is None or promo.role_id not in member_role_ids:
            return 0

    claim = (await session.execute(
        select(DepositMatchClaim).where(
            DepositMatchClaim.promo_id == promo.id,
            DepositMatchClaim.discord_user_id == user_id,
        )
    )).scalars().first()
    if claim is None:
        claim = DepositMatchClaim(
            guild_id=guild_id, promo_id=promo.id, discord_user_id=user_id,
            total_deposited=0, total_matched=0,
        )
        session.add(claim)

    headroom = max(0, promo.max_match_per_user - claim.total_matched)
    matched = min(round(deposit_chips * promo.match_pct / 100.0), headroom)
    claim.total_deposited += deposit_chips
    claim.total_matched += matched
    claim.updated_at = _now()

    if matched > 0:
        expiry_hours = (
            promo.match_bonus_expiry_days * 24
            if promo.match_bonus_expiry_days else None
        )
        await grant_bonus_to_users(
            session, guild_id, [user_id], matched, expiry_hours,
            source="DEPOSIT_MATCH", grant_id=promo.id,
        )
    return matched


# ── Settlement rebate (Bonus Chips back on settled wagers) ────────────────────
# A configurable perk: when a wager settles, give the member a slice of their
# real-chip stake back as Bonus Chips — separate rates for wins and losses. Set
# ``bonus_rebate_mode`` to OFF (default), PCT (% of the stake) or FLAT (a fixed
# Bonus Chip amount per settled wager).


async def _rebate_settings() -> dict:
    from bot.database.engine import get_setting
    import json

    async def _j(key, default):
        raw = await get_setting(key)
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw  # plain string (web dashboard writes unquoted values)

    return {
        "mode": str(await _j("bonus_rebate_mode", "OFF")).upper(),
        "win_pct": float(await _j("bonus_rebate_win_pct", 0) or 0),
        "loss_pct": float(await _j("bonus_rebate_loss_pct", 0) or 0),
        "win_flat": int(await _j("bonus_rebate_win_flat", 0) or 0),
        "loss_flat": int(await _j("bonus_rebate_loss_flat", 0) or 0),
        "expiry_days": await _j("bonus_rebate_expiry_days", None),
    }


def _rebate_amount(cfg: dict, wager_placed: int, won: bool) -> int:
    if cfg["mode"] == "PCT":
        pct = cfg["win_pct"] if won else cfg["loss_pct"]
        return int(round(max(0, wager_placed) * pct / 100.0))
    if cfg["mode"] == "FLAT":
        return cfg["win_flat"] if won else cfg["loss_flat"]
    return 0


async def award_settle_rebate(
    session, guild_id: int, user_id: int, *, wager_placed: int, won: bool,
    bet_id: int | None = None, parlay_id: int | None = None,
) -> int:
    """Grant the configured Bonus Chip rebate for a wager that just settled to
    WON (``won=True``) or LOST (``won=False``). ``wager_placed`` is the real
    chips the member risked (the bonus-funded slice is excluded). Returns the
    Bonus Chips granted, or 0 when the perk is off / rounds to nothing."""
    cfg = await _rebate_settings()
    if cfg["mode"] not in ("PCT", "FLAT"):
        return 0
    amount = _rebate_amount(cfg, wager_placed, won)
    if amount <= 0:
        return 0
    exp_days = cfg["expiry_days"]
    source = "REBATE_BET" if bet_id is not None else "REBATE_PARLAY"
    await grant_bonus_to_users(
        session, guild_id, [user_id], amount,
        int(exp_days) * 24 if exp_days else None,
        source=source, grant_id=bet_id if bet_id is not None else parlay_id,
    )
    return amount


async def revoke_settle_rebate(
    session, guild_id: int, *, bet_id: int | None = None,
    parlay_id: int | None = None,
) -> None:
    """Undo the rebate lot for a wager whose resolution is being reversed."""
    source = "REBATE_BET" if bet_id is not None else "REBATE_PARLAY"
    match_id = bet_id if bet_id is not None else parlay_id
    if match_id is None:
        return
    lot = (await session.execute(
        select(BonusBetLot).where(
            BonusBetLot.guild_id == guild_id,
            BonusBetLot.source == source,
            BonusBetLot.grant_id == match_id,
            BonusBetLot.status.in_(("ACTIVE", "EXHAUSTED")),
        ).order_by(BonusBetLot.id.desc())
    )).scalars().first()
    if lot is None:
        return
    lot.amount_remaining = 0
    lot.status = "REVOKED"


# ── Profit-boost shop (spend Bonus Chips on boost tokens) ─────────────────────


async def shop_listings(session, guild_id: int, *, active_only: bool = True):
    """Shop items paired with their live boost template. Returns
    ``[(BoostShopItem, ProfitBoostTemplate)]`` in ``sort_order`` then id order;
    listings whose template was deleted or disabled are skipped when
    ``active_only``."""
    q = select(BoostShopItem).where(BoostShopItem.guild_id == guild_id)
    if active_only:
        q = q.where(BoostShopItem.active == True)  # noqa: E712
    q = q.order_by(BoostShopItem.sort_order, BoostShopItem.id)
    out = []
    for item in (await session.execute(q)).scalars().all():
        tpl = await session.get(ProfitBoostTemplate, item.boost_template_id)
        if tpl is not None and tpl.guild_id != guild_id:
            tpl = None
        if active_only and (tpl is None or not tpl.active):
            continue
        out.append((item, tpl))
    return out


async def _shop_purchase_count(session, guild_id: int, item_id: int, user_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(BoostShopPurchase).where(
            BoostShopPurchase.guild_id == guild_id,
            BoostShopPurchase.item_id == item_id,
            BoostShopPurchase.discord_user_id == user_id,
        )
    )).scalar_one()


async def purchase_boost(
    session, guild_id: int, user_id: int, item_id: int,
) -> tuple[dict | None, str | None]:
    """Buy one shop listing: debit its Bonus Chip price, grant the boost token,
    log the purchase. Returns ``({"message", "price", "token_id"}, None)`` or
    ``(None, error)``. The caller owns the transaction."""
    item = await session.get(BoostShopItem, item_id)
    if item is None or item.guild_id != guild_id or not item.active:
        return None, "That shop item isn't available."
    tpl = await session.get(ProfitBoostTemplate, item.boost_template_id)
    if tpl is None or tpl.guild_id != guild_id or not tpl.active:
        return None, "The profit boost for this item no longer exists."
    if item.per_user_limit is not None:
        taken = await _shop_purchase_count(session, guild_id, item_id, user_id)
        if taken >= item.per_user_limit:
            return None, "You've bought this as many times as allowed."

    await expire_stale(session, guild_id, user_id)
    bal = await bonus_balance(session, guild_id, user_id)
    if bal < item.price_bonus_bets:
        return None, (
            f"Not enough Bonus Chips — this costs {item.price_bonus_bets:,}, "
            f"you have {bal:,}."
        )

    await spend_bonus(session, guild_id, user_id, item.price_bonus_bets)
    token = ProfitBoostToken(
        guild_id=guild_id, discord_user_id=user_id, template_id=tpl.id,
        boost_pct=tpl.boost_pct, scope_type=tpl.scope_type, scope_id=tpl.scope_id,
        max_wager=tpl.max_wager, status="ACTIVE",
        expires_at=_expiry_from_hours(item.expiry_days * 24 if item.expiry_days else None),
    )
    session.add(token)
    await session.flush()
    session.add(BoostShopPurchase(
        guild_id=guild_id, item_id=item_id, discord_user_id=user_id,
        template_id=tpl.id, price_paid=item.price_bonus_bets, token_id=token.id,
    ))
    label = f"+{tpl.boost_pct:g}% profit boost ({boost_scope_text(tpl.scope_type, tpl.scope_id)})"
    return {
        "message": f"Bought {label} for {item.price_bonus_bets:,} Bonus Chips.",
        "price": item.price_bonus_bets, "token_id": token.id,
    }, None

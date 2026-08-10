from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, JSON, PrimaryKeyConstraint, String, UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BettingPhase(Base):
    __tablename__ = "betting_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    markets: Mapped[list["Market"]] = relationship("Market", back_populates="phase")


class Alliance(Base):
    __tablename__ = "alliances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    members: Mapped[list["Tribute"]] = relationship("Tribute", back_populates="alliance")


class Tribute(Base):
    __tablename__ = "tributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    district: Mapped[int] = mapped_column(Integer, nullable=False)
    gender: Mapped[str] = mapped_column(String(2), nullable=False)
    # Tribute age (12–18). Older tributes start at longer odds — see age_factor.
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    training_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    face_claim: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kills: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Additive kill-boost sum: each kill contributes max(0, quality-1) * dr_factor,
    # where dr_factor shrinks past half the national kill record. Converted to a
    # multiplier at odds time: max(0.5, 1 + sum * KILL_BOOST_SCALE). Neutral = 0.0.
    kill_boost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="ALIVE", nullable=False)
    death_cause: Mapped[str | None] = mapped_column(String(200), nullable=True)
    killed_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tributes.id"), nullable=True)
    placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alliance_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("alliances.id"), nullable=True)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    member_joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sade_participant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sade_champion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    non_binary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # NULL = none; "DEBILITATED" | "MODERATELY_DEBILITATED" | "SEVERELY_DEBILITATED"
    debilitation_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Prior experience. times_played = number of Games entered before this one (0 = rookie).
    # highest_placement = best finish across all prior games (2–24; lower is better).
    times_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    highest_placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Kills scored specifically while the Bloodbath phase was active (subset of
    # `kills`), used to resolve BLOODBATH_KILLS_OU / ANY_BB_DOUBLE_KILL correctly
    # regardless of when the admin actually triggers the Bloodbath→Arena transition.
    bloodbath_kills: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    @property
    def display_gender(self) -> str:
        return "NB" if self.non_binary else self.gender

    alliance: Mapped["Alliance | None"] = relationship("Alliance", back_populates="members")
    markets_as_a: Mapped[list["Market"]] = relationship(
        "Market", foreign_keys="Market.tribute_a_id", back_populates="tribute_a",
        passive_deletes=True,
    )
    markets_as_b: Mapped[list["Market"]] = relationship(
        "Market", foreign_keys="Market.tribute_b_id", back_populates="tribute_b",
        passive_deletes=True,
    )


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    tribute_a_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tributes.id"), nullable=True)
    tribute_b_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tributes.id"), nullable=True)
    cause: Mapped[str | None] = mapped_column(String(100), nullable=True)
    placement_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ou_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    ou_side: Mapped[str | None] = mapped_column(String(5), nullable=True)   # "OVER" | "UNDER"
    phase_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("betting_phases.id"), nullable=True)
    odds: Mapped[int] = mapped_column(Integer, nullable=False)
    odds_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="CLOSED", nullable=False)
    result: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cashout_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cashout_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    phase: Mapped["BettingPhase | None"] = relationship("BettingPhase", back_populates="markets")
    tribute_a: Mapped["Tribute | None"] = relationship("Tribute", foreign_keys=[tribute_a_id], back_populates="markets_as_a")
    tribute_b: Mapped["Tribute | None"] = relationship("Tribute", foreign_keys=[tribute_b_id], back_populates="markets_as_b")
    bets: Mapped[list["Bet"]] = relationship("Bet", back_populates="market")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (PrimaryKeyConstraint("guild_id", "discord_id"),)

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    chips: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    total_wagered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Parlay(Base):
    __tablename__ = "parlays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Optional member-supplied title, shown on the tail board in place of the
    # default "{username}'s Parlay #{id}" when set.
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    total_wager: Mapped[int] = mapped_column(Integer, nullable=False)
    total_payout: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", nullable=False)
    cashout_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-parlay cashout override, same pattern as Market.cashout_allowed/rate —
    # NULL defers to the global cashout_allowed/cashout_rate GameSetting.
    cashout_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cashout_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When True the parlay is listed on the public tailing board so other members
    # can copy it. Members can opt out at submit time; tailed copies default off.
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set when this parlay was created via the tail board's "Tail & Bet" flow off
    # another member's public parlay — lets us DM the original poster on resolve.
    tailed_from_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tailed_from_parlay_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("parlays.id"), nullable=True
    )
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    legs: Mapped[list["Bet"]] = relationship("Bet", back_populates="parlay")


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parlay_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("parlays.id"), nullable=True)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    wager: Mapped[int] = mapped_column(Integer, nullable=False)
    odds_at_placement: Mapped[int] = mapped_column(Integer, nullable=False)
    payout_if_win: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", nullable=False)
    cashout_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    parlay: Mapped["Parlay | None"] = relationship("Parlay", back_populates="legs")
    market: Mapped["Market"] = relationship("Market", back_populates="bets")


class PendingParlayLeg(Base):
    __tablename__ = "pending_parlay_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    market: Mapped["Market"] = relationship("Market")


class ParlayTemplate(Base):
    """A pre-built parlay that members can tail (copy onto their own slip).

    Legs reference live markets, so odds are always read fresh from the market
    at view/tail time — the template never stores frozen odds. ``source`` is
    "ADMIN" for hand-built admin parlays or "AUTO" for the three per-phase
    auto-generated parlays. AUTO templates are regenerated each phase and removed
    once any of their legs resolve.
    """
    __tablename__ = "parlay_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="ADMIN", nullable=False)
    # For AUTO parlays: "SAFE" | "BALANCED" | "LONGSHOT". NULL for admin parlays.
    difficulty: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phase_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("betting_phases.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    legs: Mapped[list["ParlayTemplateLeg"]] = relationship(
        "ParlayTemplateLeg", back_populates="template",
        cascade="all, delete-orphan", order_by="ParlayTemplateLeg.sort_order",
    )


class ParlayTemplateLeg(Base):
    __tablename__ = "parlay_template_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("parlay_templates.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    template: Mapped["ParlayTemplate"] = relationship("ParlayTemplate", back_populates="legs")
    market: Mapped["Market"] = relationship("Market")


class MarketTemplate(Base):
    __tablename__ = "market_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False)
    default_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label_template: Mapped[str | None] = mapped_column(String(200), nullable=True)
    type_key: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Roster-style templates (e.g. "Fifth Career Alliance Member"): one Market row
    # per eligible tribute instead of a single instance, priced from
    # MULTI_OUTCOME_FACTORS instead of the standard win/kill/placement formulas.
    multi_outcome: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # "whitelist" | "blacklist" — only meaningful when multi_outcome is True.
    eligibility_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # {factor_key: weight}; a missing key defaults to weight 1.0 at compute time.
    weight_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    eligible_tributes: Mapped[list["MarketTemplateTribute"]] = relationship(
        "MarketTemplateTribute", back_populates="template", cascade="all, delete-orphan"
    )


class MarketTemplateTribute(Base):
    """Eligibility row for a multi_outcome MarketTemplate: this tribute is one of
    the mutually-exclusive outcomes the template's markets can resolve to."""
    __tablename__ = "market_template_tributes"
    __table_args__ = (UniqueConstraint("template_id", "tribute_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("market_templates.id", ondelete="CASCADE"), nullable=False
    )
    tribute_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tributes.id", ondelete="CASCADE"), nullable=False
    )

    template: Mapped["MarketTemplate"] = relationship("MarketTemplate", back_populates="eligible_tributes")
    tribute: Mapped["Tribute"] = relationship("Tribute")


class GameSetting(Base):
    __tablename__ = "game_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)


class ChipRequest(Base):
    """A /withdraw or /deposit request posted to the withdraw channel.

    Tracked so the "Mark Done" button on the posted message can survive a bot
    restart — the button's custom_id only carries this row's id, and the rest
    of the message content is regenerated from these fields rather than
    parsed back out of the message text.
    """
    __tablename__ = "chip_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # WITHDRAW | DEPOSIT
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Amount in the *other* currency, frozen at request time using the rate in
    # effect then (chips for DEPOSIT, panars for WITHDRAW) — like Bet.odds_at_placement,
    # this must not drift if an admin changes exchange rates before the request
    # is marked done, and lets render_request_content rebuild the message later
    # without recomputing anything.
    converted_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", nullable=False)  # PENDING | DONE
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class DistrictRecord(Base):
    """Aggregate historical performance for one district across all past games."""
    __tablename__ = "district_records"

    district: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Wins / victor breakdown
    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    victor_male_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    victor_female_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Runner-up breakdown
    runner_up_finishes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runner_up_male: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runner_up_female: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Placements
    avg_placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_placement_last5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top8_finishes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top5_finishes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Kills — total_kills is auto-computed as the sum of gender-specific columns
    total_kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    male_kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    female_kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bloodbath_kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bloodbath_deaths: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kill_record: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Arena-type win breakdown (natural wins = wins - manmade_arena_wins at display time)
    manmade_arena_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Training scores (current-game betting reference)
    avg_training_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_training_score_male: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_training_score_female: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # District reputation (1 = highest/best odds, 5 = lowest, 3 = neutral)
    reputation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # District funding level ("rich", "well_funded", "under_funded", "poor", or None)
    funding_level: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Modifier(Base):
    __tablename__ = "modifiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)

    assignments: Mapped[list["ModifierAssignment"]] = relationship(
        "ModifierAssignment", back_populates="modifier", cascade="all, delete-orphan"
    )


class ModifierAssignment(Base):
    __tablename__ = "modifier_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    modifier_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("modifiers.id", ondelete="CASCADE"), nullable=False
    )
    tribute_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tributes.id", ondelete="CASCADE"), nullable=True
    )
    district: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alliance_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("alliances.id", ondelete="CASCADE"), nullable=True
    )

    modifier: Mapped["Modifier"] = relationship("Modifier", back_populates="assignments")


class BettingRestriction(Base):
    """Per-user betting restrictions. type is 'ALL', 'DISTRICT', or 'TRIBUTE'."""
    __tablename__ = "betting_restrictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    restriction_type: Mapped[str] = mapped_column(String(10), nullable=False)
    district: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tribute_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tributes.id", ondelete="CASCADE"), nullable=True
    )


class TributeLock(Base):
    """A Discord user playing as an in-game Tribute, locked out of all
    sportsbook interaction (bot commands, the Activity, and the website) to
    prevent betting on themselves or their own outcome. Distinct from
    Tribute.discord_user_id, which just links a roster character to a member
    for flavor/odds purposes (e.g. seniority) and carries no access
    restriction on its own."""
    __tablename__ = "tribute_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tribute_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tributes.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class ExchangeRateOverride(Base):
    """Per-role or per-user override of the global chip<->Panar conversion rate.

    ``direction`` is "DEPOSIT" (panars -> chips, used by /deposit) or "WITHDRAW"
    (chips -> panars, used by /withdraw). ``rate`` is a multiplier applied to the
    amount the member enters, e.g. a WITHDRAW rate of 1.1 means 1 chip pays out
    1.1 Panars. Resolution order at use time: user override > highest-Discord-role
    override (mirrors how Discord's own permission overwrites resolve conflicts)
    > global GameSetting default ("deposit_rate" / "withdraw_rate", 1.0 if unset).
    """
    __tablename__ = "exchange_rate_overrides"
    __table_args__ = (
        UniqueConstraint("guild_id", "scope", "target_id", "direction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(5), nullable=False)  # ROLE | USER
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # role id or discord user id
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # DEPOSIT | WITHDRAW
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class PublicBetRestriction(Base):
    """Blocks a role or user from posting PUBLIC parlays (listed on the tail
    board). Distinct from BettingRestriction: a blocked member can still bet
    normally — a blocked public submission is silently downgraded to private
    rather than rejected outright."""
    __tablename__ = "public_bet_restrictions"
    __table_args__ = (
        UniqueConstraint("guild_id", "scope", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(5), nullable=False)  # ROLE | USER
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

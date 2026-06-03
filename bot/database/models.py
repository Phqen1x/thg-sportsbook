from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, func
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
    training_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    face_claim: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kills: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Accumulated kill-quality boost: each kill multiplies this by a factor that
    # reflects how strong the victim was (see kill_quality_multiplier). Folded
    # into the tribute's win/kill odds during recalculation.
    kill_boost: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
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

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    chips: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    total_wagered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    bets: Mapped[list["Bet"]] = relationship("Bet", back_populates="user")
    parlays: Mapped[list["Parlay"]] = relationship("Parlay", back_populates="user")
    pending_legs: Mapped[list["PendingParlayLeg"]] = relationship("PendingParlayLeg", back_populates="user")


class Parlay(Base):
    __tablename__ = "parlays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), nullable=False)
    total_wager: Mapped[int] = mapped_column(Integer, nullable=False)
    total_payout: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", nullable=False)
    cashout_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="parlays")
    legs: Mapped[list["Bet"]] = relationship("Bet", back_populates="parlay")


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), nullable=False)
    parlay_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("parlays.id"), nullable=True)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    wager: Mapped[int] = mapped_column(Integer, nullable=False)
    odds_at_placement: Mapped[int] = mapped_column(Integer, nullable=False)
    payout_if_win: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", nullable=False)
    cashout_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="bets")
    parlay: Mapped["Parlay | None"] = relationship("Parlay", back_populates="legs")
    market: Mapped["Market"] = relationship("Market", back_populates="bets")


class PendingParlayLeg(Base):
    __tablename__ = "pending_parlay_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, ForeignKey("markets.id"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="pending_legs")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class GameSetting(Base):
    __tablename__ = "game_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)


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
    kill_record: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Arena-type win breakdown (natural wins = wins - manmade_arena_wins at display time)
    manmade_arena_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Training scores (current-game betting reference)
    avg_training_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_training_score_male: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_training_score_female: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # District reputation (1 = highest/best odds, 5 = lowest, 3 = neutral)
    reputation: Mapped[int | None] = mapped_column(Integer, nullable=True)


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

    modifier: Mapped["Modifier"] = relationship("Modifier", back_populates="assignments")

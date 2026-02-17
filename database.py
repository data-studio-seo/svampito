from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, create_engine
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
import enum

from config import DATABASE_URL


# --- Engine setup ---

def _get_async_url(url: str) -> str:
    """Convert postgres:// or postgresql:// to postgresql+asyncpg://"""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

engine = create_async_engine(_get_async_url(DATABASE_URL), echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# --- Enums ---

class ReminderCategory(str, enum.Enum):
    GENERIC = "generic"
    MEDICINE = "medicine"
    BIRTHDAY = "birthday"
    CAR = "car"
    HOUSE = "house"
    HEALTH = "health"
    DOCUMENT = "document"
    HABIT = "habit"

class ReminderStatus(str, enum.Enum):
    ACTIVE = "active"
    DONE = "done"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

class RecurrenceType(str, enum.Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVERY_OTHER_DAY = "every_other_day"
    CUSTOM = "custom"


# --- Models ---

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)  # Telegram user ID
    chat_id = Column(BigInteger, nullable=False, unique=True)
    first_name = Column(String(255), nullable=True)
    timezone = Column(String(50), default="Europe/Rome")
    wake_hour = Column(Integer, default=8)
    sleep_hour = Column(Integer, default=23)
    morning_summary = Column(Boolean, default=True)
    onboarding_done = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    reminders = relationship("Reminder", back_populates="user", cascade="all, delete-orphan")


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    title = Column(String(500), nullable=False)
    category = Column(String(50), default=ReminderCategory.GENERIC)

    # Scheduling
    next_fire = Column(DateTime, nullable=False)  # UTC
    recurrence = Column(String(50), default=RecurrenceType.ONCE)
    recurrence_days = Column(String(50), nullable=True)  # e.g. "mon,wed,fri"
    fire_times = Column(Text, nullable=True)  # e.g. "08:00,14:00,21:00" for multi-time
    end_date = Column(DateTime, nullable=True)  # NULL = no end

    # Advance notice (days before)
    advance_days = Column(Integer, default=0)

    # Nudge tracking
    nudge_count = Column(Integer, default=0)
    last_nudge_at = Column(DateTime, nullable=True)

    # Status
    status = Column(String(50), default=ReminderStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # For multi-time reminders (medicine), track which time slot this is
    time_slot_index = Column(Integer, nullable=True)  # 0, 1, 2...
    time_slot_total = Column(Integer, nullable=True)  # total per day
    parent_id = Column(Integer, ForeignKey("reminders.id"), nullable=True)

    # Snooze tracking
    snooze_count = Column(Integer, default=0)

    user = relationship("User", back_populates="reminders")


class ReminderLog(Base):
    """Tracks actions on reminders for weekly summary."""
    __tablename__ = "reminder_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    reminder_id = Column(Integer, ForeignKey("reminders.id"), nullable=False)
    action = Column(String(50), nullable=False)  # done, skipped, snoozed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)


# --- Init ---

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

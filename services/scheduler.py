"""
APScheduler-based reminder and nudge delivery service.
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, and_
import pytz

from database import async_session, Reminder, User, ReminderLog, ReminderStatus, RecurrenceType, ReminderCategory
from services.messages import (
    nudge_1, nudge_2, nudge_3, morning_summary, weekly_summary,
    get_emoji, snooze_warning
)
from config import NUDGE_2_DELAY, NUDGE_3_DELAY, MEDICINE_NUDGE_DELAY, MAX_NUDGES

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_bot = None


def init_scheduler(bot):
    """Initialize scheduler with bot reference."""
    global _bot
    _bot = bot

    # Check reminders every 30 seconds
    scheduler.add_job(check_reminders, IntervalTrigger(seconds=30), id="check_reminders",
                      replace_existing=True, max_instances=1)

    # Check nudges every minute
    scheduler.add_job(check_nudges, IntervalTrigger(seconds=60), id="check_nudges",
                      replace_existing=True, max_instances=1)

    # Morning summary at every minute (we check per-user wake time)
    scheduler.add_job(send_morning_summaries, IntervalTrigger(minutes=1), id="morning_summaries",
                      replace_existing=True, max_instances=1)

    # Weekly summary on Sundays at 20:00
    scheduler.add_job(send_weekly_summaries, IntervalTrigger(minutes=5), id="weekly_summaries",
                      replace_existing=True, max_instances=1)

    scheduler.start()
    logger.info("Scheduler started")


async def check_reminders():
    """Check and fire due reminders."""
    if not _bot:
        return

    now_utc = datetime.utcnow()

    async with async_session() as session:
        stmt = select(Reminder).join(User).where(
            and_(
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.next_fire <= now_utc,
                Reminder.nudge_count == 0,
            )
        )
        result = await session.execute(stmt)
        reminders = result.scalars().all()

        for reminder in reminders:
            user = await session.get(User, reminder.user_id)
            if not user:
                continue

            # Check DND hours
            tz = pytz.timezone(user.timezone or "Europe/Rome")
            local_now = datetime.now(tz)
            if local_now.hour >= user.sleep_hour or local_now.hour < user.wake_hour:
                continue  # Skip, will be included in morning summary

            # Send nudge 1
            text = nudge_1(reminder)
            keyboard = _get_reminder_keyboard(reminder)

            try:
                await _bot.send_message(
                    chat_id=user.chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                reminder.nudge_count = 1
                reminder.last_nudge_at = now_utc
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to send reminder {reminder.id}: {e}")


async def check_nudges():
    """Send follow-up nudges for unconfirmed reminders."""
    if not _bot:
        return

    now_utc = datetime.utcnow()

    async with async_session() as session:
        stmt = select(Reminder).join(User).where(
            and_(
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.nudge_count >= 1,
                Reminder.nudge_count < MAX_NUDGES,
                Reminder.last_nudge_at.isnot(None),
            )
        )
        result = await session.execute(stmt)
        reminders = result.scalars().all()

        for reminder in reminders:
            user = await session.get(User, reminder.user_id)
            if not user:
                continue

            # Check DND
            tz = pytz.timezone(user.timezone or "Europe/Rome")
            local_now = datetime.now(tz)
            if local_now.hour >= user.sleep_hour or local_now.hour < user.wake_hour:
                continue

            # Calculate delay based on type
            if reminder.category == ReminderCategory.MEDICINE:
                delay = MEDICINE_NUDGE_DELAY
            elif reminder.nudge_count == 1:
                delay = NUDGE_2_DELAY
            else:
                delay = NUDGE_3_DELAY - NUDGE_2_DELAY

            minutes_since = (now_utc - reminder.last_nudge_at).total_seconds() / 60

            if minutes_since >= delay:
                if reminder.nudge_count == 1:
                    text = nudge_2(reminder)
                else:
                    text = nudge_3(reminder)

                keyboard = _get_nudge_keyboard(reminder, reminder.nudge_count + 1)

                try:
                    await _bot.send_message(
                        chat_id=user.chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                    reminder.nudge_count += 1
                    reminder.last_nudge_at = now_utc
                    await session.commit()
                except Exception as e:
                    logger.error(f"Failed to send nudge for {reminder.id}: {e}")


async def send_morning_summaries():
    """Send morning summary to users at their wake time."""
    if not _bot:
        return

    async with async_session() as session:
        stmt = select(User).where(User.morning_summary == True)
        result = await session.execute(stmt)
        users = result.scalars().all()

        for user in users:
            tz = pytz.timezone(user.timezone or "Europe/Rome")
            local_now = datetime.now(tz)

            # Only send at the exact wake hour, minute 0
            if local_now.hour != user.wake_hour or local_now.minute != 0:
                continue

            # Get today's reminders
            today_start = local_now.replace(hour=0, minute=0, second=0).astimezone(pytz.UTC).replace(tzinfo=None)
            today_end = today_start + timedelta(days=1)

            stmt2 = select(Reminder).where(
                and_(
                    Reminder.user_id == user.id,
                    Reminder.status == ReminderStatus.ACTIVE,
                    Reminder.next_fire >= today_start,
                    Reminder.next_fire < today_end,
                )
            ).order_by(Reminder.next_fire)
            result2 = await session.execute(stmt2)
            reminders = result2.scalars().all()

            if not reminders and not user.morning_summary:
                continue

            items = []
            seen_parents = set()
            for r in reminders:
                # Group multi-time reminders
                if r.parent_id and r.parent_id in seen_parents:
                    continue

                item = {"title": r.title, "category": r.category}

                if r.fire_times:
                    item["times"] = r.fire_times.split(",")
                else:
                    fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
                    item["times"] = [fire_local.strftime("%H:%M")]

                # Birthday special note
                if r.category == ReminderCategory.BIRTHDAY:
                    item["note"] = "hai pensato al regalo? 🎁"

                items.append(item)
                if r.id:
                    seen_parents.add(r.id)

            text = morning_summary(items)
            try:
                await _bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed morning summary for user {user.id}: {e}")


async def send_weekly_summaries():
    """Send weekly summary on Sunday evening."""
    if not _bot:
        return

    async with async_session() as session:
        stmt = select(User)
        result = await session.execute(stmt)
        users = result.scalars().all()

        for user in users:
            tz = pytz.timezone(user.timezone or "Europe/Rome")
            local_now = datetime.now(tz)

            # Sunday at 20:00
            if local_now.weekday() != 6 or local_now.hour != 20 or local_now.minute > 4:
                continue

            week_start = (local_now - timedelta(days=7)).replace(
                hour=0, minute=0, second=0
            ).astimezone(pytz.UTC).replace(tzinfo=None)

            stmt2 = select(ReminderLog).where(
                and_(
                    ReminderLog.user_id == user.id,
                    ReminderLog.created_at >= week_start,
                )
            )
            result2 = await session.execute(stmt2)
            logs = result2.scalars().all()

            done = sum(1 for l in logs if l.action == "done")
            snoozed = sum(1 for l in logs if l.action == "snoozed")
            cancelled = sum(1 for l in logs if l.action == "cancelled")

            # Count medicine doses
            med_logs = [l for l in logs if l.action in ("done", "skipped")]
            # Simplified: count upcoming
            next_week_end = datetime.utcnow() + timedelta(days=7)
            stmt3 = select(Reminder).where(
                and_(
                    Reminder.user_id == user.id,
                    Reminder.status == ReminderStatus.ACTIVE,
                    Reminder.next_fire <= next_week_end,
                )
            )
            result3 = await session.execute(stmt3)
            upcoming = len(result3.scalars().all())

            text = weekly_summary(done, snoozed, cancelled, upcoming=upcoming)
            try:
                await _bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed weekly summary for user {user.id}: {e}")


async def reschedule_reminder(reminder: Reminder, session):
    """Calculate and set the next fire time for recurring reminders."""
    if reminder.recurrence == RecurrenceType.ONCE:
        reminder.status = ReminderStatus.DONE
        return

    tz_name = "Europe/Rome"
    async with async_session() as s2:
        user = await s2.get(User, reminder.user_id)
        if user:
            tz_name = user.timezone or "Europe/Rome"

    tz = pytz.timezone(tz_name)
    current = pytz.UTC.localize(reminder.next_fire).astimezone(tz)

    if reminder.recurrence == RecurrenceType.DAILY:
        next_dt = current + timedelta(days=1)
    elif reminder.recurrence == RecurrenceType.WEEKLY:
        next_dt = current + timedelta(weeks=1)
    elif reminder.recurrence == RecurrenceType.MONTHLY:
        month = current.month + 1
        year = current.year
        if month > 12:
            month = 1
            year += 1
        try:
            next_dt = current.replace(year=year, month=month)
        except ValueError:
            next_dt = current.replace(year=year, month=month + 1, day=1) - timedelta(days=1)
    elif reminder.recurrence == RecurrenceType.EVERY_OTHER_DAY:
        next_dt = current + timedelta(days=2)
    else:
        next_dt = current + timedelta(days=1)

    # Check end date
    if reminder.end_date and next_dt.astimezone(pytz.UTC).replace(tzinfo=None) > reminder.end_date:
        reminder.status = ReminderStatus.DONE
        return

    reminder.next_fire = next_dt.astimezone(pytz.UTC).replace(tzinfo=None)
    reminder.nudge_count = 0
    reminder.last_nudge_at = None


def _get_reminder_keyboard(reminder: Reminder):
    """Get inline keyboard for a reminder notification."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rid = reminder.id
    if reminder.category == ReminderCategory.MEDICINE:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Presa", callback_data=f"done:{rid}"),
                InlineKeyboardButton("⏰ Tra 30min", callback_data=f"snooze30:{rid}"),
            ]
        ])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Fatto!", callback_data=f"done:{rid}"),
            InlineKeyboardButton("⏰ Tra 1h", callback_data=f"snooze60:{rid}"),
        ],
        [
            InlineKeyboardButton("⏰ Domani", callback_data=f"tomorrow:{rid}"),
            InlineKeyboardButton("❌ Cancella", callback_data=f"cancel:{rid}"),
        ],
    ])


def _get_nudge_keyboard(reminder: Reminder, nudge_num: int):
    """Get keyboard for follow-up nudges."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rid = reminder.id

    if nudge_num == 2:
        if reminder.category == ReminderCategory.MEDICINE:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Presa", callback_data=f"done:{rid}"),
                    InlineKeyboardButton("⏰ Tra 30min", callback_data=f"snooze30:{rid}"),
                    InlineKeyboardButton("⏭ Salta oggi", callback_data=f"skip:{rid}"),
                ]
            ])
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Fatto!", callback_data=f"done:{rid}"),
                InlineKeyboardButton("⏰ Tra 1h", callback_data=f"snooze60:{rid}"),
                InlineKeyboardButton("⏰ Domani", callback_data=f"tomorrow:{rid}"),
            ]
        ])

    # Nudge 3 (last)
    if reminder.category == ReminderCategory.MEDICINE:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Presa ora", callback_data=f"done:{rid}"),
                InlineKeyboardButton("⏭ Saltata", callback_data=f"skip:{rid}"),
            ]
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Già fatto", callback_data=f"done:{rid}"),
            InlineKeyboardButton("📅 Domani", callback_data=f"tomorrow:{rid}"),
            InlineKeyboardButton("🗑 Lascia perdere", callback_data=f"cancel:{rid}"),
        ]
    ])

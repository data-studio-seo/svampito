"""
Handles inline keyboard callbacks for reminder actions:
done, snooze, skip, cancel, tomorrow.
"""
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select
import pytz

from database import async_session, Reminder, ReminderLog, ReminderStatus, User
from services.messages import done_response, skipped_response, cancelled_response, snooze_warning
from services.scheduler import reschedule_reminder

logger = logging.getLogger(__name__)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries from reminder buttons."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if ":" not in data:
        return

    action, reminder_id_str = data.split(":", 1)
    try:
        reminder_id = int(reminder_id_str)
    except ValueError:
        return

    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder:
            await query.edit_message_text("⚠️ Reminder non trovato.")
            return

        if action == "done":
            await _handle_done(query, reminder, session)

        elif action in ("snooze30", "snooze60"):
            minutes = 30 if action == "snooze30" else 60
            await _handle_snooze(query, reminder, session, minutes)

        elif action == "tomorrow":
            await _handle_tomorrow(query, reminder, session)

        elif action == "skip":
            await _handle_skip(query, reminder, session)

        elif action == "cancel":
            await _handle_cancel(query, reminder, session)


async def _handle_done(query, reminder: Reminder, session):
    """Mark reminder as done."""
    now = datetime.utcnow()

    # Log
    log = ReminderLog(
        user_id=reminder.user_id,
        reminder_id=reminder.id,
        action="done",
    )
    session.add(log)

    # Reschedule if recurring, otherwise mark done
    await reschedule_reminder(reminder, session)
    reminder.completed_at = now
    if reminder.status != ReminderStatus.ACTIVE:
        reminder.nudge_count = 0

    await session.commit()
    await query.edit_message_text(done_response())


async def _handle_snooze(query, reminder: Reminder, session, minutes: int):
    """Snooze reminder by X minutes."""
    reminder.snooze_count += 1

    # Check if snooze warning threshold
    if reminder.snooze_count >= 3 and reminder.snooze_count % 3 == 0:
        # Send snooze warning
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Settimana prossima", callback_data=f"snooze_week:{reminder.id}"),
                InlineKeyboardButton("🗑 Cancella", callback_data=f"cancel:{reminder.id}"),
            ],
            [InlineKeyboardButton("⏰ Ancora 1 giorno", callback_data=f"tomorrow:{reminder.id}")],
        ])
        log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="snoozed")
        session.add(log)
        await session.commit()
        await query.edit_message_text(snooze_warning(reminder), parse_mode="Markdown", reply_markup=keyboard)
        return

    reminder.next_fire = datetime.utcnow() + timedelta(minutes=minutes)
    reminder.nudge_count = 0
    reminder.last_nudge_at = None

    log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="snoozed")
    session.add(log)
    await session.commit()

    label = f"{minutes} minuti" if minutes < 60 else f"{minutes // 60} ora"
    await query.edit_message_text(f"⏰ Ok, ti ricordo tra {label}.")


async def _handle_tomorrow(query, reminder: Reminder, session):
    """Reschedule to tomorrow same time."""
    user = await session.get(User, reminder.user_id)
    tz = pytz.timezone(user.timezone if user else "Europe/Rome")

    current_fire = pytz.UTC.localize(reminder.next_fire).astimezone(tz)
    tomorrow = current_fire + timedelta(days=1)
    reminder.next_fire = tomorrow.astimezone(pytz.UTC).replace(tzinfo=None)
    reminder.nudge_count = 0
    reminder.last_nudge_at = None
    reminder.snooze_count += 1

    log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="snoozed")
    session.add(log)
    await session.commit()

    await query.edit_message_text(f"📅 Ok, spostato a domani ({tomorrow.strftime('%H:%M')}).")


async def _handle_skip(query, reminder: Reminder, session):
    """Skip this occurrence (for recurring reminders)."""
    log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="skipped")
    session.add(log)

    await reschedule_reminder(reminder, session)
    await session.commit()

    await query.edit_message_text(skipped_response())


async def _handle_cancel(query, reminder: Reminder, session):
    """Cancel reminder permanently."""
    reminder.status = ReminderStatus.CANCELLED

    log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="cancelled")
    session.add(log)
    await session.commit()

    await query.edit_message_text(cancelled_response())


async def handle_snooze_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle snooze to next week."""
    query = update.callback_query
    await query.answer()

    reminder_id = int(query.data.split(":")[1])

    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder:
            await query.edit_message_text("⚠️ Reminder non trovato.")
            return

        reminder.next_fire = datetime.utcnow() + timedelta(weeks=1)
        reminder.nudge_count = 0
        reminder.last_nudge_at = None

        await session.commit()
        await query.edit_message_text("📅 Spostato a settimana prossima.")

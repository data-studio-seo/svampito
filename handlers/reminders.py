"""
Handles free-text messages to create reminders.
Also handles quick confirm responses ("ok", "fatto", "sì").
Also routes persistent keyboard button presses to the right commands.
"""
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, and_
import pytz

from database import (
    async_session, Reminder, ReminderLog, User,
    ReminderStatus, ReminderCategory, RecurrenceType
)
from services.parser import parse_reminder, parse_reminder_async, format_confirmation
from services.messages import done_response, get_emoji
from services.scheduler import reschedule_reminder

logger = logging.getLogger(__name__)

# Quick confirm keywords
QUICK_CONFIRM = {"ok", "fatto", "sì", "si", "presa", "preso", "done", "✅"}

# Persistent keyboard button mapping
KEYBOARD_COMMANDS = {
    "📋 Oggi": "oggi",
    "📅 Domani": "domani",
    "📊 Settimana": "settimana",
    "💊 Farmaci": "farmaci",
    "⚙️ Impostazioni": "impostazioni",
    "❓ Help": "help",
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any text message - keyboard buttons, quick confirm, or new reminder."""
    text = update.message.text.strip()

    # Check if it's a persistent keyboard button
    if text in KEYBOARD_COMMANDS:
        from handlers.commands import (
            cmd_oggi, cmd_domani, cmd_settimana,
            cmd_farmaci, cmd_impostazioni, cmd_help
        )
        cmd_map = {
            "oggi": cmd_oggi,
            "domani": cmd_domani,
            "settimana": cmd_settimana,
            "farmaci": cmd_farmaci,
            "impostazioni": cmd_impostazioni,
            "help": cmd_help,
        }
        cmd = KEYBOARD_COMMANDS[text]
        await cmd_map[cmd](update, context)
        return

    # Check if it's a quick confirm
    if text.lower() in QUICK_CONFIRM:
        await _quick_confirm(update, context)
        return

    # Parse as new reminder
    user_id = update.effective_user.id

    # Get user timezone
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            # Auto-create user
            user = User(
                id=user_id,
                chat_id=update.effective_chat.id,
                first_name=update.effective_user.first_name,
            )
            session.add(user)
            await session.commit()

    tz_name = user.timezone if user else "Europe/Rome"

    # Try LLM parsing first, fallback to regex
    parsed = await parse_reminder_async(text, tz_name)

    if not parsed.title or len(parsed.title) < 2:
        await update.message.reply_text(
            "🤔 Non ho capito bene. Prova a scrivere cosa vuoi ricordare, "
            "ad esempio:\n_\"domani alle 10 chiama il dentista\"_",
            parse_mode="Markdown"
        )
        return

    # Store parsed data for confirmation
    context.user_data["pending_reminder"] = {
        "title": parsed.title,
        "category": parsed.category.value,
        "fire_datetime": parsed.fire_datetime.isoformat() if parsed.fire_datetime else None,
        "recurrence": parsed.recurrence.value,
        "recurrence_days": parsed.recurrence_days,
        "fire_times": parsed.fire_times,
        "end_date": parsed.end_date.isoformat() if parsed.end_date else None,
        "advance_days": parsed.advance_days,
    }

    # Build confirmation message
    confirm_text = format_confirmation(parsed)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Conferma", callback_data="rem:confirm"),
            InlineKeyboardButton("✏️ Modifica ora", callback_data="rem:edit_time"),
        ],
        [InlineKeyboardButton("❌ Annulla", callback_data="rem:cancel")],
    ])

    await update.message.reply_text(confirm_text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation/edit/cancel of a new reminder."""
    query = update.callback_query
    await query.answer()

    action = query.data

    if action == "rem:cancel":
        context.user_data.pop("pending_reminder", None)
        await query.edit_message_text("❌ Annullato.")
        return

    if action == "rem:edit_time":
        await query.edit_message_text(
            "⏰ Scrivimi il nuovo orario (es. \"15:30\" o \"domani alle 10\")"
        )
        context.user_data["editing_time"] = True
        return

    if action == "rem:confirm":
        pending = context.user_data.pop("pending_reminder", None)
        if not pending:
            await query.edit_message_text("⚠️ Nessun reminder da confermare.")
            return

        await _save_reminder(query, pending)


async def handle_time_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle time edit after user chose 'Modifica ora'."""
    if not context.user_data.get("editing_time"):
        return False  # Not editing, let normal handler process

    context.user_data["editing_time"] = False
    text = update.message.text.strip()

    pending = context.user_data.get("pending_reminder")
    if not pending:
        await update.message.reply_text("⚠️ Nessun reminder da modificare. Creane uno nuovo.")
        return True

    # Re-parse just the time part
    user_id = update.effective_user.id
    async with async_session() as session:
        user = await session.get(User, user_id)
    tz_name = user.timezone if user else "Europe/Rome"

    from services.parser import parse_reminder as pr
    time_parsed = pr(text, tz_name)

    if time_parsed.fire_datetime:
        pending["fire_datetime"] = time_parsed.fire_datetime.isoformat()
        if time_parsed.fire_times:
            pending["fire_times"] = time_parsed.fire_times

    context.user_data["pending_reminder"] = pending

    # Rebuild confirmation
    from services.parser import ParsedReminder
    p = ParsedReminder()
    p.title = pending["title"]
    p.category = ReminderCategory(pending["category"])
    p.fire_datetime = datetime.fromisoformat(pending["fire_datetime"]) if pending["fire_datetime"] else None
    p.recurrence = RecurrenceType(pending["recurrence"])
    p.fire_times = pending.get("fire_times", [])

    confirm_text = format_confirmation(p)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Conferma", callback_data="rem:confirm"),
            InlineKeyboardButton("✏️ Modifica ora", callback_data="rem:edit_time"),
        ],
        [InlineKeyboardButton("❌ Annulla", callback_data="rem:cancel")],
    ])

    await update.message.reply_text(confirm_text, parse_mode="Markdown", reply_markup=keyboard)
    return True


async def _save_reminder(query, pending: dict):
    """Save confirmed reminder to database."""
    user_id = query.from_user.id

    async with async_session() as session:
        user = await session.get(User, user_id)
        tz = pytz.timezone(user.timezone if user else "Europe/Rome")

        fire_dt = datetime.fromisoformat(pending["fire_datetime"]) if pending.get("fire_datetime") else None
        if fire_dt:
            if fire_dt.tzinfo:
                fire_utc = fire_dt.astimezone(pytz.UTC).replace(tzinfo=None)
            else:
                fire_utc = tz.localize(fire_dt).astimezone(pytz.UTC).replace(tzinfo=None)
        else:
            fire_utc = datetime.utcnow() + timedelta(hours=1)

        fire_times = pending.get("fire_times", [])
        end_date = datetime.fromisoformat(pending["end_date"]) if pending.get("end_date") else None
        if end_date and end_date.tzinfo:
            end_date = end_date.astimezone(pytz.UTC).replace(tzinfo=None)

        if fire_times and len(fire_times) > 1:
            # Create multiple reminders for multi-time
            for idx, t in enumerate(fire_times):
                h, m = map(int, t.split(":"))
                slot_fire = fire_utc.replace(hour=h, minute=m)
                # Adjust for UTC offset
                local_fire = tz.localize(datetime.now(tz).replace(hour=h, minute=m, second=0))
                slot_fire_utc = local_fire.astimezone(pytz.UTC).replace(tzinfo=None)
                if slot_fire_utc < datetime.utcnow():
                    slot_fire_utc += timedelta(days=1)

                reminder = Reminder(
                    user_id=user_id,
                    title=pending["title"],
                    category=pending.get("category", ReminderCategory.GENERIC),
                    next_fire=slot_fire_utc,
                    recurrence=pending.get("recurrence", RecurrenceType.DAILY),
                    recurrence_days=pending.get("recurrence_days"),
                    fire_times=",".join(fire_times),
                    end_date=end_date,
                    advance_days=pending.get("advance_days", 0),
                    time_slot_index=idx,
                    time_slot_total=len(fire_times),
                )
                session.add(reminder)
        else:
            reminder = Reminder(
                user_id=user_id,
                title=pending["title"],
                category=pending.get("category", ReminderCategory.GENERIC),
                next_fire=fire_utc,
                recurrence=pending.get("recurrence", RecurrenceType.ONCE),
                recurrence_days=pending.get("recurrence_days"),
                fire_times=",".join(fire_times) if fire_times else None,
                end_date=end_date,
                advance_days=pending.get("advance_days", 0),
            )
            session.add(reminder)

        await session.commit()

    emoji = get_emoji(pending.get("category", "generic"))
    await query.edit_message_text(f"{emoji} *{pending['title']}* — salvato! ✅", parse_mode="Markdown")


async def _quick_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick text confirmations like 'ok', 'fatto'."""
    user_id = update.effective_user.id

    async with async_session() as session:
        # Find most recent active nudged reminder
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == user_id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.nudge_count > 0,
            )
        ).order_by(Reminder.last_nudge_at.desc()).limit(1)
        result = await session.execute(stmt)
        reminder = result.scalar_one_or_none()

        if not reminder:
            await update.message.reply_text(
                "🤔 Non ho reminder attivi da confermare. "
                "Scrivimi qualcosa da ricordare!"
            )
            return

        log = ReminderLog(user_id=user_id, reminder_id=reminder.id, action="done")
        session.add(log)
        await reschedule_reminder(reminder, session)
        await session.commit()

    await update.message.reply_text(
        f"✅ *{reminder.title}* — fatto!",
        parse_mode="Markdown"
    )

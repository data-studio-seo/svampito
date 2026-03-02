"""
Handles free-text messages, voice messages, and reminder callbacks.
Routes everything through the assistant brain.
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
from services.parser import parse_reminder, format_confirmation, ParsedReminder
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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages: transcribe → process through assistant."""
    voice = update.message.voice
    if not voice:
        return

    if voice.duration and voice.duration > 60:
        await update.message.reply_text(
            "🎙️ Audio troppo lungo! Registra un messaggio di massimo 60 secondi."
        )
        return

    processing_msg = await update.message.reply_text("🎙️ Sto ascoltando...")

    try:
        # Download voice file
        voice_file = await voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()
        logger.info(f"Voice message: {voice.duration}s, {len(audio_bytes)} bytes")

        # Transcribe with Whisper
        from services.llm import transcribe_audio
        text = await transcribe_audio(bytes(audio_bytes), filename="voice.ogg")

        if not text:
            await processing_msg.edit_text(
                "🎙️ Non sono riuscito a capire l'audio. "
                "Prova a parlare più chiaramente o scrivimi il reminder."
            )
            return

        # Show transcription
        await processing_msg.edit_text(f"🎙️ Ho capito: _{text}_", parse_mode="Markdown")

        # Process through assistant
        await _process_with_assistant(update, context, text)

    except Exception as e:
        logger.error(f"Voice handling error: {type(e).__name__}: {e}")
        await processing_msg.edit_text(
            "❌ Errore nell'elaborazione dell'audio. Prova a scrivere il reminder."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages: keyboard buttons, quick confirm, or assistant."""
    text = update.message.text.strip()

    # Check persistent keyboard buttons
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

    # Check quick confirm (skip LLM for these)
    if text.lower() in QUICK_CONFIRM:
        await _quick_confirm(update, context)
        return

    # Process through assistant
    await _process_with_assistant(update, context, text)


async def _process_with_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Route message through the assistant brain."""
    from services.assistant import process_message, AssistantResponse

    user = update.effective_user
    response = await process_message(
        user_id=user.id,
        chat_id=update.effective_chat.id,
        text=text,
        first_name=user.first_name or "",
    )

    # Handle different response types
    if response.show_confirm and response.pending_reminder:
        # Store pending reminder for confirmation
        context.user_data["pending_reminder"] = response.pending_reminder

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Conferma", callback_data="rem:confirm"),
                InlineKeyboardButton("✏️ Modifica ora", callback_data="rem:edit_time"),
            ],
            [InlineKeyboardButton("❌ Annulla", callback_data="rem:cancel")],
        ])
        await update.message.reply_text(
            response.text, parse_mode=response.parse_mode, reply_markup=keyboard
        )

    elif response.confirm_delete_id:
        # Store delete target for confirmation
        context.user_data["pending_delete"] = response.confirm_delete_id

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Conferma", callback_data=f"cancel:{response.confirm_delete_id}"),
                InlineKeyboardButton("❌ No", callback_data="rem:cancel_delete"),
            ],
        ])
        await update.message.reply_text(
            response.text, parse_mode=response.parse_mode, reply_markup=keyboard
        )

    else:
        # Simple text response (query results, chat, done, modify)
        await update.message.reply_text(response.text, parse_mode=response.parse_mode)


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation/edit/cancel of a new reminder."""
    query = update.callback_query
    await query.answer()

    action = query.data

    if action == "rem:cancel" or action == "rem:cancel_delete":
        context.user_data.pop("pending_reminder", None)
        context.user_data.pop("pending_delete", None)
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
        return False

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

    time_parsed = parse_reminder(text, tz_name)

    if time_parsed.fire_datetime:
        pending["fire_datetime"] = time_parsed.fire_datetime.isoformat()
        if time_parsed.fire_times:
            pending["fire_times"] = time_parsed.fire_times

    context.user_data["pending_reminder"] = pending

    # Rebuild confirmation
    p = ParsedReminder()
    p.title = pending["title"]
    try:
        p.category = ReminderCategory(pending["category"])
    except ValueError:
        p.category = ReminderCategory.GENERIC
    p.fire_datetime = datetime.fromisoformat(pending["fire_datetime"]) if pending["fire_datetime"] else None
    try:
        p.recurrence = RecurrenceType(pending["recurrence"])
    except ValueError:
        p.recurrence = RecurrenceType.ONCE
    p.recurrence_days = pending.get("recurrence_days")
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
            for idx, t in enumerate(fire_times):
                h, m = map(int, t.split(":"))
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

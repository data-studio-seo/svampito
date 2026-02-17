"""
Handlers for utility commands: /oggi, /domani, /settimana, /lista, /farmaci,
/scadenze, /fatto, /cancella, /silenzio, /export, /help, /impostazioni, /timezone.
"""
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, and_
import pytz

from database import (
    async_session, Reminder, User, ReminderLog,
    ReminderStatus, ReminderCategory, RecurrenceType
)
from services.messages import get_emoji, HELP_TEXT
from services.scheduler import reschedule_reminder

logger = logging.getLogger(__name__)


async def _get_user_tz(user_id: int) -> pytz.timezone:
    async with async_session() as session:
        user = await session.get(User, user_id)
        tz_name = user.timezone if user else "Europe/Rome"
    return pytz.timezone(tz_name)


async def _get_reminders_in_range(user_id: int, start_utc: datetime, end_utc: datetime):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == user_id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.next_fire >= start_utc,
                Reminder.next_fire < end_utc,
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        return result.scalars().all()


def _format_reminder_line(r: Reminder, tz) -> str:
    emoji = get_emoji(r.category)
    fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
    time_str = fire_local.strftime("%H:%M")

    if r.fire_times and r.time_slot_index == 0:
        times = r.fire_times.split(",")
        return f"{emoji} {r.title} _({' · '.join(times)})_"
    elif r.fire_times and r.time_slot_index and r.time_slot_index > 0:
        return None  # Skip duplicate time slots
    return f"{emoji} {r.title} _({time_str})_"


# --- /oggi ---
async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = await _get_user_tz(update.effective_user.id)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0).astimezone(pytz.UTC).replace(tzinfo=None)
    end = start + timedelta(days=1)

    reminders = await _get_reminders_in_range(update.effective_user.id, start, end)

    if not reminders:
        await update.message.reply_text("📋 Oggi non hai nulla in programma!")
        return

    lines = ["📋 *Oggi:*\n"]
    for r in reminders:
        line = _format_reminder_line(r, tz)
        if line:
            lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /domani ---
async def cmd_domani(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = await _get_user_tz(update.effective_user.id)
    now = datetime.now(tz)
    start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0).astimezone(pytz.UTC).replace(tzinfo=None)
    end = start + timedelta(days=1)

    reminders = await _get_reminders_in_range(update.effective_user.id, start, end)

    if not reminders:
        await update.message.reply_text("📋 Domani non hai nulla in programma!")
        return

    lines = ["📋 *Domani:*\n"]
    for r in reminders:
        line = _format_reminder_line(r, tz)
        if line:
            lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /settimana ---
async def cmd_settimana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = await _get_user_tz(update.effective_user.id)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0).astimezone(pytz.UTC).replace(tzinfo=None)
    end = start + timedelta(days=7)

    reminders = await _get_reminders_in_range(update.effective_user.id, start, end)

    if not reminders:
        await update.message.reply_text("📋 Nessun reminder nei prossimi 7 giorni!")
        return

    lines = ["📋 *Prossimi 7 giorni:*\n"]
    current_day = None
    day_names = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

    for r in reminders:
        fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
        day_key = fire_local.strftime("%Y-%m-%d")

        if day_key != current_day:
            current_day = day_key
            day_name = day_names[fire_local.weekday()]
            lines.append(f"\n*{day_name} {fire_local.strftime('%d/%m')}*")

        line = _format_reminder_line(r, tz)
        if line:
            lines.append(f"  {line}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /lista ---
async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    if not reminders:
        await update.message.reply_text("📋 Non hai reminder attivi.")
        return

    tz = await _get_user_tz(update.effective_user.id)
    lines = ["📋 *Tutti i reminder attivi:*\n"]
    for r in reminders:
        line = _format_reminder_line(r, tz)
        if line:
            lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /farmaci ---
async def cmd_farmaci(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.category == ReminderCategory.MEDICINE,
                Reminder.time_slot_index == 0,  # Only show once per medicine
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    if not reminders:
        await update.message.reply_text("💊 Nessun farmaco configurato.\n\nScrivimi il nome di un farmaco per aggiungerlo!")
        return

    tz = await _get_user_tz(update.effective_user.id)
    lines = ["💊 *Farmaci attivi:*\n"]
    for r in reminders:
        times_str = r.fire_times or ""
        end_str = ""
        if r.end_date:
            end_local = pytz.UTC.localize(r.end_date).astimezone(tz)
            end_str = f" — fino al {end_local.strftime('%d/%m')}"
        lines.append(f"💊 *{r.title}*")
        lines.append(f"   ⏰ {times_str.replace(',', ' · ')}{end_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /scadenze ---
async def cmd_scadenze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deadline_cats = [
        ReminderCategory.CAR, ReminderCategory.DOCUMENT,
        ReminderCategory.HOUSE, ReminderCategory.HEALTH,
    ]
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.category.in_([c.value for c in deadline_cats]),
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    if not reminders:
        await update.message.reply_text("📄 Nessuna scadenza impostata.")
        return

    tz = await _get_user_tz(update.effective_user.id)
    lines = ["📄 *Scadenze:*\n"]
    for r in reminders:
        emoji = get_emoji(r.category)
        fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
        lines.append(f"{emoji} *{r.title}* — {fire_local.strftime('%d/%m/%Y')}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /fatto ---
async def cmd_fatto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.nudge_count > 0,
            )
        ).order_by(Reminder.last_nudge_at.desc()).limit(1)
        result = await session.execute(stmt)
        reminder = result.scalar_one_or_none()

        if not reminder:
            await update.message.reply_text("Non hai reminder attivi da completare.")
            return

        log = ReminderLog(user_id=reminder.user_id, reminder_id=reminder.id, action="done")
        session.add(log)
        await reschedule_reminder(reminder, session)
        await session.commit()

    await update.message.reply_text(f"✅ *{reminder.title}* completato!", parse_mode="Markdown")


# --- /cancella ---
async def cmd_cancella(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
            )
        ).order_by(Reminder.next_fire).limit(10)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    if not reminders:
        await update.message.reply_text("Non hai reminder da cancellare.")
        return

    buttons = []
    for r in reminders:
        emoji = get_emoji(r.category)
        buttons.append([InlineKeyboardButton(
            f"{emoji} {r.title}", callback_data=f"cancel:{r.id}"
        )])

    await update.message.reply_text(
        "Quale vuoi cancellare?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# --- /silenzio ---
async def cmd_silenzio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usa: /silenzio 2h oppure /silenzio 30m\n"
            "Es: `/silenzio 2h`",
            parse_mode="Markdown"
        )
        return

    text = args[0].lower()
    minutes = 0
    if text.endswith("h"):
        minutes = int(text[:-1]) * 60
    elif text.endswith("m"):
        minutes = int(text[:-1])
    else:
        minutes = int(text)

    # Store silence end time in user data (or DB)
    context.user_data["silent_until"] = datetime.utcnow() + timedelta(minutes=minutes)

    label = f"{minutes // 60} ore" if minutes >= 60 else f"{minutes} minuti"
    await update.message.reply_text(f"🔇 Silenzio per {label}. Non ti disturbo!")


# --- /timezone ---
async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🇮🇹 Italia", callback_data="tz:Europe/Rome")],
            [InlineKeyboardButton("🇬🇧 UK", callback_data="tz:Europe/London")],
            [InlineKeyboardButton("🇺🇸 EST", callback_data="tz:US/Eastern")],
            [InlineKeyboardButton("🇺🇸 PST", callback_data="tz:US/Pacific")],
        ])
        await update.message.reply_text(
            "🌍 Seleziona il tuo fuso orario:",
            reply_markup=keyboard
        )
        return

    tz_name = args[0]
    try:
        pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text("⚠️ Fuso orario non valido. Prova ad es. Europe/Rome")
        return

    async with async_session() as session:
        user = await session.get(User, update.effective_user.id)
        if user:
            user.timezone = tz_name
            await session.commit()

    await update.message.reply_text(f"✅ Fuso orario aggiornato: {tz_name}")


async def tz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timezone selection from buttons."""
    query = update.callback_query
    await query.answer()

    tz_name = query.data.split(":", 1)[1]

    async with async_session() as session:
        user = await session.get(User, query.from_user.id)
        if user:
            user.timezone = tz_name
            await session.commit()

    await query.edit_message_text(f"✅ Fuso orario aggiornato: {tz_name}")


# --- /impostazioni ---
async def cmd_impostazioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await session.get(User, update.effective_user.id)

    if not user:
        await update.message.reply_text("⚠️ Usa /start prima.")
        return

    morning = "✅ Attivo" if user.morning_summary else "❌ Disattivato"
    text = (
        "⚙️ *Le tue impostazioni:*\n\n"
        f"⏰ Sveglia: {user.wake_hour}:00\n"
        f"🌙 Non disturbare: {user.sleep_hour}:00 – {user.wake_hour}:00\n"
        f"🌍 Fuso orario: {user.timezone}\n"
        f"☀️ Buongiorno: {morning}\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "☀️ Buongiorno ON/OFF",
            callback_data="settings:toggle_morning"
        )],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings toggles."""
    query = update.callback_query
    await query.answer()

    if query.data == "settings:toggle_morning":
        async with async_session() as session:
            user = await session.get(User, query.from_user.id)
            if user:
                user.morning_summary = not user.morning_summary
                await session.commit()
                status = "attivato ☀️" if user.morning_summary else "disattivato"
                await query.edit_message_text(f"Buongiorno {status}!")


# --- /export ---
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == update.effective_user.id,
                Reminder.status == ReminderStatus.ACTIVE,
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    if not reminders:
        await update.message.reply_text("Non hai reminder da esportare.")
        return

    data = []
    for r in reminders:
        data.append({
            "title": r.title,
            "category": r.category,
            "next_fire": r.next_fire.isoformat() if r.next_fire else None,
            "recurrence": r.recurrence,
            "fire_times": r.fire_times,
            "end_date": r.end_date.isoformat() if r.end_date else None,
        })

    json_str = json.dumps(data, indent=2, ensure_ascii=False)

    # Send as file
    import io
    buf = io.BytesIO(json_str.encode("utf-8"))
    buf.name = "nudgebot_export.json"
    await update.message.reply_document(buf, caption="📦 Ecco il tuo export!")


# --- /help ---
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

"""
Onboarding flow: /start → welcome → wake time → categories → category setup → done.
Uses ConversationHandler with states.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from datetime import datetime, timedelta
import pytz

from database import async_session, User, Reminder, ReminderCategory, RecurrenceType, ReminderStatus
from services.messages import (
    WELCOME, HOW_IT_WORKS, WAKE_TIME_ASK, CATEGORIES_ASK,
    ONBOARDING_DONE, MEDICINE_ASK_NAME, MEDICINE_ASK_FREQUENCY,
    MEDICINE_ASK_DURATION, MEDICINE_ADDED
)

logger = logging.getLogger(__name__)

# States
(WELCOME_STATE, WAKE_TIME, CATEGORIES, CAT_SETUP,
 MED_NAME, MED_FREQ, MED_TIMES_SELECT, MED_TIMES_CUSTOM,
 MED_DURATION, MED_CONFIRM) = range(10)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user

    # Create or get user in DB
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        if not db_user:
            db_user = User(
                id=user.id,
                chat_id=update.effective_chat.id,
                first_name=user.first_name,
            )
            session.add(db_user)
            await session.commit()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Partiamo", callback_data="onb:start"),
            InlineKeyboardButton("❓ Come funziona", callback_data="onb:how"),
        ]
    ])
    await update.message.reply_text(WELCOME, parse_mode="Markdown", reply_markup=keyboard)
    return WELCOME_STATE


async def welcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle welcome screen buttons."""
    query = update.callback_query
    await query.answer()

    if query.data == "onb:how":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Partiamo", callback_data="onb:start")]
        ])
        await query.edit_message_text(HOW_IT_WORKS, parse_mode="Markdown", reply_markup=keyboard)
        return WELCOME_STATE

    if query.data == "onb:start":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("6–7", callback_data="wake:6")],
            [InlineKeyboardButton("7–8", callback_data="wake:7")],
            [InlineKeyboardButton("8–9", callback_data="wake:8")],
            [InlineKeyboardButton("Dopo le 9", callback_data="wake:9")],
        ])
        await query.edit_message_text(WAKE_TIME_ASK, parse_mode="Markdown", reply_markup=keyboard)
        return WAKE_TIME


async def wake_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save wake time and show categories."""
    query = update.callback_query
    await query.answer()

    hour = int(query.data.split(":")[1])

    async with async_session() as session:
        user = await session.get(User, query.from_user.id)
        if user:
            user.wake_hour = hour
            await session.commit()

    context.user_data["wake_hour"] = hour
    context.user_data["selected_categories"] = []

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚗 Auto (bollo, tagliando, assicurazione)", callback_data="cat:car")],
        [InlineKeyboardButton("🏠 Casa (affitto, bollette)", callback_data="cat:house")],
        [InlineKeyboardButton("💊 Farmaci e integratori", callback_data="cat:medicine")],
        [InlineKeyboardButton("🩺 Visite mediche", callback_data="cat:health")],
        [InlineKeyboardButton("🎂 Compleanni", callback_data="cat:birthday")],
        [InlineKeyboardButton("📄 Documenti (CI, passaporto)", callback_data="cat:document")],
        [InlineKeyboardButton("✅ Prosegui →", callback_data="cat:done")],
        [InlineKeyboardButton("⏭ Salto, aggiungo dopo", callback_data="cat:skip")],
    ])
    await query.edit_message_text(CATEGORIES_ASK, parse_mode="Markdown", reply_markup=keyboard)
    return CATEGORIES


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection (multi-select)."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "cat:skip" or data == "cat:done":
        selected = context.user_data.get("selected_categories", [])

        if not selected or data == "cat:skip":
            # Skip all, go to done
            await _finish_onboarding(query, context)
            return ConversationHandler.END

        # Process first selected category
        context.user_data["cat_queue"] = selected.copy()
        return await _process_next_category(query, context)

    # Toggle selection
    cat = data.split(":")[1]
    selected = context.user_data.get("selected_categories", [])

    if cat in selected:
        selected.remove(cat)
    else:
        selected.append(cat)
    context.user_data["selected_categories"] = selected

    # Rebuild keyboard with checkmarks
    cat_labels = {
        "car": "🚗 Auto (bollo, tagliando, assicurazione)",
        "house": "🏠 Casa (affitto, bollette)",
        "medicine": "💊 Farmaci e integratori",
        "health": "🩺 Visite mediche",
        "birthday": "🎂 Compleanni",
        "document": "📄 Documenti (CI, passaporto)",
    }

    buttons = []
    for key, label in cat_labels.items():
        check = " ✓" if key in selected else ""
        buttons.append([InlineKeyboardButton(f"{label}{check}", callback_data=f"cat:{key}")])

    buttons.append([InlineKeyboardButton("✅ Prosegui →", callback_data="cat:done")])
    buttons.append([InlineKeyboardButton("⏭ Salto, aggiungo dopo", callback_data="cat:skip")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    return CATEGORIES


async def _process_next_category(query, context):
    """Process the next category in the queue."""
    queue = context.user_data.get("cat_queue", [])

    if not queue:
        await _finish_onboarding(query, context)
        return ConversationHandler.END

    cat = queue.pop(0)
    context.user_data["cat_queue"] = queue
    context.user_data["current_cat"] = cat

    if cat == "medicine":
        await query.edit_message_text(MEDICINE_ASK_NAME, parse_mode="Markdown")
        return MED_NAME
    else:
        # For other categories, send a simple text prompt
        prompts = {
            "car": "🚗 *Auto — configurazione rapida*\n\nQuando scade il bollo? Scrivimi mese e anno (es. \"marzo 2026\") o scrivi /salta",
            "house": "🏠 *Casa*\n\nSei in affitto? Scrivimi il giorno del mese in cui paghi (es. \"5\") o scrivi /salta",
            "health": "🩺 *Visite mediche*\n\nHai visite in programma? Scrivimi tipo e data (es. \"dentista 15 marzo\") o scrivi /salta",
            "birthday": "🎂 *Compleanni*\n\nDimmi nome e data (es. \"Marco 4 maggio\") o scrivi /salta",
            "document": "📄 *Documenti*\n\nQuando scade la carta d'identità? Scrivimi mese e anno o scrivi /salta",
        }
        await query.edit_message_text(
            prompts.get(cat, "Scrivimi i dettagli o /salta"),
            parse_mode="Markdown"
        )
        return CAT_SETUP


async def cat_setup_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free text during category setup."""
    text = update.message.text.strip()
    cat = context.user_data.get("current_cat", "")

    if text.lower() == "/salta":
        queue = context.user_data.get("cat_queue", [])
        if queue:
            # Fake a callback query by sending a new message
            await update.message.reply_text("⏭ Saltato!")
            cat = queue.pop(0)
            context.user_data["cat_queue"] = queue
            context.user_data["current_cat"] = cat
            return await _send_cat_prompt(update, context, cat)
        else:
            await _finish_onboarding_msg(update, context)
            return ConversationHandler.END

    # Parse and create reminder based on category
    from services.parser import parse_reminder
    parsed = parse_reminder(text)

    # Map category
    cat_map = {
        "car": ReminderCategory.CAR,
        "house": ReminderCategory.HOUSE,
        "health": ReminderCategory.HEALTH,
        "birthday": ReminderCategory.BIRTHDAY,
        "document": ReminderCategory.DOCUMENT,
    }
    db_cat = cat_map.get(cat, ReminderCategory.GENERIC)

    # Set default advance days
    advance_map = {
        "car": 30, "house": 5, "health": 3,
        "birthday": 3, "document": 90,
    }
    advance = advance_map.get(cat, 0)

    # Create reminder
    async with async_session() as session:
        user = await session.get(User, update.effective_user.id)
        tz = pytz.timezone(user.timezone if user else "Europe/Rome")

        fire_dt = parsed.fire_datetime
        if fire_dt and fire_dt.tzinfo:
            fire_utc = fire_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        elif fire_dt:
            fire_utc = tz.localize(fire_dt).astimezone(pytz.UTC).replace(tzinfo=None)
        else:
            fire_utc = datetime.utcnow() + timedelta(days=1)

        # For advance reminders, subtract advance days
        if advance > 0 and fire_utc > datetime.utcnow() + timedelta(days=advance):
            actual_fire = fire_utc - timedelta(days=advance)
        else:
            actual_fire = fire_utc

        reminder = Reminder(
            user_id=update.effective_user.id,
            title=parsed.title,
            category=db_cat,
            next_fire=actual_fire,
            recurrence=parsed.recurrence,
            advance_days=advance,
        )
        session.add(reminder)
        await session.commit()

    await update.message.reply_text(f"✅ *{parsed.title}* salvato! Ti ricorderò per tempo.", parse_mode="Markdown")

    # Next category
    queue = context.user_data.get("cat_queue", [])
    if queue:
        cat = queue.pop(0)
        context.user_data["cat_queue"] = queue
        context.user_data["current_cat"] = cat
        return await _send_cat_prompt(update, context, cat)
    else:
        await _finish_onboarding_msg(update, context)
        return ConversationHandler.END


async def _send_cat_prompt(update, context, cat):
    """Send prompt for a category."""
    if cat == "medicine":
        await update.message.reply_text(MEDICINE_ASK_NAME, parse_mode="Markdown")
        return MED_NAME

    prompts = {
        "car": "🚗 *Auto*\n\nQuando scade il bollo? (es. \"marzo 2026\") o /salta",
        "house": "🏠 *Casa*\n\nGiorno del mese in cui paghi l'affitto? (es. \"5\") o /salta",
        "health": "🩺 *Visite*\n\nTipo e data? (es. \"dentista 15 marzo\") o /salta",
        "birthday": "🎂 *Compleanni*\n\nNome e data? (es. \"Marco 4 maggio\") o /salta",
        "document": "📄 *Documenti*\n\nScadenza carta d'identità? (mese e anno) o /salta",
    }
    await update.message.reply_text(prompts.get(cat, "/salta"), parse_mode="Markdown")
    return CAT_SETUP


# --- Medicine flow ---

async def med_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive medicine name."""
    name = update.message.text.strip()

    if name.lower() == "/salta":
        queue = context.user_data.get("cat_queue", [])
        if queue:
            cat = queue.pop(0)
            context.user_data["cat_queue"] = queue
            context.user_data["current_cat"] = cat
            return await _send_cat_prompt(update, context, cat)
        await _finish_onboarding_msg(update, context)
        return ConversationHandler.END

    context.user_data["med_name"] = name

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 volta", callback_data="medfreq:1"),
            InlineKeyboardButton("2 volte", callback_data="medfreq:2"),
        ],
        [
            InlineKeyboardButton("3 volte", callback_data="medfreq:3"),
            InlineKeyboardButton("Altro", callback_data="medfreq:other"),
        ],
    ])
    await update.message.reply_text(MEDICINE_ASK_FREQUENCY, reply_markup=keyboard)
    return MED_FREQ


async def med_freq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle medicine frequency selection."""
    query = update.callback_query
    await query.answer()

    freq = query.data.split(":")[1]

    if freq == "other":
        await query.edit_message_text("Quante volte al giorno? Scrivimi il numero.")
        return MED_FREQ  # Will catch as text

    freq_num = int(freq)
    context.user_data["med_freq"] = freq_num

    # Show time options based on frequency
    if freq_num == 1:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Mattina (8:00)", callback_data="medtime:08:00"),
                InlineKeyboardButton("Pranzo (13:00)", callback_data="medtime:13:00"),
            ],
            [
                InlineKeyboardButton("Sera (21:00)", callback_data="medtime:21:00"),
                InlineKeyboardButton("Scelgo io", callback_data="medtime:custom"),
            ],
        ])
    elif freq_num == 2:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("8:00 + 21:00", callback_data="medtime:08:00,21:00")],
            [InlineKeyboardButton("8:00 + 13:00", callback_data="medtime:08:00,13:00")],
            [InlineKeyboardButton("13:00 + 21:00", callback_data="medtime:13:00,21:00")],
            [InlineKeyboardButton("Scelgo io", callback_data="medtime:custom")],
        ])
    else:  # 3+
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("8:00 / 14:00 / 21:00 ✅", callback_data="medtime:08:00,14:00,21:00")],
            [InlineKeyboardButton("Scelgo io", callback_data="medtime:custom")],
        ])

    await query.edit_message_text("A che ora vuoi che ti ricordi?", reply_markup=keyboard)
    return MED_TIMES_SELECT


async def med_freq_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle medicine frequency as text (for 'Altro')."""
    try:
        freq = int(update.message.text.strip())
        context.user_data["med_freq"] = freq
        await update.message.reply_text(
            'Scrivimi gli orari separati da virgola, tipo: "7:30, 15:00, 22:30"'
        )
        return MED_TIMES_CUSTOM
    except ValueError:
        await update.message.reply_text("Scrivimi un numero (es. 4)")
        return MED_FREQ


async def med_times_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle medicine time selection."""
    query = update.callback_query
    await query.answer()

    times_str = query.data.split(":", 1)[1]

    if times_str == "custom":
        await query.edit_message_text(
            'Scrivimi gli orari separati da virgola, tipo: "7:30, 15:00, 22:30"'
        )
        return MED_TIMES_CUSTOM

    context.user_data["med_times"] = times_str.split(",")

    # Ask duration
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Sempre", callback_data="meddur:0"),
            InlineKeyboardButton("7 giorni", callback_data="meddur:7"),
        ],
        [
            InlineKeyboardButton("14 giorni", callback_data="meddur:14"),
            InlineKeyboardButton("30 giorni", callback_data="meddur:30"),
        ],
        [InlineKeyboardButton("Scelgo io", callback_data="meddur:custom")],
    ])
    await query.edit_message_text(MEDICINE_ASK_DURATION, reply_markup=keyboard)
    return MED_DURATION


async def med_times_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom time input for medicine."""
    text = update.message.text.strip()
    times_raw = [t.strip().replace(".", ":") for t in text.replace(" e ", ",").split(",")]

    times = []
    for t in times_raw:
        if ":" not in t:
            t = f"{t}:00"
        if len(t.split(":")[0]) == 1:
            t = f"0{t}"
        times.append(t)

    context.user_data["med_times"] = times

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Sempre", callback_data="meddur:0"),
            InlineKeyboardButton("7 giorni", callback_data="meddur:7"),
        ],
        [
            InlineKeyboardButton("14 giorni", callback_data="meddur:14"),
            InlineKeyboardButton("30 giorni", callback_data="meddur:30"),
        ],
        [InlineKeyboardButton("Scelgo io", callback_data="meddur:custom")],
    ])
    await update.message.reply_text(MEDICINE_ASK_DURATION, reply_markup=keyboard)
    return MED_DURATION


async def med_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle medicine duration selection."""
    query = update.callback_query
    await query.answer()

    dur_str = query.data.split(":")[1]

    if dur_str == "custom":
        await query.edit_message_text("Per quanti giorni? Scrivimi il numero.")
        return MED_DURATION

    days = int(dur_str)
    context.user_data["med_duration"] = days

    return await _save_medicine(query, context, is_callback=True)


async def med_duration_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom duration as text."""
    try:
        days = int(update.message.text.strip())
        context.user_data["med_duration"] = days
        return await _save_medicine(update, context, is_callback=False)
    except ValueError:
        await update.message.reply_text("Scrivimi un numero di giorni (es. 10)")
        return MED_DURATION


async def _save_medicine(source, context, is_callback=False):
    """Save medicine reminder(s) to database."""
    user_id = source.callback_query.from_user.id if is_callback else source.effective_user.id
    chat_id = source.callback_query.message.chat_id if is_callback else source.effective_chat.id

    name = context.user_data.get("med_name", "Farmaco")
    times = context.user_data.get("med_times", ["08:00"])
    duration = context.user_data.get("med_duration", 0)

    async with async_session() as session:
        user = await session.get(User, user_id)
        tz = pytz.timezone(user.timezone if user else "Europe/Rome")
        now = datetime.now(tz)

        end_date = None
        if duration > 0:
            end_date = (now + timedelta(days=duration)).astimezone(pytz.UTC).replace(tzinfo=None)

        # Create one reminder per time slot
        for idx, t in enumerate(times):
            h, m = map(int, t.split(":"))
            fire_local = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if fire_local <= now:
                fire_local += timedelta(days=1)
            fire_utc = fire_local.astimezone(pytz.UTC).replace(tzinfo=None)

            reminder = Reminder(
                user_id=user_id,
                title=name,
                category=ReminderCategory.MEDICINE,
                next_fire=fire_utc,
                recurrence=RecurrenceType.DAILY,
                fire_times=",".join(times),
                end_date=end_date,
                time_slot_index=idx,
                time_slot_total=len(times),
            )
            session.add(reminder)

        await session.commit()

    # Build confirmation
    times_str = " · ".join(times)
    dur_str = f"{duration} giorni" if duration > 0 else "a tempo indeterminato"
    end_str = ""
    if duration > 0:
        end_dt = datetime.now() + timedelta(days=duration)
        end_str = f" (fino al {end_dt.strftime('%d/%m')})"

    confirm_text = (
        f"✅ *{name}*\n"
        f"📅 Ogni giorno — {len(times)} {'volta' if len(times) == 1 else 'volte'}\n"
        f"⏰ {times_str}\n"
        f"⏳ {dur_str}{end_str}\n\n"
        f"{MEDICINE_ADDED}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Aggiungi altro", callback_data="med:another"),
            InlineKeyboardButton("✅ Ho finito", callback_data="med:done"),
        ]
    ])

    if is_callback:
        await source.callback_query.edit_message_text(confirm_text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await source.message.reply_text(confirm_text, parse_mode="Markdown", reply_markup=keyboard)

    return MED_CONFIRM


async def med_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'add another' or 'done' after medicine."""
    query = update.callback_query
    await query.answer()

    if query.data == "med:another":
        await query.edit_message_text(MEDICINE_ASK_NAME, parse_mode="Markdown")
        return MED_NAME

    # Done with medicine, check queue
    queue = context.user_data.get("cat_queue", [])
    if queue:
        cat = queue.pop(0)
        context.user_data["cat_queue"] = queue
        context.user_data["current_cat"] = cat

        if cat == "medicine":
            await query.edit_message_text(MEDICINE_ASK_NAME, parse_mode="Markdown")
            return MED_NAME
        else:
            prompts = {
                "car": "🚗 *Auto*\n\nQuando scade il bollo? (es. \"marzo 2026\") o /salta",
                "house": "🏠 *Casa*\n\nGiorno del mese per l'affitto? (es. \"5\") o /salta",
                "health": "🩺 *Visite*\n\nTipo e data? (es. \"dentista 15 marzo\") o /salta",
                "birthday": "🎂 *Compleanni*\n\nNome e data? (es. \"Marco 4 maggio\") o /salta",
                "document": "📄 *Documenti*\n\nScadenza CI? (mese e anno) o /salta",
            }
            await query.edit_message_text(prompts.get(cat, "/salta"), parse_mode="Markdown")
            return CAT_SETUP

    await _finish_onboarding(query, context)
    return ConversationHandler.END


async def _finish_onboarding(query, context):
    """Mark onboarding as done."""
    async with async_session() as session:
        user = await session.get(User, query.from_user.id)
        if user:
            user.onboarding_done = True
            await session.commit()

    await query.edit_message_text(ONBOARDING_DONE, parse_mode="Markdown")


async def _finish_onboarding_msg(update, context):
    """Mark onboarding done (from message context)."""
    async with async_session() as session:
        user = await session.get(User, update.effective_user.id)
        if user:
            user.onboarding_done = True
            await session.commit()

    await update.message.reply_text(ONBOARDING_DONE, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel onboarding."""
    await update.message.reply_text("Ok, nessun problema! Scrivimi quando vuoi impostare qualcosa.")
    return ConversationHandler.END


def get_onboarding_handler() -> ConversationHandler:
    """Build the onboarding ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WELCOME_STATE: [CallbackQueryHandler(welcome_callback, pattern=r"^onb:")],
            WAKE_TIME: [CallbackQueryHandler(wake_time_callback, pattern=r"^wake:")],
            CATEGORIES: [CallbackQueryHandler(category_callback, pattern=r"^cat:")],
            CAT_SETUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cat_setup_text),
                CommandHandler("salta", cat_setup_text),
            ],
            MED_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, med_name)],
            MED_FREQ: [
                CallbackQueryHandler(med_freq_callback, pattern=r"^medfreq:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, med_freq_text),
            ],
            MED_TIMES_SELECT: [CallbackQueryHandler(med_times_callback, pattern=r"^medtime:")],
            MED_TIMES_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, med_times_custom)],
            MED_DURATION: [
                CallbackQueryHandler(med_duration_callback, pattern=r"^meddur:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, med_duration_text),
            ],
            MED_CONFIRM: [CallbackQueryHandler(med_confirm_callback, pattern=r"^med:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

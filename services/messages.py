"""
All bot message templates.
"""
from database import Reminder, ReminderCategory


# --- Emoji map ---
EMOJI = {
    ReminderCategory.MEDICINE: "💊",
    ReminderCategory.BIRTHDAY: "🎂",
    ReminderCategory.CAR: "🚗",
    ReminderCategory.HOUSE: "🏠",
    ReminderCategory.HEALTH: "🩺",
    ReminderCategory.DOCUMENT: "📄",
    ReminderCategory.HABIT: "💧",
    ReminderCategory.GENERIC: "📌",
}


def get_emoji(category: str) -> str:
    try:
        return EMOJI[ReminderCategory(category)]
    except (ValueError, KeyError):
        return "📌"


# --- Welcome messages ---

WELCOME = (
    "👋 *Ciao! Sono NudgeBot.*\n\n"
    "Ti aiuto a ricordare tutto quello che la tua testa dimentica: "
    "scadenze, appuntamenti, bollette, compleanni, farmaci, abitudini.\n\n"
    "Scrivimi le cose come le diresti a un amico, tipo:\n"
    '_"ricordami di pagare la palestra il 15 di ogni mese"_\n\n'
    "E io non ti mollo finché non l'hai fatto 😄\n\n"
    "Iniziamo?"
)

HOW_IT_WORKS = (
    "*Come funziono in 30 secondi:*\n\n"
    "1️⃣ Mi scrivi cosa ricordare, in italiano, come ti viene\n"
    "2️⃣ Io capisco data, ora e ricorrenza\n"
    "3️⃣ Quando arriva il momento, ti avviso\n"
    "4️⃣ Se non rispondi, insisto (gentilmente 😉)\n"
    "5️⃣ Tu confermi ✅, posticipi ⏰ o cancelli ❌\n\n"
    "Tutto qui. Niente app, niente calendari complicati."
)

WAKE_TIME_ASK = (
    "⏰ *A che ora ti svegli di solito?*\n\n"
    "Così evito di scriverti alle 6 di mattina."
)

CATEGORIES_ASK = (
    "📋 *Vuoi impostare subito qualche scadenza o promemoria ricorrente?*\n\n"
    "Scegli quelli che ti servono, li configuriamo in un attimo."
)

ONBOARDING_DONE = (
    "✅ *Tutto pronto!*\n\n"
    "Da ora in poi scrivimi qualsiasi cosa da ricordare. "
    "Qualche esempio:\n\n"
    '📌 _"domani alle 10 chiama l\'idraulico"_\n'
    '📌 _"ogni lunedì mattina metti la lavatrice"_\n'
    '📌 _"tra 3 ore controlla il forno"_\n'
    '📌 _"ricordami di bere acqua alle 10, 13, 16 e 19"_\n\n'
    "Scrivi /help se ti perdi. Ci sono! 💪"
)

# --- Medicine flow ---

MEDICINE_ASK_NAME = (
    "💊 *Che farmaco o integratore prendi?*\n\n"
    'Scrivimi il nome (es. "vitamina D", "Amoxicillina", "pillola")'
)

MEDICINE_ASK_FREQUENCY = "Quante volte al giorno lo prendi?"

MEDICINE_ASK_DURATION = "Per quanti giorni devi prenderlo?"

MEDICINE_ADDED = "✅ Ti ricorderò ogni volta e non mollo finché non confermi 😄\n\nVuoi aggiungere un altro farmaco?"

# --- Reminder nudges ---

def nudge_1(reminder: Reminder) -> str:
    """First notification when reminder fires."""
    emoji = get_emoji(reminder.category)
    if reminder.category == ReminderCategory.MEDICINE and reminder.time_slot_total:
        slot = (reminder.time_slot_index or 0) + 1
        total = reminder.time_slot_total
        suffix = ""
        if slot == total:
            suffix = " — ultimo di oggi 👏"
        return f"{emoji} *{reminder.title}* ({slot}/{total}){suffix}"
    return f"🔔 *{reminder.title}*"


def nudge_quick(reminder: Reminder) -> str:
    """Quick 5-minute gentle followup."""
    emoji = get_emoji(reminder.category)
    if reminder.category == ReminderCategory.MEDICINE:
        return f"{emoji} _{reminder.title.lower()}_ — l'hai presa?"
    return f"⏳ _{reminder.title.lower()}_ — ci stai pensando?"


def nudge_2(reminder: Reminder) -> str:
    """Second nudge (30 min after first)."""
    emoji = get_emoji(reminder.category)
    if reminder.category == ReminderCategory.MEDICINE:
        return f"{emoji} _Ehi, {reminder.title.lower()}?_"
    return f"👀 _Ehi, {reminder.title.lower()}?_"


def nudge_3(reminder: Reminder) -> str:
    """Third and final nudge."""
    if reminder.category == ReminderCategory.MEDICINE:
        return f"💊 _Ultimo reminder per {reminder.title.lower()}. Saltata?_"
    return f"🫠 _Ultimo nudge per oggi: {reminder.title.lower()}. Lo sposto a domani?_"


def snooze_warning(reminder: Reminder) -> str:
    return (
        f"🤔 Hai posticipato *\"{reminder.title}\"* {reminder.snooze_count} volte.\n\n"
        "Vuoi spostarlo a settimana prossima o lo cancelliamo?"
    )


def done_response() -> str:
    return "✅ Fatto! Una cosa in meno a cui pensare."


def skipped_response() -> str:
    return "⏭ Saltata. Ti ricorderò alla prossima."


def cancelled_response() -> str:
    return "🗑 Cancellato."


# --- Morning summary ---

def morning_summary(items: list[dict]) -> str:
    if not items:
        return "☀️ *Buongiorno!* Oggi non hai nulla in programma. Giornata libera! 🎉"

    lines = ["☀️ *Buongiorno! Oggi hai:*\n"]
    for item in items:
        emoji = get_emoji(item["category"])
        if item.get("times"):
            times_str = " · ".join(item["times"])
            lines.append(f"{emoji} {item['title']} _({times_str})_")
        else:
            lines.append(f"{emoji} {item['title']}")
        if item.get("note"):
            lines.append(f"   {item['note']}")
    return "\n".join(lines)


# --- Weekly summary ---

def weekly_summary(done: int, snoozed: int, cancelled: int,
                   med_taken: int = 0, med_total: int = 0,
                   upcoming: int = 0) -> str:
    lines = ["📊 *La tua settimana:*\n"]
    lines.append(f"✅ {done} cose fatte")
    if snoozed:
        lines.append(f"⏰ {snoozed} posticipate")
    if cancelled:
        lines.append(f"❌ {cancelled} cancellate")
    if med_total > 0:
        pct = round(med_taken / med_total * 100)
        lines.append(f"💊 Farmaci: {med_taken}/{med_total} dosi confermate ({pct}%)")
    lines.append(f"\n🔜 Prossima settimana hai {upcoming} reminder in arrivo.")
    return "\n".join(lines)


# --- Help ---

HELP_TEXT = (
    "*Comandi disponibili:*\n\n"
    "/oggi — Cosa hai oggi\n"
    "/domani — Cosa hai domani\n"
    "/settimana — Prossimi 7 giorni\n"
    "/scadenze — Scadenze future\n"
    "/lista — Tutti i reminder attivi\n"
    "/farmaci — Farmaci attivi\n"
    "/fatto — Completa l'ultimo reminder\n"
    "/salta — Salta prossima occorrenza\n"
    "/cancella — Cancella un reminder\n"
    "/silenzio — Muto temporaneo\n"
    "/timezone — Cambia fuso orario\n"
    "/impostazioni — Le tue preferenze\n"
    "/export — Esporta i tuoi dati\n"
    "/help — Questa guida\n\n"
    "Oppure scrivimi qualsiasi cosa da ricordare!\n"
    "Puoi anche inviarmi un messaggio vocale 🎙️"
)

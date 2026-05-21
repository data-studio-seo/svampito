"""
Assistant service: the brain of Svampito.
Takes user message → loads context → calls LLM → executes action → returns response.

Supports contextual replies: when the bot sends a reminder and the user replies
with text/voice within 15 minutes, the assistant handles it in context.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from sqlalchemy import select, and_
import pytz

from database import (
    async_session, Reminder, ReminderLog, User,
    ReminderStatus, ReminderCategory, RecurrenceType
)
from services.llm import classify_and_parse
from services.parser import parse_reminder, ParsedReminder, format_confirmation
from services.messages import get_emoji

logger = logging.getLogger(__name__)

# Max time (minutes) to consider a reply as contextual to a sent reminder
REPLY_CONTEXT_WINDOW = 15


@dataclass
class AssistantResponse:
    """What the assistant wants to send back to the user."""
    text: str
    parse_mode: str = "Markdown"
    pending_reminder: Optional[dict] = None
    show_confirm: bool = False
    confirm_delete_id: Optional[int] = None
    confirm_delete_title: Optional[str] = None


async def _get_active_reminders(user_id: int, tz) -> list[dict]:
    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == user_id,
                Reminder.status == ReminderStatus.ACTIVE,
            )
        ).order_by(Reminder.next_fire).limit(50)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    out = []
    for r in reminders:
        fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
        out.append({
            "id": r.id,
            "title": r.title,
            "category": r.category,
            "next_fire": fire_local.strftime("%d/%m/%Y %H:%M"),
            "recurrence": r.recurrence,
            "fire_times": r.fire_times or "",
        })
    return out


async def _get_reminders_in_range(user_id: int, start_utc: datetime, end_utc: datetime, category: str = "all"):
    async with async_session() as session:
        conditions = [
            Reminder.user_id == user_id,
            Reminder.status == ReminderStatus.ACTIVE,
            Reminder.next_fire >= start_utc,
            Reminder.next_fire < end_utc,
        ]
        if category != "all":
            conditions.append(Reminder.category == category)

        stmt = select(Reminder).where(and_(*conditions)).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        return result.scalars().all()


async def _find_reminder_by_match(user_id: int, reminder_id: Optional[int], title_match: Optional[str]):
    async with async_session() as session:
        if reminder_id:
            r = await session.get(Reminder, reminder_id)
            if r and r.user_id == user_id and r.status == ReminderStatus.ACTIVE:
                return r

        if title_match:
            stmt = select(Reminder).where(
                and_(
                    Reminder.user_id == user_id,
                    Reminder.status == ReminderStatus.ACTIVE,
                    Reminder.title.ilike(f"%{title_match}%"),
                )
            ).limit(1)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    return None


def _format_reminder_line(r: Reminder, tz) -> Optional[str]:
    emoji = get_emoji(r.category)
    fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
    time_str = fire_local.strftime("%H:%M")

    if r.fire_times and r.time_slot_index == 0:
        times = r.fire_times.split(",")
        joiner = " · "
        return "{} {} _({})_".format(emoji, r.title, joiner.join(times))
    elif r.fire_times and r.time_slot_index and r.time_slot_index > 0:
        return None
    return "{} {} _({})_".format(emoji, r.title, time_str)


def _get_recent_reminder_context(user_id: int) -> Optional[dict]:
    from services.scheduler import last_sent_reminders

    ctx = last_sent_reminders.get(user_id)
    if not ctx:
        return None

    age_minutes = (datetime.utcnow() - ctx["sent_at"]).total_seconds() / 60
    if age_minutes > REPLY_CONTEXT_WINDOW:
        return None

    return ctx


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

async def process_message(user_id: int, chat_id: int, text: str, first_name: str = "") -> AssistantResponse:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            user = User(id=user_id, chat_id=chat_id, first_name=first_name)
            session.add(user)
            await session.commit()

    tz = pytz.timezone(user.timezone if user else "Europe/Rome")
    tz_name = user.timezone if user else "Europe/Rome"

    recent_ctx = _get_recent_reminder_context(user_id)
    active_reminders = await _get_active_reminders(user_id, tz)

    llm_result = await classify_and_parse(text, tz_name, active_reminders, recent_ctx)

    if not llm_result:
        logger.info("LLM unavailable, falling back to regex create")
        return await _handle_create_fallback(text, tz_name)

    intent = llm_result.get("intent", "chat")
    data = llm_result.get("data", {})

    logger.info("Processing intent=%s for user=%s", intent, user_id)

    if intent == "create":
        return await _handle_create(data, tz_name)
    elif intent == "query":
        return await _handle_query(user_id, data, tz)
    elif intent == "delete":
        return await _handle_delete(user_id, data, tz)
    elif intent == "modify":
        return await _handle_modify(user_id, data, tz)
    elif intent == "done":
        return await _handle_done(user_id, data)
    elif intent == "reminder_reply":
        return await _handle_reminder_reply(user_id, data, tz_name)
    elif intent == "chat":
        return AssistantResponse(text=data.get("response", "Ciao! Scrivimi un reminder"))
    else:
        logger.warning("Unknown intent: %s", intent)
        return await _handle_create_fallback(text, tz_name)


# ─────────────────────────────────────────────
# Intent handlers
# ─────────────────────────────────────────────

async def _handle_create(data: dict, tz_name: str) -> AssistantResponse:
    from services.parser import _llm_dict_to_parsed

    try:
        parsed = _llm_dict_to_parsed(data, tz_name)
    except Exception as e:
        logger.error("Error converting LLM data: %s", e)
        return AssistantResponse(text="Non sono riuscito a creare il reminder. Riprova.")

    if not parsed.title or len(parsed.title) < 2:
        return AssistantResponse(
            text="Non ho capito bene. Prova a scrivere cosa vuoi ricordare, "
                 "ad esempio:\n_\"domani alle 10 chiama il dentista\"_"
        )

    pending = {
        "title": parsed.title,
        "category": parsed.category.value if isinstance(parsed.category, ReminderCategory) else parsed.category,
        "fire_datetime": parsed.fire_datetime.isoformat() if parsed.fire_datetime else None,
        "recurrence": parsed.recurrence.value if isinstance(parsed.recurrence, RecurrenceType) else parsed.recurrence,
        "recurrence_days": parsed.recurrence_days,
        "fire_times": parsed.fire_times,
        "end_date": parsed.end_date.isoformat() if parsed.end_date else None,
        "advance_days": parsed.advance_days,
    }

    confirm_text = format_confirmation(parsed)

    return AssistantResponse(
        text=confirm_text,
        pending_reminder=pending,
        show_confirm=True,
    )


async def _handle_create_fallback(text: str, tz_name: str) -> AssistantResponse:
    parsed = parse_reminder(text, tz_name)

    if not parsed.title or len(parsed.title) < 2:
        return AssistantResponse(
            text="Non ho capito bene. Prova a scrivere cosa vuoi ricordare, "
                 "ad esempio:\n_\"domani alle 10 chiama il dentista\"_"
        )

    pending = {
        "title": parsed.title,
        "category": parsed.category.value,
        "fire_datetime": parsed.fire_datetime.isoformat() if parsed.fire_datetime else None,
        "recurrence": parsed.recurrence.value,
        "recurrence_days": parsed.recurrence_days,
        "fire_times": parsed.fire_times,
        "end_date": parsed.end_date.isoformat() if parsed.end_date else None,
        "advance_days": parsed.advance_days,
    }

    confirm_text = format_confirmation(parsed)

    return AssistantResponse(
        text=confirm_text,
        pending_reminder=pending,
        show_confirm=True,
    )


async def _handle_query(user_id: int, data: dict, tz) -> AssistantResponse:
    period = data.get("period", "today")
    category = data.get("category", "all")
    search = data.get("search")
    now = datetime.now(tz)

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
        label = "Oggi"
    elif period == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
        label = "Domani"
    elif period == "week":
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=7)
        label = "Prossimi 7 giorni"
    else:
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=365)
        label = "Tutti i reminder"

    start_utc = start.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end.astimezone(pytz.UTC).replace(tzinfo=None)

    reminders = await _get_reminders_in_range(user_id, start_utc, end_utc, category)

    if search and reminders:
        search_lower = search.lower()
        reminders = [r for r in reminders if search_lower in r.title.lower()]

    if not reminders:
        cat_label = " nella categoria " + category if category != "all" else ""
        return AssistantResponse(
            text="Niente in programma per *{}*{}!".format(label.lower(), cat_label)
        )

    day_names = ["Lunedi", "Martedi", "Mercoledi", "Giovedi", "Venerdi", "Sabato", "Domenica"]

    if period in ("today", "tomorrow"):
        lines = ["*{}:*".format(label)]
        for r in reminders:
            line = _format_reminder_line(r, tz)
            if line:
                lines.append(line)
        return AssistantResponse(text="\n".join(lines))

    else:
        lines = ["*{}:*".format(label)]
        current_day = None
        for r in reminders:
            fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
            day_key = fire_local.strftime("%Y-%m-%d")
            if day_key != current_day:
                current_day = day_key
                day_name = day_names[fire_local.weekday()]
                lines.append("")
                lines.append("*{} {}*".format(day_name, fire_local.strftime("%d/%m")))
            line = _format_reminder_line(r, tz)
            if line:
                lines.append("  " + line)
        return AssistantResponse(text="\n".join(lines))


async def _handle_delete(user_id: int, data: dict, tz) -> AssistantResponse:
    reminder_id = data.get("reminder_id")
    title_match = data.get("title_match")

    reminder = await _find_reminder_by_match(user_id, reminder_id, title_match)

    if not reminder:
        msg = "Non ho trovato nessun reminder"
        if title_match:
            msg += ' con "{}"'.format(title_match)
        msg += ". Usa il bottone per vedere i tuoi reminder."
        return AssistantResponse(text=msg)

    emoji = get_emoji(reminder.category)
    fire_local = pytz.UTC.localize(reminder.next_fire).astimezone(tz)

    return AssistantResponse(
        text="Vuoi cancellare {} *{}* ({})?".format(
            emoji, reminder.title, fire_local.strftime("%d/%m/%Y %H:%M")
        ),
        confirm_delete_id=reminder.id,
        confirm_delete_title=reminder.title,
    )


async def _handle_modify(user_id: int, data: dict, tz) -> AssistantResponse:
    reminder_id = data.get("reminder_id")
    title_match = data.get("title_match")
    new_date = data.get("new_date")
    new_time = data.get("new_time")

    reminder = await _find_reminder_by_match(user_id, reminder_id, title_match)

    if not reminder:
        msg = "Non ho trovato nessun reminder"
        if title_match:
            msg += ' con "{}"'.format(title_match)
        msg += "."
        return AssistantResponse(text=msg)

    changed = False
    async with async_session() as session:
        r = await session.get(Reminder, reminder.id)
        if not r:
            return AssistantResponse(text="Errore: reminder non trovato.")

        if new_date or new_time:
            old_fire = pytz.UTC.localize(r.next_fire).astimezone(tz)
            new_dt = old_fire

            if new_date:
                try:
                    d = datetime.strptime(new_date, "%Y-%m-%d")
                    new_dt = new_dt.replace(year=d.year, month=d.month, day=d.day)
                    changed = True
                except ValueError:
                    pass

            if new_time:
                try:
                    parts = new_time.split(":")
                    new_dt = new_dt.replace(hour=int(parts[0]), minute=int(parts[1]))
                    changed = True
                except (ValueError, IndexError):
                    pass

            if changed:
                r.next_fire = new_dt.astimezone(pytz.UTC).replace(tzinfo=None)
                await session.commit()

    if changed:
        emoji = get_emoji(reminder.category)
        new_fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
        return AssistantResponse(
            text="{} *{}* spostato al {} ore {}".format(
                emoji, reminder.title,
                new_fire_local.strftime("%d/%m/%Y"),
                new_fire_local.strftime("%H:%M")
            )
        )
    else:
        return AssistantResponse(text="Non ho capito cosa modificare. Prova a specificare la nuova data o orario.")


async def _handle_done(user_id: int, data: dict) -> AssistantResponse:
    from services.scheduler import reschedule_reminder

    reminder_id = data.get("reminder_id")
    title_match = data.get("title_match")

    reminder = await _find_reminder_by_match(user_id, reminder_id, title_match)

    if not reminder:
        return AssistantResponse(text="Non ho trovato il reminder da completare.")

    async with async_session() as session:
        r = await session.get(Reminder, reminder.id)
        if r:
            log = ReminderLog(user_id=user_id, reminder_id=r.id, action="done")
            session.add(log)
            await reschedule_reminder(r, session)
            await session.commit()

    from services.scheduler import last_sent_reminders
    last_sent_reminders.pop(user_id, None)

    return AssistantResponse(text="*{}* — fatto!".format(reminder.title))


async def _handle_reminder_reply(user_id: int, data: dict, tz_name: str) -> AssistantResponse:
    from services.scheduler import reschedule_reminder, last_sent_reminders

    action = data.get("action", "done")
    snooze_minutes = data.get("snooze_minutes", 30)
    reminder_id = data.get("reminder_id")

    if not reminder_id:
        return AssistantResponse(text="Non ho capito a quale reminder ti riferisci.")

    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder or reminder.user_id != user_id:
            return AssistantResponse(text="Reminder non trovato.")

        user = await session.get(User, user_id)
        tz = pytz.timezone(user.timezone if user else "Europe/Rome")
        emoji = get_emoji(reminder.category)

        if action == "done":
            log = ReminderLog(user_id=user_id, reminder_id=reminder.id, action="done")
            session.add(log)
            await reschedule_reminder(reminder, session)
            await session.commit()
            last_sent_reminders.pop(user_id, None)
            return AssistantResponse(text="*{}* — fatto!".format(reminder.title))

        elif action == "snooze":
            reminder.next_fire = datetime.utcnow() + timedelta(minutes=snooze_minutes)
            reminder.nudge_count = 0
            reminder.last_nudge_at = None
            reminder.snooze_count += 1
            log = ReminderLog(user_id=user_id, reminder_id=reminder.id, action="snoozed")
            session.add(log)
            await session.commit()
            last_sent_reminders.pop(user_id, None)

            if snooze_minutes >= 60:
                if snooze_minutes == 60:
                    label = "1 ora"
                else:
                    label = "{} ore".format(snooze_minutes // 60)
            else:
                label = "{} minuti".format(snooze_minutes)
            return AssistantResponse(
                text="Ok, ti ricordo {} *{}* tra {}!".format(emoji, reminder.title, label)
            )

        elif action == "skip":
            log = ReminderLog(user_id=user_id, reminder_id=reminder.id, action="skipped")
            session.add(log)
            await reschedule_reminder(reminder, session)
            await session.commit()
            last_sent_reminders.pop(user_id, None)

            if reminder.status == ReminderStatus.ACTIVE and reminder.next_fire:
                next_local = pytz.UTC.localize(reminder.next_fire).astimezone(tz)
                return AssistantResponse(
                    text="{} *{}* saltato per oggi. Prossimo: {}.".format(
                        emoji, reminder.title, next_local.strftime("%d/%m alle %H:%M")
                    )
                )
            return AssistantResponse(text="{} *{}* saltato!".format(emoji, reminder.title))

        elif action == "tomorrow":
            current_fire = pytz.UTC.localize(reminder.next_fire).astimezone(tz)
            tomorrow = current_fire + timedelta(days=1)
            reminder.next_fire = tomorrow.astimezone(pytz.UTC).replace(tzinfo=None)
            reminder.nudge_count = 0
            reminder.last_nudge_at = None
            reminder.snooze_count += 1
            log = ReminderLog(user_id=user_id, reminder_id=reminder.id, action="snoozed")
            session.add(log)
            await session.commit()
            last_sent_reminders.pop(user_id, None)
            return AssistantResponse(
                text="{} *{}* spostato a domani ({}).".format(
                    emoji, reminder.title, tomorrow.strftime("%H:%M")
                )
            )

        else:
            return AssistantResponse(text="Non ho capito cosa vuoi fare con questo reminder.")

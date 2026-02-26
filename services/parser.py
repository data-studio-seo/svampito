"""
Natural language parser for Italian reminder input.
Strategy: try LLM first (Groq/Llama 3), fallback to regex if unavailable.
Extracts: action, date/time, recurrence, multiple times.
"""
import re
import logging
from datetime import datetime, timedelta, time
from typing import Optional
import dateparser
import pytz

from database import ReminderCategory, RecurrenceType

logger = logging.getLogger(__name__)


class ParsedReminder:
    def __init__(self):
        self.title: str = ""
        self.category: ReminderCategory = ReminderCategory.GENERIC
        self.fire_datetime: Optional[datetime] = None
        self.recurrence: RecurrenceType = RecurrenceType.ONCE
        self.recurrence_days: Optional[str] = None
        self.fire_times: list[str] = []  # ["08:00", "14:00"]
        self.end_date: Optional[datetime] = None
        self.advance_days: int = 0

    def summary_lines(self) -> list[str]:
        """Generate confirmation message lines."""
        lines = []

        # Category emoji
        emoji_map = {
            ReminderCategory.MEDICINE: "💊",
            ReminderCategory.BIRTHDAY: "🎂",
            ReminderCategory.CAR: "🚗",
            ReminderCategory.HOUSE: "🏠",
            ReminderCategory.HEALTH: "🩺",
            ReminderCategory.DOCUMENT: "📄",
            ReminderCategory.HABIT: "💧",
            ReminderCategory.GENERIC: "📌",
        }
        emoji = emoji_map.get(self.category, "📌")
        lines.append(f"{emoji} {self.title}")

        # Date/time
        if self.fire_datetime:
            if self.recurrence == RecurrenceType.ONCE:
                dt = self.fire_datetime
                lines.append(f"📅 {dt.strftime('%d/%m/%Y')} ore {dt.strftime('%H:%M')}")
            elif self.recurrence == RecurrenceType.DAILY:
                if self.fire_times:
                    times_str = " · ".join(self.fire_times)
                    lines.append(f"📅 Ogni giorno")
                    lines.append(f"⏰ {times_str}")
                else:
                    lines.append(f"📅 Ogni giorno ore {self.fire_datetime.strftime('%H:%M')}")
            elif self.recurrence == RecurrenceType.WEEKLY:
                day_names = {
                    "mon": "lunedì", "tue": "martedì", "wed": "mercoledì",
                    "thu": "giovedì", "fri": "venerdì", "sat": "sabato", "sun": "domenica"
                }
                if self.recurrence_days:
                    days = [day_names.get(d, d) for d in self.recurrence_days.split(",")]
                    lines.append(f"📅 Ogni {', '.join(days)} ore {self.fire_datetime.strftime('%H:%M')}")
            elif self.recurrence == RecurrenceType.MONTHLY:
                lines.append(f"📅 Ogni mese il {self.fire_datetime.day} ore {self.fire_datetime.strftime('%H:%M')}")

        # Recurrence label
        if self.recurrence == RecurrenceType.ONCE:
            lines.append("🔁 Una tantum")
        else:
            if self.end_date:
                lines.append(f"⏳ Fino al {self.end_date.strftime('%d/%m/%Y')}")
            else:
                lines.append("🔁 Ricorrente (senza scadenza)")

        return lines


def _llm_dict_to_parsed(data: dict, user_tz: str) -> ParsedReminder:
    """Convert LLM JSON output to a ParsedReminder object."""
    result = ParsedReminder()
    tz = pytz.timezone(user_tz)
    now = datetime.now(tz)

    # Title
    result.title = data.get("title", "").strip()
    if result.title:
        result.title = result.title[0].upper() + result.title[1:]

    # Category
    cat_map = {
        "medicine": ReminderCategory.MEDICINE,
        "birthday": ReminderCategory.BIRTHDAY,
        "car": ReminderCategory.CAR,
        "house": ReminderCategory.HOUSE,
        "health": ReminderCategory.HEALTH,
        "document": ReminderCategory.DOCUMENT,
        "habit": ReminderCategory.HABIT,
        "generic": ReminderCategory.GENERIC,
    }
    result.category = cat_map.get(data.get("category", "generic"), ReminderCategory.GENERIC)

    # Recurrence
    rec_map = {
        "once": RecurrenceType.ONCE,
        "daily": RecurrenceType.DAILY,
        "weekly": RecurrenceType.WEEKLY,
        "monthly": RecurrenceType.MONTHLY,
        "every_other_day": RecurrenceType.EVERY_OTHER_DAY,
    }
    result.recurrence = rec_map.get(data.get("recurrence", "once"), RecurrenceType.ONCE)
    result.recurrence_days = data.get("recurrence_days")

    # Fire times (multi-orario)
    fire_times = data.get("fire_times", [])
    if fire_times and isinstance(fire_times, list):
        result.fire_times = fire_times

    # Date and time
    date_str = data.get("date")
    time_str = data.get("time")

    if date_str and time_str:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            result.fire_datetime = tz.localize(dt)
        except (ValueError, TypeError):
            pass
    elif date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            dt = dt.replace(hour=9, minute=0)
            result.fire_datetime = tz.localize(dt)
        except (ValueError, TypeError):
            pass
    elif time_str:
        try:
            h, m = map(int, time_str.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            result.fire_datetime = dt
        except (ValueError, TypeError):
            pass

    # Default if no datetime parsed
    if not result.fire_datetime:
        if result.fire_times:
            first_time = result.fire_times[0]
            h, m = map(int, first_time.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            result.fire_datetime = dt
        else:
            tomorrow = now + timedelta(days=1)
            result.fire_datetime = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

    # End date
    end_date_str = data.get("end_date")
    if end_date_str:
        try:
            result.end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # Advance days
    result.advance_days = data.get("advance_days", 0)

    return result


async def parse_reminder_async(text: str, user_tz: str = "Europe/Rome") -> ParsedReminder:
    """
    Parse reminder text: LLM first, regex fallback.
    This is the async version that tries Groq.
    """
    try:
        from services.llm import parse_with_llm
        llm_result = await parse_with_llm(text, user_tz)

        if llm_result and llm_result.get("title"):
            logger.info(f"LLM parsing succeeded for: {text[:50]}")
            return _llm_dict_to_parsed(llm_result, user_tz)
    except Exception as e:
        logger.warning(f"LLM parsing failed, using regex fallback: {e}")

    # Fallback to regex parser
    return parse_reminder(text, user_tz)


def parse_reminder(text: str, user_tz: str = "Europe/Rome") -> ParsedReminder:
    """Parse free-text Italian input into a structured reminder (regex-based)."""
    result = ParsedReminder()
    tz = pytz.timezone(user_tz)
    now = datetime.now(tz)

    # Normalize
    original = text.strip()
    lower = original.lower()

    # --- Remove common prefixes ---
    prefixes = [
        r"^ricordami\s+(di\s+)?", r"^ricorda(mi)?\s+(di\s+)?",
        r"^reminder\s+", r"^promemoria\s+",
    ]
    cleaned = lower
    for p in prefixes:
        cleaned = re.sub(p, "", cleaned, count=1)

    # --- Detect multiple times (e.g., "alle 10, 13, 16 e 19") ---
    multi_time_match = re.search(
        r"alle?\s+(\d{1,2}(?:[:.]\d{2})?(?:\s*[,e]\s*\d{1,2}(?:[:.]\d{2})?)+)",
        cleaned
    )
    if multi_time_match:
        times_str = multi_time_match.group(1)
        times_raw = re.split(r"\s*[,e]\s*", times_str)
        for t in times_raw:
            t = t.strip().replace(".", ":")
            if ":" not in t:
                t = f"{t}:00"
            if len(t.split(":")[0]) == 1:
                t = f"0{t}"
            result.fire_times.append(t)

        # Remove times from text to get the action
        cleaned = cleaned[:multi_time_match.start()] + cleaned[multi_time_match.end():]
        result.recurrence = RecurrenceType.DAILY

    # --- Detect recurrence ---
    recurrence_patterns = [
        (r"ogni\s+giorno", RecurrenceType.DAILY, None),
        (r"tutti\s+i\s+giorni", RecurrenceType.DAILY, None),
        (r"ogni\s+mattina", RecurrenceType.DAILY, None),
        (r"ogni\s+sera", RecurrenceType.DAILY, None),
        (r"ogni\s+lunedì", RecurrenceType.WEEKLY, "mon"),
        (r"ogni\s+martedì", RecurrenceType.WEEKLY, "tue"),
        (r"ogni\s+mercoledì", RecurrenceType.WEEKLY, "wed"),
        (r"ogni\s+giovedì", RecurrenceType.WEEKLY, "thu"),
        (r"ogni\s+venerdì", RecurrenceType.WEEKLY, "fri"),
        (r"ogni\s+sabato", RecurrenceType.WEEKLY, "sat"),
        (r"ogni\s+domenica", RecurrenceType.WEEKLY, "sun"),
        (r"ogni\s+mese", RecurrenceType.MONTHLY, None),
        (r"il\s+\d+\s+di\s+ogni\s+mese", RecurrenceType.MONTHLY, None),
        (r"a\s+giorni\s+alterni", RecurrenceType.EVERY_OTHER_DAY, None),
    ]

    for pattern, rec_type, days in recurrence_patterns:
        if re.search(pattern, cleaned):
            result.recurrence = rec_type
            if days:
                result.recurrence_days = days
            cleaned = re.sub(pattern, "", cleaned).strip()
            break

    # --- Detect "tra X ore/minuti" ---
    tra_match = re.search(r"tra\s+(\d+)\s*(or[ae]|minut[oi]|min|h)", cleaned)
    if tra_match:
        amount = int(tra_match.group(1))
        unit = tra_match.group(2)
        if unit.startswith("or") or unit == "h":
            result.fire_datetime = now + timedelta(hours=amount)
        else:
            result.fire_datetime = now + timedelta(minutes=amount)
        cleaned = cleaned[:tra_match.start()] + cleaned[tra_match.end():]

    # --- Use dateparser for date/time extraction ---
    if not result.fire_datetime:
        settings = {
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": user_tz,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PARSERS": ["relative-time", "absolute-time", "custom-formats"],
        }

        time_match = re.search(r"(?:alle?\s+)?(\d{1,2})[:.:](\d{2})", cleaned)
        single_time_match = re.search(r"alle?\s+(\d{1,2})(?!\d)", cleaned)

        parsed_date = dateparser.parse(cleaned, languages=["it"], settings=settings)

        if parsed_date:
            result.fire_datetime = parsed_date
        elif time_match:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            result.fire_datetime = target
        elif single_time_match:
            h = int(single_time_match.group(1))
            target = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            result.fire_datetime = target

    # --- Default times ---
    if not result.fire_datetime:
        tomorrow = now + timedelta(days=1)
        result.fire_datetime = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

    if result.fire_times and result.fire_datetime:
        first_time = result.fire_times[0]
        h, m = map(int, first_time.split(":"))
        result.fire_datetime = result.fire_datetime.replace(hour=h, minute=m, second=0)

    # --- Extract title (the remaining action text) ---
    title = cleaned
    time_fragments = [
        r"alle?\s+\d{1,2}([:.]\d{2})?",
        r"domani\s*(mattina|pomeriggio|sera)?",
        r"oggi\s*(mattina|pomeriggio|sera)?",
        r"stasera", r"stamattina",
        r"(lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)\s*(mattina|sera|pomeriggio)?",
        r"tra\s+\d+\s*(or[ae]|minut[oi]|min|h)",
        r"il\s+\d+\s*(di\s+)?\w*",
        r"(mattina|pomeriggio|sera)",
    ]
    for frag in time_fragments:
        title = re.sub(frag, "", title).strip()

    title = re.sub(r"\s+", " ", title).strip(" ,.-")

    if title:
        title = title[0].upper() + title[1:]

    result.title = title or original

    # --- Detect category from keywords ---
    cat_keywords = {
        ReminderCategory.MEDICINE: ["farmaco", "medicina", "pillola", "pastiglia", "integratore",
                                     "vitamina", "antibiotico", "compressa", "dose"],
        ReminderCategory.BIRTHDAY: ["compleanno", "auguri"],
        ReminderCategory.CAR: ["bollo", "tagliando", "assicurazione auto", "revisione", "benzina"],
        ReminderCategory.HOUSE: ["affitto", "bolletta", "condominio", "luce", "gas", "acqua"],
        ReminderCategory.HEALTH: ["dentista", "dottore", "medico", "visita", "analisi", "esame"],
        ReminderCategory.DOCUMENT: ["carta d'identità", "passaporto", "patente", "documenti", "730", "isee"],
        ReminderCategory.HABIT: ["bere acqua", "acqua", "meditare", "camminare", "palestra",
                                  "stretching", "leggere"],
    }
    for cat, keywords in cat_keywords.items():
        if any(kw in lower for kw in keywords):
            result.category = cat
            break

    return result


def format_confirmation(parsed: ParsedReminder) -> str:
    """Format a parsed reminder as a confirmation message."""
    return "\n".join(parsed.summary_lines())

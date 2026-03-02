"""
LLM service using Groq API (free tier) for natural language understanding.
- classify_and_parse: text + context → intent + structured data (Llama 3)
- transcribe_audio: audio bytes → text (Whisper Large v3)
Uses httpx for async HTTP calls (no blocking the event loop).
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx
import pytz

logger = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

SYSTEM_PROMPT = """Sei Svampito, un assistente per reminder su Telegram. Ricevi il messaggio dell'utente, la lista dei suoi reminder attivi, e opzionalmente il contesto di un reminder appena inviato.

Devi decidere cosa vuole l'utente e rispondere con un JSON.

Rispondi SOLO con un JSON valido, senza markdown, senza testo aggiuntivo.

INTENT POSSIBILI:

1. "create" — l'utente vuole creare un nuovo reminder
2. "query" — l'utente vuole sapere cosa ha in programma
3. "delete" — l'utente vuole cancellare un reminder
4. "modify" — l'utente vuole modificare/spostare un reminder
5. "done" — l'utente dice di aver fatto qualcosa (senza contesto reminder recente)
6. "reminder_reply" — l'utente sta rispondendo a un reminder appena ricevuto
7. "chat" — l'utente sta chiacchierando o salutando

SCHEMA RISPOSTA PER OGNI INTENT:

Per "create":
{
  "intent": "create",
  "data": {
    "title": "azione pulita, prima lettera maiuscola",
    "date": "YYYY-MM-DD o null",
    "time": "HH:MM o null",
    "recurrence": "once|daily|weekly|monthly|every_other_day",
    "recurrence_days": "mon,tue,wed,thu,fri,sat,sun o null",
    "fire_times": [],
    "category": "medicine|birthday|car|house|health|document|habit|generic",
    "end_date": "YYYY-MM-DD o null",
    "advance_days": 0
  }
}

Per "query":
{"intent": "query", "data": {"period": "today|tomorrow|week|all", "category": "all|medicine|health|...", "search": null}}

Per "delete":
{"intent": "delete", "data": {"reminder_id": 123, "title_match": "testo"}}

Per "modify":
{"intent": "modify", "data": {"reminder_id": 123, "title_match": "testo", "new_date": "YYYY-MM-DD o null", "new_time": "HH:MM o null"}}

Per "done":
{"intent": "done", "data": {"reminder_id": 123, "title_match": "testo"}}

Per "reminder_reply":
{"intent": "reminder_reply", "data": {"reminder_id": 123, "action": "done|snooze|skip|tomorrow", "snooze_minutes": 30}}

Per "chat":
{"intent": "chat", "data": {"response": "risposta breve e amichevole in italiano"}}

REGOLE PER REMINDER_REPLY (PRIORITA' MASSIMA):
Se nel contesto c'e' un "Reminder appena inviato" e il messaggio sembra una risposta, usa SEMPRE "reminder_reply".

Risposte contestuali:
- "fatto"/"presa"/"ok"/"si"/"gia fatto"/"l'ho fatto" → action="done"
- "tra 10 minuti"/"tra 5 min"/"tra mezz'ora" → action="snooze", snooze_minutes=10/5/30
- "tra un'ora"/"piu tardi" → action="snooze", snooze_minutes=60
- "dopo pranzo" → action="snooze", snooze_minutes=(calcola fino alle 13:30)
- "stasera" → action="snooze", snooze_minutes=(calcola fino alle 20:00)
- "salta"/"salta oggi"/"oggi no"/"skip" → action="skip"
- "domani"/"rimanda a domani" → action="tomorrow"
- "gia presa stamattina" → action="done"

Il reminder_id viene dal contesto. Per snooze calcola i minuti dall'ora corrente.

REGOLE PER CREATE:
1. TITOLO: solo l'azione, senza date/orari.
2. DATE RELATIVE: "domani"→+1, "dopodomani"→+2, "prossimo mercoledi"→prossimo futuro
3. ORARI: "verso le 10"→10:00, "mattina presto"→07:00, "a pranzo"→13:00, "stasera"→20:00
4. MULTI-ORARIO: "alle 8, 14 e 21" → fire_times=["08:00","14:00","21:00"], time=null
5. RICORRENZA:
   - "ogni giorno"/"tutti i giorni" → daily
   - "ogni lunedi" → weekly, days="mon"
   - "dal lunedi al venerdi"/"giorni feriali"/"lavorativi" → weekly, days="mon,tue,wed,thu,fri"
   - "nel weekend"/"sabato e domenica" → weekly, days="sat,sun"
   - "a giorni alterni" → every_other_day
   - "ogni mese" → monthly
   - Nessuna indicazione → once
6. CATEGORIE: medicine(farmaco,pillola,vitamina,antibiotico), health(dentista,medico,visita), car(meccanico,bollo,tagliando), house(affitto,bolletta), birthday(compleanno), document(passaporto,patente,730), habit(palestra,yoga,corsa), generic(resto)
7. ADVANCE_DAYS: document=90, car=30, house=5, health=3, birthday=3, altri=0

ESEMPI:

Input: "domani che devo fare?"
→ {"intent":"query","data":{"period":"tomorrow","category":"all","search":null}}

Input: "cancella il dentista"
→ {"intent":"delete","data":{"reminder_id":null,"title_match":"dentista"}}

Input: "sposta il dentista al 12 marzo"
→ {"intent":"modify","data":{"reminder_id":null,"title_match":"dentista","new_date":"2026-03-12","new_time":null}}

Input: "domani alle 10 dentista"
→ {"intent":"create","data":{"title":"Dentista","date":"[domani]","time":"10:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"health","end_date":null,"advance_days":0}}

Input: "dal lunedi al venerdi ore 9:15 e 14:15 avviare la fabbrica"
→ {"intent":"create","data":{"title":"Avviare la fabbrica","date":null,"time":null,"recurrence":"weekly","recurrence_days":"mon,tue,wed,thu,fri","fire_times":["09:15","14:15"],"category":"generic","end_date":null,"advance_days":0}}

Input: "ciao!"
→ {"intent":"chat","data":{"response":"Ciao! Come posso aiutarti? :)"}}

[Contesto: Reminder appena inviato: ID:42 "Antibiotico" (medicine)]
Input: "tra 10 minuti"
→ {"intent":"reminder_reply","data":{"reminder_id":42,"action":"snooze","snooze_minutes":10}}

[Contesto: Reminder appena inviato: ID:42 "Antibiotico" (medicine)]
Input: "presa"
→ {"intent":"reminder_reply","data":{"reminder_id":42,"action":"done","snooze_minutes":0}}

[Contesto: Reminder appena inviato: ID:15 "Palestra" (habit)]
Input: "oggi no"
→ {"intent":"reminder_reply","data":{"reminder_id":15,"action":"skip","snooze_minutes":0}}

[Contesto: Reminder appena inviato: ID:7 "Vitamina D" (medicine)]
Input: "dopo pranzo"
→ {"intent":"reminder_reply","data":{"reminder_id":7,"action":"snooze","snooze_minutes":90}}
"""


async def classify_and_parse(
    text: str,
    user_tz: str = "Europe/Rome",
    active_reminders: list[dict] | None = None,
    recent_reminder_ctx: dict | None = None,
) -> Optional[dict]:
    """
    Classify user intent and extract structured data.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip()

    if not api_key:
        logger.warning("GROQ_API_KEY not set, cannot classify intent")
        return None

    try:
        tz = pytz.timezone(user_tz)
        now = datetime.now(tz)
        day_names = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
        current_day = day_names[now.weekday()]

        # Build reminder list
        reminders_text = "Nessun reminder attivo."
        if active_reminders:
            lines = []
            for r in active_reminders:
                lines.append(
                    f"- ID:{r['id']} | {r['title']} | {r['category']} | "
                    f"prossimo: {r['next_fire']} | ricorrenza: {r['recurrence']}"
                )
            reminders_text = "\n".join(lines)

        # Build recent reminder context
        recent_text = ""
        if recent_reminder_ctx:
            recent_text = (
                f"\n\nReminder appena inviato all'utente: "
                f"ID:{recent_reminder_ctx['reminder_id']} "
                f"\"{recent_reminder_ctx['title']}\" "
                f"({recent_reminder_ctx['category']})"
                f"\nSe il messaggio sembra una risposta a questo reminder, "
                f"usa intent \"reminder_reply\"."
            )

        user_message = (
            f"Data e ora corrente: {current_day} {now.strftime('%d/%m/%Y %H:%M')} "
            f"(fuso: {user_tz})\n\n"
            f"Reminder attivi:\n{reminders_text}"
            f"{recent_text}\n\n"
            f"Messaggio utente: {text}"
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.1,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        ctx_label = " [reply-ctx]" if recent_reminder_ctx else ""
        logger.info(f"LLM{ctx_label}: '{text[:60]}' ({len(active_reminders or [])} reminders)")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GROQ_CHAT_URL, json=payload, headers=headers)

        if response.status_code != 200:
            logger.error(f"Groq error {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        response_text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"LLM response: {response_text[:300]}")

        parsed = json.loads(response_text)
        intent = parsed.get("intent")
        if not intent:
            logger.warning("LLM returned no intent")
            return None

        logger.info(f"LLM intent={intent}")
        return parsed

    except httpx.TimeoutException:
        logger.error("Groq API timeout")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"LLM invalid JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM error: {type(e).__name__}: {e}")
        return None


async def parse_with_llm(text: str, user_tz: str = "Europe/Rome") -> Optional[dict]:
    """Legacy parser for backward compat."""
    result = await classify_and_parse(text, user_tz, active_reminders=None)
    if result and result.get("intent") == "create":
        return result.get("data")
    return None


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> Optional[str]:
    """Transcribe audio using Groq Whisper API."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    whisper_model = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3").strip()

    if not api_key:
        logger.warning("GROQ_API_KEY not set, cannot transcribe")
        return None

    try:
        logger.info(f"Transcribing ({len(audio_bytes)} bytes) with {whisper_model}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                GROQ_AUDIO_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={"model": whisper_model, "language": "it", "response_format": "json"},
            )

        if response.status_code != 200:
            logger.error(f"Whisper error {response.status_code}: {response.text[:200]}")
            return None

        text = response.json().get("text", "").strip()
        if not text:
            logger.warning("Whisper returned empty transcription")
            return None

        logger.info(f"Transcription: '{text[:100]}'")
        return text

    except httpx.TimeoutException:
        logger.error("Whisper timeout")
        return None
    except Exception as e:
        logger.error(f"Whisper error: {type(e).__name__}: {e}")
        return None

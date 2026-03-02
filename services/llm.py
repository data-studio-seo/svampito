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

SYSTEM_PROMPT = """Sei Svampito, un assistente per reminder su Telegram. Ricevi il messaggio dell'utente e la lista dei suoi reminder attivi.

Devi decidere cosa vuole l'utente e rispondere con un JSON.

Rispondi SOLO con un JSON valido, senza markdown, senza testo aggiuntivo.

INTENT POSSIBILI:

1. "create" — l'utente vuole creare un nuovo reminder
2. "query" — l'utente vuole sapere cosa ha in programma (oggi, domani, settimana, ecc.)
3. "delete" — l'utente vuole cancellare un reminder esistente
4. "modify" — l'utente vuole modificare/spostare un reminder esistente
5. "done" — l'utente dice di aver fatto qualcosa
6. "chat" — l'utente sta chiacchierando, saluta, o fa una domanda generica

SCHEMA RISPOSTA:

Per "create":
{
  "intent": "create",
  "data": {
    "title": "azione da fare, pulita, prima lettera maiuscola",
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
{
  "intent": "query",
  "data": {
    "period": "today|tomorrow|week|all|custom",
    "category": "all|medicine|health|car|...",
    "search": "termine di ricerca o null"
  }
}

Per "delete":
{
  "intent": "delete",
  "data": {
    "reminder_id": 123,
    "title_match": "testo per trovare il reminder"
  }
}

Per "modify":
{
  "intent": "modify",
  "data": {
    "reminder_id": 123,
    "title_match": "testo per trovare il reminder",
    "new_date": "YYYY-MM-DD o null",
    "new_time": "HH:MM o null"
  }
}

Per "done":
{
  "intent": "done",
  "data": {
    "reminder_id": 123,
    "title_match": "testo per trovare il reminder"
  }
}

Per "chat":
{
  "intent": "chat",
  "data": {
    "response": "risposta breve e amichevole in italiano"
  }
}

REGOLE PER CREATE:

1. TITOLO: solo l'azione, senza date/orari/frequenze.
2. DATE RELATIVE: calcola basandoti sulla data corrente.
   - "domani" → +1 giorno, "dopodomani" → +2
   - "il prossimo mercoledì" → prossimo mercoledì futuro
   - "la prossima settimana" → prossimo lunedì
3. ORARI APPROSSIMATIVI: "verso le 10"→10:00, "mattina presto"→07:00, "a pranzo"→13:00, "stasera"→20:00
4. MULTI-ORARIO: "alle 8, 14 e 21" → fire_times=["08:00","14:00","21:00"], time=null
5. RICORRENZA:
   - "ogni giorno"/"tutti i giorni" → daily
   - "ogni lunedì" → weekly, days="mon"
   - "dal lunedì al venerdì"/"giorni feriali"/"lavorativi" → weekly, days="mon,tue,wed,thu,fri"
   - "nel weekend"/"sabato e domenica" → weekly, days="sat,sun"
   - "a giorni alterni" → every_other_day
   - "ogni mese"/"il 5 di ogni mese" → monthly
   - Nessuna indicazione → once
6. CATEGORIE: medicine(farmaco,pillola,vitamina), health(dentista,medico,visita), car(meccanico,bollo,tagliando,revisione), house(affitto,bolletta,condominio), birthday(compleanno), document(passaporto,patente,730,ISEE), habit(palestra,yoga,corsa), generic(resto)
7. ADVANCE_DAYS: document=90, car=30, house=5, health=3, birthday=3, altri=0

REGOLE PER QUERY:
- "che ho oggi?"/"cosa devo fare oggi?" → period="today"
- "domani che programmi ho?" → period="tomorrow"
- "questa settimana?" → period="week"
- "ho visite mediche?" → category="health"
- "quanti reminder ho?" → period="all"

REGOLE PER DELETE/MODIFY:
- Usa reminder_id se riesci a identificare il reminder dalla lista fornita
- Altrimenti usa title_match per cercare per testo
- "cancella il dentista" → cerca "dentista" nei reminder
- "sposta la palestra a giovedì" → modify con new_date

REGOLE PER DONE:
- "fatto il dentista" / "presa la medicina" / "ho fatto la spesa" → trova il reminder e segnalo come fatto

REGOLE PER CHAT:
- "ciao" / "come stai?" / "grazie" → rispondi in modo amichevole e breve
- Usa il tono di un assistente simpatico ma conciso

ESEMPI:

Input: "domani che devo fare?"
→ {"intent":"query","data":{"period":"tomorrow","category":"all","search":null}}

Input: "cancella il reminder del dentista"
→ {"intent":"delete","data":{"reminder_id":null,"title_match":"dentista"}}

Input: "sposta il dentista al 12 marzo"
→ {"intent":"modify","data":{"reminder_id":null,"title_match":"dentista","new_date":"2026-03-12","new_time":null}}

Input: "domani alle 10 dentista"
→ {"intent":"create","data":{"title":"Dentista","date":"[domani]","time":"10:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"health","end_date":null,"advance_days":0}}

Input: "presa la vitamina"
→ {"intent":"done","data":{"reminder_id":null,"title_match":"vitamina"}}

Input: "ciao!"
→ {"intent":"chat","data":{"response":"Ciao! Come posso aiutarti oggi? 😊"}}

Input: "dal lunedì al venerdì ore 9:15 e 14:15 avviare la fabbrica"
→ {"intent":"create","data":{"title":"Avviare la fabbrica","date":null,"time":null,"recurrence":"weekly","recurrence_days":"mon,tue,wed,thu,fri","fire_times":["09:15","14:15"],"category":"generic","end_date":null,"advance_days":0}}

Input: "ho visite questa settimana?"
→ {"intent":"query","data":{"period":"week","category":"health","search":null}}

Input: "questa settimana ho qualcosa?"
→ {"intent":"query","data":{"period":"week","category":"all","search":null}}
"""


# ─────────────────────────────────────────────
# Intent Classification + Parsing (Llama 3)
# ─────────────────────────────────────────────

async def classify_and_parse(
    text: str,
    user_tz: str = "Europe/Rome",
    active_reminders: list[dict] | None = None,
) -> Optional[dict]:
    """
    Classify user intent and extract structured data.
    
    Args:
        text: user message text
        user_tz: user timezone
        active_reminders: list of dicts with id, title, category, next_fire, recurrence
    
    Returns:
        dict with "intent" and "data" keys, or None on failure
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip()

    if not api_key:
        logger.warning("GROQ_API_KEY not set, cannot classify intent")
        return None

    try:
        tz = pytz.timezone(user_tz)
        now = datetime.now(tz)
        day_names = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        current_day = day_names[now.weekday()]

        # Build reminder context
        reminders_text = "Nessun reminder attivo."
        if active_reminders:
            lines = []
            for r in active_reminders:
                lines.append(
                    f"- ID:{r['id']} | {r['title']} | {r['category']} | "
                    f"prossimo: {r['next_fire']} | ricorrenza: {r['recurrence']}"
                )
            reminders_text = "\n".join(lines)

        user_message = (
            f"Data e ora corrente: {current_day} {now.strftime('%d/%m/%Y %H:%M')} "
            f"(fuso: {user_tz})\n\n"
            f"Reminder attivi dell'utente:\n{reminders_text}\n\n"
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

        logger.info(f"LLM classify: '{text[:60]}' with {len(active_reminders or [])} reminders")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GROQ_CHAT_URL, json=payload, headers=headers)

        if response.status_code != 200:
            logger.error(f"Groq API error {response.status_code}: {response.text[:200]}")
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


# ─────────────────────────────────────────────
# Legacy: parse only (for fallback / backward compat)
# ─────────────────────────────────────────────

async def parse_with_llm(text: str, user_tz: str = "Europe/Rome") -> Optional[dict]:
    """
    Legacy parser: text → reminder data only.
    Used as fallback when classify_and_parse is not appropriate.
    """
    result = await classify_and_parse(text, user_tz, active_reminders=None)
    if result and result.get("intent") == "create":
        return result.get("data")
    return None


# ─────────────────────────────────────────────
# Audio → Text (Whisper Large v3)
# ─────────────────────────────────────────────

async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> Optional[str]:
    """
    Transcribe audio using Groq Whisper API.
    Returns transcribed text or None if fails.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    whisper_model = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3").strip()

    if not api_key:
        logger.warning("GROQ_API_KEY not set, cannot transcribe audio")
        return None

    try:
        logger.info(f"Transcribing audio ({len(audio_bytes)} bytes) with {whisper_model}")

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        files = {
            "file": (filename, audio_bytes, "audio/ogg"),
        }
        form_data = {
            "model": whisper_model,
            "language": "it",
            "response_format": "json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                GROQ_AUDIO_URL,
                headers=headers,
                files=files,
                data=form_data,
            )

        if response.status_code != 200:
            logger.error(f"Whisper error {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        text = data.get("text", "").strip()

        if not text:
            logger.warning("Whisper returned empty transcription")
            return None

        logger.info(f"Transcription OK: '{text[:100]}'")
        return text

    except httpx.TimeoutException:
        logger.error("Whisper timeout after 30s")
        return None
    except Exception as e:
        logger.error(f"Whisper error: {type(e).__name__}: {e}")
        return None

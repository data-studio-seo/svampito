"""
LLM service using Groq API (free tier) for natural language understanding.
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

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# System prompt for extracting reminder data from Italian text
SYSTEM_PROMPT = """Sei Svampito, un assistente per reminder. Estrai i dati strutturati dal messaggio dell'utente italiano.

Rispondi SOLO con un JSON valido, senza markdown, senza testo aggiuntivo.

Il JSON deve avere questa struttura:
{
  "title": "titolo breve del reminder (cosa fare)",
  "date": "YYYY-MM-DD o null se non specificato",
  "time": "HH:MM o null se non specificato",
  "recurrence": "once|daily|weekly|monthly|every_other_day",
  "recurrence_days": "mon,tue,wed,thu,fri,sat,sun o null (solo per weekly)",
  "fire_times": ["HH:MM", "HH:MM"],
  "category": "medicine|birthday|car|house|health|document|habit|generic",
  "end_date": "YYYY-MM-DD o null",
  "advance_days": 0
}

Regole:
- Il "title" deve essere l'AZIONE da fare, pulita da date/orari. Prima lettera maiuscola.
- Se l'utente dice "domani", calcola la data corretta basandoti sulla data corrente.
- Se dice "il prossimo mercoledì", calcola la data del prossimo mercoledì.
- Se dice "ogni lunedì e giovedì", recurrence="weekly", recurrence_days="mon,thu"
- Se dice "alle 8, 14 e 21", fire_times=["08:00","14:00","21:00"], recurrence="daily"
- Se dice "tra 2 ore", calcola l'orario basandoti sull'ora corrente.
- Se dice "verso le 10", time="10:00"
- Rileva la categoria dal contesto (farmaci→medicine, dentista→health, bolletta→house, meccanico→car, ecc.)
- Se non c'è data, metti date=null
- Se non c'è orario, metti time=null
- fire_times è un array vuoto [] se c'è un solo orario (usa "time" per quello)
- advance_days: per scadenze documenti=90, auto=30, bollette=5, visite=3, compleanni=3, altri=0
"""


async def parse_with_llm(text: str, user_tz: str = "Europe/Rome") -> Optional[dict]:
    """
    Parse a reminder text using Groq LLM via async HTTP.
    Returns parsed dict or None if LLM is unavailable/fails.
    """
    # Read at call time, not import time!
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip()

    if not api_key:
        # Log all env vars starting with GROQ for debugging
        groq_vars = {k: v[:10] + "..." for k, v in os.environ.items() if "GROQ" in k.upper()}
        logger.warning(f"GROQ_API_KEY not set or empty. GROQ env vars found: {groq_vars}")
        logger.warning(f"All env var names: {sorted(os.environ.keys())}")
        return None

    try:
        # Build context with current date/time
        tz = pytz.timezone(user_tz)
        now = datetime.now(tz)
        day_names = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        current_day = day_names[now.weekday()]

        user_message = (
            f"Data e ora corrente: {current_day} {now.strftime('%d/%m/%Y %H:%M')} "
            f"(fuso: {user_tz})\n\n"
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

        logger.info(f"Calling Groq API with model={model}, key={api_key[:8]}... for: {text[:60]}")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GROQ_URL, json=payload, headers=headers)

        logger.info(f"Groq API response status: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"Groq API error {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        response_text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"LLM raw response: {response_text[:300]}")

        # Parse JSON
        parsed = json.loads(response_text)

        # Validate required fields
        if not parsed.get("title"):
            logger.warning("LLM returned empty title")
            return None

        logger.info(f"LLM parsing OK: title='{parsed.get('title')}', date={parsed.get('date')}, time={parsed.get('time')}")
        return parsed

    except httpx.TimeoutException:
        logger.error("Groq API timeout after 10s")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM parsing error: {type(e).__name__}: {e}")
        return None

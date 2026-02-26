"""
LLM service using Groq API (free tier) with Llama 3 for natural language understanding.
Parses Italian text into structured reminder data.
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

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
  "fire_times": ["HH:MM", "HH:MM"] o [] (per multi-orario tipo farmaci),
  "category": "medicine|birthday|car|house|health|document|habit|generic",
  "end_date": "YYYY-MM-DD o null",
  "advance_days": 0
}

Regole:
- Il "title" deve essere l'AZIONE da fare, pulita da date/orari. Prima lettera maiuscola.
- Se l'utente dice "domani", calcola la data corretta basandoti sulla data corrente.
- Se dice "ogni lunedì e giovedì", recurrence="weekly", recurrence_days="mon,thu"
- Se dice "alle 8, 14 e 21", fire_times=["08:00","14:00","21:00"], recurrence="daily"
- Se dice "tra 2 ore", calcola l'orario basandoti sull'ora corrente.
- Rileva la categoria dal contesto (farmaci, dentista, bolletta, ecc.)
- Se non c'è data, metti date=null (il sistema metterà domani alle 9)
- Se non c'è orario, metti time=null (il sistema metterà 09:00)
- advance_days: per scadenze documenti=90, auto=30, bollette=5, visite=3, compleanni=3, altri=0
"""


async def parse_with_llm(text: str, user_tz: str = "Europe/Rome") -> Optional[dict]:
    """
    Parse a reminder text using Groq LLM.
    Returns parsed dict or None if LLM is unavailable/fails.
    """
    if not GROQ_API_KEY:
        logger.debug("GROQ_API_KEY not set, skipping LLM parsing")
        return None

    try:
        from groq import Groq

        client = Groq(api_key=GROQ_API_KEY)

        # Build context with current date/time
        import pytz
        tz = pytz.timezone(user_tz)
        now = datetime.now(tz)
        day_names = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        current_day = day_names[now.weekday()]

        user_message = (
            f"Data e ora corrente: {current_day} {now.strftime('%d/%m/%Y %H:%M')} "
            f"(fuso: {user_tz})\n\n"
            f"Messaggio utente: {text}"
        )

        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            model=GROQ_MODEL,
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        response_text = chat_completion.choices[0].message.content.strip()
        logger.info(f"LLM response: {response_text}")

        # Parse JSON
        parsed = json.loads(response_text)

        # Validate required fields
        if not parsed.get("title"):
            logger.warning("LLM returned empty title")
            return None

        return parsed

    except ImportError:
        logger.warning("groq package not installed")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM parsing error: {e}")
        return None

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

SYSTEM_PROMPT = """Sei Svampito, un assistente per reminder. Estrai i dati strutturati dal messaggio dell'utente italiano.

Rispondi SOLO con un JSON valido, senza markdown, senza testo aggiuntivo.

Schema JSON:
{
  "title": "azione da fare, pulita da date/orari, prima lettera maiuscola",
  "date": "YYYY-MM-DD o null",
  "time": "HH:MM o null",
  "recurrence": "once|daily|weekly|monthly|every_other_day",
  "recurrence_days": "mon,tue,wed,thu,fri,sat,sun o null",
  "fire_times": [],
  "category": "medicine|birthday|car|house|health|document|habit|generic",
  "end_date": "YYYY-MM-DD o null",
  "advance_days": 0
}

REGOLE IMPORTANTI:

1. TITOLO: solo l'azione, senza date/orari/frequenze. "Ricordami di prendere l'antibiotico alle 8" → title="Prendere l'antibiotico"

2. DATE RELATIVE: calcola sempre basandoti sulla data corrente fornita.
   - "domani" → data corrente + 1 giorno
   - "dopodomani" → data corrente + 2 giorni
   - "il prossimo mercoledì" → il prossimo mercoledì futuro
   - "fra 3 giorni" → data corrente + 3 giorni
   - "la prossima settimana" → lunedì prossimo

3. ORARI APPROSSIMATIVI:
   - "verso le 10" → time="10:00"
   - "mattina presto" → time="07:00"
   - "a pranzo" → time="13:00"
   - "nel pomeriggio" → time="15:00"
   - "stasera" → time="20:00"
   - "prima di dormire" → time="22:00"

4. MULTI-ORARIO (fire_times): usalo SOLO quando ci sono più orari distinti nello stesso giorno.
   - "alle 8, 14 e 21" → fire_times=["08:00","14:00","21:00"], time=null
   - "alle 9:15 e 14:15" → fire_times=["09:15","14:15"], time=null
   - Se c'è un solo orario, usa "time" e lascia fire_times=[]

5. RICORRENZA - ATTENZIONE AI PATTERN ITALIANI:
   - "ogni giorno" / "tutti i giorni" → recurrence="daily"
   - "ogni mattina" / "ogni sera" → recurrence="daily"
   - "ogni lunedì" → recurrence="weekly", recurrence_days="mon"
   - "ogni lunedì e giovedì" → recurrence="weekly", recurrence_days="mon,thu"
   - "dal lunedì al venerdì" / "giorni feriali" / "nei giorni lavorativi" → recurrence="weekly", recurrence_days="mon,tue,wed,thu,fri"
   - "nel weekend" / "il fine settimana" / "sabato e domenica" → recurrence="weekly", recurrence_days="sat,sun"
   - "a giorni alterni" → recurrence="every_other_day"
   - "ogni mese" / "il 5 di ogni mese" → recurrence="monthly"
   - "3 volte a settimana, lun mer ven" → recurrence="weekly", recurrence_days="mon,wed,fri"
   - Se non c'è indicazione di ricorrenza → recurrence="once"

6. DURATA: se l'utente dice "per 7 giorni" o "per 2 settimane":
   - Calcola end_date = data inizio + durata

7. CATEGORIE - deduci dal contesto:
   - medicine: farmaco, medicina, pillola, pastiglia, integratore, vitamina, antibiotico, compressa
   - health: dentista, dottore, medico, visita, analisi, esame, oculista, dermatologo
   - car: meccanico, bollo, tagliando, assicurazione auto, revisione, gomme, officina
   - house: affitto, bolletta, condominio, luce, gas, acqua, TARI
   - birthday: compleanno, auguri, festa di
   - document: carta d'identità, passaporto, patente, documenti, 730, ISEE, rinnovo
   - habit: palestra, yoga, meditare, camminare, bere acqua, stretching, corsa, allenamento
   - generic: tutto il resto

8. ADVANCE_DAYS (anticipo promemoria):
   - document: 90
   - car: 30
   - house: 5
   - health: 3
   - birthday: 3
   - altri: 0

ESEMPI:

Input: "ricordami di prendere l'antibiotico alle 8, 14 e 21 per 7 giorni"
Output: {"title":"Prendere l'antibiotico","date":"[domani]","time":null,"recurrence":"daily","recurrence_days":null,"fire_times":["08:00","14:00","21:00"],"category":"medicine","end_date":"[domani+7gg]","advance_days":0}

Input: "compleanno di Marco il 15 aprile"
Output: {"title":"Compleanno di Marco","date":"2026-04-15","time":"09:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"birthday","end_date":null,"advance_days":3}

Input: "ogni lunedì e giovedì palestra alle 18:30"
Output: {"title":"Palestra","date":null,"time":"18:30","recurrence":"weekly","recurrence_days":"mon,thu","fire_times":[],"category":"habit","end_date":null,"advance_days":0}

Input: "dal lunedì al venerdì ore 9:15 e 14:15 avviare la fabbrica di lampadine"
Output: {"title":"Avviare la fabbrica di lampadine","date":null,"time":null,"recurrence":"weekly","recurrence_days":"mon,tue,wed,thu,fri","fire_times":["09:15","14:15"],"category":"generic","end_date":null,"advance_days":0}

Input: "domani mattina visita dal dentista"
Output: {"title":"Visita dal dentista","date":"[domani]","time":"09:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"health","end_date":null,"advance_days":0}

Input: "pagare l'affitto il 5 di ogni mese"
Output: {"title":"Pagare l'affitto","date":"[prossimo 5 del mese]","time":"09:00","recurrence":"monthly","recurrence_days":null,"fire_times":[],"category":"house","end_date":null,"advance_days":0}

Input: "bere 2 litri d'acqua tutti i giorni"
Output: {"title":"Bere 2 litri d'acqua","date":null,"time":"09:00","recurrence":"daily","recurrence_days":null,"fire_times":[],"category":"habit","end_date":null,"advance_days":0}

Input: "tra 2 ore chiamare il commercialista"
Output: {"title":"Chiamare il commercialista","date":"[oggi]","time":"[ora+2h]","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"generic","end_date":null,"advance_days":0}

Input: "la prossima settimana portare la macchina a fare il tagliando"
Output: {"title":"Portare la macchina a fare il tagliando","date":"[prossimo lunedì]","time":"09:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"car","end_date":null,"advance_days":0}

Input: "rinnovo passaporto scade il 20 giugno"
Output: {"title":"Rinnovo passaporto","date":"2026-06-20","time":"09:00","recurrence":"once","recurrence_days":null,"fire_times":[],"category":"document","end_date":null,"advance_days":90}
"""


async def parse_with_llm(text: str, user_tz: str = "Europe/Rome") -> Optional[dict]:
    """
    Parse a reminder text using Groq LLM via async HTTP.
    Returns parsed dict or None if LLM is unavailable/fails.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip()

    if not api_key:
        groq_vars = {k: v[:10] + "..." for k, v in os.environ.items() if "GROQ" in k.upper()}
        logger.warning(f"GROQ_API_KEY not set or empty. GROQ env vars found: {groq_vars}")
        return None

    try:
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

        logger.info(f"Calling Groq API with model={model} for: {text[:60]}")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GROQ_URL, json=payload, headers=headers)

        logger.info(f"Groq API response status: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"Groq API error {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        response_text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"LLM raw response: {response_text[:300]}")

        parsed = json.loads(response_text)

        if not parsed.get("title"):
            logger.warning("LLM returned empty title")
            return None

        logger.info(f"LLM parsing OK: title='{parsed.get('title')}', "
                     f"date={parsed.get('date')}, time={parsed.get('time')}, "
                     f"rec={parsed.get('recurrence')}, days={parsed.get('recurrence_days')}")
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

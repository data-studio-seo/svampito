import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Railway provides PORT for webhooks
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # e.g. https://nudgebot-xxx.up.railway.app

# Groq LLM (free tier)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

# Defaults
DEFAULT_WAKE_HOUR = 8
DEFAULT_SLEEP_HOUR = 23
DEFAULT_TIMEZONE = "Europe/Rome"

# Nudge intervals (minutes)
NUDGE_2_DELAY = 60      # 1 ora dopo il primo
NUDGE_3_DELAY = 180     # 3 ore dopo il primo
MEDICINE_NUDGE_DELAY = 30  # 30 min per farmaci
MAX_NUDGES = 3

"""
Svampito Mini App — Integration with Telegram Bot

This file modifies bot.py to:
1. Add a MenuButton that opens the Mini App
2. Run FastAPI (webapp_api.py) alongside the bot on the same port

Deploy on Railway:
- Same service, one container
- FastAPI serves the webapp + API on /
- Telegram bot runs via webhook on /webhook
"""
import logging
import os
from telegram import MenuButtonWebApp, WebAppInfo
from telegram.ext import Application

from config import BOT_TOKEN, PORT, WEBHOOK_URL

logger = logging.getLogger(__name__)

# The URL where the Mini App is hosted
# On Railway, this is the same as WEBHOOK_URL
WEBAPP_URL = os.environ.get("WEBAPP_URL", WEBHOOK_URL)


async def setup_webapp_button(app: Application):
    """Register the Mini App button in the Telegram chat menu."""
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL not set, skipping WebApp button setup")
        return

    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="📱 Apri Svampito",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
        logger.info(f"WebApp menu button set: {WEBAPP_URL}")
    except Exception as e:
        logger.error(f"Failed to set WebApp button: {e}")


# ─────────────────────────────────────────────
# Updated bot.py main() to integrate FastAPI
# ─────────────────────────────────────────────

"""
In bot.py, replace the run_webhook section with this:

1. Add to imports at top:
   from webapp_api import app as fastapi_app
   from webapp_integration import setup_webapp_button

2. In post_init, add:
   await setup_webapp_button(application)

3. Replace the webhook start with combined ASGI:

   if WEBHOOK_URL:
       import uvicorn
       from starlette.routing import Mount
       
       # Mount telegram webhook under FastAPI
       @fastapi_app.post("/webhook")
       async def telegram_webhook(request):
           from starlette.requests import Request
           update = await request.json()
           await application.update_queue.put(
               Update.de_json(update, application.bot)
           )
           return {"ok": True}
       
       # Initialize bot
       await application.initialize()
       await application.start()
       await application.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
       
       # Run FastAPI (serves both API + webhook)
       uvicorn.run(fastapi_app, host="0.0.0.0", port=PORT)
"""


# ─────────────────────────────────────────────
# Updated bot.py (full replacement)
# ─────────────────────────────────────────────

BOT_PY_CODE = '''
"""
NudgeBot — Il reminder Telegram che non ti molla.
Main entry point with Mini App integration.
"""
import asyncio
import logging
import os

from fastapi import Request
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
import uvicorn

from config import BOT_TOKEN, PORT, WEBHOOK_URL
from database import init_db
from handlers.start import get_onboarding_handler
from handlers.commands import (
    cmd_oggi, cmd_domani, cmd_settimana, cmd_lista, cmd_farmaci,
    cmd_scadenze, cmd_fatto, cmd_cancella, cmd_silenzio, cmd_timezone,
    cmd_impostazioni, cmd_export, cmd_help,
    tz_callback, settings_callback,
)
from handlers.callbacks import handle_callback, handle_snooze_week
from handlers.reminders import (
    handle_text, handle_reminder_callback, handle_time_edit, handle_voice
)
from services.scheduler import init_scheduler
from webapp_api import app as fastapi_app
from webapp_integration import setup_webapp_button

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def text_router(update, context):
    """Route text messages."""
    if context.user_data.get("editing_time"):
        handled = await handle_time_edit(update, context)
        if handled:
            return
    await handle_text(update, context)


def main():
    """Start the bot with Mini App API."""
    app = Application.builder().token(BOT_TOKEN).build()

    # --- Handlers ---
    app.add_handler(get_onboarding_handler(), group=0)

    # Commands
    for cmd, handler in [
        ("oggi", cmd_oggi), ("domani", cmd_domani), ("settimana", cmd_settimana),
        ("lista", cmd_lista), ("farmaci", cmd_farmaci), ("scadenze", cmd_scadenze),
        ("fatto", cmd_fatto), ("cancella", cmd_cancella), ("silenzio", cmd_silenzio),
        ("timezone", cmd_timezone), ("impostazioni", cmd_impostazioni),
        ("export", cmd_export), ("help", cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, handler), group=1)

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern=r"^rem:"), group=1)
    app.add_handler(CallbackQueryHandler(tz_callback, pattern=r"^tz:"), group=1)
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^settings:"), group=1)
    app.add_handler(CallbackQueryHandler(handle_snooze_week, pattern=r"^snooze_week:"), group=1)
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(done|snooze30|snooze60|tomorrow|skip|cancel):"), group=1)

    # Voice & Text
    app.add_handler(MessageHandler(filters.VOICE, handle_voice), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=3)

    # --- Post-init ---
    async def post_init(application):
        await init_db()
        init_scheduler(application.bot)
        await setup_webapp_button(application)
        logger.info("Database + Scheduler + WebApp button initialized")

    app.post_init = post_init

    # --- Start ---
    if WEBHOOK_URL:
        # Combined mode: FastAPI serves API + Telegram webhook
        logger.info(f"Starting combined FastAPI + Telegram webhook on port {PORT}")

        # Telegram webhook endpoint on FastAPI
        @fastapi_app.post("/webhook")
        async def telegram_webhook(request: Request):
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.update_queue.put(update)
            return {"ok": True}

        async def start_combined():
            # Initialize telegram bot
            await app.initialize()
            await app.start()
            await app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
            logger.info(f"Webhook set: {WEBHOOK_URL}/webhook")

            # Run FastAPI with uvicorn
            config = uvicorn.Config(
                fastapi_app, host="0.0.0.0", port=PORT,
                log_level="info",
            )
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(start_combined())
    else:
        logger.info("Starting polling mode (no Mini App API)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
'''

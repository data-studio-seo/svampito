"""
NudgeBot — Il reminder Telegram che non ti molla.
Main entry point.
"""
import logging
import asyncio
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

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
from handlers.reminders import handle_text, handle_reminder_callback, handle_time_edit
from services.scheduler import init_scheduler

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def text_router(update, context):
    """Route text messages: check if editing time, otherwise handle as new reminder."""
    if context.user_data.get("editing_time"):
        handled = await handle_time_edit(update, context)
        if handled:
            return
    await handle_text(update, context)


def main():
    """Start the bot."""
    app = Application.builder().token(BOT_TOKEN).build()

    # --- Handlers (order matters!) ---

    # 1. Onboarding conversation (highest priority)
    app.add_handler(get_onboarding_handler(), group=0)

    # 2. Commands
    app.add_handler(CommandHandler("oggi", cmd_oggi), group=1)
    app.add_handler(CommandHandler("domani", cmd_domani), group=1)
    app.add_handler(CommandHandler("settimana", cmd_settimana), group=1)
    app.add_handler(CommandHandler("lista", cmd_lista), group=1)
    app.add_handler(CommandHandler("farmaci", cmd_farmaci), group=1)
    app.add_handler(CommandHandler("scadenze", cmd_scadenze), group=1)
    app.add_handler(CommandHandler("fatto", cmd_fatto), group=1)
    app.add_handler(CommandHandler("cancella", cmd_cancella), group=1)
    app.add_handler(CommandHandler("silenzio", cmd_silenzio), group=1)
    app.add_handler(CommandHandler("timezone", cmd_timezone), group=1)
    app.add_handler(CommandHandler("impostazioni", cmd_impostazioni), group=1)
    app.add_handler(CommandHandler("export", cmd_export), group=1)
    app.add_handler(CommandHandler("help", cmd_help), group=1)

    # 3. Callback queries
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern=r"^rem:"), group=1)
    app.add_handler(CallbackQueryHandler(tz_callback, pattern=r"^tz:"), group=1)
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^settings:"), group=1)
    app.add_handler(CallbackQueryHandler(handle_snooze_week, pattern=r"^snooze_week:"), group=1)
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(done|snooze30|snooze60|tomorrow|skip|cancel):"), group=1)

    # 4. Free text (lowest priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=2)

    # --- Post-init: DB + Scheduler ---
    async def post_init(application):
        await init_db()
        init_scheduler(application.bot)
        logger.info("Database initialized, scheduler started")

    app.post_init = post_init

    # --- Start ---
    if WEBHOOK_URL:
        logger.info(f"Starting webhook on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
        )
    else:
        logger.info("Starting polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

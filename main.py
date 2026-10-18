from pyrogram import Client, idle
from config import API_ID, API_HASH, BOT_TOKEN, APP_TITLE, allowed_ids
from handlers import register_handlers

from logger import setup_logger
from patch import apply_patches

logger = setup_logger("main")

# Apply non-blocking Pyrogram save_file patch
apply_patches()

# Initialize Pyrogram client
app = Client(APP_TITLE, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Register all message and callback handlers
register_handlers(app)


def run_bot():
    logger.info("Initializing and starting bot...")
    app.start()
    me = app.get_me()
    logger.info(f"Bot started successfully as @{me.username or me.id} (ID: {me.id})")
    for uid in allowed_ids:
        try:
            app.send_message(uid, "🟢 Bot started successfully")
            logger.debug(f"Startup message sent to allowed user {uid}")
        except Exception as e:
            logger.warning(f"Could not send startup message to {uid}: {e}")
    logger.info("Bot is running and listening for updates (idle)...")
    idle()
    logger.info("Stopping bot...")
    app.stop()
    logger.info("Bot stopped cleanly.")


def main():
    run_bot()


if __name__ == "__main__":
    main()

from pyrogram import Client, idle
from config import API_ID, API_HASH, BOT_TOKEN, APP_TITLE, allowed_ids
from handlers import register_handlers

# Initialize Pyrogram client
app = Client(APP_TITLE, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Register all message and callback handlers
register_handlers(app)


def run_bot():
    app.start()
    me = app.get_me()
    print(f"Bot started as @{me.username or me.id}")
    for uid in allowed_ids:
        try:
            app.send_message(uid, "🟢 Bot started successfully")
        except Exception as e:
            print(f"Could not send startup message to {uid}: {e}")
    idle()
    app.stop()


def main():
    run_bot()


if __name__ == "__main__":
    main()

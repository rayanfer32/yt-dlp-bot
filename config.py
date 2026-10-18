import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

API_ID = os.getenv("API_ID") or None
if API_ID and API_ID.isdigit():
    API_ID = int(API_ID)
API_HASH = os.getenv("API_HASH") or None
BOT_TOKEN = os.getenv("BOT_TOKEN") or None
APP_TITLE = os.getenv("APP_TITLE") or "yt_dlp_bot"
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

# Allowed user IDs from environment (comma-separated, e.g. ALLOWED_IDS=123456,789012)
allowed_ids_raw = os.getenv("ALLOWED_IDS", "")
allowed_ids = [int(x.strip()) for x in allowed_ids_raw.split(",") if x.strip().isdigit()]


def is_user_allowed(user_id):
    if not allowed_ids:
        return True
    return user_id in allowed_ids

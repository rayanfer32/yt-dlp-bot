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
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "normal").lower()
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

# Allowed user IDs from environment (comma-separated, e.g. ALLOWED_IDS=123456,789012)
allowed_ids_raw = os.getenv("ALLOWED_IDS", "")
allowed_ids = [int(x.strip()) for x in allowed_ids_raw.split(",") if x.strip().isdigit()]

# Cookies file configuration (defaults to cookies.txt in project root if present, or configured via COOKIES_FILE)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
cookies_env = os.getenv("COOKIES_FILE", "cookies.txt")
COOKIES_FILE = (
    cookies_env
    if os.path.isabs(cookies_env)
    else os.path.join(BASE_DIR, cookies_env)
)


def get_cookie_file():
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    return None


def is_user_allowed(user_id):
    if not allowed_ids:
        return True
    return user_id in allowed_ids


from concurrent.futures import ThreadPoolExecutor
from config import MAX_CONCURRENT_DOWNLOADS
from logger import setup_logger

logger = setup_logger("state")

# Thread pool executor for concurrent downloads
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)


def submit_task(fn, *args, **kwargs):
    """
    Submits a task to the ThreadPoolExecutor and attaches a done callback
    to ensure any unhandled exception is logged immediately.
    """
    future = executor.submit(fn, *args, **kwargs)

    def _on_done(fut):
        try:
            exc = fut.exception()
            if exc:
                logger.critical(f"Task crashed in thread pool with unhandled exception: {exc}", exc_info=exc)
        except Exception as e:
            logger.error(f"Error checking future exception: {e}")

    future.add_done_callback(_on_done)
    return future

# link_id -> {"link": str, "title": str, "formats": [(btn_text, format_id), ...]}
links_store = {}

# task_id -> {"cancelled": bool, "title": str, "chat_id": int, "folder": str}
active_tasks = {}

# retry_id -> {"link": str, "format_id": str, "title": str, "link_id": str}
retry_downloads = {}

# user_id -> {"link_id": str, "retry_id": str, "prompt_msg_id": int, "chat_id": int, "message_id": int}
pending_renames = {}

# user_id -> "stream" | "normal"
user_modes = {}

from config import DEFAULT_MODE

def get_user_mode(user_id):
    return user_modes.get(user_id, DEFAULT_MODE)

def set_user_mode(user_id, mode):
    user_modes[user_id] = mode


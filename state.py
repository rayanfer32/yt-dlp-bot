from concurrent.futures import ThreadPoolExecutor
from config import MAX_CONCURRENT_DOWNLOADS

# Thread pool executor for concurrent downloads
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# link_id -> {"link": str, "title": str, "formats": [(btn_text, format_id), ...]}
links_store = {}

# task_id -> {"cancelled": bool, "title": str, "chat_id": int, "folder": str}
active_tasks = {}

# retry_id -> {"link": str, "format_id": str, "title": str, "link_id": str}
retry_downloads = {}

# user_id -> {"link_id": str, "retry_id": str, "prompt_msg_id": int, "chat_id": int, "message_id": int}
pending_renames = {}

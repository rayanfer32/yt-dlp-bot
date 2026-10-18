#v1.0
import os
import subprocess
import re
import time
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from pyrogram.errors import FloodWait, MessageNotModified

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

# Create a Client instance with your API ID and API hash
app = Client(APP_TITLE, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Thread pool executor for concurrent downloads
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# link_id -> {"link": str, "title": str, "formats": [(btn_text, format_id), ...]}
links_store = {}

# task_id -> {"cancelled": bool, "title": str, "chat_id": int, "folder": str}
active_tasks = {}

# retry_id -> {"link": str, "format_id": str, "title": str, "link_id": str}
retry_downloads = {}

def is_user_allowed(user_id):
    if not allowed_ids:
        return True
    return user_id in allowed_ids

def format_size(bytes_val):
    if not bytes_val or bytes_val <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    val = float(bytes_val)
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.2f} {units[idx]}"

def format_time(seconds):
    if not seconds or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def make_progress_bar(percent, length=10):
    percent = max(0.0, min(100.0, percent))
    filled = int(round(length * percent / 100))
    bar = "■" * filled + "□" * (length - filled)
    return bar

# Define the /start command handler
@app.on_message(filters.command("start"))
def start(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if not is_user_allowed(user_id):
        return message.reply_text("You are not allowed to use this bot.", quote=True)
    message.reply_text("👋 Send me any video link or direct download URL to get started!", quote=True)

# Handler for editing video caption by replying to the video message
@app.on_message(filters.command("caption"))
def edit_caption(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id

    if not is_user_allowed(user_id):
        return

    reply = message.reply_to_message
    if not reply:
        info_msg = message.reply_text("⚠️ Please reply to a video message with `/caption <new caption>`.", quote=True)
        time.sleep(3)
        try:
            info_msg.delete()
            message.delete()
        except Exception:
            pass
        return

    if not (reply.video or reply.document or reply.audio or reply.animation or reply.photo):
        info_msg = message.reply_text("⚠️ The replied message does not contain a video or media file.", quote=True)
        time.sleep(3)
        try:
            info_msg.delete()
            message.delete()
        except Exception:
            pass
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        info_msg = message.reply_text("⚠️ Please provide the new caption. Usage: `/caption <new caption>`", quote=True)
        time.sleep(3)
        try:
            info_msg.delete()
            message.delete()
        except Exception:
            pass
        return

    new_caption = parts[1].strip()

    try:
        reply.edit_caption(new_caption)
        confirm_msg = message.reply_text("✅ Caption updated!", quote=True)
        time.sleep(3)
        try:
            confirm_msg.delete()
            message.delete()
        except Exception:
            pass
    except Exception as e:
        err_msg = message.reply_text(f"❌ Failed to update caption: {e}", quote=True)
        time.sleep(4)
        try:
            err_msg.delete()
            message.delete()
        except Exception:
            pass

def process_link(client, chat_id, link, msg_to_edit, link_id=None):
    if not link_id:
        link_id = uuid.uuid4().hex[:8]

    if link_id not in links_store:
        links_store[link_id] = {
            "link": link,
            "title": "",
            "formats": []
        }
    else:
        links_store[link_id]["link"] = link

    ydl_opts = {
        'quiet': True,
        'no_warnings': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(link, download=False)
            formats = info_dict.get('formats') or [info_dict]
            title = info_dict.get('title') or 'Video'
    except Exception as e:
        err_str = str(e)
        if len(err_str) > 3000:
            err_str = err_str[:3000] + "... (truncated)"
        retry_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry", callback_data=f"retry_link:{link_id}")],
            [InlineKeyboardButton("❌ Dismiss", callback_data=f"dismiss:{link_id}")]
        ])
        try:
            msg_to_edit.edit_text(
                f"❌ **Error fetching link info:**\n`{err_str}`",
                reply_markup=retry_markup
            )
        except Exception as edit_err:
            print(f"Edit error on link fetch failure: {edit_err}")
        return

    # Extracting available formats
    format_items = []
    seen_texts = set()
    for f in formats:
        format_id = f.get('format_id') or 'best'
        # Skip storyboard and manifest-only formats
        if str(format_id).startswith('sb') or f.get('ext') == 'mhtml':
            continue

        resolution = f.get('format_note') or (f"{f['height']}p" if f.get('height') else None) or (f['resolution'] if f.get('resolution') and f['resolution'] != 'unknown' else None)
        if not resolution or resolution == 'unknown':
            ext = f.get('ext') or 'media'
            resolution = f"Direct ({ext.upper()})"

        protocol = f.get('protocol', 'Unknown Proto')
        button_text = f"{resolution} ({protocol})"
        if button_text not in seen_texts:
            seen_texts.add(button_text)
            format_items.append((button_text, format_id))

    if not format_items:
        format_items.append(("Best / Default", "best"))

    links_store[link_id]["title"] = title
    links_store[link_id]["formats"] = format_items

    # Build inline keyboard buttons (2 per row)
    inline_keyboard = []
    current_row = []
    for idx, (btn_text, _) in enumerate(format_items):
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"dl:{link_id}:{idx}"))
        if len(current_row) == 2:
            inline_keyboard.append(current_row)
            current_row = []
    if current_row:
        inline_keyboard.append(current_row)

    inline_keyboard.append([InlineKeyboardButton("❌ Dismiss", callback_data=f"dismiss:{link_id}")])

    try:
        msg_to_edit.edit_text(
            f"🎬 **{title}**\n\nChoose resolution / quality to download:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard)
        )
    except Exception as edit_err:
        print(f"Edit error on formats display: {edit_err}")

# Handler for receiving a message with a link
@app.on_message(filters.regex(r'https?://[^\s]+'))
def handle_link(client, message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    if not is_user_allowed(user_id):
        return

    link_match = re.search(r'https?://[^\s]+', message.text)
    link = link_match.group(0) if link_match else message.text.strip()
    wait_msg = message.reply_text("🔍 Fetching video info...", quote=True)
    process_link(client, chat_id, link, wait_msg)

# Callback query handler for interactive buttons
@app.on_callback_query()
def handle_callback(client, callback_query):
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    user_id = callback_query.from_user.id if callback_query.from_user else chat_id

    if not is_user_allowed(user_id):
        callback_query.answer("You are not authorized to use this bot.", show_alert=True)
        return

    if data.startswith("dismiss"):
        try:
            callback_query.message.delete()
        except Exception:
            pass
        callback_query.answer("Dismissed.")
        return

    if data.startswith("retry_link:"):
        _, link_id = data.split(":", 1)
        link_info = links_store.get(link_id)
        if not link_info or not link_info.get("link"):
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        callback_query.answer("🔄 Retrying...")
        try:
            callback_query.message.edit_text("🔍 Retrying: Fetching video info...")
        except Exception:
            pass

        process_link(client, chat_id, link_info["link"], callback_query.message, link_id=link_id)
        return

    if data.startswith("retry_dl:"):
        _, retry_id = data.split(":", 1)
        retry_info = retry_downloads.get(retry_id)
        if not retry_info:
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        link = retry_info["link"]
        format_id = retry_info["format_id"]
        title = retry_info["title"]
        saved_link_id = retry_info.get("link_id")

        callback_query.answer("🔄 Retrying download...")

        # Create unique task for this download
        task_id = uuid.uuid4().hex[:8]
        task_folder = f"downloads/{chat_id}/{task_id}"
        os.makedirs(task_folder, exist_ok=True)

        active_tasks[task_id] = {
            "cancelled": False,
            "title": title,
            "chat_id": chat_id,
            "folder": task_folder
        }

        cancel_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel:{task_id}")
        ]])

        try:
            callback_query.message.edit_text(
                f"⏳ **Starting download:** {title[:40]}...\n`[□□□□□□□□□□]` **0.0%**",
                reply_markup=cancel_markup
            )
        except Exception:
            pass

        executor.submit(
            run_download_task,
            client,
            chat_id,
            task_id,
            link,
            format_id,
            title,
            task_folder,
            callback_query.message,
            saved_link_id
        )
        return

    if data.startswith("cancel:"):
        _, task_id = data.split(":", 1)
        task = active_tasks.get(task_id)
        if task:
            task["cancelled"] = True
            callback_query.answer("Download cancelled.")
            try:
                callback_query.message.edit_text(f"🚫 **Download cancelled by user.**\n`{task.get('title', 'Video')}`")
            except Exception as e:
                print(f"Cancel edit error: {e}")
        else:
            callback_query.answer("Download not found or already finished.", show_alert=True)
        return

    if data.startswith("dl:"):
        try:
            _, link_id, idx_str = data.split(":", 2)
            idx = int(idx_str)
        except Exception:
            callback_query.answer("Invalid request.", show_alert=True)
            return

        link_info = links_store.get(link_id)
        if not link_info or idx >= len(link_info["formats"]):
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        btn_text, format_id = link_info["formats"][idx]
        link = link_info["link"]
        title = link_info["title"]

        callback_query.answer(f"Starting {btn_text}...")

        # Create unique task for this download
        task_id = uuid.uuid4().hex[:8]
        task_folder = f"downloads/{chat_id}/{task_id}"
        os.makedirs(task_folder, exist_ok=True)

        active_tasks[task_id] = {
            "cancelled": False,
            "title": title,
            "chat_id": chat_id,
            "folder": task_folder
        }

        cancel_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel:{task_id}")
        ]])

        status_msg = callback_query.message.reply_text(
            f"⏳ **Starting download:** {title[:40]}...\n`[□□□□□□□□□□]` **0.0%**",
            reply_markup=cancel_markup,
            quote=True
        )

        # Submit task to thread pool for concurrent execution
        executor.submit(
            run_download_task,
            client,
            chat_id,
            task_id,
            link,
            format_id,
            title,
            task_folder,
            status_msg,
            link_id
        )

def run_download_task(client, chat_id, task_id, link, format_id, title, task_folder, status_msg, link_id=None):
    progress_state = {
        'last_update': 0,
        'last_percent': -1,
        'start_time': time.time()
    }

    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel:{task_id}")
    ]])

    def check_task_cancel():
        if active_tasks.get(task_id, {}).get("cancelled"):
            raise Exception("Download cancelled by user")

    def ydl_progress_hook(d):
        check_task_cancel()
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total > 0 else 0.0

            now = time.time()
            time_diff = now - progress_state['last_update']
            percent_diff = abs(percent - progress_state['last_percent'])

            # Throttle to ~1.2s or >= 5% change (with minimum 0.7s)
            if progress_state['last_update'] != 0 and time_diff < 1.2 and percent_diff < 5.0 and downloaded != total:
                return
            if progress_state['last_update'] != 0 and time_diff < 0.7:
                return

            speed = d.get('speed') or 0
            eta = d.get('eta')
            bar = make_progress_bar(percent)

            total_str = format_size(total) if total > 0 else "Unknown"
            speed_str = f"{format_size(speed)}/s" if speed else "Calculating..."
            eta_str = format_time(eta) if eta else "--:--"

            text = (
                f"📥 **Downloading:** {title[:35]}\n"
                f"`[{bar}]` **{percent:.1f}%**\n\n"
                f"📦 **Size:** `{format_size(downloaded)}` / `{total_str}`\n"
                f"⚡ **Speed:** `{speed_str}`\n"
                f"⏱️ **ETA:** `{eta_str}`"
            )

            try:
                status_msg.edit_text(text, reply_markup=cancel_markup)
                progress_state['last_update'] = now
                progress_state['last_percent'] = percent
            except FloodWait as fw:
                time.sleep(fw.value)
            except MessageNotModified:
                pass
            except Exception as e:
                print(f"[{task_id}] Download progress edit error: {e}")

        elif d.get('status') == 'finished':
            try:
                status_msg.edit_text(
                    f"⚙️ **Processing video file:** {title[:35]}...\n`[■■■■■■■■■■]` **100%**",
                    reply_markup=cancel_markup
                )
            except Exception as e:
                print(f"[{task_id}] Finished edit error: {e}")

    try:
        ydl_opts = {
            'format': format_id,
            'outtmpl': f'{task_folder}/%(title)s.%(ext)s',
            'progress_hooks': [ydl_progress_hook],
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])

        check_task_cancel()

        # Find the downloaded file in the isolated task folder
        files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
        if not files:
            status_msg.edit_text("❌ Downloaded file not found.")
            return

        full_path = files[0]
        file_size = os.path.getsize(full_path)

        # Check if file size exceeds 2GB (standard Telegram limit)
        if file_size > 2 * 1024 * 1024 * 1024:
            status_msg.edit_text("❌ Error: The downloaded video is too large to send via Telegram (> 2GB).")
            return

        # Uploading phase
        upload_state = {
            'last_update': 0,
            'last_percent': -1,
            'start_time': time.time()
        }

        try:
            status_msg.edit_text(
                f"📤 **Uploading to Telegram:** {title[:35]}\n`[□□□□□□□□□□]` **0.0%**"
            )
        except Exception as e:
            print(f"[{task_id}] Initial upload edit error: {e}")

        def upload_progress_hook(current, total):
            check_task_cancel()
            percent = (current / total * 100) if total > 0 else 0.0
            now = time.time()
            time_diff = now - upload_state['last_update']
            percent_diff = abs(percent - upload_state['last_percent'])

            if upload_state['last_update'] != 0 and time_diff < 1.2 and percent_diff < 5.0 and current != total:
                return
            if upload_state['last_update'] != 0 and time_diff < 0.7 and current != total:
                return

            elapsed = now - upload_state['start_time']
            speed = current / elapsed if elapsed > 0 else 0
            remaining_bytes = max(0, total - current)
            eta = remaining_bytes / speed if speed > 0 else 0
            bar = make_progress_bar(percent)

            speed_str = f"{format_size(speed)}/s" if speed else "Calculating..."
            eta_str = format_time(eta) if eta else "--:--"

            text = (
                f"📤 **Uploading:** {title[:35]}\n"
                f"`[{bar}]` **{percent:.1f}%**\n\n"
                f"📦 **Size:** `{format_size(current)}` / `{format_size(total)}`\n"
                f"⚡ **Speed:** `{speed_str}`\n"
                f"⏱️ **ETA:** `{eta_str}`"
            )

            try:
                status_msg.edit_text(text)
                upload_state['last_update'] = now
                upload_state['last_percent'] = percent
            except FloodWait as fw:
                time.sleep(fw.value)
            except MessageNotModified:
                pass
            except Exception as e:
                print(f"[{task_id}] Upload progress edit error: {e}")

        # Send the video file
        client.send_video(
            chat_id=chat_id,
            video=full_path,
            caption=title,
            progress=upload_progress_hook
        )

        try:
            status_msg.edit_text(f"✅ **Completed:** {title[:40]}")
            time.sleep(2)
            status_msg.delete()
        except Exception as e:
            print(f"[{task_id}] Cleanup edit error: {e}")

    except Exception as e:
        if "Download cancelled by user" in str(e):
            print(f"[{task_id}] Task cancelled cleanly.")
        else:
            print(f"[{task_id}] Download task error: {e}")
            try:
                retry_id = uuid.uuid4().hex[:8]
                retry_downloads[retry_id] = {
                    "link": link,
                    "format_id": format_id,
                    "title": title,
                    "link_id": link_id
                }
                buttons = [
                    [InlineKeyboardButton("🔄 Retry", callback_data=f"retry_dl:{retry_id}")]
                ]
                if link_id and link_id in links_store:
                    buttons.append([InlineKeyboardButton("🔗 Re-fetch Formats", callback_data=f"retry_link:{link_id}")])
                buttons.append([InlineKeyboardButton("❌ Dismiss", callback_data=f"dismiss:{retry_id}")])

                err_str = str(e)
                if len(err_str) > 3000:
                    err_str = err_str[:3000] + "... (truncated)"
                status_msg.edit_text(
                    f"❌ **Error downloading video:**\n`{err_str}`",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except Exception as edit_err:
                print(f"[{task_id}] Failed to edit error message: {edit_err}")
    finally:
        # Clean up task directory and memory
        shutil.rmtree(task_folder, ignore_errors=True)
        active_tasks.pop(task_id, None)

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

# Run the bot
if __name__ == "__main__":
    run_bot()

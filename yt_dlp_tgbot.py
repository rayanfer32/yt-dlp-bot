#v1.0
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from pyrogram.errors import FloodWait, MessageNotModified
import yt_dlp
import os
import subprocess
import re
import time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

API_ID = os.getenv("API_ID") or None
if API_ID and API_ID.isdigit():
    API_ID = int(API_ID)
API_HASH = os.getenv("API_HASH") or None
BOT_TOKEN = os.getenv("BOT_TOKEN") or None
APP_TITLE = os.getenv("APP_TITLE") or "yt_dlp_bot"

# Allowed user IDs from environment (comma-separated, e.g. ALLOWED_IDS=123456,789012)
allowed_ids_raw = os.getenv("ALLOWED_IDS", "")
allowed_ids = [int(x.strip()) for x in allowed_ids_raw.split(",") if x.strip().isdigit()]

# Create a Client instance with your API ID and API hash
app = Client(APP_TITLE, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global dictionary to store video format details
links_data = {}
user_states = {}

def is_user_allowed(chat_id):
    if not allowed_ids:
        return True
    return chat_id in allowed_ids

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
    message.reply_text("Please send a link")

# Handler for receiving a message with a link
@app.on_message(filters.regex(r'https?://[^\s]+'))
def handle_link(client, message):
    chat_id = message.chat.id

    if not is_user_allowed(chat_id):
        return

    # Check if the user is already in a downloading state
    if user_states.get(chat_id, None) == 'downloading':
        message.reply_text("Please wait, your previous request is still being processed.")
        return

    user_folder = f'downloads/{chat_id}'

    # Check if the folder exists, if not, create it
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    link = message.text
    # Get video formats using yt-dlp
    ydl_opts = {
        'quiet': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(link, download=False)
            formats = info_dict.get('formats', [info_dict])
            title = info_dict.get('title', 'Video')
    except Exception as e:
        # Error handling when link is not supported or other exceptions occur
        message.reply_text(f"Error fetching link info. The link may not be supported.")
        return

    # Extracting rows with resolutions
    button_rows = []
    current_row = []
    format_ids = {}
    for f in formats:
        format_id = f['format_id']
        resolution = f.get('format_note', f.get('resolution'))
        protocol = f.get('protocol', 'Unknown Proto')  # Extract the protocol
        if resolution and resolution != 'unknown':
            button_text = f"{resolution} ({protocol})"  # Include protocol in the button text
            if button_text not in format_ids:  # Avoid duplicate buttons
                format_ids[button_text] = format_id
                current_row.append(KeyboardButton(button_text))
                # Grouping buttons, 2 per row for better layout
                if len(current_row) == 2:
                    button_rows.append(current_row)
                    current_row = []

    # Add any remaining buttons to the last row
    if current_row:
        button_rows.append(current_row)

    # Storing link and format IDs
    links_data[message.chat.id] = (link, format_ids, title)

    # Sending a message with buttons if any were found
    if button_rows:
        reply_markup = ReplyKeyboardMarkup(button_rows, resize_keyboard=True, one_time_keyboard=True)
        message.reply_text("Choose a resolution and protocol:", reply_markup=reply_markup)
    else:
        message.reply_text("No downloadable links found.")

@app.on_message(filters.regex("Cancel Download"))
def cancel_download(client, message):
    chat_id = message.chat.id

    # Check if the user is in a downloading state
    if user_states.get(chat_id) == 'downloading':
        user_states[chat_id] = 'cancel'
        message.reply_text("Cancelling download...", reply_markup=ReplyKeyboardRemove())
    else:
        message.reply_text("No active download to cancel.", reply_markup=ReplyKeyboardRemove())

def check_cancel(data, chat_id, message, status_msg=None):
    """Check if the download should be cancelled."""
    if user_states.get(chat_id) == 'cancel':
        if status_msg:
            try:
                status_msg.edit_text("🚫 Download cancelled by user.", reply_markup=ReplyKeyboardRemove())
            except Exception:
                pass
        else:
            message.reply_text("Download cancelled by user.", reply_markup=ReplyKeyboardRemove())
        raise Exception("Download cancelled by user")

# Handler for processing the user's choice
@app.on_message(filters.text)
def download_video(client, message):
    chat_id = message.chat.id

    if not is_user_allowed(chat_id):
        return
    
    if user_states.get(chat_id, None) == 'downloading':
        message.reply_text("Please wait, your previous request is still being processed.")
        return
        
    # Check if the user is already in a downloading state
    if user_states.get(chat_id) == 'cancel':
        message.reply_text("Download cancelled by user.", reply_markup=ReplyKeyboardRemove())
        user_states.pop(chat_id, None)
        return

    user_folder = f'downloads/{chat_id}'
    if chat_id in links_data:
        link, format_ids, title = links_data[chat_id]
        chosen_resolution = message.text

        if chosen_resolution in format_ids:
            format_id = format_ids[chosen_resolution]

            # Set the user state to 'downloading'
            user_states[chat_id] = 'downloading'

            # Notify the user that the download has started
            cancel_button = KeyboardButton("Cancel Download")
            reply_markup = ReplyKeyboardMarkup([[cancel_button]], resize_keyboard=True, one_time_keyboard=True)
            status_msg = message.reply_text("⏳ Initializing download... Press 'Cancel Download' to stop.", reply_markup=reply_markup)

            progress_state = {
                'last_update': 0,
                'start_time': time.time()
            }

            def ydl_progress_hook(d):
                check_cancel(d, chat_id, message, status_msg)
                if d.get('status') == 'downloading':
                    now = time.time()
                    if now - progress_state['last_update'] < 3.5:
                        return

                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes', 0)
                    speed = d.get('speed') or 0
                    eta = d.get('eta')

                    percent = (downloaded / total * 100) if total > 0 else 0.0
                    bar = make_progress_bar(percent)

                    total_str = format_size(total) if total > 0 else "Unknown"
                    speed_str = f"{format_size(speed)}/s" if speed else "Calculating..."
                    eta_str = format_time(eta) if eta else "--:--"

                    text = (
                        f"📥 **Downloading Video:**\n"
                        f"`[{bar}]` **{percent:.1f}%**\n\n"
                        f"📦 **Size:** `{format_size(downloaded)}` / `{total_str}`\n"
                        f"⚡ **Speed:** `{speed_str}`\n"
                        f"⏱️ **ETA:** `{eta_str}`"
                    )

                    try:
                        status_msg.edit_text(text)
                        progress_state['last_update'] = now
                    except FloodWait as fw:
                        time.sleep(fw.value)
                    except MessageNotModified:
                        pass
                    except Exception:
                        pass
                elif d.get('status') == 'finished':
                    try:
                        status_msg.edit_text("⚙️ Processing video file...")
                    except Exception:
                        pass

            # Attempt to download the video using yt-dlp
            try:
                ydl_opts = {
                    'format': format_id,
                    'outtmpl': f'{user_folder}/{chat_id}_%(title)s.%(ext)s',
                    'progress_hooks': [ydl_progress_hook]
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])

                # Find the downloaded file and check its size
                for file in os.listdir(user_folder):
                    if file.startswith(str(chat_id)):
                        full_path = os.path.join(user_folder, file)
                        file_size = os.path.getsize(full_path)

                        # Check if the file size is greater than 2GB
                        if file_size > 2 * 1024 * 1024 * 1024:  # 2GB in bytes
                            try:
                                status_msg.edit_text("❌ Error: The downloaded video is too large to be sent via Telegram (> 2GB).", reply_markup=ReplyKeyboardRemove())
                            except Exception:
                                message.reply_text("Error: The downloaded video is too large to be sent via Telegram.", reply_markup=ReplyKeyboardRemove())
                            os.remove(full_path)  # Remove the large file
                            break

                        upload_state = {
                            'last_update': 0,
                            'start_time': time.time()
                        }

                        def upload_progress_hook(current, total):
                            now = time.time()
                            if now - upload_state['last_update'] < 3.5 and current != total:
                                return

                            elapsed = now - upload_state['start_time']
                            speed = current / elapsed if elapsed > 0 else 0
                            remaining_bytes = max(0, total - current)
                            eta = remaining_bytes / speed if speed > 0 else 0
                            percent = (current / total * 100) if total > 0 else 0.0
                            bar = make_progress_bar(percent)

                            speed_str = f"{format_size(speed)}/s" if speed else "Calculating..."
                            eta_str = format_time(eta) if eta else "--:--"

                            text = (
                                f"📤 **Uploading to Telegram:**\n"
                                f"`[{bar}]` **{percent:.1f}%**\n\n"
                                f"📦 **Size:** `{format_size(current)}` / `{format_size(total)}`\n"
                                f"⚡ **Speed:** `{speed_str}`\n"
                                f"⏱️ **ETA:** `{eta_str}`"
                            )

                            try:
                                status_msg.edit_text(text)
                                upload_state['last_update'] = now
                            except FloodWait as fw:
                                time.sleep(fw.value)
                            except MessageNotModified:
                                pass
                            except Exception:
                                pass

                        try:
                            status_msg.edit_text("📤 Starting upload to Telegram...", reply_markup=ReplyKeyboardRemove())
                        except Exception:
                            pass

                        try:
                            message.reply_video(
                                full_path,
                                caption=title,
                                progress=upload_progress_hook,
                                reply_markup=ReplyKeyboardRemove()
                            )
                            # Remove status message on successful send
                            try:
                                status_msg.delete()
                            except Exception:
                                pass
                        except Exception as upload_error:
                            try:
                                status_msg.edit_text(f"❌ Error uploading the video: {upload_error}", reply_markup=ReplyKeyboardRemove())
                            except Exception:
                                message.reply_text(f"Error uploading the video: {upload_error}", reply_markup=ReplyKeyboardRemove())

                        os.remove(full_path)  # Remove the file after sending
                        break
                else:
                    try:
                        status_msg.edit_text("❌ Downloaded file not found.", reply_markup=ReplyKeyboardRemove())
                    except Exception:
                        message.reply_text("Downloaded file not found.", reply_markup=ReplyKeyboardRemove())

            except Exception as e:
                if "Download cancelled by user" in str(e):
                    pass
                else:
                    try:
                        status_msg.edit_text(f"❌ Error downloading the video: {e}. Please choose another resolution or try again later.", reply_markup=ReplyKeyboardRemove())
                    except Exception:
                        message.reply_text(f"Error downloading the video: {e}. Please choose another resolution or try again later.", reply_markup=ReplyKeyboardRemove())

            finally:
                # Remove the user state lock regardless of outcome
                user_states.pop(chat_id, None)

        else:
            message.reply_text("Invalid resolution selected. Please choose another resolution or try again later.")
            user_states.pop(chat_id, None)  # Remove the state lock in case of invalid selection
    else:
        message.reply_text("No link found. Please send a link first.")
        user_states.pop(chat_id, None)  # Remove the state lock in case no link was found

# Run the bot
if __name__ == "__main__":
    app.run()


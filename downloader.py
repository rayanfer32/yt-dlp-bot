import os
import shutil
import time
import uuid
import yt_dlp
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified
from utils import clean_filename, format_size, format_time, make_progress_bar
from state import links_store, active_tasks, retry_downloads, executor


def render_format_selector(link_id):
    link_info = links_store.get(link_id)
    if not link_info:
        return None, None

    title = link_info.get("title") or "Video"
    format_items = link_info.get("formats", [])
    is_long = len(title) > 60

    text = f"🎬 **{title}**\n"
    if is_long:
        text += f"\n⚠️ **Notice:** Filename is long ({len(title)} chars). You can rename it before downloading.\n"
    text += "\nChoose resolution / quality to download:"

    inline_keyboard = []
    current_row = []
    for idx, (btn_text, _) in enumerate(format_items):
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"dl:{link_id}:{idx}"))
        if len(current_row) == 2:
            inline_keyboard.append(current_row)
            current_row = []
    if current_row:
        inline_keyboard.append(current_row)

    rename_label = "✏️ Rename (Too Long)" if is_long else "✏️ Rename"
    inline_keyboard.append([
        InlineKeyboardButton(rename_label, callback_data=f"rename:{link_id}"),
        InlineKeyboardButton("❌ Dismiss", callback_data=f"dismiss:{link_id}")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard)


def update_format_selector(client, chat_id, message_id, link_id):
    text, markup = render_format_selector(link_id)
    if text and markup:
        try:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup
            )
        except Exception as e:
            print(f"Update format selector error: {e}")


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
            raw_title = info_dict.get('title') or 'Video'
            title = clean_filename(raw_title, max_len=200)
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

    text, markup = render_format_selector(link_id)
    try:
        msg_to_edit.edit_text(text, reply_markup=markup)
    except Exception as edit_err:
        print(f"Edit error on formats display: {edit_err}")


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

    safe_title = clean_filename(title, max_len=120)
    try:
        ydl_opts = {
            'format': format_id,
            'outtmpl': f'{task_folder}/{safe_title}.%(ext)s',
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
                    [
                        InlineKeyboardButton("🔄 Retry", callback_data=f"retry_dl:{retry_id}"),
                        InlineKeyboardButton("✏️ Rename & Retry", callback_data=f"rename_dl:{retry_id}")
                    ]
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


def start_download_from_retry(client, chat_id, retry_id, callback_message=None):
    retry_info = retry_downloads.get(retry_id)
    if not retry_info:
        return

    link = retry_info["link"]
    format_id = retry_info["format_id"]
    title = retry_info["title"]
    saved_link_id = retry_info.get("link_id")

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

    msg_for_task = None
    if callback_message:
        try:
            callback_message.edit_text(
                f"⏳ **Starting download:** {title[:40]}...\n`[□□□□□□□□□□]` **0.0%**",
                reply_markup=cancel_markup
            )
            msg_for_task = callback_message
        except Exception:
            pass

    if not msg_for_task:
        msg_for_task = client.send_message(
            chat_id,
            f"⏳ **Starting download:** {title[:40]}...\n`[□□□□□□□□□□]` **0.0%**",
            reply_markup=cancel_markup
        )

    executor.submit(
        run_download_task,
        client,
        chat_id,
        task_id,
        link,
        format_id,
        title,
        task_folder,
        msg_for_task,
        saved_link_id
    )

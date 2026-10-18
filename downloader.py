import io
import os
import shutil
import time
import uuid
import threading
from concurrent.futures import Future
import yt_dlp
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified
from utils import clean_filename, format_size, format_time, make_progress_bar
from state import links_store, active_tasks, retry_downloads, executor, get_user_mode, set_user_mode


class PipelinedFile(io.IOBase):
    """
    Stream-compatible file wrapper that allows Pyrogram to read and upload
    bytes in chunks while yt-dlp is still downloading the file to disk.
    """
    def __init__(self, file_path, total_size, download_state):
        self.file_path = file_path
        self.total_size = total_size
        self.download_state = download_state
        self._pos = 0
        self._fp = None
        self.name = os.path.basename(file_path)

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self.total_size + offset
        return self._pos

    def tell(self):
        return self._pos

    def _ensure_fp(self):
        if self._fp is None:
            start_wait = time.time()
            while not os.path.exists(self.file_path):
                if self.download_state.get("cancelled") or self.download_state.get("error"):
                    raise Exception(self.download_state.get("error") or "Download cancelled by user")
                if time.time() - start_wait > 45:
                    raise TimeoutError("Timed out waiting for file to be created on disk")
                time.sleep(0.05)
            self._fp = open(self.file_path, "rb")

    def read(self, size=-1):
        if self._pos >= self.total_size:
            return b""
        if size is None or size < 0:
            size = self.total_size - self._pos

        target_pos = min(self._pos + size, self.total_size)
        needed = target_pos - self._pos
        self._ensure_fp()

        while True:
            if self.download_state.get("cancelled"):
                raise Exception("Download cancelled by user")
            if self.download_state.get("error"):
                raise Exception(self.download_state.get("error"))

            try:
                curr_size = os.path.getsize(self.file_path)
            except OSError:
                curr_size = 0

            if curr_size >= target_pos or self.download_state.get("finished"):
                break

            time.sleep(0.05)

        self._fp.seek(self._pos)
        chunk = self._fp.read(needed)
        self._pos += len(chunk)
        return chunk

    def close(self):
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None
        super().close()


def send_media(client, chat_id, media, title, progress=None):
    """
    Sends media as video when possible, with automatic fallback to document
    if video stream headers or formatting cannot be sent as video.
    """
    try:
        return client.send_video(
            chat_id=chat_id,
            video=media,
            caption=title,
            progress=progress
        )
    except Exception as video_err:
        print(f"send_video error ({video_err}), falling back to send_document")
        # If media was a PipelinedFile, ensure it is closed and use file_path
        doc_media = getattr(media, "file_path", media)
        return client.send_document(
            chat_id=chat_id,
            document=doc_media,
            caption=title,
            progress=progress
        )


def render_format_selector(link_id, user_id=None):
    link_info = links_store.get(link_id)
    if not link_info:
        return None, None

    title = link_info.get("title") or "Video"
    format_items = link_info.get("formats", [])
    is_long = len(title) > 60

    mode = get_user_mode(user_id) if user_id else "normal"
    mode_str = "⚡ Stream (Parallel)" if mode == "stream" else "📥 Normal (Sequential)"

    text = f"🎬 **{title}**\n"
    if is_long:
        text += f"\n⚠️ **Notice:** Filename is long ({len(title)} chars). You can rename it before downloading.\n"
    text += f"\n⚙️ **Mode:** `{mode_str}`\nChoose resolution / quality to download:"

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
    mode_toggle_label = "Mode: 📥 Normal" if mode == "stream" else "Mode: ⚡ Stream"

    inline_keyboard.append([
        InlineKeyboardButton(f"🔄 {mode_toggle_label}", callback_data=f"toggle_mode:{link_id}"),
        InlineKeyboardButton(rename_label, callback_data=f"rename:{link_id}")
    ])
    inline_keyboard.append([
        InlineKeyboardButton("❌ Dismiss", callback_data=f"dismiss:{link_id}")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard)


def update_format_selector(client, chat_id, message_id, link_id, user_id=None):
    text, markup = render_format_selector(link_id, user_id=user_id)
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


def process_link(client, chat_id, link, msg_to_edit, link_id=None, user_id=None):
    if not link_id:
        link_id = uuid.uuid4().hex[:8]

    if link_id not in links_store:
        links_store[link_id] = {
            "link": link,
            "title": "",
            "formats": [],
            "user_id": user_id
        }
    else:
        links_store[link_id]["link"] = link
        if user_id:
            links_store[link_id]["user_id"] = user_id

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

    text, markup = render_format_selector(link_id, user_id=user_id)
    try:
        msg_to_edit.edit_text(text, reply_markup=markup)
    except Exception as edit_err:
        print(f"Edit error on formats display: {edit_err}")


def run_download_task(client, chat_id, task_id, link, format_id, title, task_folder, status_msg, link_id=None, user_id=None):
    mode = get_user_mode(user_id or chat_id)
    safe_title = clean_filename(title, max_len=120)

    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel:{task_id}")
    ]])

    def check_task_cancel():
        if active_tasks.get(task_id, {}).get("cancelled"):
            raise Exception("Download cancelled by user")

    # State tracking for download and upload
    dl_state = {
        'total_bytes': 0,
        'downloaded_bytes': 0,
        'speed': 0,
        'finished': False,
        'cancelled': False,
        'error': None,
        'file_path': None
    }

    ul_state = {
        'uploaded_bytes': 0,
        'speed': 0,
        'start_time': time.time(),
        'finished': False
    }

    progress_tracking = {
        'last_update': 0
    }

    def update_dual_progress(force=False):
        now = time.time()
        if not force and (now - progress_tracking['last_update'] < 1.0):
            return

        dl_total = dl_state.get('total_bytes', 0)
        dl_curr = dl_state.get('downloaded_bytes', 0)
        dl_pct = (dl_curr / dl_total * 100) if dl_total > 0 else 0.0

        ul_total = dl_total
        ul_curr = ul_state.get('uploaded_bytes', 0)
        ul_pct = (ul_curr / ul_total * 100) if ul_total > 0 else 0.0

        dl_speed = dl_state.get('speed', 0)
        dl_speed_str = f"{format_size(dl_speed)}/s" if dl_speed else "Calculating..."

        ul_elapsed = now - ul_state['start_time']
        ul_speed = ul_curr / ul_elapsed if ul_elapsed > 0 else 0
        ul_speed_str = f"{format_size(ul_speed)}/s" if ul_speed else "Calculating..."

        dl_bar = make_progress_bar(dl_pct, length=8)
        ul_bar = make_progress_bar(ul_pct, length=8)
        total_str = format_size(dl_total) if dl_total > 0 else "Unknown"

        text = (
            f"⚡ **Stream Mode (Parallel):** {title[:32]}\n\n"
            f"📥 **Download:** `[{dl_bar}]` **{dl_pct:.1f}%**\n"
            f"   ⚡ `{dl_speed_str}` • `{format_size(dl_curr)}`\n\n"
            f"📤 **Upload:**   `[{ul_bar}]` **{ul_pct:.1f}%**\n"
            f"   ⚡ `{ul_speed_str}` • `{format_size(ul_curr)}`\n\n"
            f"📦 **Size:** `{total_str}`"
        )

        try:
            status_msg.edit_text(text, reply_markup=cancel_markup)
            progress_tracking['last_update'] = now
        except (FloodWait, MessageNotModified):
            pass
        except Exception as edit_err:
            print(f"[{task_id}] Stream progress edit error: {edit_err}")

    # Progress hook for yt-dlp
    def ydl_progress_hook(d):
        check_task_cancel()
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed') or 0

            dl_state['total_bytes'] = total
            dl_state['downloaded_bytes'] = downloaded
            dl_state['speed'] = speed
            if d.get('filename'):
                dl_state['file_path'] = d.get('filename')

            if mode == "stream":
                update_dual_progress()
            else:
                # Normal mode sequential download progress
                now = time.time()
                time_diff = now - progress_tracking['last_update']
                percent = (downloaded / total * 100) if total > 0 else 0.0
                if progress_tracking['last_update'] != 0 and time_diff < 1.0 and downloaded != total:
                    return

                bar = make_progress_bar(percent)
                total_str = format_size(total) if total > 0 else "Unknown"
                speed_str = f"{format_size(speed)}/s" if speed else "Calculating..."
                eta_str = format_time(d.get('eta')) if d.get('eta') else "--:--"

                text = (
                    f"📥 **Downloading:** {title[:35]}\n"
                    f"`[{bar}]` **{percent:.1f}%**\n\n"
                    f"📦 **Size:** `{format_size(downloaded)}` / `{total_str}`\n"
                    f"⚡ **Speed:** `{speed_str}`\n"
                    f"⏱️ **ETA:** `{eta_str}`"
                )
                try:
                    status_msg.edit_text(text, reply_markup=cancel_markup)
                    progress_tracking['last_update'] = now
                except (FloodWait, MessageNotModified):
                    pass
                except Exception as edit_err:
                    print(f"[{task_id}] Normal download edit error: {edit_err}")

        elif d.get('status') == 'finished':
            dl_state['finished'] = True
            if d.get('filename'):
                dl_state['file_path'] = d.get('filename')

    try:
        ydl_opts = {
            'format': format_id,
            'outtmpl': f'{task_folder}/{safe_title}.%(ext)s',
            'progress_hooks': [ydl_progress_hook],
            'nopart': True,
            'quiet': True,
            'no_warnings': True
        }

        # -----------------------------------------------------------------
        # STREAM MODE: Concurrent Download & Upload
        # -----------------------------------------------------------------
        if mode == "stream":
            upload_exception = []
            uploader_started = threading.Event()

            def run_stream_uploader():
                try:
                    # Wait for download to start and file path / total size to be known
                    start_wait = time.time()
                    while dl_state['total_bytes'] <= 0 or not dl_state['file_path']:
                        check_task_cancel()
                        if dl_state.get('finished') or time.time() - start_wait > 30:
                            break
                        time.sleep(0.05)

                    uploader_started.set()

                    # Find download file
                    file_path = dl_state.get('file_path')
                    if not file_path or not os.path.exists(file_path):
                        # Locate from folder if needed
                        for _ in range(50):
                            files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
                            if files:
                                file_path = files[0]
                                break
                            time.sleep(0.1)

                    total_size = dl_state['total_bytes']

                    # Check 2GB Telegram limit
                    if total_size > 2 * 1024 * 1024 * 1024:
                        raise ValueError("The video is too large to send via Telegram (> 2GB).")

                    def upload_hook(current, total):
                        check_task_cancel()
                        ul_state['uploaded_bytes'] = current
                        update_dual_progress()

                    if total_size > 0 and file_path:
                        piped = PipelinedFile(file_path, total_size, dl_state)
                        send_media(client, chat_id, piped, title, progress=upload_hook)
                        piped.close()
                    else:
                        # Fallback if total size wasn't determined upfront: wait for download completion
                        while not dl_state['finished']:
                            check_task_cancel()
                            time.sleep(0.1)
                        files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
                        if not files:
                            raise FileNotFoundError("Downloaded file not found on disk")
                        full_path = files[0]
                        send_media(client, chat_id, full_path, title, progress=upload_hook)

                    ul_state['finished'] = True
                    update_dual_progress(force=True)

                except Exception as exc:
                    upload_exception.append(exc)

            # Start the stream uploader thread concurrently
            uploader_thread = threading.Thread(target=run_stream_uploader, daemon=True)
            uploader_thread.start()

            # Execute yt-dlp download in main task thread
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])
            except Exception as dl_err:
                dl_state['error'] = str(dl_err)
                raise dl_err
            finally:
                dl_state['finished'] = True

            # Wait for parallel uploader to complete
            uploader_thread.join()

            if upload_exception:
                raise upload_exception[0]

        # -----------------------------------------------------------------
        # NORMAL MODE: Sequential Download then Upload
        # -----------------------------------------------------------------
        else:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([link])

            check_task_cancel()

            files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
            if not files:
                status_msg.edit_text("❌ Downloaded file not found.")
                return

            full_path = files[0]
            file_size = os.path.getsize(full_path)

            if file_size > 2 * 1024 * 1024 * 1024:
                status_msg.edit_text("❌ Error: The downloaded video is too large to send via Telegram (> 2GB).")
                return

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

                if upload_state['last_update'] != 0 and time_diff < 1.0 and percent_diff < 5.0 and current != total:
                    return

                elapsed = now - upload_state['start_time']
                speed = current / elapsed if elapsed > 0 else 0
                remaining = max(0, total - current)
                eta = remaining / speed if speed > 0 else 0
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
                    status_msg.edit_text(text, reply_markup=cancel_markup)
                    upload_state['last_update'] = now
                    upload_state['last_percent'] = percent
                except (FloodWait, MessageNotModified):
                    pass
                except Exception as e:
                    print(f"[{task_id}] Normal upload edit error: {e}")

            send_media(client, chat_id, full_path, title, progress=upload_progress_hook)

        # Cleanup notification
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
                    "link_id": link_id,
                    "user_id": user_id
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
        shutil.rmtree(task_folder, ignore_errors=True)
        active_tasks.pop(task_id, None)


def start_download_from_retry(client, chat_id, retry_id, callback_message=None, user_id=None):
    retry_info = retry_downloads.get(retry_id)
    if not retry_info:
        return

    link = retry_info["link"]
    format_id = retry_info["format_id"]
    title = retry_info["title"]
    saved_link_id = retry_info.get("link_id")
    resolved_user_id = user_id or retry_info.get("user_id")

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
        saved_link_id,
        resolved_user_id
    )

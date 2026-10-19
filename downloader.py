import io
import os
import shutil
import time
import uuid
import threading
import yt_dlp
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified
from utils import clean_filename, format_size, format_time, make_progress_bar
from state import (
    links_store,
    active_tasks,
    retry_downloads,
    submit_task,
    get_user_mode,
    set_user_mode
)
from logger import setup_logger
from config import get_cookie_file

logger = setup_logger("downloader")


class PipelinedFile(io.IOBase):
    """
    Stream-compatible file wrapper that allows Pyrogram to read and upload
    bytes in chunks while yt-dlp is still downloading the file to disk.
    """
    def __init__(self, file_path, total_size, download_state, task_id="stream"):
        self.file_path = file_path
        self.total_size = total_size
        self.download_state = download_state
        self.task_id = task_id
        self._pos = 0
        self._fp = None
        self.name = os.path.basename(file_path)
        logger.debug(
            f"[{self.task_id}] Initialized PipelinedFile: path='{self.file_path}', "
            f"total_size={self.total_size} bytes ({format_size(self.total_size)})"
        )

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
        logger.debug(
            f"[{self.task_id}] PipelinedFile.seek(offset={offset}, whence={whence}) -> new_pos={self._pos}"
        )
        return self._pos

    def tell(self):
        return self._pos

    def _ensure_fp(self):
        if self._fp is None:
            logger.debug(f"[{self.task_id}] Waiting for initial file creation on disk: {self.file_path}")
            start_wait = time.time()
            while not os.path.exists(self.file_path):
                if self.download_state.get("cancelled") or self.download_state.get("error"):
                    err_msg = self.download_state.get("error") or "Download cancelled by user"
                    logger.error(f"[{self.task_id}] Aborting file wait: {err_msg}")
                    raise Exception(err_msg)
                if time.time() - start_wait > 60:
                    logger.error(f"[{self.task_id}] Timed out waiting 60s for file on disk: {self.file_path}")
                    raise TimeoutError("Timed out waiting for file to be created on disk")
                time.sleep(0.05)
            self._fp = open(self.file_path, "rb")
            logger.debug(f"[{self.task_id}] Successfully opened binary file handle to {self.file_path}")

    def read(self, size=-1):
        if self._pos >= self.total_size:
            logger.debug(f"[{self.task_id}] PipelinedFile reached total_size ({self._pos} >= {self.total_size}), returning EOF")
            return b""
        if size is None or size < 0:
            size = self.total_size - self._pos

        target_pos = min(self._pos + size, self.total_size)
        needed = target_pos - self._pos
        self._ensure_fp()

        last_logged = time.time()
        while True:
            if self.download_state.get("cancelled"):
                logger.warning(f"[{self.task_id}] PipelinedFile read cancelled by user.")
                raise Exception("Download cancelled by user")
            if self.download_state.get("error"):
                logger.error(f"[{self.task_id}] PipelinedFile read aborted due to error: {self.download_state.get('error')}")
                raise Exception(self.download_state.get("error"))

            try:
                curr_size = os.path.getsize(self.file_path)
            except OSError:
                curr_size = 0

            if curr_size >= target_pos:
                break

            if self.download_state.get("finished"):
                if curr_size < self.total_size:
                    logger.warning(
                        f"[{self.task_id}] Download finished with disk size ({curr_size}) < total_size ({self.total_size}). "
                        f"Adjusting total_size to {curr_size}."
                    )
                    self.total_size = curr_size
                    target_pos = min(self._pos + size, self.total_size)
                    needed = max(0, target_pos - self._pos)
                break

            now = time.time()
            if now - last_logged > 10.0:
                logger.debug(
                    f"[{self.task_id}] Waiting for bytes on disk: curr={curr_size}/{target_pos} "
                    f"({(curr_size / self.total_size * 100):.1f}% of total)"
                )
                last_logged = now

            time.sleep(0.05)

        self._fp.seek(self._pos)
        chunk = self._fp.read(needed)
        actual_read = len(chunk)
        self._pos += actual_read
        logger.debug(
            f"[{self.task_id}] PipelinedFile.read: requested {size}, returned {actual_read} bytes. "
            f"New pos: {self._pos}/{self.total_size}"
        )
        return chunk

    def close(self):
        if self._fp:
            try:
                self._fp.close()
                logger.debug(f"[{self.task_id}] PipelinedFile closed file handle.")
            except Exception as e:
                logger.warning(f"[{self.task_id}] Error closing file handle: {e}")
            self._fp = None
        super().close()


def send_media(client, chat_id, media, title, progress=None, task_id=""):
    """
    Sends media as video when possible, with automatic fallback to document
    if video stream headers or formatting cannot be sent as video.
    """
    logger.debug(f"[{task_id}] Attempting client.send_video for '{title}' (media={type(media).__name__})...")
    try:
        res = client.send_video(
            chat_id=chat_id,
            video=media,
            caption=title,
            progress=progress
        )
        logger.info(f"[{task_id}] Successfully sent media as video.")
        return res
    except Exception as video_err:
        logger.warning(
            f"[{task_id}] send_video failed with error ({video_err}). Falling back to send_document...",
            exc_info=True
        )
        # If media was a PipelinedFile, close it and wait for full download to finish before sending as document
        if isinstance(media, PipelinedFile):
            doc_path = media.file_path
            media.close()
            logger.info(f"[{task_id}] Waiting for download to finish on disk before sending document fallback...")
            while not media.download_state.get("finished"):
                if media.download_state.get("cancelled") or media.download_state.get("error"):
                    raise Exception(media.download_state.get("error") or "Download cancelled by user")
                time.sleep(0.2)
            doc_media = doc_path
        else:
            doc_media = getattr(media, "file_path", media)

        logger.debug(f"[{task_id}] Attempting client.send_document with '{doc_media}'...")
        res = client.send_document(
            chat_id=chat_id,
            document=doc_media,
            caption=title,
            progress=progress
        )
        logger.info(f"[{task_id}] Successfully sent media as document fallback.")
        return res


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
            logger.debug(f"Updated format selector message {message_id} in chat {chat_id}")
        except Exception as e:
            logger.error(f"Update format selector error: {e}", exc_info=True)


def process_link(client, chat_id, link, msg_to_edit, link_id=None, user_id=None):
    if not link_id:
        link_id = uuid.uuid4().hex[:8]

    logger.info(f"[{link_id}] Processing incoming link: {link} for user {user_id} in chat {chat_id}")

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

    cookie_file = get_cookie_file()
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    }
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file
        logger.info(f"[{link_id}] Using cookies from: {cookie_file}")
    else:
        logger.warning(f"[{link_id}] No cookies file found. Proceeding without cookies.")
    try:
        logger.debug(f"[{link_id}] Extracting info with yt-dlp...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(link, download=False)
            formats = info_dict.get('formats') or [info_dict]
            raw_title = info_dict.get('title') or 'Video'
            title = clean_filename(raw_title, max_len=200)
            logger.info(f"[{link_id}] Info extracted: title='{title}', found {len(formats)} raw formats")
    except Exception as e:
        logger.error(f"[{link_id}] Error extracting info from {link}: {e}", exc_info=True)
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
            logger.error(f"[{link_id}] Edit error on link fetch failure: {edit_err}", exc_info=True)
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

    logger.debug(f"[{link_id}] Filtered down to {len(format_items)} selectable formats")

    links_store[link_id]["title"] = title
    links_store[link_id]["formats"] = format_items

    text, markup = render_format_selector(link_id, user_id=user_id)
    try:
        msg_to_edit.edit_text(text, reply_markup=markup)
        logger.debug(f"[{link_id}] Displayed format selector message to user")
    except Exception as edit_err:
        logger.error(f"[{link_id}] Edit error on formats display: {edit_err}", exc_info=True)


def run_download_task(client, chat_id, task_id, link, format_id, title, task_folder, status_msg, link_id=None, user_id=None):
    mode = get_user_mode(user_id or chat_id)
    safe_title = clean_filename(title, max_len=120)
    logger.info(
        f"[{task_id}] START DOWNLOAD TASK | Mode: {mode.upper()} | Title: '{safe_title}' | "
        f"Format: {format_id} | User: {user_id} | Chat: {chat_id}"
    )

    cancel_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel:{task_id}")
    ]])

    def check_task_cancel():
        if active_tasks.get(task_id, {}).get("cancelled"):
            logger.warning(f"[{task_id}] Cancellation detected in check_task_cancel")
            raise Exception("Download cancelled by user")
        if dl_state.get("error"):
            logger.error(f"[{task_id}] Aborting task due to internal error: {dl_state['error']}")
            raise Exception(dl_state["error"])

    # State tracking for download and upload
    dl_state = {
        'total_bytes': 0,
        'downloaded_bytes': 0,
        'speed': 0,
        'finished': False,
        'cancelled': False,
        'error': None,
        'file_path': None,
        'is_exact_total': False,
        'is_estimate': False,
        'is_postprocessing': False
    }

    ul_state = {
        'uploaded_bytes': 0,
        'speed': 0,
        'start_time': time.time(),
        'finished': False
    }

    progress_tracking = {
        'last_update': 0,
        'last_dl_log_pct': -1,
        'last_ul_log_pct': -1
    }

    edit_lock = threading.Lock()
    is_editing = False

    def async_edit_status(text, reply_markup=None, force=False):
        nonlocal is_editing
        now = time.time()
        if not force and (now - progress_tracking['last_update'] < 1.5):
            return

        with edit_lock:
            if is_editing and not force:
                return
            is_editing = True

        progress_tracking['last_update'] = now

        def _worker():
            nonlocal is_editing
            try:
                status_msg.edit_text(text, reply_markup=reply_markup)
            except (FloodWait, MessageNotModified):
                pass
            except Exception as edit_err:
                logger.debug(f"[{task_id}] Progress edit notice: {edit_err}")
            finally:
                with edit_lock:
                    is_editing = False

        threading.Thread(target=_worker, daemon=True, name=f"Edit-{task_id}").start()

    def update_dual_progress(force=False):
        dl_total = dl_state.get('total_bytes', 0)
        dl_curr = dl_state.get('downloaded_bytes', 0)
        dl_pct = (dl_curr / dl_total * 100) if dl_total > 0 else 0.0

        ul_total = dl_total
        ul_curr = ul_state.get('uploaded_bytes', 0)
        ul_pct = (ul_curr / ul_total * 100) if ul_total > 0 else 0.0

        dl_speed = dl_state.get('speed', 0)
        dl_speed_str = f"{format_size(dl_speed)}/s" if dl_speed else "Calculating..."

        now = time.time()
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
        async_edit_status(text, reply_markup=cancel_markup, force=force)

    # Progress hook for yt-dlp
    def ydl_progress_hook(d):
        check_task_cancel()
        if d.get('status') == 'downloading':
            exact_total = d.get('total_bytes')
            est_total = d.get('total_bytes_estimate')
            total = exact_total or est_total or 0
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed') or 0

            if exact_total:
                dl_state['is_exact_total'] = True
            elif est_total:
                dl_state['is_estimate'] = True

            dl_state['total_bytes'] = total
            dl_state['downloaded_bytes'] = downloaded
            dl_state['speed'] = speed
            if d.get('filename'):
                dl_state['file_path'] = d.get('filename')

            pct = (downloaded / total * 100) if total > 0 else 0.0
            # Periodic debug logging every 25%
            current_quarter = int(pct // 25) * 25
            if current_quarter > progress_tracking['last_dl_log_pct']:
                progress_tracking['last_dl_log_pct'] = current_quarter
                logger.info(
                    f"[{task_id}] Download progress: {pct:.1f}% ({format_size(downloaded)}/{format_size(total)}) "
                    f"at {format_size(speed)}/s"
                )

            if mode == "stream":
                update_dual_progress()
            else:
                percent = pct
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
                async_edit_status(text, reply_markup=cancel_markup)

        elif d.get('status') == 'finished':
            logger.info(f"[{task_id}] yt-dlp download status marked as 'finished'")
            dl_state['finished'] = True
            if d.get('filename'):
                dl_state['file_path'] = d.get('filename')

    def ydl_postprocessor_hook(d):
        status = d.get('status')
        pp_name = d.get('postprocessor')
        logger.debug(f"[{task_id}] yt-dlp postprocessor '{pp_name}': status={status}")
        if status == 'started':
            dl_state['is_postprocessing'] = True
        elif status == 'finished':
            dl_state['is_postprocessing'] = False
            info = d.get('info_dict', {})
            if info.get('filepath'):
                dl_state['file_path'] = info['filepath']
                logger.debug(f"[{task_id}] Postprocessor updated filepath: {dl_state['file_path']}")

    try:
        cookie_file = get_cookie_file()
        ydl_opts = {
            'format': format_id,
            'outtmpl': f'{task_folder}/{safe_title}.%(ext)s',
            'progress_hooks': [ydl_progress_hook],
            'postprocessor_hooks': [ydl_postprocessor_hook],
            'nopart': True,
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web']
                }
            }
        }
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file
            logger.info(f"[{task_id}] Using cookies from: {cookie_file}")
        else:
            logger.warning(f"[{task_id}] No cookies file found. Proceeding without cookies.")

        # -----------------------------------------------------------------
        # STREAM MODE: Concurrent Download & Upload
        # -----------------------------------------------------------------
        if mode == "stream":
            upload_exception = []
            uploader_started = threading.Event()

            def run_stream_uploader():
                try:
                    logger.info(f"[{task_id}] Stream uploader background thread launched.")
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
                        for _ in range(50):
                            files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
                            if files:
                                file_path = files[0]
                                break
                            time.sleep(0.1)

                    total_size = dl_state['total_bytes']
                    is_exact = dl_state.get('is_exact_total', False)
                    logger.info(
                        f"[{task_id}] Uploader ready: file='{file_path}', total_size={total_size} "
                        f"({format_size(total_size)}), is_exact={is_exact}"
                    )

                    # Check 2GB Telegram limit
                    if total_size > 2 * 1024 * 1024 * 1024:
                        logger.error(f"[{task_id}] File exceeds 2GB limit: {total_size} bytes")
                        raise ValueError("The video is too large to send via Telegram (> 2GB).")

                    def upload_hook(current, total):
                        check_task_cancel()
                        ul_state['uploaded_bytes'] = current
                        pct = (current / total * 100) if total > 0 else 0.0
                        q = int(pct // 25) * 25
                        if q > progress_tracking['last_ul_log_pct']:
                            progress_tracking['last_ul_log_pct'] = q
                            logger.info(f"[{task_id}] Upload progress: {pct:.1f}% ({format_size(current)}/{format_size(total)})")
                        update_dual_progress()

                    if total_size > 0 and file_path and is_exact:
                        logger.info(f"[{task_id}] Starting PipelinedFile parallel upload to Telegram...")
                        piped = PipelinedFile(file_path, total_size, dl_state, task_id=task_id)
                        try:
                            send_media(client, chat_id, piped, title, progress=upload_hook, task_id=task_id)
                        finally:
                            piped.close()
                    else:
                        logger.info(
                            f"[{task_id}] Exact size unknown or estimated (is_exact={is_exact}). "
                            "Safely waiting for yt-dlp to finish download before uploading to prevent Telegram part error..."
                        )
                        while not dl_state.get('finished') or dl_state.get('is_postprocessing'):
                            check_task_cancel()
                            time.sleep(0.2)

                        files = [os.path.join(task_folder, f) for f in os.listdir(task_folder) if not f.endswith('.temp') and not f.endswith('.part')]
                        if not files:
                            files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
                        if not files:
                            raise FileNotFoundError(f"Downloaded file not found in {task_folder}")
                        full_path = files[0]
                        disk_size = os.path.getsize(full_path)
                        logger.info(f"[{task_id}] Finished on disk. Uploading {full_path} ({format_size(disk_size)})...")
                        send_media(client, chat_id, full_path, title, progress=upload_hook, task_id=task_id)

                    ul_state['finished'] = True
                    update_dual_progress(force=True)
                    logger.info(f"[{task_id}] Stream uploader thread finished successfully.")

                except Exception as exc:
                    logger.exception(f"[{task_id}] CRITICAL: Stream uploader thread failed: {exc}")
                    upload_exception.append(exc)
                    # Notify dl_state so yt-dlp aborts downloading immediately
                    dl_state['error'] = f"Uploader error: {exc}"

            # Start the stream uploader thread concurrently
            uploader_thread = threading.Thread(target=run_stream_uploader, daemon=True, name=f"Uploader-{task_id}")
            uploader_thread.start()

            # Execute yt-dlp download in main task thread
            try:
                logger.info(f"[{task_id}] Starting yt-dlp download...")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([link])
                logger.info(f"[{task_id}] yt-dlp download call completed.")
            except Exception as dl_err:
                logger.error(f"[{task_id}] yt-dlp download raised exception: {dl_err}", exc_info=True)
                dl_state['error'] = str(dl_err)
                raise dl_err
            finally:
                dl_state['finished'] = True

            # Wait for parallel uploader to complete
            uploader_thread.join()
            logger.debug(f"[{task_id}] Uploader thread joined.")

            if upload_exception:
                raise upload_exception[0]

        # -----------------------------------------------------------------
        # NORMAL MODE: Sequential Download then Upload
        # -----------------------------------------------------------------
        else:
            logger.info(f"[{task_id}] Starting sequential yt-dlp download...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([link])

            check_task_cancel()

            files = [os.path.join(task_folder, f) for f in os.listdir(task_folder)]
            if not files:
                logger.error(f"[{task_id}] No downloaded file found in {task_folder}")
                status_msg.edit_text("❌ Downloaded file not found.")
                return

            full_path = files[0]
            file_size = os.path.getsize(full_path)
            logger.info(f"[{task_id}] Download finished on disk: {full_path} ({format_size(file_size)})")

            if file_size > 2 * 1024 * 1024 * 1024:
                logger.error(f"[{task_id}] File exceeds 2GB limit: {file_size} bytes")
                status_msg.edit_text("❌ Error: The downloaded video is too large to send via Telegram (> 2GB).")
                return

            upload_state = {
                'last_update': 0,
                'last_percent': -1,
                'start_time': time.time(),
                'last_log_quarter': -1
            }

            try:
                status_msg.edit_text(
                    f"📤 **Uploading to Telegram:** {title[:35]}\n`[□□□□□□□□□□]` **0.0%**"
                )
            except Exception as e:
                logger.debug(f"[{task_id}] Initial upload edit notice: {e}")

            def upload_progress_hook(current, total):
                check_task_cancel()
                percent = (current / total * 100) if total > 0 else 0.0
                now = time.time()
                time_diff = now - upload_state['last_update']
                percent_diff = abs(percent - upload_state['last_percent'])

                q = int(percent // 25) * 25
                if q > upload_state['last_log_quarter']:
                    upload_state['last_log_quarter'] = q
                    logger.info(f"[{task_id}] Normal upload: {percent:.1f}% ({format_size(current)}/{format_size(total)})")

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
                async_edit_status(text, reply_markup=cancel_markup)

            logger.info(f"[{task_id}] Uploading media to Telegram...")
            send_media(client, chat_id, full_path, title, progress=upload_progress_hook, task_id=task_id)

        # Cleanup notification
        try:
            status_msg.edit_text(f"✅ **Completed:** {title[:40]}")
            time.sleep(2)
            status_msg.delete()
            logger.info(f"[{task_id}] Task completed cleanly and status message deleted.")
        except Exception as e:
            logger.debug(f"[{task_id}] Cleanup edit notice: {e}")

    except Exception as e:
        if "Download cancelled by user" in str(e):
            logger.info(f"[{task_id}] Task cancelled cleanly by user.")
        else:
            logger.exception(f"[{task_id}] CRITICAL: Download task failed: {e}")
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
                logger.info(f"[{task_id}] Displayed error status message with retry options.")
            except Exception as edit_err:
                logger.error(f"[{task_id}] Failed to edit error message: {edit_err}", exc_info=True)
    finally:
        shutil.rmtree(task_folder, ignore_errors=True)
        active_tasks.pop(task_id, None)
        logger.debug(f"[{task_id}] Task cleanup complete: removed folder '{task_folder}'")


def start_download_from_retry(client, chat_id, retry_id, callback_message=None, user_id=None):
    retry_info = retry_downloads.get(retry_id)
    if not retry_info:
        logger.warning(f"Retry request for unknown retry_id: {retry_id}")
        return

    link = retry_info["link"]
    format_id = retry_info["format_id"]
    title = retry_info["title"]
    saved_link_id = retry_info.get("link_id")
    resolved_user_id = user_id or retry_info.get("user_id")

    task_id = uuid.uuid4().hex[:8]
    task_folder = f"downloads/{chat_id}/{task_id}"
    os.makedirs(task_folder, exist_ok=True)

    logger.info(f"[{task_id}] Starting download from retry_id {retry_id} for user {resolved_user_id}")

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
        except Exception as e:
            logger.debug(f"Could not reuse callback message: {e}")

    if not msg_for_task:
        msg_for_task = client.send_message(
            chat_id,
            f"⏳ **Starting download:** {title[:40]}...\n`[□□□□□□□□□□]` **0.0%**",
            reply_markup=cancel_markup
        )

    submit_task(
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

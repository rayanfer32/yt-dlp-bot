import os
import re
import time
import uuid
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from config import is_user_allowed
from utils import clean_filename
from state import links_store, active_tasks, retry_downloads, pending_renames, executor
from downloader import (
    process_link,
    update_format_selector,
    start_download_from_retry,
    run_download_task,
)


def handle_start(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if not is_user_allowed(user_id):
        return message.reply_text("You are not allowed to use this bot.", quote=True)
    message.reply_text("👋 Send me any video link or direct download URL to get started!", quote=True)


def handle_caption(client, message):
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


def handle_link(client, message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    if not is_user_allowed(user_id):
        return

    # Clear any pending rename if user sends a new link
    pending_renames.pop(user_id, None)

    link_match = re.search(r'https?://[^\s]+', message.text)
    link = link_match.group(0) if link_match else message.text.strip()
    wait_msg = message.reply_text("🔍 Fetching video info...", quote=True)
    process_link(client, chat_id, link, wait_msg)


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

    if data.startswith("rename:"):
        _, link_id = data.split(":", 1)
        link_info = links_store.get(link_id)
        if not link_info:
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        callback_query.answer()
        current_title = link_info.get("title", "Video")
        prompt_msg = callback_query.message.reply_text(
            f"✏️ **Rename Video Filename**\n\n"
            f"Current filename ({len(current_title)} chars):\n`{current_title}`\n\n"
            f"💬 Send your new filename below (or `/cancel` to abort):",
            quote=True
        )
        pending_renames[user_id] = {
            "link_id": link_id,
            "prompt_msg_id": prompt_msg.id,
            "chat_id": chat_id,
            "message_id": callback_query.message.id
        }
        return

    if data.startswith("rename_dl:"):
        _, retry_id = data.split(":", 1)
        retry_info = retry_downloads.get(retry_id)
        if not retry_info:
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        callback_query.answer()
        current_title = retry_info.get("title", "Video")
        prompt_msg = callback_query.message.reply_text(
            f"✏️ **Rename & Retry Download**\n\n"
            f"Current filename ({len(current_title)} chars):\n`{current_title}`\n\n"
            f"💬 Send your new filename below (or `/cancel` to abort):",
            quote=True
        )
        pending_renames[user_id] = {
            "retry_id": retry_id,
            "prompt_msg_id": prompt_msg.id,
            "chat_id": chat_id,
            "message_id": callback_query.message.id
        }
        return

    if data.startswith("retry_dl:"):
        _, retry_id = data.split(":", 1)
        retry_info = retry_downloads.get(retry_id)
        if not retry_info:
            callback_query.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
            return

        callback_query.answer("🔄 Retrying download...")
        start_download_from_retry(client, chat_id, retry_id, callback_query.message)
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


def handle_rename(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if not is_user_allowed(user_id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        message.reply_text(
            "ℹ️ **How to rename:**\n"
            "• Click the **✏️ Rename** button on any video message\n"
            "• Or reply to a video message with `/rename <new filename>`",
            quote=True
        )
        return

    new_name = clean_filename(parts[1].strip(), max_len=120)

    if user_id in pending_renames:
        rename_info = pending_renames.pop(user_id)
        link_id = rename_info.get("link_id")
        retry_id = rename_info.get("retry_id")

        if link_id and link_id in links_store:
            links_store[link_id]["title"] = new_name
            chat_id = rename_info.get("chat_id")
            msg_id = rename_info.get("message_id")
            if chat_id and msg_id:
                update_format_selector(client, chat_id, msg_id, link_id)
            message.reply_text(f"✅ Filename updated to:\n`{new_name}`", quote=True)
            return

        if retry_id and retry_id in retry_downloads:
            retry_downloads[retry_id]["title"] = new_name
            message.reply_text(f"✅ Filename set to:\n`{new_name}`\n⏳ Retrying download...", quote=True)
            start_download_from_retry(client, message.chat.id, retry_id)
            return

    reply = message.reply_to_message
    if reply and reply.reply_markup and reply.reply_markup.inline_keyboard:
        for row in reply.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    if btn.callback_data.startswith("dl:") or btn.callback_data.startswith("rename:"):
                        link_id = btn.callback_data.split(":")[1]
                        if link_id in links_store:
                            links_store[link_id]["title"] = new_name
                            update_format_selector(client, reply.chat.id, reply.id, link_id)
                            message.reply_text(f"✅ Filename updated to:\n`{new_name}`", quote=True)
                            return
                    elif btn.callback_data.startswith("retry_dl:") or btn.callback_data.startswith("rename_dl:"):
                        retry_id = btn.callback_data.split(":")[1]
                        if retry_id in retry_downloads:
                            retry_downloads[retry_id]["title"] = new_name
                            message.reply_text(f"✅ Filename set to:\n`{new_name}`\n⏳ Retrying download...", quote=True)
                            start_download_from_retry(client, message.chat.id, retry_id)
                            return

    message.reply_text("⚠️ Could not find an active link to rename. Please click the **✏️ Rename** button on the video message.", quote=True)


def handle_text(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if not is_user_allowed(user_id):
        return

    # Check for cancel
    if message.text.strip().lower() in ["/cancel", "cancel"]:
        if user_id in pending_renames:
            pending_renames.pop(user_id, None)
            message.reply_text("❌ Rename cancelled.", quote=True)
            return

    # If user is in pending renames, process the new filename
    if user_id in pending_renames:
        rename_info = pending_renames.pop(user_id)
        raw_text = message.text.strip()
        new_name = clean_filename(raw_text, max_len=120)

        link_id = rename_info.get("link_id")
        retry_id = rename_info.get("retry_id")

        if link_id and link_id in links_store:
            links_store[link_id]["title"] = new_name
            chat_id = rename_info.get("chat_id")
            msg_id = rename_info.get("message_id")
            if chat_id and msg_id:
                update_format_selector(client, chat_id, msg_id, link_id)
            message.reply_text(f"✅ Filename updated to:\n`{new_name}`", quote=True)
            return

        if retry_id and retry_id in retry_downloads:
            retry_downloads[retry_id]["title"] = new_name
            message.reply_text(f"✅ Filename set to:\n`{new_name}`\n⏳ Starting download...", quote=True)
            start_download_from_retry(client, message.chat.id, retry_id)
            return


def register_handlers(app: Client):
    app.on_message(filters.command("start"))(handle_start)
    app.on_message(filters.command("caption"))(handle_caption)
    app.on_message(filters.command("rename"))(handle_rename)
    app.on_message(filters.regex(r'https?://[^\s]+'))(handle_link)
    app.on_message(filters.text & ~filters.command(["start", "caption", "rename"]))(handle_text)
    app.on_callback_query()(handle_callback)

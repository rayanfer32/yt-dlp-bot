# 🎬 Telegram Video Downloader Bot

A fast, feature-rich Telegram video downloader bot powered by [Pyrogram](https://github.com/pyrogram/pyrogram) and [yt-dlp](https://github.com/yt-dlp/yt-dlp), with modern project and dependency management using [uv](https://docs.astral.sh/uv/).

---

## ✨ Features

- 🌐 **Massive Site Support**: Supports 1,000+ video platforms supported by `yt-dlp` (YouTube, Instagram, Twitter/X, TikTok, Reddit, Facebook, Vimeo, Twitch, etc.) as well as **direct media links** (`.mp4`, `.mkv`, etc.).
- 📊 **Real-Time Progress Indicators**:
  - Visual graphical progress bar (`[■■■■■□□□□□]`) for both **downloading** and **uploading**.
  - Displays live percentage, downloaded/total file size, transfer speed, and remaining ETA.
  - Smart throttling (3.5s interval) and rate-limit handling to avoid Telegram `FloodWait`.
  - Self-cleaning status message that updates in-place and removes itself once the video is delivered.
- 🎛️ **Resolution & Format Selector**:
  - Parses video formats and presents interactive buttons for available resolutions (1080p, 720p, 480p, etc.) and protocols.
  - Graceful fallback for direct files and single-stream URLs.
- 🛑 **Interactive Cancel Button**:
  - Cancel ongoing downloads on demand with a single tap.
- 🔒 **User Whitelist / Access Control**:
  - Optional `ALLOWED_IDS` setting to restrict bot access to specified Telegram User IDs.
  - Open to all users if left blank.
- 🟢 **Startup Readiness Notification**:
  - Sends a `🟢 Bot started successfully` ping to authorized users when the bot goes online.
- 🧹 **Automatic Cleanup & Size Safety**:
  - Per-user download directories to avoid collisions.
  - Automatically deletes downloaded files from server disk after sending.
  - Prevents Telegram API errors by verifying file size before upload (2GB standard Telegram bot limit).
- ⚡ **Modern Python Tooling (`uv`)**:
  - Fast, reproducible dependency resolution with `uv.lock`.
  - Secure `.env` configuration for credentials and tokens.

---

## 📋 Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** package manager:
  ```bash
  # Linux / macOS
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **Telegram API Credentials**:
  - `API_ID` & `API_HASH` from [my.telegram.org](https://my.telegram.org)
  - `BOT_TOKEN` from [@BotFather](https://t.me/BotFather)

---

## 🚀 Quick Start

### 1. Clone the repository
```bash
git clone git@github.com:rayanfer32/yt-dlp-bot.git
cd yt-dlp-bot
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Open `.env` in your text editor and fill in your values:
```env
# Telegram API Credentials (from https://my.telegram.org)
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef

# Telegram Bot Token (from @BotFather)
BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ

# App / Session Name (optional, defaults to yt_dlp_bot)
APP_TITLE=yt_dlp_bot

# Allowed Telegram User IDs (optional, comma-separated)
# If empty, all users can use the bot
ALLOWED_IDS=628650705
```

### 3. Install Dependencies
```bash
uv sync
```

### 4. Run the Bot
```bash
uv run main.py
# or
uv run python yt_dlp_tgbot.py
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
| :--- | :---: | :--- |
| `API_ID` | **Yes** | Telegram App API ID from [my.telegram.org](https://my.telegram.org). |
| `API_HASH` | **Yes** | Telegram App API Hash from [my.telegram.org](https://my.telegram.org). |
| `BOT_TOKEN` | **Yes** | Telegram Bot Token from [@BotFather](https://t.me/BotFather). |
| `APP_TITLE` | No | Pyrogram session file name (default: `yt_dlp_bot`). |
| `ALLOWED_IDS`| No | Comma-separated list of Telegram user IDs permitted to use the bot. |
| `MAX_CONCURRENT_DOWNLOADS` | No | Number of downloads permitted to run in parallel (default: `5`). |

---

## 📖 Bot Usage

1. Open a chat with your bot on Telegram and send `/start`.
2. Paste any supported video link or direct media URL.
3. Choose your preferred resolution / format from the reply keyboard.
4. Watch real-time download and upload progress indicators.
5. Tap **Cancel Download** on any download progress message to cancel that specific task.
6. The bot uploads the video directly into the chat with the original video title as caption, then cleans up temporary files.
7. **Edit Caption**: Reply to any sent video with `/caption <new caption>` to instantly update its caption. The bot updates the caption and auto-deletes the command and confirmation after 3 seconds to keep your chat clean.

---

## 🛠️ Production Deployment

### Running with PM2
To keep the bot running in the background and auto-restart across crashes or server reboots:

```bash
# Start bot under pm2
pm2 start "uv run main.py" --name tgdlbot

# View live logs
pm2 logs tgdlbot

# Restart bot after pulling updates
pm2 restart tgdlbot
```

### Useful One-Liner for Server Updates:
```bash
(cd ~/bots/yt-dlp-bot && git pull && pm2 restart tgdlbot && pm2 logs)
```

### Uploading Files up to 4GB (Telegram Premium)
Standard Telegram bot accounts can upload files up to **2GB**. If using a bot associated with a Telegram Premium account or custom local Bot API server supporting 4GB:
Update line 265 in [`yt_dlp_tgbot.py`](file:///c:/CODE/yt-dlp-bot/yt_dlp_tgbot.py):
```python
if file_size > 4 * 1024 * 1024 * 1024:  # 4GB in bytes
```

---

## 🔗 Supported Platforms
See [yt-dlp Supported Sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) for the full list of compatible websites.

---

## 📜 Credits & Libraries
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - Video extraction and downloading engine
- [Pyrogram](https://github.com/pyrogram/pyrogram) - Elegant Telegram MTProto API framework
- [uv](https://docs.astral.sh/uv/) - Fast Python package and environment manager

# start bot once
pm2 start "uv run main.py" --name tgdlbot

# update and restart bot
(cd ~/bots/yt-dlp-bot && git pull && uv sync && pm2 restart tgdlbot && pm2 logs tgdlbot)

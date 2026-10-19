# start bot once
# 2. Start Python directly with main.py
pm2 start .venv/bin/python --name tgdlbot -- main.py

# update and restart bot
(cd ~/bots/yt-dlp-bot && git pull && uv sync && pm2 restart tgdlbot && pm2 logs tgdlbot)
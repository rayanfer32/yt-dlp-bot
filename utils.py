import re


def clean_filename(name, max_len=120):
    if not name:
        return "Video"
    cleaned = re.sub(r'[\/\\:*?"<>|\x00-\x1f]', '', str(name)).strip(" .")
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "Video"


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

import os
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler
from config import LOG_LEVEL

# Ensure line-buffering on stdout/stderr for real-time PM2 output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass


class FlushStreamHandler(logging.StreamHandler):
    """
    StreamHandler that flushes immediately after every write,
    ensuring pm2 logs and terminal see output in real time without buffer lag.
    """
    def emit(self, record):
        super().emit(record)
        self.flush()


_is_initialized = False


def setup_logger(name: str = "yt_dlp_bot") -> logging.Logger:
    global _is_initialized
    level = getattr(logging, LOG_LEVEL, logging.DEBUG)

    root_logger = logging.getLogger()

    if not _is_initialized:
        root_logger.setLevel(level)

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | [%(name)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # Console handler with instant flush
        console_handler = FlushStreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # Rotating file handler (10MB per file, 5 backups)
        try:
            file_handler = RotatingFileHandler(
                "bot.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8"
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            sys.stderr.write(f"Warning: Could not setup bot.log file handler: {e}\n")

        # Global unhandled exception hook for main thread
        def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return
            root_logger.critical(
                "CRITICAL: Unhandled exception in main thread:",
                exc_info=(exc_type, exc_value, exc_traceback)
            )

        # Unhandled exception hook for background threads (Python 3.8+)
        def handle_thread_exception(args):
            root_logger.critical(
                f"CRITICAL: Unhandled exception in thread '{args.thread.name}':",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
            )

        sys.excepthook = handle_unhandled_exception
        if hasattr(threading, "excepthook"):
            threading.excepthook = handle_thread_exception

        # Tune Pyrogram logger
        pyrogram_level = logging.INFO if level != logging.DEBUG else logging.DEBUG
        logging.getLogger("pyrogram").setLevel(pyrogram_level)

        _is_initialized = True

    return logging.getLogger(name)


logger = setup_logger("yt_dlp_bot")

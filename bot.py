"""
Telegram Save Bot — downloads videos and music from YouTube, Instagram, TikTok, etc.

Features:
    - Send a URL → downloads video (max 1080p)
    - /music + URL → downloads audio as MP3
    - Progress bar during download
    - Caching via Telegram file_id (no files stored on server)
"""

import json
import logging
import os
import re
import asyncio
import time
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, MAX_VIDEO_HEIGHT, MAX_VIDEO_SIZE_MB, MAX_AUDIO_SIZE_MB, DOWNLOAD_DIR

# Resolution fallback chain: try each height in order if the file is too large
FALLBACK_HEIGHTS: list[int] = [1080, 720, 480, 360]

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Silence httpx to prevent bot token from leaking in request URLs
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── URL detection regex ──────────────────────────────────────────────────────
URL_REGEX = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|"
    r"twitter\.com|x\.com|facebook\.com|fb\.watch|"
    r"vimeo\.com|dailymotion\.com|twitch\.tv|"
    r"reddit\.com|soundcloud\.com|bandcamp\.com|"
    r"vk\.com|ok\.ru|rutube\.ru|"
    r"[\w.-]+\.\w{2,})"  # fallback for other yt-dlp supported sites
    r"[^\s]*"
)

# Track users in "music mode" — they sent /music and we await a URL
music_mode_users: set[int] = set()


# ─── Cache (URL → Telegram file_id) ───────────────────────────────────────────

CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache.json")


def _load_cache() -> dict:
    """Load the file_id cache from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load cache: {e}")
    return {}


def _save_cache(cache: dict) -> None:
    """Save the file_id cache to disk."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Failed to save cache: {e}")


def _get_cache_key(url: str, media_type: str) -> str:
    """Generate a cache key from URL and media type."""
    # Normalize URL: remove tracking params, trailing slashes
    clean_url = url.rstrip("/")
    return f"{media_type}:{clean_url}"


def _cache_get(url: str, media_type: str) -> dict | None:
    """
    Look up a cached entry.
    Returns dict with 'file_id', 'title', 'file_type' or None.
    """
    cache = _load_cache()
    key = _get_cache_key(url, media_type)
    return cache.get(key)


def _cache_set(url: str, media_type: str, file_id: str, title: str, file_type: str) -> None:
    """Store a file_id in the cache."""
    cache = _load_cache()
    key = _get_cache_key(url, media_type)
    cache[key] = {
        "file_id": file_id,
        "title": title,
        "file_type": file_type,
        "cached_at": int(time.time()),
    }
    _save_cache(cache)


# ─── Progress Bar ──────────────────────────────────────────────────────────────

def _make_progress_bar(percent: float, width: int = 20) -> str:
    """Create a text-based progress bar."""
    filled = int(width * percent / 100)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percent:.0f}%"


class ProgressTracker:
    """
    Tracks yt-dlp download progress and provides data
    for async status message updates.
    """

    def __init__(self):
        self.percent: float = 0.0
        self.speed: str = ""
        self.eta: str = ""
        self.status: str = "downloading"
        self.last_update_time: float = 0.0

    def hook(self, d: dict) -> None:
        """yt-dlp progress hook callback."""
        self.status = d.get("status", "downloading")

        if self.status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)

            if total > 0:
                self.percent = (downloaded / total) * 100
            else:
                self.percent = 0

            # Speed
            speed = d.get("speed")
            if speed:
                if speed >= 1024 * 1024:
                    self.speed = f"{speed / (1024 * 1024):.1f} MB/s"
                elif speed >= 1024:
                    self.speed = f"{speed / 1024:.0f} KB/s"
                else:
                    self.speed = f"{speed:.0f} B/s"

            # ETA
            eta = d.get("eta")
            if eta is not None:
                mins, secs = divmod(int(eta), 60)
                self.eta = f"{mins}:{secs:02d}"

        elif self.status == "finished":
            self.percent = 100

    def should_update_message(self, interval: float = 2.0) -> bool:
        """Rate-limit message updates to avoid Telegram API flood."""
        now = time.time()
        if now - self.last_update_time >= interval:
            self.last_update_time = now
            return True
        return False

    def format_status(self, media_type: str = "видео") -> str:
        """Format current progress as a status message."""
        bar = _make_progress_bar(self.percent)
        parts = [f"⏳ Скачиваю {media_type}...\n\n{bar}"]
        if self.speed:
            parts.append(f"⚡ {self.speed}")
        if self.eta:
            parts.append(f"⏱ Осталось: {self.eta}")
        return "\n".join(parts)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _get_yt_dlp_video_opts(download_dir: str, progress_hook=None, max_height: int = MAX_VIDEO_HEIGHT) -> dict:
    """yt-dlp options for video download with configurable max height."""
    opts = {
        "format": (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={max_height}]+bestaudio"
            f"/best[height<={max_height}]"
            f"/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "writethumbnail": False,
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _get_yt_dlp_audio_opts(download_dir: str, progress_hook=None) -> dict:
    """yt-dlp options for audio-only download (MP3 320kbps)."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


async def _download_with_progress(
    url: str,
    opts: dict,
    status_msg,
    media_label: str,
    tracker: ProgressTracker,
) -> tuple[str | None, dict | None]:
    """
    Download media using yt-dlp in a thread pool while updating
    the status message with a progress bar.
    Returns (file_path, info_dict) or (None, None) on error.
    """
    loop = asyncio.get_event_loop()
    download_done = asyncio.Event()
    result: list = [None, None]  # [filepath, info]

    def _do_download():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return
                filename = ydl.prepare_filename(info)
                # For audio, extension changes to mp3 after postprocessing
                if opts.get("postprocessors"):
                    for pp in opts["postprocessors"]:
                        if pp.get("key") == "FFmpegExtractAudio":
                            filename = os.path.splitext(filename)[0] + ".mp3"
                            break
                result[0] = filename
                result[1] = info
        except Exception as e:
            logger.error(f"Download error: {e}")
        finally:
            loop.call_soon_threadsafe(download_done.set)

    # Start download in background thread
    loop.run_in_executor(None, _do_download)

    # Update progress message while downloading
    while not download_done.is_set():
        await asyncio.sleep(1.0)
        if tracker.should_update_message(interval=2.0):
            try:
                new_text = tracker.format_status(media_label)
                await status_msg.edit_text(new_text)
            except Exception:
                pass  # Ignore edit errors (e.g. message not modified)

    return result[0], result[1]


def _cleanup_file(filepath: str) -> None:
    """Remove downloaded file after sending."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning(f"Failed to remove file {filepath}: {e}")


def _extract_url(text: str) -> str | None:
    """Extract the first URL from text."""
    match = URL_REGEX.search(text)
    return match.group(0) if match else None


def _format_title(info: dict | None) -> str:
    """Format a human-readable title from yt-dlp info."""
    if not info:
        return "Unknown"
    title = info.get("title", "Unknown")
    duration = info.get("duration")
    if duration:
        mins, secs = divmod(int(duration), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{title} [{hours}:{mins:02d}:{secs:02d}]"
        return f"{title} [{mins}:{secs:02d}]"
    return title


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command — full usage instruction."""
    welcome = (
        "👋 *Привет!*\n\n"
        "🎬 *Видео* — отправь ссылку\n"
        "🎵 *Музыка* — /music, затем ссылку\n\n"
        "/help — справка"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "*Команды:*\n\n"
        "/music — скачать музыку\n"
        "/cancel — отменить режим музыки\n\n"
        "Просто отправь ссылку — получишь видео.\n"
        "Отправь /music и ссылку — получишь аудио."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /music command — enter music download mode."""
    user_id = update.effective_user.id

    # Check if URL was provided with the command: /music https://...
    if context.args:
        url = " ".join(context.args)
        extracted = _extract_url(url)
        if extracted:
            await _process_audio_download(update, context, extracted)
            return

    music_mode_users.add(user_id)
    await update.message.reply_text(
        "🎵 Отправь ссылку — скачаю музыку.\n"
        "Отмена: /cancel",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command — exit music mode."""
    user_id = update.effective_user.id
    if user_id in music_mode_users:
        music_mode_users.discard(user_id)
        await update.message.reply_text("✅ Режим музыки отключён.")
    else:
        await update.message.reply_text("Режим музыки не активен.")


# ─── Message Handler (URL detection) ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — detect URLs and download media."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    url = _extract_url(text)

    if not url:
        await update.message.reply_text("🔗 Отправь ссылку.")
        return

    user_id = update.effective_user.id

    if user_id in music_mode_users:
        music_mode_users.discard(user_id)  # One-time mode
        await _process_audio_download(update, context, url)
    else:
        await _process_video_download(update, context, url)


# ─── Download Processors ──────────────────────────────────────────────────────

async def _process_video_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    """Download and send video, with caching, progress bar, and resolution fallback."""

    # ── Check cache first ──
    cached = _cache_get(url, "video")
    if cached:
        try:
            await update.message.reply_video(
                video=cached["file_id"],
                caption=f"🎬 {cached['title']}",
                supports_streaming=True,
            )
            return
        except Exception as e:
            logger.warning(f"Cache hit failed, re-downloading: {e}")

    # ── Download with resolution fallback ──
    status_msg = await update.message.reply_text("⏳ Скачиваю...")
    filepath = None
    info = None
    used_height = MAX_VIDEO_HEIGHT

    for height in FALLBACK_HEIGHTS:
        tracker = ProgressTracker()
        opts = _get_yt_dlp_video_opts(DOWNLOAD_DIR, progress_hook=tracker.hook, max_height=height)
        filepath, info = await _download_with_progress(url, opts, status_msg, "видео", tracker)

        if not filepath or not os.path.exists(filepath):
            await status_msg.edit_text("❌ Не удалось скачать. Проверь ссылку.")
            return

        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)

        if file_size_mb <= MAX_VIDEO_SIZE_MB:
            used_height = height
            break  # File fits — proceed to send

        # File too large — try a lower resolution
        logger.info(f"File too large at {height}p ({file_size_mb:.0f} MB), trying lower resolution")
        _cleanup_file(filepath)
        filepath = None

        # Pick the next resolution to show in status
        current_idx = FALLBACK_HEIGHTS.index(height)
        if current_idx + 1 < len(FALLBACK_HEIGHTS):
            next_height = FALLBACK_HEIGHTS[current_idx + 1]
            await status_msg.edit_text(
                f"📐 В {height}p файл слишком большой.\n"
                f"⏳ Пробую {next_height}p..."
            )
        else:
            # Exhausted all resolutions
            await status_msg.edit_text("❌ Файл слишком большой даже в 360p. Попробуй что-то покороче.")
            return

    if not filepath or not os.path.exists(filepath):
        await status_msg.edit_text("❌ Не удалось скачать. Проверь ссылку.")
        return

    title = _format_title(info)
    quality_note = f" ({used_height}p)" if used_height < MAX_VIDEO_HEIGHT else ""

    try:
        await status_msg.edit_text("📤 Отправляю...")
        with open(filepath, "rb") as video_file:
            sent_message = await update.message.reply_video(
                video=video_file,
                caption=f"🎬 {title}{quality_note}",
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )
        await status_msg.delete()

        # ── Save to cache ──
        if sent_message.video:
            _cache_set(url, "video", sent_message.video.file_id, title, "video")
            logger.info(f"Cached video: {title} at {used_height}p")

    except Exception as e:
        logger.error(f"Error sending video: {e}")
        await status_msg.edit_text("❌ Не удалось отправить. Попробуй ещё раз.")
    finally:
        _cleanup_file(filepath)


async def _process_audio_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    """Download and send audio, with caching and progress bar."""

    # ── Check cache first ──
    cached = _cache_get(url, "audio")
    if cached:
        try:
            await update.message.reply_audio(
                audio=cached["file_id"],
                caption=f"🎵 {cached['title']}",
            )
            return
        except Exception as e:
            logger.warning(f"Cache hit failed, re-downloading: {e}")

    # ── Download with progress ──
    tracker = ProgressTracker()
    status_msg = await update.message.reply_text("⏳ Скачиваю...")

    opts = _get_yt_dlp_audio_opts(DOWNLOAD_DIR, progress_hook=tracker.hook)
    filepath, info = await _download_with_progress(url, opts, status_msg, "аудио", tracker)

    if not filepath or not os.path.exists(filepath):
        await status_msg.edit_text("❌ Не удалось скачать. Проверь ссылку.")
        return

    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    title = _format_title(info)

    if file_size_mb > MAX_AUDIO_SIZE_MB:
        _cleanup_file(filepath)
        await status_msg.edit_text("❌ Файл слишком большой. Попробуй что-то покороче.")
        return

    try:
        await status_msg.edit_text("📤 Отправляю...")

        performer = info.get("artist", info.get("uploader", "Unknown")) if info else "Unknown"
        track_title = info.get("track", info.get("title", "Unknown")) if info else "Unknown"

        with open(filepath, "rb") as audio_file:
            sent_message = await update.message.reply_audio(
                audio=audio_file,
                caption=f"🎵 {title}",
                performer=performer,
                title=track_title,
                read_timeout=120,
                write_timeout=120,
            )
        await status_msg.delete()

        # ── Save to cache ──
        if sent_message.audio:
            _cache_set(url, "audio", sent_message.audio.file_id, title, "audio")
            logger.info(f"Cached audio: {title}")

    except Exception as e:
        logger.error(f"Error sending audio: {e}")
        await status_msg.edit_text("❌ Не удалось отправить. Попробуй ещё раз.")
    finally:
        _cleanup_file(filepath)


# ─── Error Handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error(f"Exception while handling an update: {context.error}")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Start the bot."""
    logger.info("🚀 Starting Telegram Save Bot...")

    # Load cache on startup
    cache = _load_cache()
    logger.info(f"📦 Cache loaded: {len(cache)} entries")
    
    app = Application.builder().token(BOT_TOKEN).base_url('http://127.0.0.1:8081/bot').build()
    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    app.add_error_handler(error_handler)

    # Run the bot
    logger.info("✅ Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

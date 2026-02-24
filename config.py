"""
Configuration module.
Loads the bot token from .env file. All other settings are constants.
"""

import os
import sys
from dotenv import load_dotenv

# Load .env file from the same directory as this script
load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN is not set. Please create a .env file with BOT_TOKEN=your_token")
    sys.exit(1)

# ─── Download Settings (constants) ────────────────────────────────────────────
MAX_VIDEO_HEIGHT: int = 1080          # Max video resolution height in pixels
MAX_VIDEO_SIZE_MB: int = 2048         # Telegram video upload limit (2 GB)
MAX_AUDIO_SIZE_MB: int = 50           # Telegram audio upload limit (50 MB)
DOWNLOAD_DIR: str = "/tmp/tg_downloads"

# Ensure download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

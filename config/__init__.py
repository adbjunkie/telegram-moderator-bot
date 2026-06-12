import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

PORT = int(os.getenv("PORT", "8000"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_raw_url = os.getenv("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
if not _raw_url.startswith(("http://", "https://")):
    _raw_url = f"https://{_raw_url}"
PUBLIC_BASE_URL = _raw_url

_use_webhook = PUBLIC_BASE_URL.startswith("https://") and "localhost" not in PUBLIC_BASE_URL
USE_WEBHOOK = _use_webhook
WEBHOOK_PATH = "/webhook"

# Bot admin IDs (comma-separated Telegram user IDs who can use /config etc.)
BOT_ADMINS = set(
    int(uid.strip())
    for uid in os.getenv("BOT_ADMINS", "").split(",")
    if uid.strip().isdigit()
)

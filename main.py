import asyncio
import logging
import os
import sys
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, Message
from aiogram import F, Router
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8000))

_raw_url = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
if not _raw_url.startswith(("http://", "https://")):
    _raw_url = f"https://{_raw_url}"
PUBLIC_BASE_URL = _raw_url
USE_WEBHOOK = "localhost" not in PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")
WEBHOOK_PATH = "/webhook"

# How many recent media messages to keep before deleting older ones
MEDIA_KEEP_COUNT = int(os.environ.get("MEDIA_KEEP_COUNT", 10))

# Backup bot links posted periodically (one per line in env var, or edit here)
_raw_links = os.environ.get(
    "BACKUP_LINKS",
    "https://t.me/your_backup_bot_1\nhttps://t.me/your_backup_bot_2",
)
BACKUP_LINKS: list[str] = [l.strip() for l in _raw_links.splitlines() if l.strip()]

# How often to post backup links (seconds). Default: every 6 hours.
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL_SECONDS", 6 * 3600))

# Chats the bot should post backup links to (comma-separated chat IDs).
# If empty, posts to every chat it has seen media in.
_raw_chats = os.environ.get("BACKUP_CHAT_IDS", "")
BACKUP_CHAT_IDS: list[int] = [int(c.strip()) for c in _raw_chats.split(",") if c.strip().lstrip("-").isdigit()]

# ── State ─────────────────────────────────────────────────────────────────────

# Per-chat deque of (message_id,) for media messages, newest last
media_queues: dict[int, deque] = defaultdict(lambda: deque())

# All chats we've seen (used when BACKUP_CHAT_IDS is empty)
known_chats: set[int] = set()

# ── Bot / Dispatcher ──────────────────────────────────────────────────────────

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ── Media handler ─────────────────────────────────────────────────────────────

@router.message(F.photo | F.video)
async def handle_media(message: Message):
    chat_id = message.chat.id
    known_chats.add(chat_id)
    q = media_queues[chat_id]
    q.append(message.message_id)

    # Delete anything beyond the keep window
    while len(q) > MEDIA_KEEP_COUNT:
        old_id = q.popleft()
        try:
            await bot.delete_message(chat_id, old_id)
            logger.info(f"Deleted old media message {old_id} in chat {chat_id}")
        except Exception as e:
            logger.warning(f"Could not delete message {old_id} in chat {chat_id}: {e}")


# ── Backup link poster ────────────────────────────────────────────────────────

async def post_backup_links():
    """Post backup bot links to configured chats, then repeat on interval."""
    await asyncio.sleep(BACKUP_INTERVAL)  # wait before first post
    while True:
        targets = BACKUP_CHAT_IDS if BACKUP_CHAT_IDS else list(known_chats)
        if not targets:
            logger.info("No chats to post backup links to yet.")
        else:
            text = "🔗 <b>Backup bots:</b>\n" + "\n".join(BACKUP_LINKS)
            for chat_id in targets:
                try:
                    await bot.send_message(chat_id, text, disable_web_page_preview=True)
                    logger.info(f"Posted backup links to {chat_id}")
                except Exception as e:
                    logger.warning(f"Failed to post backup links to {chat_id}: {e}")
        await asyncio.sleep(BACKUP_INTERVAL)


# ── FastAPI / lifespan ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if USE_WEBHOOK:
        webhook_url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook set: {webhook_url}")
    else:
        logger.info("Running in polling mode")

    asyncio.create_task(post_backup_links())

    yield

    if USE_WEBHOOK:
        await bot.delete_webhook()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "media_keep": MEDIA_KEEP_COUNT, "backup_interval_hours": BACKUP_INTERVAL // 3600}


async def _handle(data: dict):
    try:
        await dp.feed_update(bot, Update.model_validate(data))
    except Exception as e:
        logger.error(f"Update error: {e}", exc_info=True)


if USE_WEBHOOK:
    @app.post(WEBHOOK_PATH)
    async def webhook(request: Request):
        asyncio.create_task(_handle(await request.json()))
        return PlainTextResponse("ok")


if __name__ == "__main__":
    if USE_WEBHOOK:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        async def _poll():
            await bot.delete_webhook(drop_pending_updates=True)
            asyncio.create_task(post_backup_links())
            await dp.start_polling(bot)
        asyncio.run(_poll())

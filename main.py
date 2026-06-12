import asyncio
import json
import logging
import os
import sys
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Update, Message
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
PORT           = int(os.environ.get("PORT", 8000))
OWNER_ID       = 5815775162

_raw_url = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
if not _raw_url.startswith(("http://", "https://")):
    _raw_url = f"https://{_raw_url}"
PUBLIC_BASE_URL = _raw_url
USE_WEBHOOK     = "localhost" not in PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")
WEBHOOK_PATH    = "/webhook"

DATA_DIR  = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# ── Persistent state ───────────────────────────────────────────────────────────
# Stored in DATA_DIR/state.json so settings survive restarts.
#
# Schema:
# {
#   "media_keep":    10,
#   "backup_links":  ["https://t.me/..."],
#   "interval":      21600,
#   "group_ids":     [-100123456789],
# }

DEFAULTS = {
    "media_keep":   10,
    "backup_links": [],
    "interval":     21600,   # seconds (6 hours)
    "group_ids":    [],
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


state = load_state()

# ── Runtime state ──────────────────────────────────────────────────────────────

# Per-group deque of message IDs for media, newest last
media_queues: dict[int, deque] = defaultdict(deque)

# ── Bot / Dispatcher ───────────────────────────────────────────────────────────

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
router = Router()
dp.include_router(router)

# ── Owner guard ────────────────────────────────────────────────────────────────

def owner_only(func):
    """Decorator: ignore messages not from the owner in private chat."""
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != OWNER_ID or message.chat.type != "private":
            return
        return await func(message, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Group media handler ────────────────────────────────────────────────────────

@router.message(F.photo | F.video)
async def handle_media(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    chat_id = message.chat.id
    # Auto-register groups we see traffic in
    if chat_id not in state["group_ids"]:
        state["group_ids"].append(chat_id)
        save_state(state)

    q = media_queues[chat_id]
    q.append(message.message_id)

    while len(q) > state["media_keep"]:
        old_id = q.popleft()
        try:
            await bot.delete_message(chat_id, old_id)
            logger.info(f"Deleted media {old_id} in {chat_id}")
        except Exception as e:
            logger.warning(f"Could not delete {old_id} in {chat_id}: {e}")


# ── Private commands (owner only) ──────────────────────────────────────────────

@router.message(Command("start"))
@owner_only
async def cmd_start(message: Message):
    await message.answer(
        "<b>Bot config</b>\n\n"
        "/status — show current settings\n"
        "/setkeep &lt;n&gt; — keep last N media messages per group (default 10)\n"
        "/setlinks &lt;url1&gt; &lt;url2&gt; ... — set backup bot links (space or newline separated)\n"
        "/setinterval &lt;hours&gt; — how often to post links (default 6h)\n"
        "/addgroup &lt;chat_id&gt; — add a group to post links to\n"
        "/removegroup &lt;chat_id&gt; — remove a group\n"
        "/postlinks — post backup links to all groups right now\n"
    )


@router.message(Command("status"))
@owner_only
async def cmd_status(message: Message):
    s = state
    links = "\n".join(s["backup_links"]) or "none"
    groups = ", ".join(str(g) for g in s["group_ids"]) or "none (auto-detected from traffic)"
    await message.answer(
        f"<b>Current settings</b>\n\n"
        f"Media keep: <code>{s['media_keep']}</code>\n"
        f"Post interval: <code>{s['interval'] // 3600}h {(s['interval'] % 3600) // 60}m</code>\n"
        f"Groups: <code>{groups}</code>\n\n"
        f"Backup links:\n{links}"
    )


@router.message(Command("setkeep"))
@owner_only
async def cmd_setkeep(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /setkeep &lt;number&gt;  e.g. /setkeep 10")
        return
    state["media_keep"] = int(arg)
    save_state(state)
    await message.answer(f"✅ Keeping last <b>{state['media_keep']}</b> media messages per group.")


@router.message(Command("setlinks"))
@owner_only
async def cmd_setlinks(message: Message, command: CommandObject):
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Usage: /setlinks &lt;url1&gt; &lt;url2&gt; ...\nOne URL per line or space-separated.")
        return
    links = [l.strip() for l in raw.replace("\n", " ").split() if l.strip().startswith("http")]
    if not links:
        await message.answer("No valid URLs found. Make sure they start with http.")
        return
    state["backup_links"] = links
    save_state(state)
    await message.answer(f"✅ Backup links set:\n" + "\n".join(links))


@router.message(Command("setinterval"))
@owner_only
async def cmd_setinterval(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /setinterval &lt;hours&gt;  e.g. /setinterval 6")
        return
    hours = int(arg)
    state["interval"] = hours * 3600
    save_state(state)
    await message.answer(f"✅ Backup links will be posted every <b>{hours}h</b>.")


@router.message(Command("addgroup"))
@owner_only
async def cmd_addgroup(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Usage: /addgroup &lt;chat_id&gt;")
        return
    chat_id = int(arg)
    if chat_id not in state["group_ids"]:
        state["group_ids"].append(chat_id)
        save_state(state)
    await message.answer(f"✅ Group <code>{chat_id}</code> added.")


@router.message(Command("removegroup"))
@owner_only
async def cmd_removegroup(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Usage: /removegroup &lt;chat_id&gt;")
        return
    chat_id = int(arg)
    if chat_id in state["group_ids"]:
        state["group_ids"].remove(chat_id)
        save_state(state)
    await message.answer(f"✅ Group <code>{chat_id}</code> removed.")


@router.message(Command("postlinks"))
@owner_only
async def cmd_postlinks(message: Message):
    await _post_backup_links()
    await message.answer("✅ Posted backup links to all groups.")


# ── Backup link poster ─────────────────────────────────────────────────────────

async def _post_backup_links():
    if not state["backup_links"]:
        logger.info("No backup links configured, skipping post.")
        return
    text = "\n".join(state["backup_links"])
    for chat_id in list(state["group_ids"]):
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
            logger.info(f"Posted backup links to {chat_id}")
        except Exception as e:
            logger.warning(f"Failed to post to {chat_id}: {e}")


async def backup_link_loop():
    while True:
        await asyncio.sleep(state["interval"])
        await _post_backup_links()


# ── FastAPI / lifespan ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if USE_WEBHOOK:
        webhook_url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook: {webhook_url}")
    else:
        logger.info("Polling mode")

    asyncio.create_task(backup_link_loop())
    yield

    if USE_WEBHOOK:
        await bot.delete_webhook()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok"}


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
            asyncio.create_task(backup_link_loop())
            await dp.start_polling(bot)
        asyncio.run(_poll())

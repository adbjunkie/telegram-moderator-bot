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
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Update, Message, MessageReactionUpdated
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
PORT        = int(os.environ.get("PORT", 8000))
OWNER_ID    = 5815775162

_raw_url = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
if not _raw_url.startswith(("http://", "https://")):
    _raw_url = f"https://{_raw_url}"
PUBLIC_BASE_URL = _raw_url
USE_WEBHOOK     = "localhost" not in PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")
WEBHOOK_PATH    = "/webhook"

DATA_DIR   = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# ── Persistent state ───────────────────────────────────────────────────────────
# {
#   "keep_count":        10,       # delete messages once there are >N in the queue
#   "keep_minutes":      0,        # delete messages older than N minutes (0 = disabled)
#   "delete_mode":       "count",  # "count" or "time"
#   "reaction_threshold": 3,       # messages with >= this many reactions are kept forever
#   "backup_links":      [],
#   "interval":          1800,     # seconds between backup posts (default 30 min)
#   "group_ids":         [],
# }

DEFAULTS = {
    "keep_count":         10,
    "keep_minutes":       60,
    "delete_mode":        "count",   # "count" or "time"
    "reaction_threshold": 3,
    "backup_links":       [],
    "interval":           1800,      # 30 minutes
    "group_ids":          [],
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


state = load_state()

# ── Runtime state ──────────────────────────────────────────────────────────────

# Per-group queue of message_ids (all messages, not just media), newest last
msg_queues: dict[int, deque] = defaultdict(deque)

# message_ids that have been saved by reactions and must never be deleted
# key: (chat_id, message_id)
saved_by_reactions: set[tuple] = set()

# pending timed-deletion tasks: (chat_id, message_id) -> asyncio.Task
deletion_tasks: dict[tuple, asyncio.Task] = {}

# ── Bot / Dispatcher ───────────────────────────────────────────────────────────

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
router = Router()
dp.include_router(router)


# ── Owner guard ────────────────────────────────────────────────────────────────

class OwnerPrivate(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return (
            message.from_user is not None
            and message.from_user.id == OWNER_ID
            and message.chat.type == "private"
        )


# ── Deletion helpers ───────────────────────────────────────────────────────────

def _is_saved(chat_id: int, message_id: int) -> bool:
    return (chat_id, message_id) in saved_by_reactions


async def _delete_if_not_saved(chat_id: int, message_id: int):
    """Delete a message unless reactions have saved it."""
    if _is_saved(chat_id, message_id):
        logger.info(f"Skipping deletion of {message_id} in {chat_id} — saved by reactions")
        return
    try:
        await bot.delete_message(chat_id, message_id)
        logger.info(f"Deleted message {message_id} in {chat_id}")
    except Exception as e:
        logger.warning(f"Could not delete {message_id} in {chat_id}: {e}")


async def _timed_delete(chat_id: int, message_id: int, delay_seconds: int):
    """Wait delay_seconds then delete, unless saved by reactions."""
    await asyncio.sleep(delay_seconds)
    key = (chat_id, message_id)
    deletion_tasks.pop(key, None)
    await _delete_if_not_saved(chat_id, message_id)


def _schedule_timed_delete(chat_id: int, message_id: int):
    """Schedule a message for time-based deletion."""
    delay = state["keep_minutes"] * 60
    key = (chat_id, message_id)
    # Cancel any existing task for this message
    if key in deletion_tasks:
        deletion_tasks[key].cancel()
    task = asyncio.create_task(_timed_delete(chat_id, message_id, delay))
    deletion_tasks[key] = task


def _cancel_deletion(chat_id: int, message_id: int):
    """Cancel a scheduled deletion (called when reactions save a message)."""
    key = (chat_id, message_id)
    task = deletion_tasks.pop(key, None)
    if task:
        task.cancel()
        logger.info(f"Cancelled deletion of {message_id} in {chat_id} — saved by reactions")


# ── Group message handler (all messages) ──────────────────────────────────────

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_message(message: Message):
    chat_id = message.chat.id

    # Auto-register group
    if chat_id not in state["group_ids"]:
        state["group_ids"].append(chat_id)
        save_state(state)

    # Silently delete any command messages so /commands are invisible to everyone
    if message.text and message.text.startswith("/"):
        try:
            await bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        return

    # Don't track service messages (joins, pins, etc.) — they have no from_user text
    if message.from_user is None:
        return

    q = msg_queues[chat_id]
    q.append(message.message_id)

    mode = state["delete_mode"]

    if mode == "time":
        _schedule_timed_delete(chat_id, message.message_id)

    elif mode == "count":
        keep = state["keep_count"]
        while len(q) > keep:
            old_id = q.popleft()
            await _delete_if_not_saved(chat_id, old_id)


# ── Reaction handler ───────────────────────────────────────────────────────────

@router.message_reaction()
async def handle_reaction(event: MessageReactionUpdated):
    """Track reactions. Once a message hits the threshold, cancel its deletion."""
    chat_id = event.chat.id
    message_id = event.message_id
    key = (chat_id, message_id)

    if key in saved_by_reactions:
        return  # already saved, nothing to do

    total_reactions = len(event.new_reaction)
    if total_reactions >= state["reaction_threshold"]:
        saved_by_reactions.add(key)
        # Cancel any pending timed deletion
        _cancel_deletion(chat_id, message_id)
        # Remove from count queue so it won't be evicted
        q = msg_queues.get(chat_id)
        if q and message_id in q:
            try:
                q.remove(message_id)
            except ValueError:
                pass
        logger.info(
            f"Message {message_id} in {chat_id} saved — "
            f"{total_reactions} reactions >= threshold {state['reaction_threshold']}"
        )


# ── Private commands (owner only) ──────────────────────────────────────────────

@router.message(Command("start"), OwnerPrivate())
async def cmd_start(message: Message):
    await message.answer(
        "<b>Bot config</b>\n\n"
        "<b>Deletion:</b>\n"
        "/setmode count — delete oldest once queue exceeds limit\n"
        "/setmode time — delete every message after N minutes\n"
        "/setkeep &lt;n&gt; — max messages to keep (count mode)\n"
        "/settime &lt;minutes&gt; — delete messages after N minutes (time mode)\n"
        "/setreactions &lt;n&gt; — reactions needed to save a message (default 3)\n\n"
        "<b>Backup links:</b>\n"
        "/setlinks &lt;url1&gt; &lt;url2&gt; ... — set backup bot links\n"
        "/setinterval &lt;minutes&gt; — how often to post links (default 30m)\n"
        "/postlinks — post links right now\n\n"
        "<b>Groups:</b>\n"
        "/addgroup &lt;chat_id&gt; — add group\n"
        "/removegroup &lt;chat_id&gt; — remove group\n"
        "/status — show all settings\n"
    )


@router.message(Command("status"), OwnerPrivate())
async def cmd_status(message: Message):
    s = state
    links = "\n".join(s["backup_links"]) or "none"
    groups = ", ".join(str(g) for g in s["group_ids"]) or "none (auto-detected)"
    interval_min = s["interval"] // 60
    await message.answer(
        f"<b>Current settings</b>\n\n"
        f"Delete mode: <code>{s['delete_mode']}</code>\n"
        f"Keep count: <code>{s['keep_count']}</code> messages\n"
        f"Keep time: <code>{s['keep_minutes']}</code> minutes\n"
        f"Reaction save threshold: <code>{s['reaction_threshold']}</code> reactions\n\n"
        f"Backup interval: <code>{interval_min}m</code>\n"
        f"Groups: <code>{groups}</code>\n\n"
        f"Backup links:\n{links}"
    )


@router.message(Command("setmode"), OwnerPrivate())
async def cmd_setmode(message: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("count", "time"):
        await message.answer("Usage: /setmode count  or  /setmode time")
        return
    state["delete_mode"] = arg
    save_state(state)
    if arg == "count":
        await message.answer(f"✅ Mode: <b>count</b> — keep last {state['keep_count']} messages.")
    else:
        await message.answer(f"✅ Mode: <b>time</b> — delete messages after {state['keep_minutes']} minutes.")


@router.message(Command("setkeep"), OwnerPrivate())
async def cmd_setkeep(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /setkeep &lt;number&gt;  e.g. /setkeep 10")
        return
    state["keep_count"] = int(arg)
    save_state(state)
    await message.answer(f"✅ Keeping last <b>{state['keep_count']}</b> messages (count mode).")


@router.message(Command("settime"), OwnerPrivate())
async def cmd_settime(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /settime &lt;minutes&gt;  e.g. /settime 60")
        return
    state["keep_minutes"] = int(arg)
    save_state(state)
    await message.answer(f"✅ Messages will be deleted after <b>{state['keep_minutes']} minutes</b> (time mode).")


@router.message(Command("setreactions"), OwnerPrivate())
async def cmd_setreactions(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /setreactions &lt;number&gt;  e.g. /setreactions 3")
        return
    state["reaction_threshold"] = int(arg)
    save_state(state)
    await message.answer(
        f"✅ Messages with <b>{state['reaction_threshold']}+ reactions</b> will never be deleted."
    )


@router.message(Command("setlinks"), OwnerPrivate())
async def cmd_setlinks(message: Message, command: CommandObject):
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Usage: /setlinks &lt;url1&gt; &lt;url2&gt; ...")
        return
    links = [l.strip() for l in raw.replace("\n", " ").split() if l.strip().startswith("http")]
    if not links:
        await message.answer("No valid URLs found. Make sure they start with http.")
        return
    state["backup_links"] = links
    save_state(state)
    await message.answer("✅ Backup links set:\n" + "\n".join(links))


@router.message(Command("setinterval"), OwnerPrivate())
async def cmd_setinterval(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Usage: /setinterval &lt;minutes&gt;  e.g. /setinterval 30")
        return
    state["interval"] = int(arg) * 60
    save_state(state)
    await message.answer(f"✅ Backup links posted every <b>{arg} minutes</b>.")


@router.message(Command("addgroup"), OwnerPrivate())
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


@router.message(Command("removegroup"), OwnerPrivate())
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


@router.message(Command("postlinks"), OwnerPrivate())
async def cmd_postlinks(message: Message):
    await _post_backup_links()
    await message.answer("✅ Posted backup links to all groups.")


# ── Backup link poster ─────────────────────────────────────────────────────────

async def _post_backup_links():
    if not state["backup_links"]:
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
        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "message_reaction"],
            drop_pending_updates=True,
        )
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
            await dp.start_polling(bot, allowed_updates=["message", "message_reaction"])
        asyncio.run(_poll())

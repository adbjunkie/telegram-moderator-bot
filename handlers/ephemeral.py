import logging
from datetime import timedelta

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    get_group_settings,
    enqueue_ephemeral,
    remove_ephemeral_batch,
    remove_ephemeral_by_message,
    get_conn,
)
from utils.helpers import utcnow

logger = logging.getLogger(__name__)


def _should_track(message: Message, settings: dict) -> bool:
    if not settings.get("ephemeral_enabled", False):
        return False
    if message.chat.type not in ("group", "supergroup"):
        return False
    if message.pinned_message is not None:
        return False
    # Don't track service messages
    if message.from_user is None:
        return False
    return True


class EphemeralTrackingMiddleware(BaseMiddleware):
    """
    Outer middleware that records every group message for ephemeral cleanup,
    regardless of which handler ultimately processes it.
    """

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            try:
                settings = get_group_settings(event.chat.id)
                if _should_track(event, settings):
                    enqueue_ephemeral(event.chat.id, event.message_id, event.from_user.id)
            except Exception as e:
                logger.warning(f"Ephemeral tracking failed: {e}")
        return await handler(event, data)


async def run_ephemeral_cleanup(bot: Bot):
    """Periodically delete old messages based on each group's ephemeral settings."""
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT chat_id FROM ephemeral_queue").fetchall()

    for row in rows:
        chat_id = row["chat_id"]
        try:
            settings = get_group_settings(chat_id)
        except Exception:
            continue

        if not settings.get("ephemeral_enabled", False):
            continue

        if settings["ephemeral_mode"] == "hours":
            cutoff = (utcnow().replace(tzinfo=None) - timedelta(hours=settings["ephemeral_hours"]))
            with get_conn() as conn:
                expired = conn.execute(
                    """SELECT id, message_id FROM ephemeral_queue
                       WHERE chat_id = ? AND created_at < ?
                       ORDER BY created_at ASC""",
                    (chat_id, cutoff.isoformat()),
                ).fetchall()
        else:
            max_count = settings["ephemeral_max_count"]
            with get_conn() as conn:
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM ephemeral_queue WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
                if count_row["cnt"] > max_count:
                    excess = count_row["cnt"] - max_count
                    expired = conn.execute(
                        """SELECT id, message_id FROM ephemeral_queue
                           WHERE chat_id = ?
                           ORDER BY created_at ASC LIMIT ?""",
                        (chat_id, excess),
                    ).fetchall()
                else:
                    expired = []

        if not expired:
            continue

        for entry in expired:
            try:
                await bot.delete_message(chat_id, entry["message_id"])
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

        remove_ephemeral_batch([e["id"] for e in expired])
        logger.info(f"Deleted {len(expired)} ephemeral messages in chat {chat_id}")


def setup_ephemeral_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_ephemeral_cleanup,
        trigger="interval",
        minutes=5,
        args=[bot],
        id="ephemeral_cleanup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Ephemeral cleanup scheduler started (every 5 minutes)")
    return scheduler

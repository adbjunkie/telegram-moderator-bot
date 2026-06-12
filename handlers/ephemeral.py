import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from aiogram import Router, Bot, F
from aiogram.types import Message
from aiogram.filters import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    get_group_settings,
    enqueue_ephemeral,
    get_oldest_ephemeral,
    remove_ephemeral_batch,
    remove_ephemeral_by_message,
)

logger = logging.getLogger(__name__)
router = Router(name="ephemeral")


def _should_track(message: Message, settings: dict) -> bool:
    if not settings.get("ephemeral_enabled", False):
        return False
    if message.pinned_message is not None:
        return False
    return True


@router.message()
async def track_message_for_ephemeral(message: Message, bot: Bot):
    settings = get_group_settings(message.chat.id)
    if not _should_track(message, settings):
        return

    enqueue_ephemeral(message.chat.id, message.message_id, message.from_user.id)


@router.edited_message()
async def track_edited_for_ephemeral(message: Message, bot: Bot):
    settings = get_group_settings(message.chat.id)
    if not _should_track(message, settings):
        return

    remove_ephemeral_by_message(message.message_id)
    enqueue_ephemeral(message.chat.id, message.message_id, message.from_user.id)


async def run_ephemeral_cleanup(bot: Bot):
    """Called periodically by scheduler to delete old messages."""
    from database import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM ephemeral_queue"
        ).fetchall()

    for row in rows:
        chat_id = row["chat_id"]
        try:
            settings = get_group_settings(chat_id)
        except Exception:
            continue

        if not settings.get("ephemeral_enabled", False):
            continue

        if settings["ephemeral_mode"] == "hours":
            cutoff = datetime.utcnow() - timedelta(hours=settings["ephemeral_hours"])
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
            except TelegramBadRequest:
                pass

        ids_to_remove = [e["id"] for e in expired]
        remove_ephemeral_batch(ids_to_remove)

        if expired:
            logger.info(f"Deleted {len(expired)} ephemeral messages in chat {chat_id}")


def setup_ephemeral_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Start the periodic ephemeral cleanup scheduler."""
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

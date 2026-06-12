import logging
import time
from datetime import timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from database import (
    get_group_settings,
    check_rate_limit,
    get_trust,
    increment_trust,
    is_new_user,
    is_banned,
    get_active_mute,
    increment_raid_messages,
    check_raid_state,
    add_mute,
    unmute_user,
)
from utils.helpers import check_duplicate, contains_url, escape_html, utcnow
from utils.permissions import mute_member, get_admin_ids

logger = logging.getLogger(__name__)
router = Router(name="anti_spam")

# Cache admin sets per chat to avoid an API call on every message.
# chat_id -> (set_of_admin_ids, expires_at_epoch)
_admin_cache: dict[int, tuple[set, float]] = {}
_ADMIN_CACHE_TTL = 300  # 5 minutes


async def _is_admin_cached(bot: Bot, chat_id: int, user_id: int) -> bool:
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if not cached or cached[1] < now:
        admins = await get_admin_ids(bot, chat_id)
        _admin_cache[chat_id] = (admins, now + _ADMIN_CACHE_TTL)
        cached = _admin_cache[chat_id]
    return user_id in cached[0]


@router.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def message_spam_filter(message: Message, bot: Bot):
    # Only operate in groups
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    settings = get_group_settings(chat_id)

    # Admins are never moderated
    if await _is_admin_cached(bot, chat_id, user_id):
        return

    # Raid mode: drop messages from non-admins
    if check_raid_state(chat_id, settings):
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    # Banned users
    if is_banned(chat_id, user_id):
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    # Muted users (clean up expired mutes)
    mute = get_active_mute(chat_id, user_id)
    if mute:
        if mute["muted_until"] > utcnow().replace(tzinfo=None):
            try:
                await message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            return
        else:
            unmute_user(chat_id, user_id)

    # Track for raid detection
    increment_raid_messages(chat_id)

    # Anti-flood
    if check_rate_limit(
        chat_id, user_id,
        settings["anti_flood_max_per_window"],
        settings["anti_flood_window_seconds"],
    ):
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

        trust = get_trust(chat_id, user_id)
        if trust["trust_score"] < 5:
            until = utcnow() + timedelta(minutes=15)
            if await mute_member(bot, chat_id, user_id, until):
                add_mute(chat_id, user_id, (await bot.get_me()).id, "Auto-mute: flooding", 15)
                await _notify(bot, chat_id, f"🔇 {escape_html(message.from_user.full_name)} muted for 15 min (flooding).")
        return

    # Anti-duplicate
    if settings.get("anti_duplicate_enabled") and message.text:
        if check_duplicate(
            chat_id, user_id, message.text,
            settings["duplicate_threshold"],
            settings["duplicate_window_seconds"],
        ):
            try:
                await message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

            trust = get_trust(chat_id, user_id)
            if trust["trust_score"] < 10:
                until = utcnow() + timedelta(minutes=30)
                if await mute_member(bot, chat_id, user_id, until):
                    add_mute(chat_id, user_id, (await bot.get_me()).id, "Auto-mute: duplicate spam", 30)
                    await _notify(bot, chat_id, f"🔇 {escape_html(message.from_user.full_name)} muted for 30 min (spam).")
            return

    # New-user link restriction
    if is_new_user(chat_id, user_id, settings):
        if settings.get("new_user_block_links") and contains_url(message.text or ""):
            try:
                await message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            return

    # Reward clean activity
    increment_trust(chat_id, user_id, settings.get("trust_score_per_message", 1))


async def _notify(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

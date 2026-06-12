import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

from database import (
    get_group_settings,
    check_rate_limit,
    get_trust,
    increment_trust,
    is_new_user,
    is_banned,
    get_active_mute,
    check_raid_state,
    increment_raid_messages,
)
from utils.helpers import check_duplicate, contains_url, escape_html

logger = logging.getLogger(__name__)
router = Router(name="anti_spam")


@router.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def message_spam_filter(message: Message, bot: Bot):
    chat_id = message.chat.id
    user_id = message.from_user.id
    settings = get_group_settings(chat_id)

    # Skip admins and bots
    if message.from_user.is_bot:
        return

    # Check for raid mode first
    if check_raid_state(chat_id, settings):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # Check if user is banned
    if is_banned(chat_id, user_id):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # Check if user is muted
    mute = get_active_mute(chat_id, user_id)
    if mute:
        if mute["muted_until"] > datetime.utcnow():
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            return
        else:
            from database import unmute_user as _unmute
            _unmute(chat_id, user_id)

    # Track message for raid detection
    increment_raid_messages(chat_id)

    # Anti-flood (rate limiting)
    if check_rate_limit(
        chat_id,
        user_id,
        settings["anti_flood_max_per_window"],
        settings["anti_flood_window_seconds"],
    ):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

        trust = get_trust(chat_id, user_id)
        if trust["trust_score"] < 5:
            # Low-trust flooders get muted
            from database import add_mute
            add_mute(chat_id, user_id, (await bot.get_me()).id, "Auto-mute: flooding", 15)
            try:
                await bot.restrict_chat_member(
                    chat_id, user_id,
                    permissions=None,
                    until_date=datetime.utcnow() + timedelta(minutes=15),
                )
            except TelegramBadRequest:
                pass
            await bot.send_message(
                chat_id,
                f"🔇 {escape_html(message.from_user.full_name)} has been muted for 15 minutes (flooding).",
            )
        return

    # Anti-duplicate
    if settings.get("anti_duplicate_enabled") and message.text:
        if check_duplicate(
            chat_id,
            user_id,
            message.text,
            settings["duplicate_threshold"],
            settings["duplicate_window_seconds"],
        ):
            try:
                await message.delete()
            except TelegramBadRequest:
                pass

            trust = get_trust(chat_id, user_id)
            if trust["trust_score"] < 10:
                from database import add_mute
                add_mute(chat_id, user_id, (await bot.get_me()).id, "Auto-mute: spam (duplicate messages)", 30)
                try:
                    await bot.restrict_chat_member(
                        chat_id, user_id,
                        permissions=None,
                        until_date=datetime.utcnow() + timedelta(minutes=30),
                    )
                except TelegramBadRequest:
                    pass
                await bot.send_message(
                    chat_id,
                    f"🔇 {escape_html(message.from_user.full_name)} has been muted for 30 minutes (spam).",
                )
            return

    # New user link check (handled in join_protection too, but double-check)
    if is_new_user(chat_id, user_id, settings):
        if settings.get("new_user_block_links") and contains_url(message.text or ""):
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            return

    # Increment trust for normal messages
    increment_trust(chat_id, user_id, settings.get("trust_score_per_message", 1))

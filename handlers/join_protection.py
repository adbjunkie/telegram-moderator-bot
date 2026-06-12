import asyncio
import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery
from aiogram.filters import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, CommandObject, Command
from aiogram.exceptions import TelegramBadRequest

from database import (
    get_group_settings,
    ensure_trust_record,
    set_captcha_passed,
    create_captcha,
    get_captcha,
    mark_captcha_answered,
    increment_raid_joins,
    check_raid_state,
    reset_raid_state,
    is_new_user,
)
from utils.helpers import generate_captcha, escape_html

logger = logging.getLogger(__name__)
router = Router(name="join_protection")


def generate_captcha_keyboard(captcha_text: str, user_id: int):
    import random
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    chars = list(captcha_text)
    all_chars = chars + random.sample(
        [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if c not in chars],
        max(0, 6 - len(chars)),
    )
    random.shuffle(all_chars)

    buttons = []
    row = []
    for i, char in enumerate(all_chars):
        row.append(InlineKeyboardButton(text=char, callback_data=f"captcha:{user_id}:{char}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("captcha:"))
async def captcha_button_handler(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) < 3:
        return
    target_user_id = int(parts[1])
    chosen_char = parts[2]

    if callback.from_user.id != target_user_id:
        await callback.answer("This CAPTCHA is not for you.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    captcha = get_captcha(chat_id, target_user_id)
    if not captcha:
        await callback.answer("CAPTCHA expired. You can chat now.", show_alert=True)
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        return

    # Build answer
    if chosen_char == captcha["captcha_text"] and callback.message.message_id == captcha["message_id"]:
        mark_captcha_answered(chat_id, target_user_id)
        set_captcha_passed(chat_id, target_user_id)
        await callback.answer("Verified! Welcome to the group.", show_alert=True)
        try:
            await callback.message.edit_text(
                f"✅ <b>CAPTCHA PASSED</b>\n"
                f"User {escape_html(callback.from_user.full_name)} has been verified.\n\n"
                f"This message will be deleted shortly.",
            )
            await asyncio.sleep(5)
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    else:
        await callback.answer("Wrong letter! Try again.", show_alert=True)


@router.callback_query(F.data.startswith("captcha_text_"))
async def captcha_text_handler(callback: CallbackQuery, bot: Bot):
    """Fallback handler for the 'text answer' approach (message-based captchas)."""
    parts = callback.data.split("_", 2)
    if len(parts) < 3:
        return
    target_user_id = int(parts[1])
    expected_text = parts[2]

    if callback.from_user.id != target_user_id:
        await callback.answer("This CAPTCHA is not for you.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    captcha = get_captcha(chat_id, target_user_id)
    if not captcha:
        await callback.answer("CAPTCHA expired. You can chat now.", show_alert=True)
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        return

    mark_captcha_answered(chat_id, target_user_id)
    set_captcha_passed(chat_id, target_user_id)
    await callback.answer("Verified! Welcome to the group.", show_alert=True)
    try:
        await callback.message.edit_text(
            f"✅ <b>CAPTCHA PASSED</b>\n"
            f"User {escape_html(callback.from_user.full_name)} has been verified.\n\n"
            f"This message will be deleted shortly.",
        )
        await asyncio.sleep(5)
        await callback.message.delete()
    except TelegramBadRequest:
        pass


@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    """Handle new members joining the group."""
    chat = event.chat
    new_member = event.new_chat_member.user
    if new_member.is_bot:
        return

    chat_id = chat.id
    user_id = new_member.id
    settings = get_group_settings(chat_id)

    # Anti-raid
    increment_raid_joins(chat_id)
    if check_raid_state(chat_id, settings):
        # Lockdown: kick and join-lock
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        except TelegramBadRequest:
            pass

        try:
            await bot.send_message(
                chat_id,
                "🚨 <b>RAID DETECTED</b>. Group is temporarily locked. New joins will be kicked.",
            )
        except TelegramBadRequest:
            pass
        return

    # Initialize trust record
    ensure_trust_record(chat_id, user_id, joined_at=datetime.utcnow())

    if not settings.get("captcha_enabled", True):
        return

    captcha_text = generate_captcha()
    if settings.get("captcha_type") == "button":
        keyboard = generate_captcha_keyboard(captcha_text, user_id)
        msg = await bot.send_message(
            chat_id,
            f"👋 Welcome {escape_html(new_member.full_name)}!\n\n"
            f"Please tap the button that matches this letter to verify you're human:\n\n"
            f"<b><code>{captcha_text}</code></b>\n\n"
            f"You have 2 minutes to complete this.",
            reply_markup=keyboard,
        )
    else:
        import random
        all_chars = list(captcha_text) + random.sample(
            [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if c not in captcha_text],
            max(0, 6 - len(captcha_text)),
        )
        random.shuffle(all_chars)
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        buttons = []
        row = []
        for ch in all_chars:
            row.append(InlineKeyboardButton(text=ch, callback_data=f"captcha_text_{user_id}_{ch}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        msg = await bot.send_message(
            chat_id,
            f"👋 Welcome {escape_html(new_member.full_name)}!\n\n"
            f"To verify you're human, send this code in the chat:\n\n"
            f"<b><code>{captcha_text}</code></b>\n\n"
            f"You have 2 minutes to complete this.",
            reply_markup=keyboard,
        )

    create_captcha(chat_id, user_id, captcha_text, msg.message_id)

    # Auto-delete captcha message after 2 minutes if not answered
    await asyncio.sleep(120)
    captcha = get_captcha(chat_id, user_id)
    if captcha and not captcha["answered"]:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # unban so they can rejoin
            await msg.delete()
        except TelegramBadRequest:
            pass


@router.message(F.text)
async def new_user_text_check(message: Message, bot: Bot):
    """Check if new users are sending links/media; delete if restricted."""
    settings = get_group_settings(message.chat.id)
    if not is_new_user(message.chat.id, message.from_user.id, settings):
        return

    from utils.helpers import contains_url

    if settings.get("new_user_block_links") and contains_url(message.text or ""):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        warning = await message.answer(
            f"⚠️ {escape_html(message.from_user.first_name)}, new users cannot send links. "
            f"Please wait until you've been active for a while.",
        )
        await asyncio.sleep(5)
        try:
            await warning.delete()
        except TelegramBadRequest:
            pass
        return

    # Check for captcha responses in text mode
    captcha = get_captcha(message.chat.id, message.from_user.id)
    if captcha and message.text and message.text.strip().upper() == captcha["captcha_text"]:
        mark_captcha_answered(message.chat.id, message.from_user.id)
        set_captcha_passed(message.chat.id, message.from_user.id)
        await message.delete()
        await bot.send_message(
            message.chat.id,
            f"✅ {escape_html(message.from_user.full_name)} verified! Welcome to the group.",
        )
        # Delete the captcha prompt
        try:
            await bot.delete_message(message.chat.id, captcha["message_id"])
        except TelegramBadRequest:
            pass


@router.message(F.photo | F.video | F.document | F.sticker | F.animation | F.voice | F.video_note)
async def new_user_media_check(message: Message, bot: Bot):
    """Block media from new users."""
    settings = get_group_settings(message.chat.id)
    if not is_new_user(message.chat.id, message.from_user.id, settings):
        return

    if settings.get("new_user_block_media"):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        warning = await message.answer(
            f"⚠️ {escape_html(message.from_user.first_name)}, new users cannot send media. "
            f"Please send text messages first.",
        )
        await asyncio.sleep(5)
        try:
            await warning.delete()
        except TelegramBadRequest:
            pass

import asyncio
import logging
import random

from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    ChatMemberUpdated,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from database import (
    get_group_settings,
    ensure_trust_record,
    set_captcha_passed,
    create_captcha,
    get_captcha,
    mark_captcha_answered,
    increment_raid_joins,
    check_raid_state,
    is_new_user,
)
from utils.helpers import generate_captcha, escape_html, contains_url, delete_after, utcnow
from utils.permissions import unmute_member, kick_member

logger = logging.getLogger(__name__)
router = Router(name="join_protection")

CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _build_captcha_keyboard(correct_char: str, user_id: int) -> InlineKeyboardMarkup:
    """Build a keyboard of single characters; one is correct."""
    distractors = random.sample([c for c in CHARSET if c != correct_char], 5)
    options = distractors + [correct_char]
    random.shuffle(options)

    buttons, row = [], []
    for ch in options:
        row.append(InlineKeyboardButton(text=ch, callback_data=f"cap:{user_id}:{ch}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _pass_captcha(bot: Bot, chat_id: int, user_id: int, full_name: str, captcha_msg_id: int):
    mark_captcha_answered(chat_id, user_id)
    set_captcha_passed(chat_id, user_id)
    await unmute_member(bot, chat_id, user_id)
    try:
        await bot.edit_message_text(
            f"✅ {escape_html(full_name)} verified! Welcome.",
            chat_id=chat_id,
            message_id=captcha_msg_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    delete_after(bot, chat_id, captcha_msg_id, 5)


@router.callback_query(F.data.startswith("cap:"))
async def captcha_button_handler(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) < 3:
        return
    target_user_id = int(parts[1])
    chosen = parts[2]

    if callback.from_user.id != target_user_id:
        await callback.answer("This CAPTCHA is not for you.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    captcha = get_captcha(chat_id, target_user_id)
    if not captcha:
        await callback.answer("CAPTCHA expired or already solved.", show_alert=True)
        return

    if chosen == captcha["captcha_text"]:
        await callback.answer("Verified! Welcome.", show_alert=False)
        await _pass_captcha(bot, chat_id, target_user_id, callback.from_user.full_name, callback.message.message_id)
    else:
        await callback.answer("Wrong! Try again.", show_alert=True)


@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    chat_id = event.chat.id
    new_member = event.new_chat_member.user
    if new_member.is_bot:
        return

    user_id = new_member.id
    settings = get_group_settings(chat_id)

    # Anti-raid
    increment_raid_joins(chat_id)
    if check_raid_state(chat_id, settings):
        await kick_member(bot, chat_id, user_id)
        try:
            warn = await bot.send_message(
                chat_id,
                "🚨 <b>RAID DETECTED</b>. New joins are being kicked temporarily.",
            )
            delete_after(bot, chat_id, warn.message_id, 30)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    ensure_trust_record(chat_id, user_id, joined_at=utcnow().replace(tzinfo=None))

    if not settings.get("captcha_enabled", True):
        return

    captcha_text = generate_captcha(length=1)  # single-character button captcha
    keyboard = _build_captcha_keyboard(captcha_text, user_id)

    try:
        msg = await bot.send_message(
            chat_id,
            f"👋 Welcome {escape_html(new_member.full_name)}!\n\n"
            f"To verify you're human, tap this character:\n\n"
            f"<b><code>{captcha_text}</code></b>\n\n"
            f"You have 2 minutes.",
            reply_markup=keyboard,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        return

    create_captcha(chat_id, user_id, captcha_text, msg.message_id)

    # Timeout: kick if not solved in 2 minutes
    await asyncio.sleep(120)
    captcha = get_captcha(chat_id, user_id)
    if captcha and not captcha["answered"]:
        await kick_member(bot, chat_id, user_id)
        try:
            await msg.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


@router.message(F.text & ~F.text.startswith("/"))
async def new_user_text_check(message: Message, bot: Bot):
    """Restrict links from new users (after CAPTCHA, during probation window)."""
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    settings = get_group_settings(chat_id)

    if not is_new_user(chat_id, user_id, settings):
        return

    if settings.get("new_user_block_links") and contains_url(message.text or ""):
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        warn = await message.answer(
            f"⚠️ {escape_html(message.from_user.first_name)}, new users cannot send links yet."
        )
        delete_after(bot, chat_id, warn.message_id, 5)


@router.message(
    (F.photo | F.video | F.document | F.sticker | F.animation | F.voice | F.video_note)
)
async def new_user_media_check(message: Message, bot: Bot):
    """Block media from new users."""
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    settings = get_group_settings(chat_id)

    if not is_new_user(chat_id, user_id, settings):
        return

    if settings.get("new_user_block_media"):
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        warn = await message.answer(
            f"⚠️ {escape_html(message.from_user.first_name)}, new users cannot send media yet."
        )
        delete_after(bot, chat_id, warn.message_id, 5)

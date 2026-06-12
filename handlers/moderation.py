import logging
from datetime import timedelta

from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from database import (
    get_group_settings,
    add_warning,
    get_active_warnings,
    clear_warnings,
    warn_count,
    add_mute,
    get_active_mute,
    unmute_user,
    add_ban,
    unban_user,
)
from utils.helpers import escape_html, delete_after, utcnow
from utils.permissions import (
    is_chat_admin,
    bot_can_restrict,
    mute_member,
    unmute_member,
)

logger = logging.getLogger(__name__)
router = Router(name="moderation")

GROUP_TYPES = ("group", "supergroup")


def _mention(user) -> str:
    return escape_html(user.full_name)


async def _resolve_target(message: Message, command: CommandObject):
    """
    Resolve the target user from a reply or from the first arg (numeric ID).
    Returns (user_obj_or_id, remaining_args) or (None, None) if not found.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, (command.args or "")

    # Try numeric user id as the first arg
    args = (command.args or "").strip().split(maxsplit=1)
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), (args[1] if len(args) > 1 else "")

    return None, None


async def _guard(message: Message, bot: Bot, action: str) -> bool:
    """Common guard: must be group + sender must be admin + bot must have rights."""
    if message.chat.type not in GROUP_TYPES:
        await message.answer("This command can only be used in groups.")
        return False

    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer(f"<b>Access denied.</b> Only admins can {action}.")
        return False

    if not await bot_can_restrict(bot, message.chat.id):
        await message.answer(
            "I don't have permission to restrict members. "
            "Please make me an admin with <b>Ban users</b> permission."
        )
        return False

    return True


def _reply_and_autodelete(message: Message, bot: Bot, text: str):
    """Send a reply that auto-deletes after the configured delay."""
    settings = get_group_settings(message.chat.id)
    delay = settings.get("delete_service_messages_after", 60)
    return text, delay


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot):
    if not await _guard(message, bot, "warn users"):
        return

    target, reason = await _resolve_target(message, command)
    if target is None:
        await message.answer("Reply to a user's message to warn them.\nUsage: <code>/warn [reason]</code>")
        return

    user_id = target.id if not isinstance(target, int) else target
    name = _mention(target) if not isinstance(target, int) else f"User {target}"

    if not isinstance(target, int) and target.is_bot:
        await message.answer("Cannot warn bots.")
        return

    if await is_chat_admin(bot, message.chat.id, user_id):
        await message.answer("Cannot warn an admin.")
        return

    reason = (reason or "").strip() or "No reason provided"
    chat_id = message.chat.id
    settings = get_group_settings(chat_id)

    add_warning(chat_id, user_id, message.from_user.id, reason)
    current = warn_count(chat_id, user_id)
    ban_limit = settings["warn_limit_before_ban"]
    mute_limit = settings["warn_limit_before_mute"]

    text = (
        f"⚠️ {name} has been warned.\n"
        f"Reason: {escape_html(reason)}\n"
        f"Warnings: {current}/{ban_limit}"
    )

    if current >= ban_limit:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            add_ban(chat_id, user_id, message.from_user.id, f"Auto-ban: {current} warnings")
            clear_warnings(chat_id, user_id)
            text += f"\n\n🚫 User banned (reached {ban_limit} warnings)."
        except (TelegramBadRequest, TelegramForbiddenError):
            text += "\n\n⚠️ Failed to ban user (check bot permissions)."
    elif current >= mute_limit:
        if not get_active_mute(chat_id, user_id):
            duration = settings["mute_duration_minutes"]
            until = utcnow() + timedelta(minutes=duration)
            if await mute_member(bot, chat_id, user_id, until):
                add_mute(chat_id, user_id, message.from_user.id, f"Auto-mute: {current} warnings", duration)
                text += f"\n\n🔇 User auto-muted for {duration} minutes."

    sent = await message.answer(text)
    _, delay = _reply_and_autodelete(message, bot, text)
    delete_after(bot, chat_id, sent.message_id, delay)


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot):
    if not await _guard(message, bot, "mute users"):
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to mute them.\nUsage: <code>/mute [minutes] [reason]</code>")
        return

    user = message.reply_to_message.from_user
    if user.is_bot:
        await message.answer("Cannot mute bots.")
        return
    if await is_chat_admin(bot, message.chat.id, user.id):
        await message.answer("Cannot mute an admin.")
        return

    args = (command.args or "").strip().split(maxsplit=1)
    duration_str = args[0] if args else "60"
    reason = args[1] if len(args) > 1 else "No reason provided"

    try:
        duration = int(duration_str)
        if duration <= 0:
            raise ValueError
    except ValueError:
        # Treat the whole arg string as a reason, default 60 min
        duration = 60
        reason = (command.args or "").strip() or "No reason provided"

    chat_id = message.chat.id
    until = utcnow() + timedelta(minutes=duration)

    if not await mute_member(bot, chat_id, user.id, until):
        await message.answer("Failed to mute user. Check my permissions.")
        return

    add_mute(chat_id, user.id, message.from_user.id, reason, duration)
    sent = await message.answer(
        f"🔇 {_mention(user)} muted for {duration} minutes.\n"
        f"Reason: {escape_html(reason)}"
    )
    settings = get_group_settings(chat_id)
    delete_after(bot, chat_id, sent.message_id, settings.get("delete_service_messages_after", 60))


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject, bot: Bot):
    if not await _guard(message, bot, "unmute users"):
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to unmute them.")
        return

    user = message.reply_to_message.from_user
    chat_id = message.chat.id

    unmute_user(chat_id, user.id)
    if not await unmute_member(bot, chat_id, user.id):
        await message.answer("Failed to unmute user. Check my permissions.")
        return

    sent = await message.answer(f"🔊 {_mention(user)} has been unmuted.")
    settings = get_group_settings(chat_id)
    delete_after(bot, chat_id, sent.message_id, settings.get("delete_service_messages_after", 60))


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot):
    if not await _guard(message, bot, "ban users"):
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to ban them.\nUsage: <code>/ban [reason]</code>")
        return

    user = message.reply_to_message.from_user
    if user.is_bot:
        await message.answer("Cannot ban bots.")
        return
    if await is_chat_admin(bot, message.chat.id, user.id):
        await message.answer("Cannot ban an admin.")
        return

    reason = (command.args or "").strip() or "No reason provided"
    chat_id = message.chat.id

    try:
        await bot.ban_chat_member(chat_id, user.id)
        add_ban(chat_id, user.id, message.from_user.id, reason)
        clear_warnings(chat_id, user.id)
        sent = await message.answer(
            f"🚫 {_mention(user)} has been banned.\nReason: {escape_html(reason)}"
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await message.answer(f"Failed to ban user: {e}")
        return

    settings = get_group_settings(chat_id)
    delete_after(bot, chat_id, sent.message_id, settings.get("delete_service_messages_after", 60))


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot):
    if not await _guard(message, bot, "unban users"):
        return

    target, _ = await _resolve_target(message, command)
    if target is None:
        await message.answer(
            "Reply to a user's message or provide a user ID.\nUsage: <code>/unban [user_id]</code>"
        )
        return

    user_id = target.id if not isinstance(target, int) else target
    name = _mention(target) if not isinstance(target, int) else f"User {target}"
    chat_id = message.chat.id

    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        unban_user(chat_id, user_id)
        sent = await message.answer(f"✅ {name} has been unbanned.")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await message.answer(f"Failed to unban user: {e}")
        return

    settings = get_group_settings(chat_id)
    delete_after(bot, chat_id, sent.message_id, settings.get("delete_service_messages_after", 60))


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type not in GROUP_TYPES:
        await message.answer("This command can only be used in groups.")
        return
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("<b>Access denied.</b> Only admins can view warnings.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to see their warnings.")
        return

    user = message.reply_to_message.from_user
    chat_id = message.chat.id
    warnings = get_active_warnings(chat_id, user.id)

    if not warnings:
        sent = await message.answer(f"✅ {_mention(user)} has no active warnings.")
        delete_after(bot, chat_id, sent.message_id, 15)
        return

    lines = [f"⚠️ <b>Warnings for {_mention(user)}:</b>\n"]
    for i, w in enumerate(warnings, 1):
        lines.append(f"{i}. {escape_html(w['reason'])} — {w['created_at']}")

    sent = await message.answer("\n".join(lines))
    settings = get_group_settings(chat_id)
    delete_after(bot, chat_id, sent.message_id, settings.get("delete_service_messages_after", 60))


@router.message(Command("clearwarns"))
async def cmd_clearwarns(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type not in GROUP_TYPES:
        await message.answer("This command can only be used in groups.")
        return
    if not await is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("<b>Access denied.</b> Only admins can clear warnings.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to clear their warnings.")
        return

    user = message.reply_to_message.from_user
    clear_warnings(message.chat.id, user.id)

    sent = await message.answer(f"✅ Warnings cleared for {_mention(user)}.")
    settings = get_group_settings(message.chat.id)
    delete_after(bot, message.chat.id, sent.message_id, settings.get("delete_service_messages_after", 60))

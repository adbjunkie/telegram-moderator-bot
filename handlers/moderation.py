import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command, CommandObject, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.formatting import Text

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
    is_banned,
    unban_user,
    get_trust,
    set_captcha_passed,
)
from utils.helpers import format_time_remaining, escape_html

logger = logging.getLogger(__name__)
router = Router(name="moderation")


def _is_admin_or_creator(message: Message) -> bool:
    """Check if the sender is an admin or creator of the group."""
    if message.chat.type == "private":
        return True
    member = message.from_user
    chat_member = getattr(message, "_chat_member", None)
    if chat_member:
        return chat_member.status in ("administrator", "creator")
    return False


async def _check_admin(message: Message, bot: Bot) -> bool:
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        message._chat_member = chat_member
        return chat_member.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can warn users.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to warn them.\nUsage: /warn reason (optional)")
        return

    user = message.reply_to_message.from_user
    if user.is_bot:
        await message.answer("Cannot warn bots.")
        return

    reason = command.args or "No reason provided"

    chat_id = message.chat.id
    user_id = user.id
    settings = get_group_settings(chat_id)

    add_warning(chat_id, user_id, message.from_user.id, reason)
    current_warns = warn_count(chat_id, user_id)
    total_allowed = settings["warn_limit_before_ban"]

    mute_trigger = settings["warn_limit_before_mute"]

    text = f"⚠️ {escape_html(user.mention or user.full_name)} has been warned.\nReason: {escape_html(reason)}\nWarnings: {current_warns}/{total_allowed}"

    if current_warns >= mute_trigger:
        mute_duration = settings["mute_duration_minutes"]
        active_mute = get_active_mute(chat_id, user_id)
        if not active_mute:
            add_mute(chat_id, user_id, message.from_user.id, f"Auto-mute: {current_warns} warnings", mute_duration)
            try:
                await bot.restrict_chat_member(
                    chat_id, user_id,
                    permissions=None,
                    until_date=datetime.utcnow() + timedelta(minutes=mute_duration),
                )
                text += f"\n\n🔇 User auto-muted for {mute_duration} minutes."
            except TelegramBadRequest:
                pass

    if current_warns >= total_allowed:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            add_ban(chat_id, user_id, message.from_user.id, f"Auto-ban: {current_warns} warnings")
            clear_warnings(chat_id, user_id)
            text += f"\n\n🚫 User banned (reached {total_allowed} warnings)."
        except TelegramBadRequest:
            pass

    msg = await message.answer(text)
    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can mute users.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to mute them.\nUsage: /mute &lt;minutes&gt; reason")
        return

    user = message.reply_to_message.from_user
    if user.is_bot:
        await message.answer("Cannot mute bots.")
        return

    args = (command.args or "").strip().split(maxsplit=1)
    duration_str = args[0] if args else "60"
    reason = args[1] if len(args) > 1 else "No reason provided"

    try:
        duration = int(duration_str)
        if duration <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Invalid duration. Usage: /mute &lt;minutes&gt; reason")
        return

    chat_id = message.chat.id
    user_id = user.id

    muted_until = add_mute(chat_id, user_id, message.from_user.id, reason, duration)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=None,
            until_date=muted_until,
        )
        msg = await message.answer(
            f"🔇 {escape_html(user.mention or user.full_name)} muted for {duration} minutes.\n"
            f"Reason: {escape_html(reason)}\n"
            f"Until: {muted_until.strftime('%Y-%m-%d %H:%M UTC')}"
        )
    except TelegramBadRequest as e:
        await message.answer(f"Failed to mute user: {e}")

    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can unmute users.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to unmute them.")
        return

    user = message.reply_to_message.from_user
    chat_id = message.chat.id
    user_id = user.id

    unmute_user(chat_id, user_id)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=await bot.get_chat(chat_id).get_default_permissions(),
            use_independent_chat_permissions=False,
        )
    except TelegramBadRequest:
        pass

    msg = await message.answer(f"🔊 {escape_html(user.mention or user.full_name)} has been unmuted.")
    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can ban users.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to ban them.\nUsage: /ban reason (optional)")
        return

    user = message.reply_to_message.from_user
    if user.is_bot:
        await message.answer("Cannot ban bots.")
        return

    reason = command.args or "No reason provided"
    chat_id = message.chat.id
    user_id = user.id

    try:
        await bot.ban_chat_member(chat_id, user_id)
        add_ban(chat_id, user_id, message.from_user.id, reason)
        clear_warnings(chat_id, user_id)
        msg = await message.answer(
            f"🚫 {escape_html(user.mention or user.full_name)} has been banned.\nReason: {escape_html(reason)}"
        )
    except TelegramBadRequest as e:
        await message.answer(f"Failed to ban user: {e}")

    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can unban users.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to unban them.\nUsage: /unban reason (optional)")
        return

    user = message.reply_to_message.from_user
    chat_id = message.chat.id
    user_id = user.id

    try:
        await bot.unban_chat_member(chat_id, user_id)
        unban_user(chat_id, user_id)
        msg = await message.answer(f"✅ {escape_html(user.mention or user.full_name)} has been unbanned.")
    except TelegramBadRequest as e:
        await message.answer(f"Failed to unban user: {e}")

    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("warnings"))
async def cmd_warnings(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can view warnings.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to see their warnings.")
        return

    user = message.reply_to_message.from_user
    chat_id = message.chat.id
    user_id = user.id

    warnings = get_active_warnings(chat_id, user_id)
    if not warnings:
        msg = await message.answer(f"✅ {escape_html(user.mention or user.full_name)} has no active warnings.")
        await asyncio.sleep(15)
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        return

    lines = [f"⚠️ <b>Warnings for {escape_html(user.mention or user.full_name)}:</b>\n"]
    for i, w in enumerate(warnings, 1):
        lines.append(f"{i}. {escape_html(w['reason'])} (by {w['warned_by']}, {w['created_at']})")

    msg = await message.answer("\n".join(lines))
    settings = get_group_settings(chat_id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.message(Command("clearwarns"))
async def cmd_clearwarns(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only admins can clear warnings.")
        return

    if not message.reply_to_message:
        await message.answer("Reply to a user's message to clear their warnings.")
        return

    user = message.reply_to_message.from_user
    clear_warnings(message.chat.id, user.id)

    msg = await message.answer(
        f"✅ Warnings cleared for {escape_html(user.mention or user.full_name)}."
    )
    settings = get_group_settings(message.chat.id)
    await asyncio.sleep(settings.get("delete_service_messages_after", 60))
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass

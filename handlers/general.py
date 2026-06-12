import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandStart
from aiogram.utils.formatting import Text, Bold

logger = logging.getLogger(__name__)
router = Router(name="general")


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Hello!</b> I am a Telegram moderator bot.\n\n"
        "Add me to a group as an admin and I'll help protect it:\n"
        "• CAPTCHA for new members\n"
        "• Anti-spam &amp; anti-flood\n"
        "• Anti-raid protection\n"
        "• Auto-delete old messages (ephemeral mode)\n"
        "• Warn / mute / ban commands with auto-escalation\n\n"
        "<b>Commands:</b>\n"
        "/warn @user reason - Warn a user\n"
        "/mute @user minutes reason - Mute for N minutes\n"
        "/unmute @user - Remove mute\n"
        "/ban @user reason - Ban a user\n"
        "/unban @user - Unban a user\n"
        "/warnings @user - View active warnings\n"
        "/clearwarns @user - Clear warnings\n"
        "/config - Configure bot settings (group admins only)\n"
        "/help - Show this message\n\n"
        "All moderation commands require you to be a group admin."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)

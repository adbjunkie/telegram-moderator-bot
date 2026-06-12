import logging

from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command, CommandStart

logger = logging.getLogger(__name__)
router = Router(name="general")

HELP_TEXT = (
    "👋 <b>Telegram Moderator Bot</b>\n\n"
    "Add me to a group and make me an <b>admin</b> with permission to "
    "<b>delete messages</b> and <b>ban users</b>. I'll then protect it with:\n"
    "• CAPTCHA verification for new members\n"
    "• Anti-spam, anti-flood &amp; anti-duplicate\n"
    "• Anti-raid lockdown\n"
    "• Auto-delete old messages (ephemeral mode)\n"
    "• Warn / mute / ban with auto-escalation\n\n"
    "<b>Admin commands</b> (reply to a user's message):\n"
    "/warn [reason] — warn a user\n"
    "/mute [minutes] [reason] — mute a user\n"
    "/unmute — remove a mute\n"
    "/ban [reason] — ban a user\n"
    "/unban [user_id] — unban a user\n"
    "/warnings — view a user's warnings\n"
    "/clearwarns — clear a user's warnings\n"
    "/config — open the settings panel\n"
    "/setephemeral — set auto-delete limits\n"
    "/help — show this message\n\n"
    "Admins and the group owner are never moderated automatically."
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)

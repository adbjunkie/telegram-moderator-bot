"""Shared permission helpers: admin checks, mute/unmute, restriction permissions."""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import Message, ChatPermissions
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)

# Permissions that fully mute a user (cannot send anything)
MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

# Full member permissions (used when unmuting / lifting restriction)
UNMUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
    can_manage_topics=False,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Return True if the user is an administrator or creator of the chat."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"is_chat_admin failed (chat={chat_id}, user={user_id}): {e}")
        return False


async def get_admin_ids(bot: Bot, chat_id: int) -> set:
    """Return set of admin user IDs for a chat (cached per call)."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return {a.user.id for a in admins}
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"get_admin_ids failed (chat={chat_id}): {e}")
        return set()


async def bot_can_restrict(bot: Bot, chat_id: int) -> bool:
    """Check the bot itself has rights to restrict/ban members."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status != "administrator":
            return False
        return bool(getattr(member, "can_restrict_members", False))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"bot_can_restrict failed (chat={chat_id}): {e}")
        return False


async def mute_member(bot: Bot, chat_id: int, user_id: int, until: datetime) -> bool:
    """Mute a member until the given time. Returns True on success."""
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=MUTED_PERMISSIONS,
            until_date=until,
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"mute_member failed (chat={chat_id}, user={user_id}): {e}")
        return False


async def unmute_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Lift all restrictions on a member. Returns True on success."""
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=UNMUTED_PERMISSIONS,
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"unmute_member failed (chat={chat_id}, user={user_id}): {e}")
        return False


async def restrict_new_user(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Restrict a new user from sending anything until they pass CAPTCHA."""
    return await mute_member(bot, chat_id, user_id, utcnow() + timedelta(hours=1))


async def kick_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Kick a member (ban + immediate unban so they can rejoin)."""
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning(f"kick_member failed (chat={chat_id}, user={user_id}): {e}")
        return False

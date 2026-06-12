import logging

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_group_settings,
    save_group_settings,
    reset_raid_state,
)
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)
router = Router(name="admin_config")

GROUP_TYPES = ("group", "supergroup")

try:
    from config import BOT_ADMINS
except ImportError:
    BOT_ADMINS = set()


async def _check_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id in BOT_ADMINS:
        return True
    return await is_chat_admin(bot, chat_id, user_id)


def _render_settings(settings: dict) -> str:
    lines = [
        "<b>⚙️ Group Configuration</b>\n",
        "<b>Join Protection</b>",
        f"  CAPTCHA: {'✅' if settings['captcha_enabled'] else '❌'}",
        f"  Restrict new users: {settings['new_user_restrict_messages']} msgs / {settings['new_user_restrict_minutes']} min",
        f"  Block links (new users): {'✅' if settings['new_user_block_links'] else '❌'}",
        f"  Block media (new users): {'✅' if settings['new_user_block_media'] else '❌'}",
        "",
        "<b>Anti-Spam</b>",
        f"  Rate limit: {settings['anti_flood_max_per_window']} msgs / {settings['anti_flood_window_seconds']}s",
        f"  Anti-duplicate: {'✅' if settings['anti_duplicate_enabled'] else '❌'} (x{settings['duplicate_threshold']})",
        "",
        "<b>Anti-Raid</b>",
        f"  Join threshold: {settings['anti_raid_join_threshold']} / {settings['anti_raid_window_seconds']}s",
        f"  Msg threshold: {settings['anti_raid_message_threshold']} / {settings['anti_raid_window_seconds']}s",
        "",
        "<b>Enforcement</b>",
        f"  Mute after: {settings['warn_limit_before_mute']} warns ({settings['mute_duration_minutes']} min)",
        f"  Ban after: {settings['warn_limit_before_ban']} warns",
        "",
        "<b>Ephemeral</b>",
        f"  Enabled: {'✅' if settings['ephemeral_enabled'] else '❌'} (mode: {settings['ephemeral_mode']})",
    ]
    if settings["ephemeral_mode"] == "hours":
        lines.append(f"  Max age: {settings['ephemeral_hours']}h")
    else:
        lines.append(f"  Max messages: {settings['ephemeral_max_count']}")
    return "\n".join(lines)


def _main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Edit", callback_data="cfg:edit")
    kb.button(text="🔄 Reset", callback_data="cfg:reset")
    kb.button(text="❌ Close", callback_data="cfg:close")
    kb.adjust(3)
    return kb.as_markup()


def _edit_keyboard(settings: dict):
    kb = InlineKeyboardBuilder()

    def lbl(name, key):
        return f"{name}: {'✅' if settings.get(key) else '❌'}"

    kb.button(text=lbl("CAPTCHA", "captcha_enabled"), callback_data="cfg:t:captcha_enabled")
    kb.button(text=lbl("Block Links", "new_user_block_links"), callback_data="cfg:t:new_user_block_links")
    kb.button(text=lbl("Block Media", "new_user_block_media"), callback_data="cfg:t:new_user_block_media")
    kb.button(text=lbl("Anti-Dup", "anti_duplicate_enabled"), callback_data="cfg:t:anti_duplicate_enabled")
    kb.button(text=lbl("Ephemeral", "ephemeral_enabled"), callback_data="cfg:t:ephemeral_enabled")
    kb.button(text=f"Eph. mode: {settings['ephemeral_mode']}", callback_data="cfg:cycle:ephemeral_mode")
    kb.button(text="🧹 Reset Raid", callback_data="cfg:reset_raid")
    kb.button(text="« Back", callback_data="cfg:show")
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("config"))
async def cmd_config(message: Message, bot: Bot):
    if message.chat.type not in GROUP_TYPES:
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_admin(bot, message.chat.id, message.from_user.id):
        await message.answer(
            "<b>Access denied.</b> Only group admins can use this command.\n"
            "Also make sure I am an admin in this group."
        )
        return

    settings = get_group_settings(message.chat.id)
    await message.answer(_render_settings(settings), reply_markup=_main_keyboard())


@router.callback_query(F.data == "cfg:show")
async def cfg_show(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    settings = get_group_settings(callback.message.chat.id)
    await callback.message.edit_text(_render_settings(settings), reply_markup=_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "cfg:edit")
async def cfg_edit(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    settings = get_group_settings(callback.message.chat.id)
    await callback.message.edit_text(
        "<b>Quick Settings</b>\nTap to toggle.",
        reply_markup=_edit_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "cfg:reset")
async def cfg_reset(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    save_group_settings(callback.message.chat.id, {})
    settings = get_group_settings(callback.message.chat.id)
    await callback.message.edit_text(_render_settings(settings), reply_markup=_main_keyboard())
    await callback.answer("Reset to defaults!", show_alert=True)


@router.callback_query(F.data == "cfg:close")
async def cfg_close(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        await callback.message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg:t:"))
async def cfg_toggle(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    key = callback.data.split(":", 2)[2]
    settings = get_group_settings(callback.message.chat.id)
    settings[key] = not settings.get(key, False)
    save_group_settings(callback.message.chat.id, settings)

    await callback.message.edit_reply_markup(reply_markup=_edit_keyboard(settings))
    await callback.answer(f"{key}: {'ON' if settings[key] else 'OFF'}")


@router.callback_query(F.data.startswith("cfg:cycle:"))
async def cfg_cycle(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    key = callback.data.split(":", 2)[2]
    settings = get_group_settings(callback.message.chat.id)
    if key == "ephemeral_mode":
        settings[key] = "count" if settings.get(key) == "hours" else "hours"
    save_group_settings(callback.message.chat.id, settings)

    await callback.message.edit_reply_markup(reply_markup=_edit_keyboard(settings))
    await callback.answer(f"{key}: {settings[key]}")


@router.callback_query(F.data == "cfg:reset_raid")
async def cfg_reset_raid(callback: CallbackQuery, bot: Bot):
    if not await _check_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    reset_raid_state(callback.message.chat.id)
    await callback.answer("Raid state reset!", show_alert=True)

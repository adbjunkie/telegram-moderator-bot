import asyncio
import json
import logging
from typing import Optional

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_group_settings,
    save_group_settings,
    DEFAULT_GROUP_SETTINGS,
    reset_raid_state,
)
from utils.helpers import escape_html

logger = logging.getLogger(__name__)
router = Router(name="admin_config")

ADMIN_IDS = set()
try:
    from config import BOT_ADMINS
    ADMIN_IDS = BOT_ADMINS
except ImportError:
    pass


def _is_bot_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def _check_group_admin(message: Message, bot: Bot) -> bool:
    if _is_bot_admin(message.from_user.id):
        return True
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return chat_member.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False


@router.message(Command("config"))
async def cmd_config(message: Message, bot: Bot):
    if message.chat.type == "private":
        await message.answer("This command can only be used in groups.")
        return

    if not await _check_group_admin(message, bot):
        await message.answer("<b>Access denied.</b> Only group admins can use this command.")
        return

    settings = get_group_settings(message.chat.id)

    lines = [
        f"<b>Group Configuration</b>\n",
        "<b>Join Protection:</b>",
        f"  CAPTCHA: {'✅ On' if settings['captcha_enabled'] else '❌ Off'} (type: {settings['captcha_type']})",
        f"  Restrict messages: {settings['new_user_restrict_messages']} or {settings['new_user_restrict_minutes']}min",
        f"  Block links: {'✅ Yes' if settings['new_user_block_links'] else '❌ No'}",
        f"  Block media: {'✅ Yes' if settings['new_user_block_media'] else '❌ No'}",
        "",
        "<b>Anti-Spam:</b>",
        f"  Rate limit: {settings['anti_flood_max_per_window']} msgs / {settings['anti_flood_window_seconds']}s",
        f"  Duplicate detection: {'✅ On' if settings['anti_duplicate_enabled'] else '❌ Off'} (threshold: {settings['duplicate_threshold']})",
        "",
        "<b>Anti-Raid:</b>",
        f"  Join threshold: {settings['anti_raid_join_threshold']} in {settings['anti_raid_window_seconds']}s",
        f"  Message threshold: {settings['anti_raid_message_threshold']} in {settings['anti_raid_window_seconds']}s",
        "",
        "<b>Warnings:</b>",
        f"  Mute after: {settings['warn_limit_before_mute']} warns (for {settings['mute_duration_minutes']}min)",
        f"  Ban after: {settings['warn_limit_before_ban']} warns",
        "",
        "<b>Ephemeral:</b>",
        f"  Enabled: {'✅ Yes' if settings['ephemeral_enabled'] else '❌ No'}",
        f"  Mode: {settings['ephemeral_mode']}",
    ]

    if settings["ephemeral_mode"] == "hours":
        lines.append(f"  Max age: {settings['ephemeral_hours']}h")
    else:
        lines.append(f"  Max count: {settings['ephemeral_max_count']}")

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✏️ Edit Settings", callback_data="config:edit")
    keyboard.button(text="🔄 Reset to Defaults", callback_data="config:reset")
    keyboard.adjust(2)

    msg = await message.answer("\n".join(lines), reply_markup=keyboard.as_markup())

    await asyncio.sleep(120)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "config:edit")
async def config_edit_callback(callback: CallbackQuery, bot: Bot):
    if not await _check_group_admin(callback.message, bot):
        await callback.answer("Access denied.", show_alert=True)
        return

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Toggle CAPTCHA", callback_data="config:toggle_captcha")
    keyboard.button(text="Force CAPTCHA type", callback_data="config:toggle_captcha_type")
    keyboard.button(text="Toggle Link Block", callback_data="config:toggle_links")
    keyboard.button(text="Toggle Media Block", callback_data="config:toggle_media")
    keyboard.button(text="Toggle Anti-Duplicate", callback_data="config:toggle_duplicate")
    keyboard.button(text="Toggle Ephemeral", callback_data="config:toggle_ephemeral")
    keyboard.button(text="Toggle Ephemeral Mode", callback_data="config:toggle_ephemeral_mode")
    keyboard.button(text="Reset Raid State", callback_data="config:reset_raid")
    keyboard.button(text="« Back", callback_data="config:show")
    keyboard.adjust(2)

    await callback.message.edit_text(
        "<b>Quick Settings</b>\n\nTap a button to toggle the setting.",
        reply_markup=keyboard.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "config:show")
async def config_show_callback(callback: CallbackQuery, bot: Bot):
    settings = get_group_settings(callback.message.chat.id)
    await cmd_config(callback.message, bot)
    await callback.answer()


@router.callback_query(F.data == "config:reset")
async def config_reset_callback(callback: CallbackQuery, bot: Bot):
    if not await _check_group_admin(callback.message, bot):
        await callback.answer("Access denied.", show_alert=True)
        return

    save_group_settings(callback.message.chat.id, {})
    settings = get_group_settings(callback.message.chat.id)
    await cmd_config(callback.message, bot)
    await callback.answer("Settings reset to defaults!", show_alert=True)


@router.callback_query(F.data.startswith("config:toggle_"))
async def config_toggle_handler(callback: CallbackQuery, bot: Bot):
    if not await _check_group_admin(callback.message, bot):
        await callback.answer("Access denied.", show_alert=True)
        return

    toggle_key = callback.data.replace("config:toggle_", "")
    settings = get_group_settings(callback.message.chat.id)

    toggle_map = {
        "captcha": "captcha_enabled",
        "links": "new_user_block_links",
        "media": "new_user_block_media",
        "duplicate": "anti_duplicate_enabled",
        "ephemeral": "ephemeral_enabled",
    }

    toggle_labels = {
        "captcha": "CAPTCHA",
        "links": "Link Block",
        "media": "Media Block",
        "duplicate": "Anti-Duplicate",
        "ephemeral": "Ephemeral Mode",
    }

    if toggle_key == "captcha_type":
        settings["captcha_type"] = "button" if settings.get("captcha_type") == "text" else "text"
        action = f"CAPTCHA type set to: <b>{settings['captcha_type']}</b>"
    elif toggle_key == "ephemeral_mode":
        settings["ephemeral_mode"] = "count" if settings.get("ephemeral_mode") == "hours" else "hours"
        action = f"Ephemeral mode set to: <b>{settings['ephemeral_mode']}</b>"
    elif toggle_key in toggle_map:
        key = toggle_map[toggle_key]
        settings[key] = not settings.get(key, False)
        label = toggle_labels.get(toggle_key, toggle_key)
        state = "ON" if settings[key] else "OFF"
        action = f"{label}: <b>{state}</b>"
    else:
        await callback.answer("Unknown setting.", show_alert=True)
        return

    save_group_settings(callback.message.chat.id, settings)
    await callback.answer(action, show_alert=True)
    await config_edit_callback(callback, bot)


@router.callback_query(F.data == "config:reset_raid")
async def config_reset_raid_callback(callback: CallbackQuery, bot: Bot):
    if not await _check_group_admin(callback.message, bot):
        await callback.answer("Access denied.", show_alert=True)
        return

    reset_raid_state(callback.message.chat.id)
    await callback.answer("Raid state reset!", show_alert=True)

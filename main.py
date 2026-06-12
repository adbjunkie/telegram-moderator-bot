import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, BotCommand

from config import (
    TELEGRAM_BOT_TOKEN,
    PUBLIC_BASE_URL,
    WEBHOOK_PATH,
    USE_WEBHOOK,
    PORT,
)
from database import init_db
from handlers.general import router as general_router
from handlers.join_protection import router as join_router
from handlers.anti_spam import router as antispam_router
from handlers.moderation import router as moderation_router
from handlers.ephemeral import EphemeralTrackingMiddleware, setup_ephemeral_scheduler
from handlers.admin_config import router as admin_config_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Track every message for ephemeral cleanup before any handler runs.
dp.message.outer_middleware(EphemeralTrackingMiddleware())

dp.include_routers(
    general_router,
    moderation_router,
    admin_config_router,
    join_router,
    antispam_router,
)

_scheduler = None


BOT_COMMANDS = [
    BotCommand(command="start", description="Show help"),
    BotCommand(command="help", description="Show help"),
    BotCommand(command="config", description="Configure the bot (admins)"),
    BotCommand(command="setephemeral", description="Set ephemeral limits (admins)"),
    BotCommand(command="warn", description="Warn a user (reply)"),
    BotCommand(command="mute", description="Mute a user (reply)"),
    BotCommand(command="unmute", description="Unmute a user (reply)"),
    BotCommand(command="ban", description="Ban a user (reply)"),
    BotCommand(command="unban", description="Unban a user"),
    BotCommand(command="warnings", description="View a user's warnings (reply)"),
    BotCommand(command="clearwarns", description="Clear a user's warnings (reply)"),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler

    init_db()
    logger.info("Database initialized")

    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"Failed to set bot commands: {e}")

    if USE_WEBHOOK:
        webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to {webhook_url}")
    else:
        logger.info("Running in polling mode (no webhook)")

    _scheduler = setup_ephemeral_scheduler(bot)

    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)

    if USE_WEBHOOK:
        await bot.delete_webhook()
        logger.info("Webhook removed")

    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok"}


async def _process_update(data: dict):
    try:
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Update processing error: {e}", exc_info=True)


if USE_WEBHOOK:

    @app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
            # Process in the background so long-running handlers (e.g. CAPTCHA
            # timeouts) don't block the webhook HTTP response.
            asyncio.create_task(_process_update(data))
        except Exception as e:
            logger.error(f"Webhook parse error: {e}", exc_info=True)
        return PlainTextResponse("ok")


if __name__ == "__main__":
    import uvicorn

    if USE_WEBHOOK:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        logger.info("Starting bot in polling mode...")

        async def main():
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")

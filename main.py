import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import (
    TELEGRAM_BOT_TOKEN,
    PUBLIC_BASE_URL,
    WEBHOOK_PATH,
    USE_WEBHOOK,
    PORT,
)
from database import init_db
from handlers.join_protection import router as join_router
from handlers.anti_spam import router as antispam_router
from handlers.moderation import router as moderation_router
from handlers.ephemeral import router as ephemeral_router, setup_ephemeral_scheduler
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

dp.include_routers(
    join_router,
    antispam_router,
    moderation_router,
    ephemeral_router,
    admin_config_router,
)

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler

    init_db()
    logger.info("Database initialized")

    webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}{WEBHOOK_PATH}"

    if USE_WEBHOOK:
        await bot.set_webhook(url=webhook_url, allowed_updates=dp.resolve_used_update_types())
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


if USE_WEBHOOK:

    @app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
            update = Update.model_validate(data)
            await dp.feed_webhook_update(bot, update)
        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
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

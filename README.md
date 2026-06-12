# Telegram Moderator Bot

A robust Telegram group moderation bot built with **aiogram 3**, **FastAPI**, and **SQLite**. Designed to make groups hard to disrupt with spam, raids, and abuse.

## Features

- **CAPTCHA join protection** — new members are restricted until they tap the correct character. Unsolved within 2 minutes → kicked.
- **Anti-spam / anti-flood** — per-user rate limiting with auto-mute for repeat offenders.
- **Anti-duplicate** — detects repeated messages and mutes spammers.
- **Anti-raid** — detects bursts of joins/messages and locks the group down.
- **New-user restrictions** — block links/media from users until they've been active.
- **Graduated enforcement** — warnings escalate to auto-mute then auto-ban.
- **Ephemeral mode** — auto-delete old messages by age or rolling count (pinned messages are kept).
- **Per-group settings** — configure everything via the `/config` inline panel.
- **Admins are never auto-moderated.**

## Commands

Admin commands (reply to a user's message):

| Command | Description |
|---------|-------------|
| `/warn [reason]` | Warn a user |
| `/mute [minutes] [reason]` | Mute a user |
| `/unmute` | Remove a mute |
| `/ban [reason]` | Ban a user |
| `/unban [user_id]` | Unban a user |
| `/warnings` | View a user's warnings |
| `/clearwarns` | Clear a user's warnings |
| `/config` | Open the settings panel |
| `/help` | Show help |

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`.
3. Install dependencies and run:

```bash
pip install -r requirements.txt
python main.py
```

Locally (no `PUBLIC_BASE_URL`) the bot runs in **long-polling** mode.

## Deploy on Railway

1. Connect this repo to a new Railway service.
2. Add environment variables:
   - `TELEGRAM_BOT_TOKEN` — your BotFather token
   - `PUBLIC_BASE_URL` — your Railway public URL (e.g. `https://your-app.up.railway.app`) to enable webhook mode
   - `DATA_DIR=/data`
3. **Attach a volume** mounted at `/data` so the SQLite database survives redeploys.
4. Railway builds the included `Dockerfile` automatically.

## Required bot permissions

Add the bot to your group as an **admin** with at least:
- Delete messages
- Ban users
- Restrict members

Without these, moderation actions silently fail.

## Project structure

```
config/        Environment configuration
database/      SQLite schema + data access
handlers/      general, join_protection, anti_spam, moderation, admin_config, ephemeral
utils/         helpers (captcha, urls, dedup) + permissions (mute/ban helpers)
main.py        FastAPI + aiogram entry point (webhook + polling)
```

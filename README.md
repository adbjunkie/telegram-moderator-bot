# Telegram Group Bot

Two things:
1. **Deletes media (photos/videos) older than the last 10** in each group.
2. **Posts backup bot links** to the group every 6 hours (configurable).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | From @BotFather |
| `PUBLIC_BASE_URL` | — | Your Railway HTTPS URL (enables webhook mode) |
| `MEDIA_KEEP_COUNT` | `10` | How many recent media messages to keep |
| `BACKUP_LINKS` | — | Newline-separated list of backup bot links |
| `BACKUP_INTERVAL_SECONDS` | `21600` | How often to post links (default 6h) |
| `BACKUP_CHAT_IDS` | — | Comma-separated chat IDs to post links to (auto-detected if empty) |

## Deploy on Railway

1. Connect this repo.
2. Set `TELEGRAM_BOT_TOKEN` and `PUBLIC_BASE_URL`.
3. Set `BACKUP_LINKS` to your backup bot URLs (one per line).
4. No volume needed — no database.

## Bot permissions needed

Add the bot as admin with **Delete messages** permission.

import asyncio
import logging
import random
import string
import re
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"https?://\S+|www\.\S+|t\.me/\S+|\S+\.(com|org|net|ru|io|gg|xyz|info|biz|online|site|tk|ml|ga|cf|gq)\b",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def delete_after(bot, chat_id: int, message_id: int, delay: int):
    """Schedule deletion of a message after `delay` seconds without blocking."""
    async def _task():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    asyncio.create_task(_task())


def generate_captcha(length: int = 5) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def contains_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


def format_time_remaining(until: datetime) -> str:
    delta = until - utcnow()
    if delta.total_seconds() <= 0:
        return "0m"
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# In-memory duplicate detection (per-chat ring buffer)
# key: (chat_id, user_id) -> (text_hash, [timestamps])
_duplicate_cache: Dict[Tuple[int, int], Tuple[int, List[datetime]]] = defaultdict(
    lambda: (0, [])
)


def check_duplicate(chat_id: int, user_id: int, text: str, threshold: int, window_seconds: int) -> bool:
    """
    Returns True if the user has sent the same text >= threshold times within the window.
    """
    key = (chat_id, user_id)
    text_hash = hash(text.lower().strip())
    cached_hash, timestamps = _duplicate_cache[key]

    now = utcnow()
    cutoff = now - timedelta(seconds=window_seconds)

    # Reset if it's a different message
    if cached_hash != text_hash:
        _duplicate_cache[key] = (text_hash, [now])
        return False

    # Filter old timestamps
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    _duplicate_cache[key] = (text_hash, timestamps)

    return len(timestamps) >= threshold


def clean_duplicate_cache():
    """Remove entries older than 5 minutes to prevent memory leak."""
    now = utcnow()
    cutoff = now - timedelta(minutes=5)
    to_delete = []
    for key, (_, timestamps) in _duplicate_cache.items():
        if all(t < cutoff for t in timestamps):
            to_delete.append(key)
    for key in to_delete:
        del _duplicate_cache[key]

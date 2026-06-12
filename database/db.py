import sqlite3
import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "moderator.db"

_conn_pool: Optional[sqlite3.Connection] = None


def init_db():
    global _conn_pool
    db_dir = DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    _conn_pool = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn_pool.row_factory = sqlite3.Row
    _conn_pool.execute("PRAGMA journal_mode=WAL")
    _conn_pool.execute("PRAGMA foreign_keys=ON")

    _conn_pool.executescript("""
        CREATE TABLE IF NOT EXISTS group_settings (
            chat_id INTEGER PRIMARY KEY,
            settings_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            warned_by INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_warnings_chat_user ON user_warnings(chat_id, user_id);

        CREATE TABLE IF NOT EXISTS user_mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            muted_by INTEGER NOT NULL,
            reason TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            muted_until TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_mutes_chat_user ON user_mutes(chat_id, user_id);

        CREATE TABLE IF NOT EXISTS user_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            banned_by INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            unbanned INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_bans_chat_user ON user_bans(chat_id, user_id);

        CREATE TABLE IF NOT EXISTS user_trust (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            trust_score INTEGER NOT NULL DEFAULT 0,
            messages_sent INTEGER NOT NULL DEFAULT 0,
            joined_at TIMESTAMP,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            captcha_passed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trust_chat ON user_trust(chat_id);

        CREATE TABLE IF NOT EXISTS captcha_challenges (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            captcha_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            answered INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS ephemeral_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ephemeral_chat_time ON ephemeral_queue(chat_id, created_at);

        CREATE TABLE IF NOT EXISTS anti_raid_state (
            chat_id INTEGER PRIMARY KEY,
            join_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        );
    """)
    _conn_pool.commit()
    print(f"[DB] Initialized at {DB_PATH}")


@contextmanager
def get_conn():
    if _conn_pool is None:
        init_db()
    try:
        yield _conn_pool
    except Exception:
        _conn_pool.rollback()
        raise


# --------------- Group Settings ---------------

DEFAULT_GROUP_SETTINGS = {
    "captcha_enabled": True,
    "captcha_type": "text",  # "text" or "button"
    "new_user_restrict_messages": 5,
    "new_user_restrict_minutes": 30,
    "new_user_block_links": True,
    "new_user_block_media": True,
    "anti_flood_max_per_window": 5,
    "anti_flood_window_seconds": 10,
    "anti_duplicate_enabled": True,
    "duplicate_threshold": 3,
    "duplicate_window_seconds": 30,
    "anti_raid_join_threshold": 10,
    "anti_raid_message_threshold": 30,
    "anti_raid_window_seconds": 30,
    "warn_limit_before_mute": 3,
    "mute_duration_minutes": 60,
    "warn_limit_before_ban": 5,
    "ephemeral_enabled": False,
    "ephemeral_mode": "hours",  # "hours" or "count"
    "ephemeral_hours": 24,
    "ephemeral_max_count": 500,
    "delete_service_messages_after": 60,  # seconds before deleting bot's own msgs
    "trust_messages_for_level_up": 50,
    "trust_score_per_message": 1,
}


def get_group_settings(chat_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT settings_json FROM group_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            stored = json.loads(row["settings_json"])
            merged = {**DEFAULT_GROUP_SETTINGS, **stored}
            return merged
        return dict(DEFAULT_GROUP_SETTINGS)


def save_group_settings(chat_id: int, settings: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO group_settings (chat_id, settings_json, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(chat_id) DO UPDATE SET
               settings_json = excluded.settings_json,
               updated_at = CURRENT_TIMESTAMP""",
            (chat_id, json.dumps(settings)),
        )
        conn.commit()


# --------------- Warnings ---------------

def add_warning(chat_id: int, user_id: int, warned_by: int, reason: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO user_warnings (chat_id, user_id, warned_by, reason)
               VALUES (?, ?, ?, ?)""",
            (chat_id, user_id, warned_by, reason),
        )
        conn.commit()
        return cur.lastrowid


def get_active_warnings(chat_id: int, user_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, warned_by, reason, created_at
               FROM user_warnings
               WHERE chat_id = ? AND user_id = ? AND active = 1
               ORDER BY created_at DESC""",
            (chat_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_warnings(chat_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_warnings SET active = 0 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


def warn_count(chat_id: int, user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM user_warnings WHERE chat_id = ? AND user_id = ? AND active = 1",
            (chat_id, user_id),
        ).fetchone()
        return row["cnt"]


# --------------- Mutes ---------------

def add_mute(chat_id: int, user_id: int, muted_by: int, reason: str, duration_minutes: int) -> Optional[datetime]:
    muted_until = datetime.utcnow() + timedelta(minutes=duration_minutes)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_mutes (chat_id, user_id, muted_by, reason, duration_minutes, muted_until)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, user_id, muted_by, reason, duration_minutes, muted_until.isoformat()),
        )
        conn.commit()
    return muted_until


def get_active_mute(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, muted_by, reason, duration_minutes, muted_until, created_at
               FROM user_mutes
               WHERE chat_id = ? AND user_id = ? AND active = 1
               ORDER BY created_at DESC LIMIT 1""",
            (chat_id, user_id),
        ).fetchone()
        if row:
            d = dict(row)
            d["muted_until"] = datetime.fromisoformat(d["muted_until"])
            return d
        return None


def unmute_user(chat_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_mutes SET active = 0 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# --------------- Bans ---------------

def add_ban(chat_id: int, user_id: int, banned_by: int, reason: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_bans (chat_id, user_id, banned_by, reason)
               VALUES (?, ?, ?, ?)""",
            (chat_id, user_id, banned_by, reason),
        )
        conn.commit()


def is_banned(chat_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_bans WHERE chat_id = ? AND user_id = ? AND unbanned = 0 LIMIT 1",
            (chat_id, user_id),
        ).fetchone()
        return row is not None


def unban_user(chat_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_bans SET unbanned = 1 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# --------------- Trust ---------------

def ensure_trust_record(chat_id: int, user_id: int, joined_at: Optional[datetime] = None):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO user_trust (chat_id, user_id, trust_score, messages_sent, joined_at)
               VALUES (?, ?, 0, 0, ?)""",
            (chat_id, user_id, (joined_at or datetime.utcnow()).isoformat()),
        )
        conn.commit()


def get_trust(chat_id: int, user_id: int) -> Dict[str, Any]:
    ensure_trust_record(chat_id, user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_trust WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return dict(row)


def increment_trust(chat_id: int, user_id: int, points: int = 1):
    ensure_trust_record(chat_id, user_id)
    with get_conn() as conn:
        conn.execute(
            """UPDATE user_trust SET
               trust_score = trust_score + ?,
               messages_sent = messages_sent + 1,
               last_activity = CURRENT_TIMESTAMP
               WHERE chat_id = ? AND user_id = ?""",
            (points, chat_id, user_id),
        )
        conn.commit()


def set_captcha_passed(chat_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO user_trust
               (chat_id, user_id, trust_score, messages_sent, captcha_passed, joined_at, last_activity)
               VALUES (?, ?, 10, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (chat_id, user_id),
        )
        conn.commit()


def is_new_user(chat_id: int, user_id: int, settings: dict) -> bool:
    trust = get_trust(chat_id, user_id)
    if trust["captcha_passed"]:
        return False
    if trust["messages_sent"] >= settings["new_user_restrict_messages"]:
        return False
    if trust["joined_at"]:
        try:
            joined = datetime.fromisoformat(trust["joined_at"])
        except (ValueError, TypeError):
            return True
        minutes_since_join = (datetime.utcnow() - joined).total_seconds() / 60
        if minutes_since_join >= settings["new_user_restrict_minutes"]:
            return False
    return True


# --------------- CAPTCHA ---------------

def create_captcha(chat_id: int, user_id: int, captcha_text: str, message_id: int):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO captcha_challenges (chat_id, user_id, captcha_text, created_at, answered, message_id)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0, ?)""",
            (chat_id, user_id, captcha_text, message_id),
        )
        conn.commit()


def get_captcha(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM captcha_challenges WHERE chat_id = ? AND user_id = ? AND answered = 0",
            (chat_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def mark_captcha_answered(chat_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE captcha_challenges SET answered = 1 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# --------------- Ephemeral Queue ---------------

def enqueue_ephemeral(chat_id: int, message_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ephemeral_queue (chat_id, message_id, user_id) VALUES (?, ?, ?)",
            (chat_id, message_id, user_id),
        )
        conn.commit()


def get_oldest_ephemeral(chat_id: int, max_count: int) -> List[Dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(hours=720)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, message_id, created_at FROM ephemeral_queue
               WHERE chat_id = ? AND created_at < ?
               ORDER BY created_at ASC""",
            (chat_id, cutoff.isoformat()),
        ).fetchall()
        if len(rows) > 0:
            return [dict(r) for r in rows]

        count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM ephemeral_queue WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if count_row["cnt"] > max_count:
            excess = count_row["cnt"] - max_count
            rows = conn.execute(
                """SELECT id, message_id, created_at FROM ephemeral_queue
                   WHERE chat_id = ?
                   ORDER BY created_at ASC LIMIT ?""",
                (chat_id, excess),
            ).fetchall()
            return [dict(r) for r in rows]
        return []


def remove_ephemeral_batch(ids: List[int]):
    if not ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM ephemeral_queue WHERE id IN ({placeholders})", ids
        )
        conn.commit()


def remove_ephemeral_by_message(message_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM ephemeral_queue WHERE message_id = ?", (message_id,))
        conn.commit()


# --------------- Anti-Raid ---------------

def check_raid_state(chat_id: int, settings: dict) -> bool:
    now = datetime.utcnow()
    window = timedelta(seconds=settings["anti_raid_window_seconds"])
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM anti_raid_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not row:
            return False

        window_start = datetime.fromisoformat(row["window_start"])
        if now - window_start > window:
            return False

        return (
            row["join_count"] >= settings["anti_raid_join_threshold"]
            or row["message_count"] >= settings["anti_raid_message_threshold"]
        )


def increment_raid_joins(chat_id: int):
    now = datetime.utcnow()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO anti_raid_state (chat_id, join_count, message_count, window_start)
               VALUES (?, 1, 0, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
               join_count = join_count + 1,
               window_start = CASE
                   WHEN (julianday(?) - julianday(window_start)) * 86400 > 30
                   THEN ? ELSE window_start
               END""",
            (chat_id, now.isoformat(), now.isoformat(), now.isoformat()),
        )
        conn.commit()


def increment_raid_messages(chat_id: int):
    now = datetime.utcnow()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO anti_raid_state (chat_id, join_count, message_count, window_start)
               VALUES (?, 0, 1, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
               message_count = message_count + 1,
               window_start = CASE
                   WHEN (julianday(?) - julianday(window_start)) * 86400 > 30
                   THEN ? ELSE window_start
               END""",
            (chat_id, now.isoformat(), now.isoformat(), now.isoformat()),
        )
        conn.commit()


def reset_raid_state(chat_id: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM anti_raid_state WHERE chat_id = ?", (chat_id,)
        )
        conn.commit()


# --------------- Rate Limiting ---------------

def check_rate_limit(chat_id: int, user_id: int, max_messages: int, window_seconds: int) -> bool:
    """
    Returns True if the user is being rate limited (too many messages in the window).
    """
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=window_seconds)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM rate_limit_buckets WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()

        if row:
            bucket_start = datetime.fromisoformat(row["window_start"])
            if now - bucket_start > timedelta(seconds=window_seconds):
                conn.execute(
                    """UPDATE rate_limit_buckets
                       SET message_count = 1, window_start = ?
                       WHERE chat_id = ? AND user_id = ?""",
                    (now.isoformat(), chat_id, user_id),
                )
                conn.commit()
                return False
            else:
                conn.execute(
                    """UPDATE rate_limit_buckets
                       SET message_count = message_count + 1
                       WHERE chat_id = ? AND user_id = ?""",
                    (chat_id, user_id),
                )
                conn.commit()
                exceeded = row["message_count"] + 1 > max_messages
                return exceeded
        else:
            conn.execute(
                """INSERT INTO rate_limit_buckets (chat_id, user_id, message_count, window_start)
                   VALUES (?, ?, 1, ?)""",
                (chat_id, user_id, now.isoformat()),
            )
            conn.commit()
            return False

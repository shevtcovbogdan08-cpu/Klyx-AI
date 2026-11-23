# database.py
"""
Слой работы с базой данных Klyx AI.

Сущности:
  - users            : пользователи, суточные лимиты, настройки, язык, даты регистрации и входа
  - memory_facts     : долгосрочная память
  - dialog_history   : история текущего диалога
  - tasks            : умный To-Do трекер
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from config import (
    DAILY_IMG2IMG_LIMIT,
    DAILY_USER_LIMIT,
    DAILY_VIDEO_LIMIT,
    DATABASE_PATH,
    MAX_HISTORY_MESSAGES,
    MAX_MEMORY_FACTS,
    MAX_TASKS_PER_USER,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {row["name"] for row in rows}


async def init_db() -> None:
    """Создаёт таблицы, индексы и мягко мигрирует существующую БД."""
    db = await aiosqlite.connect(DATABASE_PATH)
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id       INTEGER PRIMARY KEY,
                username          TEXT,
                first_name        TEXT,
                created_at        TEXT NOT NULL,
                requests_today    INTEGER NOT NULL DEFAULT 0,
                limit_reset_at    TEXT NOT NULL,
                style             TEXT NOT NULL DEFAULT 'detailed',
                expertise         TEXT NOT NULL DEFAULT 'novice',
                tone              TEXT NOT NULL DEFAULT 'friendly',
                current_model     TEXT,
                current_role      TEXT,
                img2img_used_today INTEGER NOT NULL DEFAULT 0,
                img2img_reset_at   TEXT,
                lang              TEXT NOT NULL DEFAULT 'ru',
                last_seen_at      TEXT,
                video_used_today  INTEGER NOT NULL DEFAULT 0,
                video_reset_at    TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_facts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                fact          TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialog_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                role          TEXT NOT NULL,
                content       TEXT NOT NULL,
                model         TEXT,
                created_at    TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                text          TEXT NOT NULL,
                is_done       INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
            """
        )

        existing = await _table_columns(db, "users")
        if "img2img_used_today" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN img2img_used_today INTEGER NOT NULL DEFAULT 0"
            )
        if "img2img_reset_at" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN img2img_reset_at TEXT")
        if "lang" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'")
        if "last_seen_at" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
        if "video_used_today" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN video_used_today INTEGER NOT NULL DEFAULT 0"
            )
        if "video_reset_at" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN video_reset_at TEXT")
        await db.execute("UPDATE users SET last_seen_at = created_at WHERE last_seen_at IS NULL")

        future_iso = _to_iso(_utcnow() + timedelta(hours=24))
        await db.execute(
            "UPDATE users SET img2img_reset_at = ? WHERE img2img_reset_at IS NULL",
            (future_iso,),
        )

        await db.execute(
            "UPDATE users SET video_reset_at = ? WHERE video_reset_at IS NULL",
            (future_iso,),
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_facts (telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_created ON users (created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user ON dialog_history (telegram_id, id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (telegram_id, id)"
        )

        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
async def get_or_create_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> dict[str, Any]:
    now = _utcnow()
    reset_at = now + timedelta(hours=24)
    db = await _connect()
    try:
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()

        if row is not None:
            result = dict(row)
            profile_changed = row["username"] != username or row["first_name"] != first_name
            last_seen_raw = row["last_seen_at"]
            seen_stale = True
            if last_seen_raw:
                try:
                    seen_stale = (now - _from_iso(last_seen_raw)).total_seconds() >= 60
                except ValueError:
                    seen_stale = True
            if profile_changed or seen_stale:
                await db.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_seen_at = ? "
                    "WHERE telegram_id = ?",
                    (username, first_name, _to_iso(now), telegram_id),
                )
                await db.commit()
                result["username"] = username
                result["first_name"] = first_name
                result["last_seen_at"] = _to_iso(now)
            return result

        await db.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, created_at,
                requests_today, limit_reset_at,
                img2img_used_today, img2img_reset_at,
                last_seen_at, video_used_today, video_reset_at
            ) VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?, 0, ?)
            """,
            (
                telegram_id,
                username,
                first_name,
                _to_iso(now),
                _to_iso(reset_at),
                _to_iso(reset_at),
                _to_iso(now),
                _to_iso(reset_at),
            ),
        )
        await db.commit()

        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else {}
    finally:
        await db.close()


async def get_user(telegram_id: int) -> dict[str, Any] | None:
    db = await _connect()
    try:
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        await db.close()


async def update_user_field(telegram_id: int, field: str, value: Any) -> bool:
    allowed = {
        "style",
        "expertise",
        "tone",
        "current_model",
        "current_role",
        "lang",
    }
    if field not in allowed:
        raise ValueError(f"Недопустимое поле для обновления: {field}")

    db = await _connect()
    try:
        cur = await db.execute(
            f"UPDATE users SET {field} = ? WHERE telegram_id = ?",
            (value, telegram_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Суточные лимиты: текст
# ---------------------------------------------------------------------------
async def get_user_daily_status(telegram_id: int) -> dict[str, Any]:
    now = _utcnow()
    db = await _connect()
    try:
        async with db.execute(
            "SELECT requests_today, limit_reset_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return {
                "used": 0,
                "limit": DAILY_USER_LIMIT,
                "remaining": DAILY_USER_LIMIT,
                "reset_at": now + timedelta(hours=24),
            }

        reset_at = _from_iso(row["limit_reset_at"])
        used = int(row["requests_today"])

        if now >= reset_at:
            used = 0
            reset_at = now + timedelta(hours=24)
            await db.execute(
                "UPDATE users SET requests_today = 0, limit_reset_at = ? WHERE telegram_id = ?",
                (_to_iso(reset_at), telegram_id),
            )
            await db.commit()

        return {
            "used": used,
            "limit": DAILY_USER_LIMIT,
            "remaining": max(0, DAILY_USER_LIMIT - used),
            "reset_at": reset_at,
        }
    finally:
        await db.close()


async def consume_request(telegram_id: int) -> tuple[bool, int]:
    now_iso = _to_iso(_utcnow())
    future_iso = _to_iso(_utcnow() + timedelta(hours=24))

    db = await _connect()
    try:
        await db.execute(
            "UPDATE users SET requests_today = 0, limit_reset_at = ? "
            "WHERE telegram_id = ? AND limit_reset_at <= ?",
            (future_iso, telegram_id, now_iso),
        )

        cur = await db.execute(
            "UPDATE users SET requests_today = requests_today + 1 "
            "WHERE telegram_id = ? AND requests_today < ?",
            (telegram_id, DAILY_USER_LIMIT),
        )
        allowed = cur.rowcount > 0
        await db.commit()

        async with db.execute(
            "SELECT requests_today FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur2:
            row = await cur2.fetchone()

        used = int(row["requests_today"]) if row is not None else 0
        remaining = max(0, DAILY_USER_LIMIT - used)
        return allowed, remaining
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Суточные лимиты: img2img
# ---------------------------------------------------------------------------
async def get_img2img_status(telegram_id: int) -> dict[str, Any]:
    now = _utcnow()
    db = await _connect()
    try:
        async with db.execute(
            "SELECT img2img_used_today, img2img_reset_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return {
                "used": 0,
                "limit": DAILY_IMG2IMG_LIMIT,
                "remaining": DAILY_IMG2IMG_LIMIT,
                "reset_at": now + timedelta(hours=24),
            }

        reset_raw = row["img2img_reset_at"]
        reset_at = _from_iso(reset_raw) if reset_raw else now + timedelta(hours=24)
        used = int(row["img2img_used_today"] or 0)

        if now >= reset_at:
            used = 0
            reset_at = now + timedelta(hours=24)
            await db.execute(
                "UPDATE users SET img2img_used_today = 0, img2img_reset_at = ? "
                "WHERE telegram_id = ?",
                (_to_iso(reset_at), telegram_id),
            )
            await db.commit()

        return {
            "used": used,
            "limit": DAILY_IMG2IMG_LIMIT,
            "remaining": max(0, DAILY_IMG2IMG_LIMIT - used),
            "reset_at": reset_at,
        }
    finally:
        await db.close()


async def consume_img2img_request(telegram_id: int) -> tuple[bool, int]:
    now_iso = _to_iso(_utcnow())
    future_iso = _to_iso(_utcnow() + timedelta(hours=24))

    db = await _connect()
    try:
        await db.execute(
            "UPDATE users SET img2img_used_today = 0, img2img_reset_at = ? "
            "WHERE telegram_id = ? AND img2img_reset_at <= ?",
            (future_iso, telegram_id, now_iso),
        )

        cur = await db.execute(
            "UPDATE users SET img2img_used_today = img2img_used_today + 1 "
            "WHERE telegram_id = ? AND img2img_used_today < ?",
            (telegram_id, DAILY_IMG2IMG_LIMIT),
        )
        allowed = cur.rowcount > 0
        await db.commit()

        async with db.execute(
            "SELECT img2img_used_today FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur2:
            row = await cur2.fetchone()

        used = int(row["img2img_used_today"]) if row is not None else 0
        remaining = max(0, DAILY_IMG2IMG_LIMIT - used)
        return allowed, remaining
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Язык интерфейса и фиксация входа
# ---------------------------------------------------------------------------
SUPPORTED_LANGS = ("ru", "en")
_TOUCH_INTERVAL_SECONDS = 60.0
_TOUCH_CACHE: dict[int, tuple[float, str]] = {}


def cached_lang(telegram_id: int) -> str:
    """Возвращает язык из памяти процесса (без обращения к БД). По умолчанию — ru."""
    cached = _TOUCH_CACHE.get(telegram_id)
    return cached[1] if cached else "ru"


def _remember_lang(telegram_id: int, lang: str) -> None:
    _TOUCH_CACHE[telegram_id] = (time.monotonic(), lang)


async def touch_user(telegram_id: int, username: str | None, first_name: str | None) -> str:
    """Записывает пользователя и дату последнего входа. Возвращает его язык."""
    cached = _TOUCH_CACHE.get(telegram_id)
    if cached and time.monotonic() - cached[0] < _TOUCH_INTERVAL_SECONDS:
        return cached[1]
    user = await get_or_create_user(telegram_id, username, first_name)
    lang = user.get("lang") if user.get("lang") in SUPPORTED_LANGS else "ru"
    _remember_lang(telegram_id, lang)
    return lang


async def get_lang(telegram_id: int) -> str:
    if telegram_id in _TOUCH_CACHE:
        return _TOUCH_CACHE[telegram_id][1]
    user = await get_user(telegram_id)
    lang = user.get("lang") if user and user.get("lang") in SUPPORTED_LANGS else "ru"
    _remember_lang(telegram_id, lang)
    return lang


async def set_user_lang(telegram_id: int, lang: str) -> bool:
    if lang not in SUPPORTED_LANGS:
        return False
    ok = await update_user_field(telegram_id, "lang", lang)
    if ok:
        _remember_lang(telegram_id, lang)
    return ok


# ---------------------------------------------------------------------------
# Суточные лимиты: видео
# ---------------------------------------------------------------------------
async def get_video_status(telegram_id: int) -> dict[str, Any]:
    now = _utcnow()
    db = await _connect()
    try:
        async with db.execute(
            "SELECT video_used_today, video_reset_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return {
                "used": 0,
                "limit": DAILY_VIDEO_LIMIT,
                "remaining": DAILY_VIDEO_LIMIT,
                "reset_at": now + timedelta(hours=24),
            }

        reset_raw = row["video_reset_at"]
        reset_at = _from_iso(reset_raw) if reset_raw else now + timedelta(hours=24)
        used = int(row["video_used_today"] or 0)

        if now >= reset_at:
            used = 0
            reset_at = now + timedelta(hours=24)
            await db.execute(
                "UPDATE users SET video_used_today = 0, video_reset_at = ? WHERE telegram_id = ?",
                (_to_iso(reset_at), telegram_id),
            )
            await db.commit()

        return {
            "used": used,
            "limit": DAILY_VIDEO_LIMIT,
            "remaining": max(0, DAILY_VIDEO_LIMIT - used),
            "reset_at": reset_at,
        }
    finally:
        await db.close()


async def consume_video_request(telegram_id: int) -> tuple[bool, int]:
    now_iso = _to_iso(_utcnow())
    future_iso = _to_iso(_utcnow() + timedelta(hours=24))

    db = await _connect()
    try:
        await db.execute(
            "UPDATE users SET video_used_today = 0, video_reset_at = ? "
            "WHERE telegram_id = ? AND (video_reset_at IS NULL OR video_reset_at <= ?)",
            (future_iso, telegram_id, now_iso),
        )
        cur = await db.execute(
            "UPDATE users SET video_used_today = video_used_today + 1 "
            "WHERE telegram_id = ? AND video_used_today < ?",
            (telegram_id, DAILY_VIDEO_LIMIT),
        )
        allowed = cur.rowcount > 0
        await db.commit()

        async with db.execute(
            "SELECT video_used_today FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur2:
            row = await cur2.fetchone()
        used = int(row["video_used_today"]) if row is not None else 0
        return allowed, max(0, DAILY_VIDEO_LIMIT - used)
    finally:
        await db.close()


async def refund_video_request(telegram_id: int) -> None:
    """Возвращает попытку, если видео не удалось создать."""
    db = await _connect()
    try:
        await db.execute(
            "UPDATE users SET video_used_today = MAX(0, video_used_today - 1) "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Админ-панель: статистика пользователей
# ---------------------------------------------------------------------------
async def get_users_stats() -> dict[str, int]:
    since_iso = _to_iso(_utcnow() - timedelta(hours=24))
    db = await _connect()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN last_seen_at >= ? THEN 1 ELSE 0 END) AS active_24h, "
            "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS new_24h "
            "FROM users",
            (since_iso, since_iso),
        ) as cur:
            row = await cur.fetchone()
        return {
            "total": int(row["total"] or 0) if row else 0,
            "active_24h": int(row["active_24h"] or 0) if row else 0,
            "new_24h": int(row["new_24h"] or 0) if row else 0,
        }
    finally:
        await db.close()


async def get_recent_users(limit: int = 20) -> list[dict[str, Any]]:
    db = await _connect()
    try:
        async with db.execute(
            "SELECT telegram_id, username, first_name, created_at, last_seen_at "
            "FROM users ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_all_users() -> list[dict[str, Any]]:
    db = await _connect()
    try:
        async with db.execute(
            "SELECT telegram_id, username, first_name, created_at, last_seen_at, lang "
            "FROM users ORDER BY created_at ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Долгосрочная память
# ---------------------------------------------------------------------------
async def add_memory_fact(telegram_id: int, fact: str) -> tuple[bool, str | None]:
    fact = fact.strip()
    if not fact:
        return False, "empty"
    if len(fact) > 500:
        return False, "too_long"

    db = await _connect()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS c FROM memory_facts WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
        count = int(row["c"]) if row is not None else 0
        if count >= MAX_MEMORY_FACTS:
            return False, "limit"

        await db.execute(
            "INSERT INTO memory_facts (telegram_id, fact, created_at) VALUES (?, ?, ?)",
            (telegram_id, fact, _to_iso(_utcnow())),
        )
        await db.commit()
        return True, None
    finally:
        await db.close()


async def get_memory_facts(telegram_id: int) -> list[dict[str, Any]]:
    db = await _connect()
    try:
        async with db.execute(
            "SELECT id, fact, created_at FROM memory_facts "
            "WHERE telegram_id = ? ORDER BY id DESC",
            (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def delete_memory_fact(telegram_id: int, fact_id: int) -> bool:
    db = await _connect()
    try:
        cur = await db.execute(
            "DELETE FROM memory_facts WHERE id = ? AND telegram_id = ?",
            (fact_id, telegram_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def clear_all_memory(telegram_id: int) -> int:
    db = await _connect()
    try:
        cur = await db.execute(
            "DELETE FROM memory_facts WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        return cur.rowcount
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# История диалога
# ---------------------------------------------------------------------------
async def add_history_message(
    telegram_id: int,
    role: str,
    content: str,
    model: str | None = None,
) -> None:
    db = await _connect()
    try:
        await db.execute(
            "INSERT INTO dialog_history (telegram_id, role, content, model, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, role, content, model, _to_iso(_utcnow())),
        )
        await db.execute(
            """
            DELETE FROM dialog_history
            WHERE telegram_id = ? AND id NOT IN (
                SELECT id FROM dialog_history
                WHERE telegram_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (telegram_id, telegram_id, MAX_HISTORY_MESSAGES),
        )
        await db.commit()
    finally:
        await db.close()


async def get_history(telegram_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    take = limit if limit is not None else MAX_HISTORY_MESSAGES
    db = await _connect()
    try:
        async with db.execute(
            "SELECT role, content, model, created_at FROM dialog_history "
            "WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, take),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        await db.close()


async def clear_history(telegram_id: int) -> int:
    db = await _connect()
    try:
        cur = await db.execute(
            "DELETE FROM dialog_history WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        return cur.rowcount
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Профиль
# ---------------------------------------------------------------------------
async def get_full_profile(telegram_id: int) -> dict[str, Any] | None:
    user = await get_user(telegram_id)
    if user is None:
        return None
    facts = await get_memory_facts(telegram_id)
    user["facts"] = facts
    return user


# ---------------------------------------------------------------------------
# Задачи (To-Do)
# ---------------------------------------------------------------------------
async def add_task(telegram_id: int, text: str) -> tuple[bool, str | None, int | None]:
    """Добавляет задачу. Возвращает (ok, reason, task_id)."""
    text = text.strip()
    if not text:
        return False, "empty", None
    if len(text) > 300:
        return False, "too_long", None

    db = await _connect()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
        count = int(row["c"]) if row is not None else 0
        if count >= MAX_TASKS_PER_USER:
            return False, "limit", None

        now_iso = _to_iso(_utcnow())
        cur = await db.execute(
            "INSERT INTO tasks (telegram_id, text, is_done, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (telegram_id, text, now_iso, now_iso),
        )
        await db.commit()
        return True, None, cur.lastrowid
    finally:
        await db.close()


async def get_tasks(telegram_id: int) -> list[dict[str, Any]]:
    db = await _connect()
    try:
        async with db.execute(
            "SELECT id, text, is_done, created_at, updated_at FROM tasks "
            "WHERE telegram_id = ? ORDER BY is_done ASC, id DESC",
            (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_tasks_summary(telegram_id: int) -> dict[str, int]:
    """Возвращает количество задач: всего, выполнено, активно."""
    db = await _connect()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN is_done = 1 THEN 1 ELSE 0 END) AS done "
            "FROM tasks WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
        total = int(row["total"]) if row and row["total"] is not None else 0
        done = int(row["done"]) if row and row["done"] is not None else 0
        return {"total": total, "done": done, "active": total - done}
    finally:
        await db.close()


async def toggle_task(telegram_id: int, task_id: int) -> bool | None:
    """
    Переключает статус задачи.
    Возвращает новое значение is_done (True/False) или None, если задача не найдена.
    """
    db = await _connect()
    try:
        async with db.execute(
            "SELECT is_done FROM tasks WHERE id = ? AND telegram_id = ?",
            (task_id, telegram_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None

        new_value = 0 if int(row["is_done"]) == 1 else 1
        await db.execute(
            "UPDATE tasks SET is_done = ?, updated_at = ? WHERE id = ? AND telegram_id = ?",
            (new_value, _to_iso(_utcnow()), task_id, telegram_id),
        )
        await db.commit()
        return bool(new_value)
    finally:
        await db.close()


async def delete_task(telegram_id: int, task_id: int) -> bool:
    db = await _connect()
    try:
        cur = await db.execute(
            "DELETE FROM tasks WHERE id = ? AND telegram_id = ?",
            (task_id, telegram_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def clear_completed_tasks(telegram_id: int) -> int:
    db = await _connect()
    try:
        cur = await db.execute(
            "DELETE FROM tasks WHERE telegram_id = ? AND is_done = 1",
            (telegram_id,),
        )
        await db.commit()
        return cur.rowcount
    finally:
        await db.close()
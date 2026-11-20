# database.py
"""
Слой работы с базой данных Klyx AI.

Сущности:
  - users            : пользователи, суточные лимиты (текст + img2img), настройки
  - memory_facts     : долгосрочная память
  - dialog_history   : история текущего диалога
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from config import (
    DAILY_IMG2IMG_LIMIT,
    DAILY_USER_LIMIT,
    DATABASE_PATH,
    MAX_HISTORY_MESSAGES,
    MAX_MEMORY_FACTS,
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
                img2img_reset_at   TEXT
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

        # Мягкая миграция: добавляем колонки, если их нет в старой базе
        existing = await _table_columns(db, "users")
        if "img2img_used_today" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN img2img_used_today INTEGER NOT NULL DEFAULT 0"
            )
        if "img2img_reset_at" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN img2img_reset_at TEXT")

        # Для старых записей без img2img_reset_at проставляем окно 24 ч от текущего момента
        now_iso = _to_iso(_utcnow())
        future_iso = _to_iso(_utcnow() + timedelta(hours=24))
        await db.execute(
            "UPDATE users SET img2img_reset_at = ? WHERE img2img_reset_at IS NULL",
            (future_iso,),
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_facts (telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user ON dialog_history (telegram_id, id)"
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
            if row["username"] != username or row["first_name"] != first_name:
                await db.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                    (username, first_name, telegram_id),
                )
                await db.commit()
                updated = dict(row)
                updated["username"] = username
                updated["first_name"] = first_name
                return updated
            return dict(row)

        await db.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, created_at,
                requests_today, limit_reset_at,
                img2img_used_today, img2img_reset_at
            ) VALUES (?, ?, ?, ?, 0, ?, 0, ?)
            """,
            (
                telegram_id,
                username,
                first_name,
                _to_iso(now),
                _to_iso(reset_at),
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
# Суточные лимиты: img2img (10 в сутки)
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
    """Атомарно списывает одну попытку img2img. Возвращает (allowed, remaining)."""
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
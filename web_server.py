# web_server.py
"""
Веб-сервер Klyx AI для Telegram Mini App.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import config
import database as db
from ai_service import (
    AIServiceError,
    AllModelsFailedError,
    AudioConversionError,
    ImageEditingError,
    ImageGenerationError,
    MemeGenerationError,
    PptxGenerationError,
    TranscriptionError,
    WordReportError,
    BATTLE_MODELS,
    analyze_image,
    build_rewrite_messages,
    build_url_context,
    clean_response_text,
    convert_audio_to_voice,
    edit_image_img2img,
    generate_image_txt2img,
    generate_meme,
    generate_pptx_presentation,
    generate_word_report,
    get_system_prompt,
    get_text_models,
    image_to_data_url,
    request_ai,
    request_two_models,
    stream_ai,
    transcribe_voice,
)
from i18n import current_lang, current_user_id, translate_text
from media_service import (
    cancel_job,
    is_job_active,
    make_temp_dir,
    start_note_job,
    start_pptx_job,
    start_video_job,
)

logger = logging.getLogger(__name__)


class _UploadError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


async def _read_json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Тело запроса должно быть JSON-объектом.")
    return data


def _extract_init_data(payload: dict[str, Any]) -> str:
    init_data = payload.get("initData")
    if not isinstance(init_data, str) or not init_data.strip():
        raise web.HTTPUnauthorized(text="Отсутствует initData.")
    return init_data


def _validate_init_data(bot: Bot, init_data: str) -> int:
    try:
        parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    except ValueError as exc:
        logger.warning("Невалидный initData: %s", exc)
        raise web.HTTPUnauthorized(text="Невалидный initData.") from exc
    if parsed.user is None:
        raise web.HTTPUnauthorized(text="В initData отсутствует пользователь.")
    telegram_id = int(parsed.user.id)
    current_user_id.set(telegram_id)
    current_lang.set(db.cached_lang(telegram_id))
    return telegram_id


async def _ensure_user(telegram_id: int, parsed_user: Any) -> dict[str, Any]:
    """Регистрирует пользователя, обновляет дату последнего входа и выставляет язык запроса."""
    lang = await db.touch_user(
        telegram_id,
        getattr(parsed_user, "username", None),
        getattr(parsed_user, "first_name", None),
    )
    current_lang.set(lang)
    try:
        await game_checkin(telegram_id)
    except Exception:
        logger.exception("Не удалось обновить streak пользователя %s", telegram_id)
    user = await db.get_user(telegram_id)
    if user is not None:
        return user
    return await db.get_or_create_user(
        telegram_id,
        getattr(parsed_user, "username", None),
        getattr(parsed_user, "first_name", None),
    )


# ---------------------------------------------------------------------------
# Геймификация: xp, ранги Klyx, огонёк дней подряд (streak) и бонусные попытки.
# Хранится в отдельной SQLite-таблице klyx_profiles (файл рядом с основной БД),
# поэтому существующая схема database.py не затрагивается.
# ---------------------------------------------------------------------------
_RANKS: tuple[tuple[int, str, str, str], ...] = (
    (0, "Новичок", "Newbie", "🌱"),
    (100, "Исследователь", "Explorer", "🧭"),
    (400, "Кибер-мастер", "Cyber Master", "⚡"),
    (1000, "Легенда Klyx", "Klyx Legend", "👑"),
)

XP_CHAT = 3
XP_VISION = 6
XP_IMAGE = 4
XP_WORD = 5
XP_PPTX = 8
XP_VIDEO = 10
XP_BATTLE = 4
XP_VOTE = 5

_GAME_LOCK = threading.Lock()
_GAME_READY = False
_CHECKED_TODAY: dict[int, str] = {}


def _resolve_game_db_path() -> Path:
    for name in ("DB_PATH", "DATABASE_PATH", "DB_FILE", "DATABASE_FILE", "SQLITE_PATH"):
        value = getattr(config, name, None)
        if value and not str(value).startswith(("postgres", "mysql", "http")):
            return Path(str(value)).parent / "klyx_profiles.sqlite3"
    return Path(config.BASE_DIR) / "klyx_profiles.sqlite3"


_GAME_DB_PATH = _resolve_game_db_path()


def _game_connect() -> sqlite3.Connection:
    global _GAME_READY
    _GAME_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_GAME_DB_PATH), timeout=10)
    if not _GAME_READY:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS klyx_profiles ("
            "telegram_id INTEGER PRIMARY KEY, "
            "xp INTEGER NOT NULL DEFAULT 0, "
            "streak_days INTEGER NOT NULL DEFAULT 0, "
            "best_streak INTEGER NOT NULL DEFAULT 0, "
            "last_login_day TEXT, "
            "bonus_attempts INTEGER NOT NULL DEFAULT 0, "
            "pending_bonus INTEGER NOT NULL DEFAULT 0, "
            "battles_won INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
        _GAME_READY = True
    return conn


def _game_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _checkin_sync(telegram_id: int, today: str) -> None:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        row = conn.execute(
            "SELECT xp, streak_days, best_streak, last_login_day, bonus_attempts, pending_bonus "
            "FROM klyx_profiles WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO klyx_profiles (telegram_id, xp, streak_days, best_streak, last_login_day) "
                "VALUES (?, ?, 1, 1, ?)",
                (telegram_id, 11, today),
            )
            conn.commit()
            return
        xp, streak, best, last_day, bonus, pending = row
        if last_day == today:
            return
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        streak = streak + 1 if last_day == yesterday else 1
        best = max(best, streak)
        xp += 10 + min(streak, 7)
        if streak % 3 == 0:  # каждые 3 дня подряд — бонусная попытка
            bonus += 1
            pending += 1
        conn.execute(
            "UPDATE klyx_profiles SET xp=?, streak_days=?, best_streak=?, last_login_day=?, "
            "bonus_attempts=?, pending_bonus=? WHERE telegram_id=?",
            (xp, streak, best, today, bonus, pending, telegram_id),
        )
        conn.commit()


def _game_row_sync(telegram_id: int) -> dict[str, int | str | None]:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        row = conn.execute(
            "SELECT xp, streak_days, best_streak, last_login_day, bonus_attempts, pending_bonus, battles_won "
            "FROM klyx_profiles WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if row is None:
        return {
            "xp": 0, "streak_days": 0, "best_streak": 0, "last_login_day": None,
            "bonus_attempts": 0, "pending_bonus": 0, "battles_won": 0,
        }
    keys = ("xp", "streak_days", "best_streak", "last_login_day", "bonus_attempts", "pending_bonus", "battles_won")
    return dict(zip(keys, row))


def _game_add_sync(telegram_id: int, xp: int = 0, bonus: int = 0, battles_won: int = 0) -> None:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO klyx_profiles (telegram_id) VALUES (?)", (telegram_id,)
        )
        conn.execute(
            "UPDATE klyx_profiles SET xp = xp + ?, bonus_attempts = bonus_attempts + ?, "
            "battles_won = battles_won + ? WHERE telegram_id = ?",
            (xp, bonus, battles_won, telegram_id),
        )
        conn.commit()


def _use_bonus_sync(telegram_id: int) -> bool:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        cursor = conn.execute(
            "UPDATE klyx_profiles SET bonus_attempts = bonus_attempts - 1 "
            "WHERE telegram_id = ? AND bonus_attempts > 0",
            (telegram_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def _ack_bonus_sync(telegram_id: int) -> None:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        conn.execute("UPDATE klyx_profiles SET pending_bonus = 0 WHERE telegram_id = ?", (telegram_id,))
        conn.commit()


def _game_totals_sync() -> dict[str, int]:
    with _GAME_LOCK, contextlib.closing(_game_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(xp), 0), COALESCE(MAX(best_streak), 0), "
            "COALESCE(SUM(battles_won), 0), COALESCE(SUM(CASE WHEN streak_days >= 3 THEN 1 ELSE 0 END), 0) "
            "FROM klyx_profiles"
        ).fetchone()
    return {
        "players": int(row[0]), "total_xp": int(row[1]), "best_streak": int(row[2]),
        "battles_won": int(row[3]), "streak3": int(row[4]),
    }


async def game_totals() -> dict[str, int]:
    """Сводная игровая статистика для админ-панели бота."""
    return await asyncio.to_thread(_game_totals_sync)


def _rank_info(xp: int, lang: str) -> dict[str, Any]:
    index = 0
    for position, (threshold, _ru, _en, _icon) in enumerate(_RANKS):
        if xp >= threshold:
            index = position
    threshold, name_ru, name_en, icon = _RANKS[index]
    if index + 1 < len(_RANKS):
        next_threshold = _RANKS[index + 1][0]
        span = max(next_threshold - threshold, 1)
        percent = int(min(100, max(0, (xp - threshold) * 100 / span)))
        xp_to_next: int | None = max(next_threshold - xp, 0)
        next_name = _RANKS[index + 1][2] if lang == "en" else _RANKS[index + 1][1]
    else:
        next_threshold, percent, xp_to_next, next_name = None, 100, None, None
    return {
        "rank_index": index,
        "rank": name_en if lang == "en" else name_ru,
        "rank_ru": name_ru,
        "rank_en": name_en,
        "rank_icon": icon,
        "next_rank": next_name,
        "next_xp": next_threshold,
        "xp_to_next": xp_to_next,
        "progress_pct": percent,
    }


async def game_checkin(telegram_id: int) -> None:
    """Ежедневный вход: обновляет streak, начисляет xp и каждые 3 дня — бонусную попытку."""
    today = _game_today()
    if _CHECKED_TODAY.get(telegram_id) == today:
        return
    await asyncio.to_thread(_checkin_sync, telegram_id, today)
    _CHECKED_TODAY[telegram_id] = today


async def game_state(telegram_id: int) -> dict[str, Any]:
    row = await asyncio.to_thread(_game_row_sync, telegram_id)
    state: dict[str, Any] = {
        "xp": int(row["xp"] or 0),
        "streak_days": int(row["streak_days"] or 0),
        "best_streak": int(row["best_streak"] or 0),
        "bonus_attempts": int(row["bonus_attempts"] or 0),
        "pending_bonus": int(row["pending_bonus"] or 0),
        "battles_won": int(row["battles_won"] or 0),
        "next_bonus_in": 3 - (int(row["streak_days"] or 0) % 3),
    }
    state.update(_rank_info(state["xp"], current_lang.get()))
    return state


async def award_xp(telegram_id: int, amount: int) -> None:
    try:
        await asyncio.to_thread(_game_add_sync, telegram_id, amount, 0, 0)
    except Exception:
        logger.exception("Не удалось начислить xp пользователю %s", telegram_id)


async def refund_text_attempt(telegram_id: int) -> None:
    """Возвращает потраченную попытку (в виде бонусной попытки — работает для любого источника)."""
    try:
        await asyncio.to_thread(_game_add_sync, telegram_id, 0, 1, 0)
    except Exception:
        logger.exception("Не удалось вернуть попытку пользователю %s", telegram_id)


async def _consume_text_attempt(telegram_id: int) -> tuple[bool, int]:
    """Списывает суточную попытку, а при её исчерпании — бонусную. Возвращает (разрешено, остаток всего)."""
    allowed, remaining = await db.consume_request(telegram_id)
    if allowed:
        row = await asyncio.to_thread(_game_row_sync, telegram_id)
        return True, int(remaining) + int(row["bonus_attempts"] or 0)
    if await asyncio.to_thread(_use_bonus_sync, telegram_id):
        row = await asyncio.to_thread(_game_row_sync, telegram_id)
        return True, int(row["bonus_attempts"] or 0)
    return False, 0


# ---------------------------------------------------------------------------
# Отмена запроса клиентом («⏹ Стоп» / AbortController) и потоковые ответы (NDJSON)
# ---------------------------------------------------------------------------
_ACTIVE_STREAMS: set[int] = set()
_REWRITE_COUNTS: dict[int, tuple[str, int]] = {}
_REWRITE_DAILY_LIMIT = 40


def _err_text(text: str) -> str:
    return translate_text(text) if current_lang.get() == "en" else text


def _client_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


async def _run_guarded(request: web.Request, awaitable: Any) -> tuple[bool, Any]:
    """
    Выполняет awaitable и следит за соединением. Если клиент отменил запрос (AbortController),
    задача мгновенно отменяется. Возвращает (клиент_ушёл, результат).
    """
    task = asyncio.ensure_future(awaitable)

    async def _watch() -> None:
        while True:
            await asyncio.sleep(0.4)
            if _client_gone(request):
                return

    watcher = asyncio.create_task(_watch())
    try:
        done, _pending = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return False, task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True, None
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()


def _gone_response() -> web.Response:
    return web.json_response({"ok": False, "error": "Запрос остановлен."}, status=499)


async def _open_ndjson(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/x-ndjson; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    return response


async def _send_line(response: web.StreamResponse, payload: dict[str, Any]) -> None:
    await response.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _spawn_refund(telegram_id: int) -> None:
    asyncio.create_task(refund_text_attempt(telegram_id))


def _model_label(model: str) -> str:
    lowered = model.lower()
    if "gemma" in lowered:
        return "Gemma 3"
    if "qwen" in lowered:
        return "Qwen 3"
    if "llama" in lowered:
        return "Llama"
    if "deepseek" in lowered:
        return "DeepSeek"
    return model.split("/")[-1].split(":")[0]


async def _stream_answer(
    request: web.Request,
    telegram_id: int,
    messages: list[dict[str, Any]],
    preferred_model: str | None,
    *,
    charged: bool,
    save: Any = None,
    xp: int = 0,
    max_tokens: int = 2048,
) -> web.StreamResponse:
    """Потоково отдаёт ответ модели строками JSON: start -> delta* -> done | error."""
    response = await _open_ndjson(request)
    info: dict[str, Any] = {}
    parts: list[str] = []
    completed = False
    started_at = time.monotonic()
    generator = stream_ai(messages, preferred_model=preferred_model, info=info, max_tokens=max_tokens)
    buffer = ""
    last_flush = 0.0
    try:
        await _send_line(response, {"t": "start"})
        async for delta in generator:
            parts.append(delta)
            buffer += delta
            now = time.monotonic()
            if now - last_flush >= 0.04:
                await _send_line(response, {"t": "delta", "text": buffer})
                buffer = ""
                last_flush = now
            if _client_gone(request):
                raise ConnectionResetError("client gone")
        if buffer:
            await _send_line(response, {"t": "delta", "text": buffer})
            buffer = ""
        answer = clean_response_text("".join(parts).strip())
        if not answer:
            raise AIServiceError("Модель вернула пустой ответ.")
        model_used = str(info.get("model") or "")
        if save is not None:
            await save(answer, model_used)
        if xp:
            await award_xp(telegram_id, xp)
        completed = True
        await _send_line(
            response,
            {
                "t": "done",
                "answer": answer,
                "model": model_used,
                "elapsed": round(time.monotonic() - started_at, 2),
                "game": await game_state(telegram_id),
            },
        )
    except (ConnectionError, ConnectionResetError):
        logger.info("Клиент остановил генерацию (user %s)", telegram_id)
    except AIServiceError as exc:
        try:
            await _send_line(response, {"t": "error", "error": _err_text(f"Ошибка ИИ-сервиса: {exc}")})
        except ConnectionError:
            pass
    finally:
        with contextlib.suppress(Exception):
            await generator.aclose()
        if not completed and charged:
            _spawn_refund(telegram_id)
    with contextlib.suppress(Exception):
        await response.write_eof()
    return response


def _build_system_messages(user: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": get_system_prompt(user.get("lang") or "ru")},
        {
            "role": "system",
            "content": (
                "Ты — Klyx AI, бесплатный и дружелюбный ИИ-ассистент. "
                "Отвечай полезно и прозрачно. Не упоминай платные подписки или рекламу."
            ),
        },
    ]

    length = user.get("style") or "detailed"
    expertise = user.get("expertise") or "novice"
    tone = user.get("tone") or "friendly"

    style_parts: list[str] = []
    if length == "short":
        style_parts.append("Отвечай кратко, по существу.")
    else:
        style_parts.append("Отвечай подробно, с пояснениями и примерами.")
    if expertise == "novice":
        style_parts.append("Объясняй простыми словами.")
    else:
        style_parts.append("Можно использовать профессиональные термины.")
    if tone == "friendly":
        style_parts.append("Тон дружелюбный и поддерживающий.")
    else:
        style_parts.append("Тон строгий и деловой.")

    messages.append({"role": "system", "content": " ".join(style_parts)})

    role_key = user.get("current_role")
    role_prompts = {
        "programmer": "Ты опытный Senior Python-разработчик.",
        "teacher": "Ты терпеливый учитель.",
        "copywriter": "Ты профессиональный копирайтер.",
        "translator": "Ты профессиональный переводчик.",
        "socrates": "Ты Сократ, веди диалог через вопросы.",
        "critic": "Ты строгий, но конструктивный критик.",
    }
    if role_key and role_key in role_prompts:
        messages.append({"role": "system", "content": role_prompts[role_key]})

    if facts:
        facts_text = "\n".join(f"- {fact['fact']}" for fact in facts)
        messages.append(
            {"role": "system", "content": f"Что ты знаешь о пользователе:\n{facts_text}"}
        )

    return messages


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(config.BASE_DIR / "webapp" / "index.html")


async def handle_static(request: web.Request) -> web.FileResponse:
    filename = request.match_info.get("filename", "")
    if not filename or "/" in filename or ".." in filename:
        raise web.HTTPNotFound()
    return web.FileResponse(config.BASE_DIR / "webapp" / filename)


async def api_limits(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    text_status = await db.get_user_daily_status(telegram_id)
    img_status = await db.get_img2img_status(telegram_id)
    video_status = await db.get_video_status(telegram_id)
    game = await game_state(telegram_id)

    return web.json_response(
        {
            "ok": True,
            "limits": {
                "text": {
                    "used": text_status["used"],
                    "limit": text_status["limit"],
                    "remaining": int(text_status["remaining"]) + game["bonus_attempts"],
                    "bonus": game["bonus_attempts"],
                },
                "img2img": {
                    "used": img_status["used"],
                    "limit": img_status["limit"],
                    "remaining": img_status["remaining"],
                },
                "video": {
                    "used": video_status["used"],
                    "limit": video_status["limit"],
                    "remaining": video_status["remaining"],
                },
            },
            "game": game,
        }
    )


async def api_models(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    _validate_init_data(bot, init_data)
    return web.json_response(
        {
            "ok": True,
            "models": list(get_text_models()),
            "battle_models": [
                {"id": model, "label": _model_label(model)} for model in BATTLE_MODELS
            ],
        }
    )


async def api_profile(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    profile = await db.get_full_profile(telegram_id)
    if profile is None:
        return web.json_response({"ok": False, "error": "Профиль не найден."}, status=404)

    return web.json_response(
        {
            "ok": True,
            "profile": {
                "telegram_id": profile["telegram_id"],
                "first_name": profile.get("first_name"),
                "lang": profile.get("lang") or "ru",
                "style": profile.get("style"),
                "expertise": profile.get("expertise"),
                "tone": profile.get("tone"),
                "current_model": profile.get("current_model"),
                "current_role": profile.get("current_role"),
                "facts": [
                    {"id": f["id"], "fact": f["fact"]} for f in profile.get("facts", [])
                ],
            },
            "game": await game_state(telegram_id),
        }
    )


async def _prepare_chat(
    telegram_id: int, user: dict[str, Any], message_text: str
) -> list[dict[str, Any]]:
    """Собирает сообщения для модели: системные + история + текущий вопрос (со страницей по ссылке)."""
    facts = await db.get_memory_facts(telegram_id)
    messages: list[dict[str, Any]] = list(_build_system_messages(user, facts))
    history = await db.get_history(telegram_id)
    for item in history:
        if item["role"] in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item["content"]})
    model_text, _has_url = await build_url_context(message_text, user.get("lang") or "ru")
    messages.append({"role": "user", "content": model_text})
    return messages


def _read_message(payload: dict[str, Any]) -> str | None:
    message_text = payload.get("message")
    if not isinstance(message_text, str) or not message_text.strip():
        return None
    return message_text.strip()[:4000]


async def api_chat(request: web.Request) -> web.Response:
    """Обычный (непотоковый) ответ. Потоковая версия — /api/chat/stream."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    message_text = _read_message(payload)
    if message_text is None:
        return web.json_response({"ok": False, "error": "Пустое сообщение."}, status=400)

    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    user = await _ensure_user(telegram_id, parsed.user)

    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    try:
        disconnected, result = await _run_guarded(
            request, _chat_once(telegram_id, user, message_text)
        )
    except AllModelsFailedError as exc:
        await refund_text_attempt(telegram_id)
        return web.json_response(
            {"ok": False, "error": f"Все бесплатные модели недоступны: {exc}"}, status=502
        )
    except AIServiceError as exc:
        await refund_text_attempt(telegram_id)
        return web.json_response(
            {"ok": False, "error": f"Ошибка ИИ-сервиса: {exc}"}, status=502
        )
    if disconnected:
        await refund_text_attempt(telegram_id)
        return _gone_response()

    answer, elapsed = result
    await award_xp(telegram_id, XP_CHAT)
    return web.json_response(
        {
            "ok": True,
            "answer": answer,
            "elapsed": elapsed,
            "remaining": remaining,
            "game": await game_state(telegram_id),
        }
    )


async def _chat_once(telegram_id: int, user: dict[str, Any], message_text: str) -> tuple[str, float]:
    messages = await _prepare_chat(telegram_id, user, message_text)
    answer, model_used, elapsed = await request_ai(
        messages, preferred_model=user.get("current_model")
    )
    answer = clean_response_text(answer)
    await db.add_history_message(telegram_id, "user", message_text)
    await db.add_history_message(telegram_id, "assistant", answer, model_used)
    return answer, elapsed


async def api_chat_stream(request: web.Request) -> web.StreamResponse:
    """Потоковая печать ответа в реальном времени (NDJSON: start, delta, done, error)."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    message_text = _read_message(payload)
    if message_text is None:
        return web.json_response({"ok": False, "error": "Пустое сообщение."}, status=400)

    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    user = await _ensure_user(telegram_id, parsed.user)

    if telegram_id in _ACTIVE_STREAMS:
        return web.json_response(
            {"ok": False, "error": "Предыдущий ответ ещё генерируется."}, status=429
        )
    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    _ACTIVE_STREAMS.add(telegram_id)
    try:
        try:
            disconnected, messages = await _run_guarded(
                request, _prepare_chat(telegram_id, user, message_text)
            )
        except Exception:
            await refund_text_attempt(telegram_id)
            raise
        if disconnected:
            await refund_text_attempt(telegram_id)
            return _gone_response()

        async def _save(answer: str, model_used: str) -> None:
            await db.add_history_message(telegram_id, "user", message_text)
            await db.add_history_message(telegram_id, "assistant", answer, model_used)

        return await _stream_answer(
            request,
            telegram_id,
            messages,
            user.get("current_model"),
            charged=True,
            save=_save,
            xp=XP_CHAT,
        )
    finally:
        _ACTIVE_STREAMS.discard(telegram_id)


async def api_chat_rewrite(request: web.Request) -> web.StreamResponse:
    """Кнопки «Кратко» / «Подробнее»: потоково переписывает готовый ответ."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    user = await _ensure_user(telegram_id, parsed.user)

    text = payload.get("text")
    mode = payload.get("mode")
    if not isinstance(text, str) or not text.strip() or mode not in ("short", "long"):
        return web.json_response({"ok": False, "error": "Некорректный запрос."}, status=400)

    today = _game_today()
    day, count = _REWRITE_COUNTS.get(telegram_id, (today, 0))
    if day != today:
        count = 0
    if count >= _REWRITE_DAILY_LIMIT:
        return web.json_response(
            {"ok": False, "error": "Слишком много переписываний на сегодня."}, status=429
        )
    if telegram_id in _ACTIVE_STREAMS:
        return web.json_response(
            {"ok": False, "error": "Предыдущий ответ ещё генерируется."}, status=429
        )
    _REWRITE_COUNTS[telegram_id] = (today, count + 1)

    messages = build_rewrite_messages(text.strip()[:6000], mode, user.get("lang") or "ru")
    _ACTIVE_STREAMS.add(telegram_id)
    try:
        return await _stream_answer(
            request,
            telegram_id,
            messages,
            user.get("current_model"),
            charged=False,
            max_tokens=900 if mode == "short" else 2200,
        )
    finally:
        _ACTIVE_STREAMS.discard(telegram_id)


async def api_chat_battle(request: web.Request) -> web.StreamResponse:
    """«⚔️ Битва ИИ»: один вопрос параллельно двум моделям (Gemma 3 и Qwen 3)."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    message_text = _read_message(payload)
    if message_text is None:
        return web.json_response({"ok": False, "error": "Пустое сообщение."}, status=400)

    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    user = await _ensure_user(telegram_id, parsed.user)

    if telegram_id in _ACTIVE_STREAMS:
        return web.json_response(
            {"ok": False, "error": "Предыдущий ответ ещё генерируется."}, status=429
        )
    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    _ACTIVE_STREAMS.add(telegram_id)
    task: asyncio.Future[Any] | None = None
    completed = False
    try:
        try:
            disconnected, messages = await _run_guarded(
                request, _prepare_chat(telegram_id, user, message_text)
            )
        except Exception:
            await refund_text_attempt(telegram_id)
            raise
        if disconnected:
            await refund_text_attempt(telegram_id)
            return _gone_response()

        response = await _open_ndjson(request)
        task = asyncio.ensure_future(
            request_two_models(messages, BATTLE_MODELS[0], BATTLE_MODELS[1], max_tokens=1400)
        )
        try:
            await _send_line(response, {"t": "start"})
            while not task.done():
                await asyncio.wait({task}, timeout=2.5)
                if _client_gone(request):
                    raise ConnectionResetError("client gone")
                if not task.done():
                    await _send_line(response, {"t": "ping"})
            first, second = task.result()
            answers = []
            for model, (text, model_used, elapsed) in zip(BATTLE_MODELS, (first, second)):
                answers.append(
                    {
                        "model": model_used or model,
                        "label": _model_label(model_used or model),
                        "text": text,
                        "elapsed": elapsed,
                        "ok": not text.startswith("⚠️"),
                    }
                )
            if not any(item["ok"] for item in answers):
                await _send_line(
                    response, {"t": "error", "error": _err_text("Обе модели сейчас недоступны.")}
                )
            else:
                completed = True
                await award_xp(telegram_id, XP_BATTLE)
                await _send_line(
                    response,
                    {
                        "t": "battle",
                        "answers": answers,
                        "remaining": remaining,
                        "game": await game_state(telegram_id),
                    },
                )
        except (ConnectionError, ConnectionResetError):
            logger.info("Клиент остановил битву (user %s)", telegram_id)
        finally:
            if not task.done():
                task.cancel()
            if not completed:
                _spawn_refund(telegram_id)
        with contextlib.suppress(Exception):
            await response.write_eof()
        return response
    finally:
        _ACTIVE_STREAMS.discard(telegram_id)


async def api_battle_vote(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    if payload.get("winner") not in (1, 2):
        return web.json_response({"ok": False, "error": "Некорректный выбор."}, status=400)
    await asyncio.to_thread(_game_add_sync, telegram_id, XP_VOTE, 0, 1)
    return web.json_response({"ok": True, "game": await game_state(telegram_id)})


async def api_job_cancel(request: web.Request) -> web.Response:
    """Кнопка «⏹ Стоп» для фоновых задач (видео, презентация, кружок)."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    kind = payload.get("kind")
    if kind not in ("video", "pptx", "note"):
        return web.json_response({"ok": False, "error": "Некорректный тип задачи."}, status=400)
    cancelled = cancel_job(telegram_id, kind)
    if cancelled and kind == "pptx":
        await refund_text_attempt(telegram_id)  # видео возвращает попытку само, в media_service
    return web.json_response({"ok": True, "cancelled": cancelled, "refunded": cancelled and kind != "note"})


async def api_game(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)
    return web.json_response({"ok": True, "game": await game_state(telegram_id)})


async def api_game_ack(request: web.Request) -> web.Response:
    """Клиент показал уведомление о бонусной попытке — сбрасываем флаг."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    await asyncio.to_thread(_ack_bonus_sync, telegram_id)
    return web.json_response({"ok": True})


async def api_memory_add(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    fact = payload.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        return web.json_response({"ok": False, "error": "Пустой факт."}, status=400)

    ok, reason = await db.add_memory_fact(telegram_id, fact.strip())
    if not ok:
        messages = {
            "limit": f"Достигнут лимит фактов ({config.MAX_MEMORY_FACTS}).",
            "too_long": "Факт слишком длинный (максимум 500 символов).",
            "empty": "Пустой факт.",
        }
        return web.json_response(
            {"ok": False, "error": messages.get(reason, "Не удалось сохранить факт.")},
            status=400,
        )
    return web.json_response({"ok": True})


async def api_memory_delete(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        fact_id = int(payload.get("fact_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный fact_id."}, status=400)

    deleted = await db.delete_memory_fact(telegram_id, fact_id)
    if not deleted:
        return web.json_response({"ok": False, "error": "Факт не найден."}, status=404)
    return web.json_response({"ok": True})


async def api_memory_clear(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    removed = await db.clear_all_memory(telegram_id)
    return web.json_response({"ok": True, "removed": removed})


async def api_image_txt2img(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return web.json_response({"ok": False, "error": "Пустой промт."}, status=400)

    try:
        disconnected, image_bytes = await _run_guarded(
            request, generate_image_txt2img(prompt.strip())
        )
    except ImageGenerationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if disconnected:
        return _gone_response()

    await award_xp(telegram_id, XP_IMAGE)
    return web.json_response(
        {
            "ok": True,
            "image": image_to_data_url(image_bytes),
            "game": await game_state(telegram_id),
        }
    )


async def api_image_img2img(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    prompt = payload.get("prompt")
    image_url = payload.get("image_url")

    if not isinstance(prompt, str) or not prompt.strip():
        return web.json_response({"ok": False, "error": "Пустой промт."}, status=400)
    if not isinstance(image_url, str) or not image_url.strip():
        return web.json_response({"ok": False, "error": "Не передано изображение."}, status=400)

    # Попытка списывается только после успешной генерации: остановка или сбой её не тратят.
    status = await db.get_img2img_status(telegram_id)
    if int(status["remaining"]) <= 0:
        return web.json_response(
            {
                "ok": False,
                "error": f"Лимит редактирования фото исчерпан (10 в сутки). Осталось: {status['remaining']}.",
            },
            status=429,
        )

    try:
        if image_url.startswith("data:"):
            _, encoded = image_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
        else:
            image_bytes = base64.b64decode(image_url)
        disconnected, result_bytes = await _run_guarded(
            request, edit_image_img2img(prompt.strip(), image_bytes)
        )
    except (ImageEditingError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if disconnected:
        return _gone_response()

    await db.consume_img2img_request(telegram_id)
    await award_xp(telegram_id, XP_IMAGE)
    return web.json_response(
        {
            "ok": True,
            "image": image_to_data_url(result_bytes),
            "game": await game_state(telegram_id),
        }
    )


async def api_image_meme(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    try:
        disconnected, meme_bytes = await _run_guarded(
            request, generate_meme(topic.strip(), current_lang.get())
        )
    except MemeGenerationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if disconnected:
        return _gone_response()

    await award_xp(telegram_id, XP_IMAGE)
    return web.json_response(
        {
            "ok": True,
            "image": image_to_data_url(meme_bytes),
            "game": await game_state(telegram_id),
        }
    )


async def api_word_report(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    try:
        disconnected, docx_bytes = await _run_guarded(
            request, generate_word_report(topic.strip(), current_lang.get())
        )
    except WordReportError as exc:
        await refund_text_attempt(telegram_id)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if disconnected:
        await refund_text_attempt(telegram_id)
        return _gone_response()

    filename = "klyx_report.docx"
    sent = await _send_document_to_user(
        bot, telegram_id, docx_bytes, filename, f"Word-отчёт: {html.escape(topic.strip())[:200]}"
    )
    await award_xp(telegram_id, XP_WORD)
    response: dict[str, Any] = {"ok": True, "sent": sent, "filename": filename}
    if not sent:
        response["file"] = base64.b64encode(docx_bytes).decode("ascii")
    return web.json_response(response)


async def api_presentation(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    try:
        slides = int(payload.get("slides", 7))
    except (TypeError, ValueError):
        slides = 0
    if not (config.PPTX_MIN_SLIDES <= slides <= config.PPTX_MAX_SLIDES):
        return web.json_response(
            {"ok": False, "error": "Некорректное число слайдов (нужно от 3 до 15)."}, status=400
        )
    if is_job_active(telegram_id, "pptx"):
        return web.json_response(
            {"ok": False, "error": "Предыдущая задача ещё выполняется. Дождитесь результата."},
            status=429,
        )

    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    # Генерация с иллюстрациями занимает минуты: запускаем в фоне, файл придёт в чат с ботом.
    if not start_pptx_job(bot, telegram_id, telegram_id, topic.strip(), slides, current_lang.get()):
        await refund_text_attempt(telegram_id)
        return web.json_response(
            {"ok": False, "error": "Предыдущая задача ещё выполняется. Дождитесь результата."},
            status=429,
        )
    await award_xp(telegram_id, XP_PPTX)
    return web.json_response({"ok": True, "queued": True, "slides": slides})


async def _send_document_to_user(
    bot: Bot, telegram_id: int, data: bytes, filename: str, caption: str
) -> bool:
    """Отправляет файл пользователю в личные сообщения от имени бота (Bot API sendDocument)."""
    try:
        await bot.send_document(
            chat_id=telegram_id,
            document=BufferedInputFile(data, filename=filename),
            caption=caption,
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить документ пользователю %s: %s", telegram_id, exc)
        return False


async def _read_audio_upload(bot: Bot, request: web.Request) -> tuple[int, bytes, str | None]:
    """Читает аудио из multipart/form-data (initData + file) или из JSON (initData + audio в Base64)."""
    max_size = config.MAX_AUDIO_FILE_SIZE_BYTES
    too_large = _UploadError(
        f"Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ.", status=413
    )

    if (request.content_type or "").startswith("multipart/"):
        telegram_id: int | None = None
        audio: bytes | None = None
        filename: str | None = None
        reader = await request.multipart()
        async for part in reader:
            if part.name == "initData":
                telegram_id = _validate_init_data(bot, (await part.text()).strip())
            elif part.name == "file":
                if telegram_id is None:
                    raise web.HTTPUnauthorized(text="Отсутствует initData.")
                filename = part.filename
                buffer = bytearray()
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > max_size:
                        raise too_large
                audio = bytes(buffer)
        if telegram_id is None:
            raise web.HTTPUnauthorized(text="Отсутствует initData.")
        if not audio:
            raise _UploadError("Не передан аудиофайл.")
        return telegram_id, audio, filename

    payload = await _read_json(request)
    telegram_id = _validate_init_data(bot, _extract_init_data(payload))
    raw = payload.get("audio")
    if not isinstance(raw, str) or not raw.strip():
        raise _UploadError("Не передан аудиофайл.")
    encoded = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        audio = base64.b64decode(encoded)
    except ValueError as exc:
        raise _UploadError("Некорректные данные аудио.") from exc
    if len(audio) > max_size:
        raise too_large
    name = payload.get("filename")
    return telegram_id, audio, name if isinstance(name, str) else None


async def api_audio_to_voice(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        telegram_id, audio_bytes, filename = await _read_audio_upload(bot, request)
    except _UploadError as exc:
        return web.json_response({"ok": False, "error": exc.message}, status=exc.status)

    try:
        ogg_bytes = await convert_audio_to_voice(audio_bytes, filename)
    except AudioConversionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    try:
        await bot.send_voice(
            chat_id=telegram_id,
            voice=BufferedInputFile(ogg_bytes, filename="voice.ogg"),
        )
    except TelegramForbiddenError:
        return web.json_response(
            {"ok": False, "error": "Бот не может написать вам. Откройте чат с ботом и нажмите /start."},
            status=403,
        )
    except TelegramAPIError as exc:
        logger.warning("sendVoice не удался для %s: %s", telegram_id, exc)
        return web.json_response(
            {
                "ok": False,
                "error": "Telegram не принял голосовое сообщение. Проверьте настройки приватности голосовых.",
            },
            status=502,
        )

    return web.json_response({"ok": True, "sent": True, "filename": filename or "audio"})


async def api_vision(request: web.Request) -> web.Response:
    """Анализ фото: ответ на вопрос по картинке или разбор + 3-5 вариантов действий."""
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    raw_image = payload.get("image")
    if not isinstance(raw_image, str) or not raw_image.strip():
        return web.json_response({"ok": False, "error": "Не передано фото."}, status=400)
    encoded = raw_image.split(",", 1)[1] if raw_image.startswith("data:") else raw_image
    try:
        image_bytes = base64.b64decode(encoded)
    except ValueError:
        return web.json_response({"ok": False, "error": "Некорректное изображение."}, status=400)
    if len(image_bytes) > config.MAX_IMAGE_FILE_SIZE_BYTES:
        return web.json_response(
            {
                "ok": False,
                "error": f"Файл слишком большой. Максимум {config.MAX_IMAGE_FILE_SIZE_BYTES // (1024 * 1024)} МБ.",
            },
            status=413,
        )

    question = payload.get("message")
    question = question.strip()[:1000] if isinstance(question, str) else ""

    allowed, remaining = await _consume_text_attempt(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    try:
        disconnected, result = await _run_guarded(
            request, analyze_image(image_bytes, question, current_lang.get())
        )
    except AIServiceError as exc:
        await refund_text_attempt(telegram_id)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    if disconnected:
        await refund_text_attempt(telegram_id)
        return _gone_response()

    answer, model_used = result
    answer = clean_response_text(answer)
    await db.add_history_message(telegram_id, "user", f"[Фото] {question}".strip())
    await db.add_history_message(telegram_id, "assistant", answer, model_used)
    await award_xp(telegram_id, XP_VISION)
    return web.json_response(
        {
            "ok": True,
            "answer": answer,
            "model": model_used,
            "remaining": remaining,
            "game": await game_state(telegram_id),
        }
    )


async def api_audio_transcribe(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        _telegram_id, audio_bytes, filename = await _read_audio_upload(bot, request)
    except _UploadError as exc:
        return web.json_response({"ok": False, "error": exc.message}, status=exc.status)
    try:
        text = await transcribe_voice(audio_bytes, filename or "audio.ogg")
    except TranscriptionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    return web.json_response({"ok": True, "text": text})


async def _read_video_upload(bot: Bot, request: web.Request) -> tuple[int, str, str]:
    """Принимает видео (multipart: initData + file) сразу в файл на диске, без загрузки в RAM."""
    if not (request.content_type or "").startswith("multipart/"):
        raise _UploadError("Не передано видео.")
    max_size = config.MAX_VIDEO_FILE_SIZE_BYTES
    tmp_dir = make_temp_dir("note")
    src_path = os.path.join(tmp_dir, "source")
    telegram_id: int | None = None
    received = False
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "initData":
                telegram_id = _validate_init_data(bot, (await part.text()).strip())
            elif part.name == "file":
                if telegram_id is None:
                    raise web.HTTPUnauthorized(text="Отсутствует initData.")
                size = 0
                with open(src_path, "wb") as handle:
                    while True:
                        chunk = await part.read_chunk(512 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_size:
                            raise _UploadError(
                                f"Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ.",
                                status=413,
                            )
                        handle.write(chunk)
                received = size > 0
        if telegram_id is None:
            raise web.HTTPUnauthorized(text="Отсутствует initData.")
        if not received:
            raise _UploadError("Не передано видео.")
        return telegram_id, tmp_dir, src_path
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


async def api_video_to_note(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        telegram_id, tmp_dir, src_path = await _read_video_upload(bot, request)
    except _UploadError as exc:
        return web.json_response({"ok": False, "error": exc.message}, status=exc.status)
    started = start_note_job(bot, telegram_id, telegram_id, tmp_dir, src_path, current_lang.get())
    if not started:
        return web.json_response(
            {"ok": False, "error": "Предыдущая задача ещё выполняется. Дождитесь результата."},
            status=429,
        )
    return web.json_response({"ok": True, "queued": True})


async def api_video_generate(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return web.json_response({"ok": False, "error": "Не передан промт."}, status=400)
    prompt = prompt.strip()[:500]

    if is_job_active(telegram_id, "video"):
        return web.json_response(
            {"ok": False, "error": "Предыдущая задача ещё выполняется. Дождитесь результата."},
            status=429,
        )
    allowed, remaining = await db.consume_video_request(telegram_id)
    if not allowed:
        return web.json_response(
            {
                "ok": False,
                "error": f"Лимит видео на сегодня исчерпан ({config.DAILY_VIDEO_LIMIT} в сутки).",
            },
            status=429,
        )

    # Видео делается 1–3 минуты: отвечаем сразу, готовый ролик придёт в чат с ботом.
    # Остановка — /api/job/cancel (kind="video"), попытка возвращается автоматически.
    if not start_video_job(bot, telegram_id, telegram_id, prompt, current_lang.get()):
        await db.refund_video_request(telegram_id)
        return web.json_response(
            {"ok": False, "error": "Предыдущая задача ещё выполняется. Дождитесь результата."},
            status=429,
        )
    await award_xp(telegram_id, XP_VIDEO)
    return web.json_response({"ok": True, "queued": True, "remaining": remaining})


async def api_lang(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    lang = payload.get("lang")
    if lang not in ("ru", "en"):
        return web.json_response({"ok": False, "error": "Некорректный язык."}, status=400)
    await db.set_user_lang(telegram_id, lang)
    current_lang.set(lang)
    return web.json_response({"ok": True, "lang": lang})


@web.middleware
async def i18n_errors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Переводит поле error в JSON-ответах для пользователей с английским интерфейсом."""
    response = await handler(request)
    try:
        if (
            current_lang.get() == "en"
            and isinstance(response, web.Response)
            and response.content_type == "application/json"
            and response.body
        ):
            data = json.loads(response.body)
            if isinstance(data, dict) and isinstance(data.get("error"), str):
                data["error"] = translate_text(data["error"])
                return web.json_response(data, status=response.status)
    except Exception:
        logger.exception("Не удалось локализовать ответ")
    return response


async def api_tasks_list(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    tasks = await db.get_tasks(telegram_id)
    summary = await db.get_tasks_summary(telegram_id)
    return web.json_response(
        {
            "ok": True,
            "tasks": [
                {"id": t["id"], "text": t["text"], "is_done": int(t["is_done"])}
                for t in tasks
            ],
            "summary": summary,
        }
    )


async def api_tasks_add(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"ok": False, "error": "Пустая задача."}, status=400)

    ok, reason, task_id = await db.add_task(telegram_id, text.strip())
    if not ok:
        messages = {
            "limit": f"Достигнут лимит задач ({config.MAX_TASKS_PER_USER}).",
            "too_long": "Задача длиннее 300 символов.",
            "empty": "Пустая задача.",
        }
        return web.json_response(
            {"ok": False, "error": messages.get(reason, "Не удалось добавить задачу.")},
            status=400,
        )
    return web.json_response({"ok": True, "task_id": task_id})


async def api_tasks_toggle(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        task_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный task_id."}, status=400)

    new_state = await db.toggle_task(telegram_id, task_id)
    if new_state is None:
        return web.json_response({"ok": False, "error": "Задача не найдена."}, status=404)
    return web.json_response({"ok": True, "is_done": int(new_state)})


async def api_tasks_delete(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        task_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный task_id."}, status=400)

    deleted = await db.delete_task(telegram_id, task_id)
    if not deleted:
        return web.json_response({"ok": False, "error": "Задача не найдена."}, status=404)
    return web.json_response({"ok": True})


async def api_tasks_clear_done(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    removed = await db.clear_completed_tasks(telegram_id)
    return web.json_response({"ok": True, "removed": removed})


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application(
        client_max_size=20 * 1024 * 1024, middlewares=[i18n_errors_middleware]
    )
    app["bot"] = bot

    app.router.add_get("/", handle_index)
    app.router.add_get("/webapp/{filename}", handle_static)

    app.router.add_post("/api/limits", api_limits)
    app.router.add_post("/api/models", api_models)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/chat/stream", api_chat_stream)
    app.router.add_post("/api/chat/rewrite", api_chat_rewrite)
    app.router.add_post("/api/chat/battle", api_chat_battle)
    app.router.add_post("/api/battle/vote", api_battle_vote)
    app.router.add_post("/api/job/cancel", api_job_cancel)
    app.router.add_post("/api/game", api_game)
    app.router.add_post("/api/game/ack", api_game_ack)
    app.router.add_post("/api/memory/add", api_memory_add)
    app.router.add_post("/api/memory/delete", api_memory_delete)
    app.router.add_post("/api/memory/clear", api_memory_clear)
    app.router.add_post("/api/image/txt2img", api_image_txt2img)
    app.router.add_post("/api/image/img2img", api_image_img2img)
    app.router.add_post("/api/image/meme", api_image_meme)
    app.router.add_post("/api/audio/to_voice", api_audio_to_voice)
    app.router.add_post("/api/audio/transcribe", api_audio_transcribe)
    app.router.add_post("/api/vision", api_vision)
    app.router.add_post("/api/video/generate", api_video_generate)
    app.router.add_post("/api/video/to_note", api_video_to_note)
    app.router.add_post("/api/lang", api_lang)
    app.router.add_post("/api/word_report", api_word_report)
    app.router.add_post("/api/presentation", api_presentation)
    app.router.add_post("/api/tasks/list", api_tasks_list)
    app.router.add_post("/api/tasks/add", api_tasks_add)
    app.router.add_post("/api/tasks/toggle", api_tasks_toggle)
    app.router.add_post("/api/tasks/delete", api_tasks_delete)
    app.router.add_post("/api/tasks/clear_done", api_tasks_clear_done)

    return app


async def run_web_server(bot: Bot) -> None:
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.WEBAPP_PORT)
    await site.start()
    logger.info("Веб-сервер Klyx AI запущен на порту %s", config.WEBAPP_PORT)
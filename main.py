# main.py
"""
Klyx AI — главный файл бота с запуском веб-сервера для Mini App.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import html
import io
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    WebAppInfo,
)
from aiogram.utils.chat_action import ChatActionSender
from pypdf import PdfReader

import config
import database as db
from ai_service import (
    AUDIO_EXTENSIONS,
    BATTLE_MODELS,
    AIServiceError,
    AllModelsFailedError,
    AudioConversionError,
    ImageEditingError,
    ImageGenerationError,
    MemeGenerationError,
    PptxGenerationError,
    TranscriptionError,
    WordReportError,
    clean_response_text,
    convert_audio_to_voice,
    analyze_image,
    build_rewrite_messages,
    build_url_context,
    edit_image_img2img,
    ensure_meme_font,
    estimate_processing_seconds,
    format_seconds,
    generate_image_txt2img,
    generate_meme,
    generate_pptx_presentation,
    generate_word_report,
    get_system_prompt,
    request_ai,
    request_two_models,
    stream_ai,
    transcribe_voice,
)
from keyboards import (
    BTN_IMAGE,
    BTN_MEME,
    BTN_PPTX,
    BTN_TASKS,
    BTN_WORD,
    ROLES,
    admin_keyboard,
    answer_actions_keyboard,
    audio_choice_keyboard,
    audio_menu_keyboard,
    back_keyboard,
    battle_keyboard,
    cancel_keyboard,
    confirm_keyboard,
    image_cancel_keyboard,
    image_menu_keyboard,
    inline_main_menu,
    lang_keyboard,
    main_menu_keyboard as _base_main_menu_keyboard,
    meme_cancel_keyboard,
    models_keyboard,
    note_choice_keyboard,
    pptx_cancel_keyboard,
    pptx_count_keyboard,
    profile_keyboard,
    roles_keyboard,
    style_keyboard,
    tasks_cancel_keyboard,
    tasks_keyboard,
    webapp_keyboard,
    word_cancel_keyboard,
)
from i18n import (
    IncomingButtonMiddleware,
    TranslateRequestMiddleware,
    UserContextMiddleware,
    current_lang,
    language_name,
)
from media_service import (
    cancel_job,
    cleanup_stale_temp_dirs,
    is_job_active,
    make_temp_dir,
    start_note_job,
    start_pptx_job,
    start_video_job,
)
from web_server import (
    XP_BATTLE,
    XP_CHAT,
    XP_IMAGE,
    XP_VISION,
    XP_VOTE,
    XP_WORD,
    _consume_text_attempt as consume_text_attempt,
    _game_add_sync,
    award_xp,
    game_checkin,
    game_state,
    game_totals,
    refund_text_attempt,
    run_web_server,
)

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------
class MemoryStates(StatesGroup):
    waiting_for_fact = State()


class ChatStates(StatesGroup):
    chatting = State()


class ImageStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_photo = State()


class TaskStates(StatesGroup):
    waiting_for_task = State()


class MemeStates(StatesGroup):
    waiting_for_topic = State()


class WordStates(StatesGroup):
    waiting_for_topic = State()


class PptxStates(StatesGroup):
    waiting_for_topic = State()
    choosing_count = State()


class AudioStates(StatesGroup):
    waiting_for_file = State()


class VideoStates(StatesGroup):
    waiting_for_prompt = State()


class NoteStates(StatesGroup):
    waiting_for_video = State()


HELP_TEXT = (
    "❓ Klyx AI — возможности\n\n"
    "💚 Бот полностью бесплатный. Без рекламы и подписок.\n\n"
    "💬 Чат — просто напишите сообщение (ответ печатается в реальном времени). Пришлите ссылку — прочитаю страницу.\n"
    "✂️ Кратко / 📖 Подробнее — кнопки под ответом. ⏹ Остановить — отмена генерации с возвратом попытки.\n"
    "🤖 Модель — выберите бесплатную модель.\n"
    "🎭 Роль — задайте характер ассистента.\n"
    "🎨 Генерация фото — по тексту или по фото с подписью.\n"
    "😂 Мемы — тема, картинка и подписи.\n"
    "✅ Задачи — умный To-Do трекер.\n"
    "📄 Word-отчет — структурированный .docx файл.\n"
    "📊 Презентация — .pptx файл по теме с иллюстрациями.\n"
    "⚔️ Битва ИИ — один запрос, две модели.\n"
    "🧐 Второе мнение — проверка ответа другой моделью.\n"
    "📋 В шпаргалку — сжатие диалога.\n"
    "🧠 Память — факты о вас.\n"
    "🎤 Голосовые — расшифровка через Whisper.\n"
    "🎙 Аудио и голос — распознавание речи и конвертация аудиофайла (mp3, wav, m4a, ogg, aac) в голосовое.\n"
    "📷 Анализ фото — пришлите фото с вопросом в подписи или без подписи: опишу снимок и предложу 3–5 вариантов, что с ним сделать.\n"
    "🚀 Klyx Mini App — кнопка в меню: робот Klyx, Битва ИИ, огонёк дней подряд и ранги.\n"
    "🎬 Видео по тексту — короткий ролик по описанию (1–2 в сутки).\n"
    "⭕ Видео в кружок — обычное видео превращается в видеосообщение.\n"
    "🌐 /lang — язык интерфейса (RU / EN).\n"
    "📄 Файлы — .txt и .pdf.\n"
    "📊 Лимиты — сколько запросов потрачено.\n\n"
    "Команда /menu переотправляет клавиатуру."
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_reset_time(reset_at: datetime) -> str:
    delta = reset_at - _now_utc()
    total_seconds = max(0, int(delta.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"через {hours} ч {minutes} мин"
    return f"через {minutes} мин"


def _user_style_hint(user: dict[str, Any]) -> str:
    parts: list[str] = []
    length = user.get("style") or "detailed"
    expertise = user.get("expertise") or "novice"
    tone = user.get("tone") or "friendly"
    if length == "short":
        parts.append("Отвечай кратко, по существу.")
    else:
        parts.append("Отвечай подробно, с пояснениями и примерами.")
    if expertise == "novice":
        parts.append("Объясняй простыми словами.")
    else:
        parts.append("Можно использовать профессиональные термины.")
    if tone == "friendly":
        parts.append("Тон дружелюбный и поддерживающий.")
    else:
        parts.append("Тон строгий и деловой.")
    return " ".join(parts)


def _role_hint(role_key: str | None) -> str | None:
    if not role_key or role_key == "default":
        return None
    prompts = {
        "programmer": "Ты опытный Senior Python-разработчик.",
        "teacher": "Ты терпеливый учитель.",
        "copywriter": "Ты профессиональный копирайтер.",
        "translator": "Ты профессиональный переводчик.",
        "socrates": "Ты Сократ, веди диалог через вопросы.",
        "critic": "Ты строгий, но конструктивный критик.",
    }
    return prompts.get(role_key)


def _build_system_messages(user: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": get_system_prompt(current_lang.get())},
        {
            "role": "system",
            "content": (
                "Ты — Klyx AI, бесплатный и дружелюбный ИИ-ассистент. "
                "Не упоминай платные подписки, рекламу или ограничения, "
                "кроме честного отображения суточного лимита."
            ),
        },
    ]
    style_hint = _user_style_hint(user)
    if style_hint:
        messages.append({"role": "system", "content": style_hint})
    role_prompt = _role_hint(user.get("current_role"))
    if role_prompt:
        messages.append({"role": "system", "content": role_prompt})
    if facts:
        facts_text = "\n".join(f"- {fact['fact']}" for fact in facts)
        messages.append(
            {
                "role": "system",
                "content": f"Что ты знаешь о пользователе (долгосрочная память):\n{facts_text}",
            }
        )
    return messages


async def _prepare_messages(
    telegram_id: int,
    user_prompt: str,
    include_history: bool = True,
) -> list[dict[str, str]]:
    user = await db.get_user(telegram_id)
    if user is None:
        user = await db.get_or_create_user(telegram_id, None, None)
    facts = await db.get_memory_facts(telegram_id)
    messages = _build_system_messages(user, facts)
    if include_history:
        history = await db.get_history(telegram_id)
        for item in history:
            if item["role"] in ("user", "assistant"):
                messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": user_prompt})
    return messages


async def _send_limits_status(message: Message, telegram_id: int) -> None:
    status = await db.get_user_daily_status(telegram_id)
    img_status = await db.get_img2img_status(telegram_id)
    video_status = await db.get_video_status(telegram_id)
    reset_text = _format_reset_time(status["reset_at"])
    text = (
        "📊 Лимиты и статус\n\n"
        "💚 Klyx AI — полностью бесплатен. Без рекламы и подписок.\n\n"
        f"📈 Текстовые запросы: {status['used']} / {status['limit']}\n"
        f"🟢 Осталось: {status['remaining']}\n"
        f"⏳ Сброс: {reset_text}\n\n"
        f"🎨 Редактирование фото: {img_status['used']} / {img_status['limit']}\n"
        f"🟢 Осталось: {img_status['remaining']}\n\n"
        f"🎬 Видео: {video_status['used']} / {video_status['limit']}\n"
        f"🟢 Осталось: {video_status['remaining']}\n\n"
        f"🌐 Глобальный лимит API: до {config.GLOBAL_REQUESTS_PER_MINUTE} запросов в минуту."
    )
    await message.answer(text)


async def _send_long_message(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    """Разбивает длинный текст на части, экранирует HTML, таблицы и код оформляет как <pre>."""
    text = clean_response_text(text) if text else text
    if not text:
        text = "(пустой ответ)"
    parts = _split_plain(text)
    last: Message | None = None
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        last = await message.answer(
            _to_telegram_html(part), reply_markup=reply_markup if is_last else None
        )
    if last is not None and reply_markup is not None:
        _remember_answer(last, text)


def _is_presentation_request(text: str) -> str | None:
    """
    Возвращает тему презентации, если текст является запросом на презентацию.
    Иначе None.
    """
    lowered = text.lower().strip()
    triggers = [
        "сделай презентацию про ",
        "создай презентацию про ",
        "презентация про ",
        "сделай презентацию о ",
        "создай презентацию о ",
        "презентация о ",
        "сделай презентацию ",
        "создай презентацию ",
    ]
    for trigger in triggers:
        if lowered.startswith(trigger):
            return text[len(trigger):].strip()
    if "презентацию" in lowered or "презентация" in lowered:
        return text
    return None


# ---------------------------------------------------------------------------
# Меню: «📷 Анализ фото» и кнопка запуска Mini App (WebAppInfo) в главном меню
# ---------------------------------------------------------------------------
def _t(ru: str, en: str) -> str:
    return en if current_lang.get() == "en" else ru


def main_menu_keyboard(*args: Any, **kwargs: Any) -> Any:
    """Главное меню из keyboards.py + отдельный ряд: «📷 Анализ фото» и «🚀 Klyx Mini App»."""
    markup = _base_main_menu_keyboard(*args, **kwargs)
    try:
        rows = [list(row) for row in markup.keyboard]
        labels = [button.text for row in rows for button in row]
        extra: list[KeyboardButton] = []
        if not any("Анализ фото" in label or "Photo analysis" in label for label in labels):
            extra.append(KeyboardButton(text=_t("📷 Анализ фото", "📷 Photo analysis")))
        has_webapp = any(getattr(button, "web_app", None) for row in rows for button in row)
        if config.WEBAPP_URL and not has_webapp:
            extra.append(
                KeyboardButton(text="🚀 Klyx Mini App", web_app=WebAppInfo(url=config.WEBAPP_URL))
            )
        if extra:
            rows.append(extra)
        return markup.model_copy(update={"keyboard": rows})
    except Exception:
        logger.exception("Не удалось дополнить главное меню")
        return markup


@router.message(F.text.contains("Анализ фото") | F.text.contains("Photo analysis"))
async def show_photo_analysis(message: Message, state: FSMContext) -> None:
    await state.clear()
    hint = _t(
        "📷 Анализ фото\n\n"
        "Пришлите фото или снимите его на камеру (в Mini App есть кнопка «📷 Фото»).\n"
        "• Фото с вопросом в подписи — отвечу на ваш вопрос.\n"
        "• Фото без подписи — разберу снимок и предложу 3–5 вариантов, что с ним сделать.",
        "📷 Photo analysis\n\n"
        "Send a photo or take one with the camera (the Mini App has a «📷 Photo» button).\n"
        "• A photo with a question in the caption — I will answer the question.\n"
        "• A photo without a caption — I will describe it and suggest 3–5 things you can do with it.",
    )
    await message.answer(hint, reply_markup=webapp_keyboard())


# ---------------------------------------------------------------------------
# Оформление ответов: Markdown-таблицы -> <pre>, экранирование HTML, разбиение на части
# ---------------------------------------------------------------------------
_FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?(?:```|\Z))")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CELL_MAX = 40


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 or (stripped.startswith("|") and stripped.endswith("|"))


def _table_to_pre(rows: list[str]) -> str:
    """Markdown-таблица -> ровный моноширинный блок <pre>...</pre>."""
    parsed: list[list[str]] = []
    for row in rows:
        if _TABLE_SEP_RE.match(row):
            continue
        body = row.strip()
        if body.startswith("|"):
            body = body[1:]
        if body.endswith("|"):
            body = body[:-1]
        cells = []
        for cell in body.split("|"):
            cell = cell.replace("**", "").replace("`", "").strip()
            cells.append(cell if len(cell) <= _CELL_MAX else cell[: _CELL_MAX - 1] + "…")
        parsed.append(cells)
    if not parsed:
        return ""
    columns = max(len(cells) for cells in parsed)
    for cells in parsed:
        cells.extend([""] * (columns - len(cells)))
    widths = [max(len(cells[i]) for cells in parsed) for i in range(columns)]
    lines = [" | ".join(cells[i].ljust(widths[i]) for i in range(columns)).rstrip() for cells in parsed]
    if len(lines) > 1:
        lines.insert(1, "-+-".join("-" * widths[i] for i in range(columns)))
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _to_telegram_html(text: str) -> str:
    """Экранирует HTML, таблицы и блоки кода оформляет как <pre>."""
    out: list[str] = []
    for index, part in enumerate(_FENCE_SPLIT_RE.split(text)):
        if index % 2 == 1:
            body = part[3:]
            if body.endswith("```"):
                body = body[:-3]
            first, _, rest = body.partition("\n")
            if rest and re.fullmatch(r"[\w+#.-]*", first.strip()):
                body = rest
            out.append("<pre>" + html.escape(body.strip("\n")) + "</pre>")
            continue
        lines = part.split("\n")
        buffer: list[str] = []
        i = 0
        while i < len(lines):
            if _is_table_row(lines[i]) and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
                if buffer:
                    out.append(html.escape("\n".join(buffer)) + "\n")
                    buffer = []
                j = i
                rows: list[str] = []
                while j < len(lines) and (_is_table_row(lines[j]) or _TABLE_SEP_RE.match(lines[j])):
                    rows.append(lines[j])
                    j += 1
                out.append(_table_to_pre(rows))
                i = j
            else:
                buffer.append(lines[i])
                i += 1
        if buffer:
            out.append(html.escape("\n".join(buffer)))
    return "".join(out)


def _split_plain(text: str, limit: int = 3300) -> list[str]:
    """Режет текст на части по абзацам (таблицы остаются целыми, пока помещаются)."""
    rest = text.strip() or "(пустой ответ)"
    chunks: list[str] = []
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


_LAST_ANSWERS: dict[tuple[int, int], str] = {}


def _remember_answer(message: Message, answer: str) -> None:
    _LAST_ANSWERS[(message.chat.id, message.message_id)] = answer
    while len(_LAST_ANSWERS) > 300:
        _LAST_ANSWERS.pop(next(iter(_LAST_ANSWERS)))


# ---------------------------------------------------------------------------
# Кнопки: «⏹ Остановить», «✂️ Кратко» / «📖 Подробнее»
# ---------------------------------------------------------------------------
def _stop_kb(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t("⏹ Остановить", "⏹ Stop"), callback_data=f"stop:{kind}")]
        ]
    )


def _answer_kb() -> InlineKeyboardMarkup:
    """Стандартные кнопки под ответом + ряд «Кратко» / «Подробнее»."""
    base = answer_actions_keyboard()
    rows = [list(row) for row in getattr(base, "inline_keyboard", [])]
    rows.append(
        [
            InlineKeyboardButton(text=_t("✂️ Кратко", "✂️ Shorter"), callback_data="rw:short"),
            InlineKeyboardButton(text=_t("📖 Подробнее", "📖 In detail"), callback_data="rw:long"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(target: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await target.edit_text(text, reply_markup=markup)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(min(float(exc.retry_after), 3.0))
    except TelegramAPIError:
        pass
    return False


# ---------------------------------------------------------------------------
# Остановка генерации: задача регистрируется, кнопка «⏹ Остановить» её отменяет
# ---------------------------------------------------------------------------
_RUNNING: dict[tuple[int, str], asyncio.Future[Any]] = {}
_STOP_FLAGS: set[tuple[int, str]] = set()


def _is_running(user_id: int, kind: str) -> bool:
    task = _RUNNING.get((user_id, kind))
    return task is not None and not task.done()


def _request_stop(user_id: int, kind: str) -> bool:
    task = _RUNNING.get((user_id, kind))
    if task is None or task.done():
        return False
    _STOP_FLAGS.add((user_id, kind))
    task.cancel()
    return True


async def _run_stoppable(user_id: int, kind: str, coro: Any) -> tuple[bool, Any]:
    """Выполняет coro; возвращает (остановлено_пользователем, результат)."""
    key = (user_id, kind)
    task = asyncio.ensure_future(coro)
    _RUNNING[key] = task
    try:
        return False, await task
    except asyncio.CancelledError:
        if key in _STOP_FLAGS:
            _STOP_FLAGS.discard(key)
            return True, None
        raise
    finally:
        if _RUNNING.get(key) is task:
            _RUNNING.pop(key, None)


def _stopped_text() -> str:
    return _t("⏹ Остановлено. Попытка возвращена.", "⏹ Stopped. The attempt has been refunded.")


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(callback: CallbackQuery) -> None:
    if callback.from_user is None or not callback.data:
        await callback.answer()
        return
    kind = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    if kind in ("video", "pptx", "note"):
        cancelled = cancel_job(user_id, kind)  # media_service сам вернёт попытку и напишет в чат
        if cancelled and kind == "pptx":
            await refund_text_attempt(user_id)
    else:
        cancelled = _request_stop(user_id, kind)
    await callback.answer(
        _t("⏹ Останавливаю…", "⏹ Stopping…") if cancelled else _t("Уже завершено.", "Already finished.")
    )
    if not cancelled and isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Потоковый ответ: сообщение плавно редактируется раз в ~1.3 сек
# ---------------------------------------------------------------------------
_STREAM_EDIT_INTERVAL = 1.3
_PREVIEW_LIMIT = 3300


async def _stream_into(
    status_msg: Message,
    messages: list[dict[str, Any]],
    preferred_model: str | None,
    kind: str = "text",
) -> tuple[str, str, float]:
    info: dict[str, Any] = {}
    parts: list[str] = []
    started = time.monotonic()
    last_edit = 0.0
    last_preview = ""
    generator = stream_ai(messages, preferred_model=preferred_model, info=info)
    try:
        async for delta in generator:
            parts.append(delta)
            now = time.monotonic()
            if now - last_edit >= _STREAM_EDIT_INTERVAL:
                preview = clean_response_text("".join(parts)).strip()
                if len(preview) > _PREVIEW_LIMIT:
                    preview = "…" + preview[-_PREVIEW_LIMIT:]
                if preview and preview != last_preview:
                    await _safe_edit(status_msg, _to_telegram_html(preview) + " ▌", _stop_kb(kind))
                    last_preview = preview
                last_edit = time.monotonic()
    finally:
        with contextlib.suppress(Exception):
            await generator.aclose()
    answer = clean_response_text("".join(parts)).strip()
    if not answer:
        raise AIServiceError("Модель вернула пустой ответ.")
    return answer, str(info.get("model") or ""), round(time.monotonic() - started, 2)


async def _deliver_answer(
    message: Message,
    status_msg: Message | None,
    answer: str,
    markup: InlineKeyboardMarkup | None,
    elapsed: float | None = None,
    model: str = "",
) -> None:
    chunks = _split_plain(answer)
    footer = ""
    if elapsed is not None:
        label = html.escape(model.split("/")[-1].split(":")[0]) if model else ""
        footer = f"\n\n<i>⏱ {elapsed:.1f} {_t('сек', 's')}{' · ' + label if label else ''}</i>"
    last_msg: Message | None = status_msg
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        body = _to_telegram_html(chunk) + (footer if is_last else "")
        keyboard = markup if is_last else None
        sent: Message | None = None
        if index == 0 and status_msg is not None:
            try:
                await status_msg.edit_text(body, reply_markup=keyboard)
                sent = status_msg
            except TelegramAPIError:
                sent = None
        if sent is None:
            sent = await message.answer(body, reply_markup=keyboard)
        last_msg = sent
    if last_msg is not None and markup is not None:
        _remember_answer(last_msg, answer)


async def _respond_streaming(message: Message, user: dict[str, Any], text: str, user_id: int) -> None:
    """Ответ на текст со стримингом, ссылками и кнопками «Кратко/Подробнее/⏹». Попытка уже списана."""
    if message.bot is None:
        return
    if _is_running(user_id, "text"):
        await refund_text_attempt(user_id)
        await message.answer(
            _t(
                "⏳ Предыдущий ответ ещё генерируется. Нажмите «⏹ Остановить» или дождитесь.",
                "⏳ The previous answer is still being generated. Press «⏹ Stop» or wait.",
            )
        )
        return

    messages = await _prepare_messages(user_id, text, include_history=True)
    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(
        _t(f"⏳ Примерно {format_seconds(estimated)}…", f"⏳ About {format_seconds(estimated)}…"),
        reply_markup=_stop_kb("text"),
    )
    lang = current_lang.get()
    bot = message.bot

    async def _job() -> tuple[str, str, float]:
        model_text, _has_url = await build_url_context(text, lang)
        if model_text != text:
            messages[-1] = {"role": "user", "content": model_text}
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            return await _stream_into(status_msg, messages, user.get("current_model"))

    try:
        stopped, result = await _run_stoppable(user_id, "text", _job())
    except AllModelsFailedError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        return
    except AIServiceError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Ошибка сервиса: {exc}"))
        return
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return

    answer, model_used, elapsed = result
    await db.add_history_message(user_id, "user", text)
    await db.add_history_message(user_id, "assistant", answer, model_used)
    await award_xp(user_id, XP_CHAT)
    await _deliver_answer(message, status_msg, answer, _answer_kb(), elapsed, model_used)


_REWRITES: dict[int, tuple[str, int]] = {}
_REWRITE_DAILY_LIMIT = 40


@router.callback_query(F.data.in_({"rw:short", "rw:long"}))
async def cb_rewrite(callback: CallbackQuery) -> None:
    """«✂️ Кратко» / «📖 Подробнее»: переписывает ответ потоково."""
    if callback.from_user is None or callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user_id = callback.from_user.id
    mode = callback.data.split(":", 1)[1]
    source = _LAST_ANSWERS.get((callback.message.chat.id, callback.message.message_id)) or (
        callback.message.text or ""
    )
    if len(source.strip()) < 2:
        await callback.answer(_t("Нет текста для переписывания.", "Nothing to rewrite."), show_alert=True)
        return
    today = _now_utc().date().isoformat()
    day, count = _REWRITES.get(user_id, (today, 0))
    if day != today:
        count = 0
    if count >= _REWRITE_DAILY_LIMIT:
        await callback.answer(_t("Слишком много переписываний на сегодня.", "Too many rewrites today."), show_alert=True)
        return
    if _is_running(user_id, "text"):
        await callback.answer(_t("Предыдущий ответ ещё генерируется.", "The previous answer is still running."), show_alert=True)
        return
    _REWRITES[user_id] = (today, count + 1)
    await callback.answer(_t("Переписываю…", "Rewriting…"))

    status_msg = await callback.message.answer(
        _t("✂️ Сжимаю…", "✂️ Shortening…") if mode == "short" else _t("📖 Разворачиваю…", "📖 Expanding…"),
        reply_markup=_stop_kb("text"),
    )
    messages = build_rewrite_messages(source[:6000], mode, current_lang.get())
    try:
        stopped, result = await _run_stoppable(user_id, "text", _stream_into(status_msg, messages, None))
    except AIServiceError as exc:
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        return
    if stopped:
        await _safe_edit(status_msg, _t("⏹ Остановлено.", "⏹ Stopped."))
        return
    answer, model_used, elapsed = result
    await _deliver_answer(callback.message, status_msg, answer, _answer_kb(), elapsed, model_used)


@router.callback_query(F.data.startswith("bvote:"))
async def cb_battle_vote(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    await asyncio.to_thread(_game_add_sync, callback.from_user.id, XP_VOTE, 0, 1)
    await callback.answer(_t("🏆 Голос засчитан!", "🏆 Vote counted!"))
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


def _battle_vote_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t("🏆 Победил 1-й", "🏆 1st wins"), callback_data="bvote:1"),
                InlineKeyboardButton(text=_t("🏆 Победил 2-й", "🏆 2nd wins"), callback_data="bvote:2"),
            ]
        ]
    )


def _model_label(model: str) -> str:
    lowered = model.lower()
    if "gemma" in lowered:
        return "Gemma 3"
    if "qwen" in lowered:
        return "Qwen 3"
    return model.split("/")[-1].split(":")[0]


async def _game_line(user_id: int) -> str:
    try:
        state = await game_state(user_id)
    except Exception:
        return ""
    return (
        f"🔥 {_t('Огонёк', 'Streak')}: {state['streak_days']} {_t('дн.', 'd')} · "
        f"{state['rank_icon']} {state['rank']} · XP: {state['xp']}\n"
        f"🎁 {_t('Бонусных попыток', 'Bonus attempts')}: {state['bonus_attempts']}"
    )


# ---------------------------------------------------------------------------
# Админ-панель: сверху статистика за ВСЁ время, ниже — за последние 24 часа
# ---------------------------------------------------------------------------
def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# КОМАНДЫ
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return
    await db.get_or_create_user(user.id, user.username, user.first_name)
    try:
        await game_checkin(user.id)
    except Exception:
        logger.exception("Не удалось обновить streak пользователя %s", user.id)

    text = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        "Я — Klyx AI, твой бесплатный нейросетевой помощник.\n\n"
        "💚 Важно: я полностью бесплатен. Без рекламы и подписок.\n\n"
        "Выбери действие в меню ниже или просто напиши сообщение.\n\n"
        "❓ Подробности — /help.\n"
        "🔄 Если кнопки пропали — /menu."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())
    keyboard = webapp_keyboard()
    if keyboard is not None:
        await message.answer("🚀 Открой Mini App для премиального интерфейса:", reply_markup=keyboard)


@router.message(Command("help"))
@router.message(F.text.contains("Возможности"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🔄 Клавиатура обновлена. Если кнопки не появились — закройте и снова откройте чат.",
        reply_markup=main_menu_keyboard(),
    )
    await message.answer("Дублирующее меню:", reply_markup=inline_main_menu())


# ---------------------------------------------------------------------------
# REPLY-КНОПКИ МЕНЮ
# ---------------------------------------------------------------------------
@router.message(F.text.contains("Новый диалог"))
async def new_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    await db.clear_history(message.from_user.id)
    await message.answer("🔄 История диалога очищена. Память сохранена.", reply_markup=main_menu_keyboard())


@router.message(F.text.contains("Модель"))
async def show_models(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    current = user.get("current_model") if user else None
    await message.answer("Выберите бесплатную модель:", reply_markup=models_keyboard(current))


@router.message(F.text.contains("Роль"))
async def show_roles(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    current = user.get("current_role") if user else None
    await message.answer("Выберите роль ассистента:", reply_markup=roles_keyboard(current))


@router.message(F.text.contains("Битва"))
async def show_battle(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚔️ Битва нейросетей\n\nОтправьте запрос — спрошу две модели параллельно.",
        reply_markup=battle_keyboard(),
    )


@router.message(F.text.contains("Генерация фото"))
async def show_image_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎨 Генерация фото\n\n"
        "• По тексту — сгенерирую изображение по описанию.\n"
        "• По фото — пришлите фото с подписью, что изменить.",
        reply_markup=image_menu_keyboard(),
    )


@router.message(F.text.contains("Мемы"))
async def show_meme_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "😂 Генератор мемов\n\nНапишите тему, и я придумаю подписи и картинку.",
        reply_markup=meme_cancel_keyboard(),
    )
    await state.set_state(MemeStates.waiting_for_topic)


@router.message(F.text.contains("Список задач"))
async def show_tasks(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    tasks = await db.get_tasks(message.from_user.id)
    summary = await db.get_tasks_summary(message.from_user.id)
    text = (
        "✅ Список задач\n\n"
        f"Всего: {summary['total']} · Выполнено: {summary['done']} · Активно: {summary['active']}"
    )
    await message.answer(text, reply_markup=tasks_keyboard(tasks))


@router.message(F.text.contains("Word-отчет"))
async def show_word_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📄 Создание Word-отчета\n\nНапишите тему отчёта.",
        reply_markup=word_cancel_keyboard(),
    )
    await state.set_state(WordStates.waiting_for_topic)


@router.message(F.text.contains("Презентация"))
async def show_pptx_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📊 Создание презентации .pptx\n\nНапишите тему презентации.",
        reply_markup=pptx_cancel_keyboard(),
    )
    await state.set_state(PptxStates.waiting_for_topic)


@router.message(F.text.contains("Лимиты"))
async def show_status(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    await _send_limits_status(message, message.from_user.id)


@router.message(F.text.contains("Профиль"))
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    profile = await db.get_full_profile(message.from_user.id)
    if profile is None:
        await message.answer("Профиль не найден. Попробуйте /start.")
        return
    facts = profile.get("facts", [])
    facts_text = "\n".join(f"• {f['fact']}" for f in facts) if facts else "— пока пусто —"
    game_line = await _game_line(message.from_user.id)
    text = (
        "👤 Профиль и память\n\n"
        f"🆔 ID: {profile['telegram_id']}\n"
        f"🎭 Роль: {ROLES.get(profile.get('current_role') or 'default', 'Без роли')}\n"
        f"✍️ Стиль: {profile.get('style')}, {profile.get('expertise')}, {profile.get('tone')}\n"
        f"🤖 Модель: {profile.get('current_model') or 'по умолчанию'}\n"
        f"🌐 Язык: {language_name(profile.get('lang') or 'ru')}\n\n"
        f"🧠 Память:\n{facts_text}\n\n{game_line}"
    )
    await message.answer(text, reply_markup=profile_keyboard(facts))


# ---------------------------------------------------------------------------
# ЯЗЫК, АДМИН-ПАНЕЛЬ, АУДИО/ВИДЕО-МЕНЮ (кнопки и команды)
# ---------------------------------------------------------------------------
@router.message(Command("lang"))
@router.message(F.text.contains("Язык / Language"))
async def cmd_lang(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🌐 Выберите язык / Choose language:", reply_markup=lang_keyboard())


@router.callback_query(F.data == "lang:menu")
async def cb_lang_menu(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🌐 Выберите язык / Choose language:", reply_markup=lang_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("lang:set:"))
async def cb_lang_set(callback: CallbackQuery) -> None:
    if callback.from_user is None or not callback.data:
        await callback.answer()
        return
    lang = callback.data.split(":")[2]
    if lang not in ("ru", "en"):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    await db.set_user_lang(callback.from_user.id, lang)
    current_lang.set(lang)
    if isinstance(callback.message, Message):
        await callback.message.edit_text("✅ Язык переключён на русский.")
        await callback.message.answer("Меню обновлено.", reply_markup=main_menu_keyboard())
    await callback.answer()


def _is_admin(user_id: int | None) -> bool:
    return bool(config.ADMIN_ID) and user_id == config.ADMIN_ID


def _format_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value[:16]


async def _build_admin_text() -> str:
    stats = await db.get_users_stats()
    users = await db.get_all_users()
    recent = await db.get_recent_users(20)
    total = int(stats["total"])
    ru_count = sum(1 for item in users if (item.get("lang") or "ru") == "ru")
    en_count = len(users) - ru_count
    created = [dt for dt in (_parse_dt(item.get("created_at")) for item in users) if dt is not None]
    seen = [dt for dt in (_parse_dt(item.get("last_seen_at")) for item in users) if dt is not None]
    now = _now_utc()
    lines = [
        "🛠 Админ-панель",
        "",
        "📊 За всё время работы бота",
        f"👥 Всего пользователей: {total}",
        f"🌐 Язык: RU {ru_count} · EN {en_count}",
    ]
    if created:
        first = min(created)
        days = max(1, (now - first).days + 1)
        lines.append(f"📅 Бот работает с: {first.strftime('%d.%m.%Y')} ({days} дн.)")
        lines.append(f"📈 Новых в день в среднем: {total / days:.1f}")
    if seen:
        lines.append(f"🕒 Последняя активность: {max(seen).strftime('%d.%m.%Y %H:%M')}")
    try:
        game = await game_totals()
        lines.append(
            f"🎮 Игроков Klyx: {game['players']} · суммарный XP: {game['total_xp']} · "
            f"рекорд огонька: {game['best_streak']} дн. · побед в Битве: {game['battles_won']} · "
            f"с огоньком 3+ дн.: {game['streak3']}"
        )
    except Exception:
        logger.exception("Не удалось получить игровую статистику")
    lines += [
        "",
        "⏱ За последние 24 часа",
        f"🕒 Были активны: {stats['active_24h']}",
        f"🆕 Новых пользователей: {stats['new_24h']}",
        "",
        "Последние 20 пользователей (Имя | @username | ID | Дата):",
    ]
    if not recent:
        lines.append("Пользователей пока нет.")
    for item in recent:
        name = html.escape(item.get("first_name") or "—")
        username = f"@{html.escape(item['username'])}" if item.get("username") else "—"
        lines.append(
            f"{name} | {username} | {item['telegram_id']} | {_format_date(item.get('created_at'))}"
        )
    return "\n".join(lines)


@router.message(Command("admin"))
@router.message(F.text.contains("Админ-панель"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return  # команда скрытая: остальным пользователям бот не отвечает
    await state.clear()
    await message.answer(await _build_admin_text(), reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:refresh")
async def cb_admin_refresh(callback: CallbackQuery) -> None:
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                await _build_admin_text(), reply_markup=admin_keyboard()
            )
        except TelegramAPIError:
            pass  # текст не изменился
    await callback.answer()


@router.callback_query(F.data == "admin:export")
async def cb_admin_export(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.bot is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    users = await db.get_all_users()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["telegram_id", "username", "first_name", "registered_at", "last_seen_at", "lang"])
    for item in users:
        writer.writerow(
            [
                item["telegram_id"],
                item.get("username") or "",
                item.get("first_name") or "",
                item.get("created_at") or "",
                item.get("last_seen_at") or "",
                item.get("lang") or "ru",
            ]
        )
    # Файл формируется в памяти и на диск не записывается — удалять нечего.
    data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    stamp = _now_utc().strftime("%Y%m%d_%H%M")
    await callback.bot.send_document(
        callback.from_user.id,
        BufferedInputFile(data, filename=f"klyx_users_{stamp}.csv"),
        caption="Полная база пользователей Klyx AI.",
    )
    await callback.answer()


@router.message(F.text.contains("Аудио и голос"))
async def show_audio_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🎙 Аудио и голос\n\nВыберите действие:", reply_markup=audio_menu_keyboard())


@router.message(F.text.contains("Генерация видео"))
async def show_video_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(VideoStates.waiting_for_prompt)
    await message.answer(
        "🎬 Опишите видео, например: «собака на луне». /cancel — отмена.",
        reply_markup=cancel_keyboard("video:cancel"),
    )


@router.message(F.text.contains("Видео в кружок"))
async def show_note_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NoteStates.waiting_for_video)
    await message.answer(
        "⭕ Пришлите видео (до 20 МБ) — сделаю из него кружок до 59 секунд. /cancel — отмена.",
        reply_markup=cancel_keyboard("note:cancel"),
    )


# ---------------------------------------------------------------------------
# INLINE: навигация
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Главное меню:", reply_markup=back_keyboard("menu:main"))
    await callback.answer()


@router.callback_query(F.data == "menu:models")
async def cb_menu_models(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await db.get_user(callback.from_user.id)
    current = user.get("current_model") if user else None
    await callback.message.edit_text("Выберите модель:", reply_markup=models_keyboard(current))
    await callback.answer()


@router.callback_query(F.data == "menu:roles")
async def cb_menu_roles(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user = await db.get_user(callback.from_user.id)
    current = user.get("current_role") if user else None
    await callback.message.edit_text("Выберите роль:", reply_markup=roles_keyboard(current))
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_menu_profile(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    profile = await db.get_full_profile(callback.from_user.id)
    if profile is None:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    facts = profile.get("facts", [])
    facts_text = "\n".join(f"• {f['fact']}" for f in facts) if facts else "— пока пусто —"
    game_line = await _game_line(callback.from_user.id)
    text = (
        "👤 Профиль\n\n"
        f"🎭 Роль: {ROLES.get(profile.get('current_role') or 'default', 'Без роли')}\n"
        f"✍️ Стиль: {profile.get('style')}, {profile.get('expertise')}, {profile.get('tone')}\n"
        f"🤖 Модель: {profile.get('current_model') or 'по умолчанию'}\n"
        f"🌐 Язык: {language_name(profile.get('lang') or 'ru')}\n\n"
        f"🧠 Память:\n{facts_text}\n\n{game_line}"
    )
    await callback.message.edit_text(text, reply_markup=profile_keyboard(facts))
    await callback.answer()


@router.callback_query(F.data == "menu:limits")
async def cb_menu_limits(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    status = await db.get_user_daily_status(callback.from_user.id)
    img_status = await db.get_img2img_status(callback.from_user.id)
    video_status = await db.get_video_status(callback.from_user.id)
    reset_text = _format_reset_time(status["reset_at"])
    text = (
        "📊 Лимиты\n\n"
        f"📈 Текст: {status['used']} / {status['limit']} (осталось {status['remaining']})\n"
        f"⏳ Сброс: {reset_text}\n\n"
        f"🎨 Фото: {img_status['used']} / {img_status['limit']} (осталось {img_status['remaining']})\n"
        f"🎬 Видео: {video_status['used']} / {video_status['limit']} (осталось {video_status['remaining']})"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard("menu:main"))
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def cb_choose_model(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    model = callback.data.split(":", 1)[1]
    if model not in config.FREE_MODELS:
        await callback.answer("Модель недоступна.", show_alert=True)
        return
    await db.update_user_field(callback.from_user.id, "current_model", model)
    await callback.answer("Модель обновлена ✅")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"✅ Модель: {model}", reply_markup=models_keyboard(model))


@router.callback_query(F.data.startswith("role:"))
async def cb_choose_role(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    role = callback.data.split(":", 1)[1]
    if role not in ROLES:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return
    await db.update_user_field(callback.from_user.id, "current_role", role)
    await callback.answer("Роль обновлена ✅")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"🎭 Роль: {ROLES[role]}", reply_markup=roles_keyboard(role))


@router.callback_query(F.data.startswith("style:"))
async def cb_choose_style(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    _, dimension, value = parts
    field_map = {"length": "style", "expertise": "expertise", "tone": "tone"}
    field = field_map.get(dimension)
    if field is None:
        await callback.answer("Неизвестный стиль.", show_alert=True)
        return
    await db.update_user_field(callback.from_user.id, field, value)
    user = await db.get_user(callback.from_user.id)
    await callback.answer("Стиль обновлён ✅")
    if isinstance(callback.message, Message) and user is not None:
        await callback.message.edit_text(
            "🎛 Стиль:",
            reply_markup=style_keyboard(
                current_length=user.get("style"),
                current_expertise=user.get("expertise"),
                current_tone=user.get("tone"),
            ),
        )


# ---------------------------------------------------------------------------
# INLINE: память
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "memory:add")
async def cb_memory_add(callback: CallbackQuery, state: FSMContext) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer("🧠 Напишите факт для запоминания. /cancel — отмена.")
    await state.set_state(MemoryStates.waiting_for_fact)
    await callback.answer()


@router.message(StateFilter(MemoryStates.waiting_for_fact), F.text == "/cancel")
async def cancel_memory_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(MemoryStates.waiting_for_fact))
async def save_memory_fact(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    ok, reason = await db.add_memory_fact(message.from_user.id, message.text)
    if ok:
        await message.answer("✅ Факт сохранён.")
    elif reason == "limit":
        await message.answer(f"⚠️ Лимит фактов ({config.MAX_MEMORY_FACTS}).")
    elif reason == "too_long":
        await message.answer("⚠️ Факт длиннее 500 символов.")
    else:
        await message.answer("⚠️ Не удалось сохранить.")
    await state.clear()


@router.callback_query(F.data.startswith("memory:del:"))
async def cb_memory_delete(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    try:
        fact_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    deleted = await db.delete_memory_fact(callback.from_user.id, fact_id)
    await callback.answer("Удалено 🗑" if deleted else "Не найдено.", show_alert=not deleted)
    profile = await db.get_full_profile(callback.from_user.id)
    if profile and isinstance(callback.message, Message):
        facts = profile.get("facts", [])
        facts_text = "\n".join(f"• {f['fact']}" for f in facts) if facts else "— пусто —"
        await callback.message.edit_text(
            f"👤 Профиль\n\n🧠 Факты:\n{facts_text}",
            reply_markup=profile_keyboard(facts),
        )


@router.callback_query(F.data == "confirm:clear_memory")
async def cb_confirm_clear_memory(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🗑 Удалить все факты из памяти?",
            reply_markup=confirm_keyboard("clear_memory"),
        )
    await callback.answer()


@router.callback_query(F.data == "confirm:clear_memory:yes")
async def cb_do_clear_memory(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    removed = await db.clear_all_memory(callback.from_user.id)
    await callback.answer(f"Удалено: {removed}")
    if isinstance(callback.message, Message):
        await callback.message.edit_text("🧠 Память очищена.", reply_markup=back_keyboard("menu:main"))


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Отменено.", reply_markup=back_keyboard("menu:main"))
    await callback.answer()


# ---------------------------------------------------------------------------
# INLINE: битва, критик, шпаргалка
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "battle:start")
async def cb_battle_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChatStates.chatting)
    await state.update_data(mode="battle")
    if isinstance(callback.message, Message):
        await callback.message.answer("⚔️ Режим Битва активирован. Отправьте запрос.")
    await callback.answer()


@router.callback_query(F.data == "critic:run")
async def cb_critic_run(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    history = await db.get_history(callback.from_user.id, limit=2)
    assistant_messages = [m for m in history if m["role"] == "assistant"]
    if not assistant_messages:
        await callback.answer("Нет ответа для проверки.", show_alert=True)
        return
    last_answer = assistant_messages[-1]["content"]
    await callback.answer("Запускаю второе мнение…")
    messages = [
        {"role": "system", "content": get_system_prompt(current_lang.get())},
        {"role": "system", "content": "Ты строгий, но конструктивный AI-Критик."},
        {
            "role": "user",
            "content": (
                "Проанализируй ответ модели и оцени его. Найди ошибки, спорные места.\n\n"
                f"Ответ:\n---\n{last_answer}\n---"
            ),
        },
    ]
    try:
        async with ChatActionSender.typing(bot=callback.bot, chat_id=callback.message.chat.id):
            text, _model_used, _elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось: {exc}")
        return
    await _send_long_message(callback.message, f"🧐 Второе мнение\n\n{text}")


@router.callback_query(F.data == "digest:run")
async def cb_digest_run(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    history = await db.get_history(callback.from_user.id, limit=20)
    if len(history) < 2:
        await callback.answer("Слишком короткий диалог.", show_alert=True)
        return
    dialog_text = "\n".join(
        f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
        for m in history
        if m["role"] in ("user", "assistant")
    )
    await callback.answer("Сжимаю…")
    messages = [
        {"role": "system", "content": get_system_prompt(current_lang.get())},
        {"role": "system", "content": "Ты — редактор шпаргалок."},
        {
            "role": "user",
            "content": (
                "Сожми диалог в шпаргалку: резюме, идеи, решения, TODO.\n\n"
                f"Диалог:\n---\n{dialog_text}\n---"
            ),
        },
    ]
    try:
        async with ChatActionSender.typing(bot=callback.bot, chat_id=callback.message.chat.id):
            text, _model_used, _elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось: {exc}")
        return
    await _send_long_message(callback.message, f"📋 Шпаргалка\n\n{text}")


# ---------------------------------------------------------------------------
# ИЗОБРАЖЕНИЯ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "img:txt2img")
async def cb_img_txt2img(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImageStates.waiting_for_prompt)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🖼 Опишите изображение. /cancel — отмена.",
            reply_markup=image_cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "img:img2img")
async def cb_img_img2img(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImageStates.waiting_for_photo)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "✏️ Пришлите фото с подписью, что изменить.\n"
            "Например: фото + «добавь ананас».\n"
            "/cancel — отмена.",
            reply_markup=image_cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "img:cancel")
async def cb_img_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(StateFilter(ImageStates.waiting_for_prompt), F.text == "/cancel")
async def cancel_img_prompt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Генерация отменена.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(ImageStates.waiting_for_photo), F.text == "/cancel")
async def cancel_img_photo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Редактирование отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(ImageStates.waiting_for_prompt), F.text)
async def handle_img_prompt(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    prompt = message.text.strip()
    if not prompt:
        return
    user_id = message.from_user.id
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer(
        "🎨 Генерирую… Улучшаю промт и рисую картинку (бесплатно, до минуты).",
        reply_markup=_stop_kb("image"),
    )
    bot = message.bot

    async def _job() -> bytes:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            return await generate_image_txt2img(prompt)

    try:
        stopped, image_bytes = await _run_stoppable(user_id, "image", _job())
    except ImageGenerationError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        await state.clear()
        return
    await state.clear()
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return
    await _safe_edit(status_msg, "✅ Готово.")
    await message.answer_photo(BufferedInputFile(image_bytes, filename="image.jpg"))
    await award_xp(user_id, XP_IMAGE)


@router.message(StateFilter(ImageStates.waiting_for_photo), F.photo)
async def handle_img_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo or message.bot is None:
        return
    prompt = (message.caption or "").strip()
    if not prompt:
        await message.answer("⚠️ Пришлите фото с подписью, что изменить.")
        return
    user_id = message.from_user.id
    # Попытка списывается только после успешной генерации: остановка или сбой её не тратят.
    status = await db.get_img2img_status(user_id)
    if int(status["remaining"]) <= 0:
        await message.answer(f"⛔ Лимит редактирования исчерпан (10/сутки). Осталось: {status['remaining']}.")
        await state.clear()
        return

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    image_bytes = buffer.getvalue()

    status_msg = await message.answer(
        "✏️ Создаю новую сцену по вашему описанию (бесплатно, до минуты)…",
        reply_markup=_stop_kb("image"),
    )
    bot = message.bot

    async def _job() -> bytes:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            return await edit_image_img2img(prompt, image_bytes)

    try:
        stopped, result_bytes = await _run_stoppable(user_id, "image", _job())
    except ImageEditingError as exc:
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        await state.clear()
        return
    await state.clear()
    if stopped:
        await _safe_edit(status_msg, _stopped_text())
        return
    await db.consume_img2img_request(user_id)
    await _safe_edit(status_msg, "✅ Готово.")
    await message.answer_photo(BufferedInputFile(result_bytes, filename="image.jpg"))
    await award_xp(user_id, XP_IMAGE)


# ---------------------------------------------------------------------------
# МЕМЫ
# ---------------------------------------------------------------------------
@router.message(StateFilter(MemeStates.waiting_for_topic), F.text == "/cancel")
async def cancel_meme(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(MemeStates.waiting_for_topic), F.text)
async def handle_meme_topic(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    topic = message.text.strip()
    if not topic:
        return
    user_id = message.from_user.id
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer(
        "😂 Придумываю мем… Это может занять до 30 секунд.", reply_markup=_stop_kb("meme")
    )
    bot = message.bot
    lang = current_lang.get()

    async def _job() -> bytes:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            return await generate_meme(topic, lang)

    try:
        stopped, meme_bytes = await _run_stoppable(user_id, "meme", _job())
    except MemeGenerationError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        await state.clear()
        return
    await state.clear()
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return
    await _safe_edit(status_msg, "✅ Готово.")
    await message.answer_photo(
        BufferedInputFile(meme_bytes, filename="meme.jpg"), caption=f"😂 {html.escape(topic)}"
    )
    await award_xp(user_id, XP_IMAGE)


@router.callback_query(F.data == "meme:start")
async def cb_meme_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MemeStates.waiting_for_topic)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "😂 Напишите тему мема. /cancel — отмена.",
            reply_markup=meme_cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "meme:cancel")
async def cb_meme_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# ЗАДАЧИ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    tasks = await db.get_tasks(callback.from_user.id)
    summary = await db.get_tasks_summary(callback.from_user.id)
    text = (
        "✅ Список задач\n\n"
        f"Всего: {summary['total']} · Выполнено: {summary['done']} · Активно: {summary['active']}"
    )
    await callback.message.edit_text(text, reply_markup=tasks_keyboard(tasks))
    await callback.answer()


@router.callback_query(F.data == "tasks:add")
async def cb_tasks_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TaskStates.waiting_for_task)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "➕ Напишите задачу. /cancel — отмена.",
            reply_markup=tasks_cancel_keyboard(),
        )
    await callback.answer()


@router.message(StateFilter(TaskStates.waiting_for_task), F.text == "/cancel")
async def cancel_task_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(TaskStates.waiting_for_task), F.text)
async def save_task(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    ok, reason, _task_id = await db.add_task(message.from_user.id, message.text)
    if ok:
        await message.answer("✅ Задача добавлена.")
    elif reason == "limit":
        await message.answer(f"⚠️ Лимит задач ({config.MAX_TASKS_PER_USER}).")
    elif reason == "too_long":
        await message.answer("⚠️ Задача длиннее 300 символов.")
    else:
        await message.answer("⚠️ Не удалось добавить.")
    await state.clear()
    tasks = await db.get_tasks(message.from_user.id)
    summary = await db.get_tasks_summary(message.from_user.id)
    await message.answer(
        f"✅ Задачи: всего {summary['total']}, выполнено {summary['done']}.",
        reply_markup=tasks_keyboard(tasks),
    )


@router.callback_query(F.data.startswith("tasks:toggle:"))
async def cb_tasks_toggle(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    try:
        task_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    new_state = await db.toggle_task(callback.from_user.id, task_id)
    if new_state is None:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    await callback.answer("Выполнено ✅" if new_state else "Возвращено в работу ⬜️")
    tasks = await db.get_tasks(callback.from_user.id)
    summary = await db.get_tasks_summary(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Задачи\n\nВсего: {summary['total']} · Выполнено: {summary['done']} · Активно: {summary['active']}",
        reply_markup=tasks_keyboard(tasks),
    )


@router.callback_query(F.data.startswith("tasks:del:"))
async def cb_tasks_delete(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    try:
        task_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    deleted = await db.delete_task(callback.from_user.id, task_id)
    await callback.answer("Удалено 🗑" if deleted else "Не найдено.", show_alert=not deleted)
    tasks = await db.get_tasks(callback.from_user.id)
    summary = await db.get_tasks_summary(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Задачи\n\nВсего: {summary['total']} · Выполнено: {summary['done']} · Активно: {summary['active']}",
        reply_markup=tasks_keyboard(tasks),
    )


@router.callback_query(F.data == "tasks:clear_done")
async def cb_tasks_clear_done(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    removed = await db.clear_completed_tasks(callback.from_user.id)
    await callback.answer(f"Удалено выполненных: {removed}")
    tasks = await db.get_tasks(callback.from_user.id)
    summary = await db.get_tasks_summary(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Задачи\n\nВсего: {summary['total']} · Выполнено: {summary['done']} · Активно: {summary['active']}",
        reply_markup=tasks_keyboard(tasks),
    )


@router.callback_query(F.data == "tasks:cancel")
async def cb_tasks_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# WORD
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "word:start")
async def cb_word_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WordStates.waiting_for_topic)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "📄 Напишите тему отчёта. /cancel — отмена.",
            reply_markup=word_cancel_keyboard(),
        )
    await callback.answer()


@router.message(StateFilter(WordStates.waiting_for_topic), F.text == "/cancel")
async def cancel_word(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(WordStates.waiting_for_topic), F.text)
async def handle_word_topic(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    topic = message.text.strip()
    if not topic:
        return
    user_id = message.from_user.id
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer(
        "📄 Генерирую отчёт и упаковываю в .docx…", reply_markup=_stop_kb("word")
    )
    bot = message.bot
    lang = current_lang.get()

    async def _job() -> bytes:
        async with ChatActionSender.upload_document(bot=bot, chat_id=message.chat.id):
            return await generate_word_report(topic, lang)

    try:
        stopped, docx_bytes = await _run_stoppable(user_id, "word", _job())
    except WordReportError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        await state.clear()
        return
    await state.clear()
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return
    await _safe_edit(status_msg, "✅ Готово.")
    await message.answer_document(
        BufferedInputFile(docx_bytes, filename=f"klyx_report_{user_id}.docx")
    )
    await award_xp(user_id, XP_WORD)


@router.callback_query(F.data == "word:cancel")
async def cb_word_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "pptx:start")
async def cb_pptx_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PptxStates.waiting_for_topic)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "📊 Напишите тему презентации. /cancel — отмена.",
            reply_markup=pptx_cancel_keyboard(),
        )
    await callback.answer()


@router.message(StateFilter(PptxStates.waiting_for_topic), F.text == "/cancel")
async def cancel_pptx(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(PptxStates.waiting_for_topic), F.text)
async def handle_pptx_topic(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    topic = message.text.strip()
    if not topic:
        return
    await _ask_slide_count(message, state, topic)


@router.callback_query(F.data == "pptx:cancel")
async def cb_pptx_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# ПРЕЗЕНТАЦИЯ: выбор количества слайдов
# ---------------------------------------------------------------------------
async def _launch_pptx(message: Message, user_id: int, state: FSMContext, topic: str, slides: int) -> None:
    await state.clear()
    if message.bot is None:
        return
    if is_job_active(user_id, "pptx"):
        await message.answer("⏳ Предыдущая задача ещё выполняется. Дождитесь результата.")
        return
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        return
    if not start_pptx_job(message.bot, message.chat.id, user_id, topic, slides, current_lang.get()):
        await refund_text_attempt(user_id)
        await message.answer("⏳ Предыдущая задача ещё выполняется. Дождитесь результата.")
        return
    await message.answer(
        f"📊 Создаю презентацию на {slides} слайдов с иллюстрациями. "
        "Это займёт несколько минут, пришлю файл прямо в чат.",
        reply_markup=main_menu_keyboard(),
    )
    await message.answer(
        _t("Можно остановить генерацию:", "You can stop the generation:"),
        reply_markup=_stop_kb("pptx"),
    )


async def _ask_slide_count(message: Message, state: FSMContext, topic: str) -> None:
    await state.clear()
    await state.update_data(pptx_topic=topic[:300])
    await state.set_state(PptxStates.choosing_count)
    await message.answer(
        "📊 Сколько слайдов сделать? Выберите 3, 5, 7 или 10 либо отправьте своё число от 3 до 15.",
        reply_markup=pptx_count_keyboard(),
    )


@router.callback_query(F.data.startswith("pptx:count:"))
async def cb_pptx_count(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not callback.data or not isinstance(callback.message, Message):
        await callback.answer()
        return
    value = callback.data.split(":")[2]
    if value == "custom":
        await callback.message.answer("✏️ Отправьте число слайдов от 3 до 15.")
        await callback.answer()
        return
    data = await state.get_data()
    topic = data.get("pptx_topic")
    if not topic or not value.isdigit():
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    slides = max(config.PPTX_MIN_SLIDES, min(config.PPTX_MAX_SLIDES, int(value)))
    await callback.answer()
    await _launch_pptx(callback.message, callback.from_user.id, state, topic, slides)


@router.message(StateFilter(PptxStates.choosing_count), F.text == "/cancel")
async def cancel_pptx_count(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(PptxStates.choosing_count), F.text)
async def handle_pptx_count_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    raw = message.text.strip()
    if not raw.isdigit() or not (config.PPTX_MIN_SLIDES <= int(raw) <= config.PPTX_MAX_SLIDES):
        await message.answer("⚠️ Нужно число от 3 до 15.")
        return
    data = await state.get_data()
    topic = data.get("pptx_topic")
    if not topic:
        await state.clear()
        return
    await _launch_pptx(message, message.from_user.id, state, topic, int(raw))


# ---------------------------------------------------------------------------
# АУДИО: распознавание речи / конвертация в голосовое
# ---------------------------------------------------------------------------
_PENDING: dict[tuple[int, str], dict[str, Any]] = {}


def _remember_pending(user_id: int, kind: str, **payload: Any) -> None:
    now = time.monotonic()
    for key in [k for k, v in _PENDING.items() if now - v["ts"] > 3600]:
        _PENDING.pop(key, None)
    if len(_PENDING) > 1000:
        _PENDING.clear()
    _PENDING[(user_id, kind)] = {"ts": now, **payload}


def _extract_audio_info(message: Message) -> tuple[str, int | None, str] | None:
    if message.voice is not None:
        return message.voice.file_id, message.voice.file_size, "voice.ogg"
    if message.audio is not None:
        audio = message.audio
        return audio.file_id, audio.file_size, audio.file_name or "audio.mp3"
    document = message.document
    if document is not None:
        name = document.file_name or "audio"
        if name.lower().endswith(AUDIO_EXTENSIONS) or (document.mime_type or "").startswith("audio/"):
            return document.file_id, document.file_size, name
    return None


async def _offer_audio_choice(message: Message, info: tuple[str, int | None, str]) -> None:
    if message.from_user is None:
        return
    _remember_pending(
        message.from_user.id, "audio", file_id=info[0], size=info[1], name=info[2]
    )
    await message.answer("🎙 Что сделать с этим аудио?", reply_markup=audio_choice_keyboard())


async def _transcribe_and_reply(
    message: Message, file_id: str, file_size: int | None, file_name: str
) -> None:
    if message.bot is None:
        return
    max_size = config.MAX_AUDIO_FILE_SIZE_BYTES
    if (file_size or 0) > max_size:
        await message.answer(f"⚠️ Аудио слишком большое. Максимум {max_size // (1024 * 1024)} МБ.")
        return
    status_msg = await message.answer("🎙 Распознаю речь…")
    try:
        file = await message.bot.get_file(file_id)
        if not file.file_path:
            await status_msg.edit_text("⚠️ Не удалось получить файл от Telegram.")
            return
        buffer = io.BytesIO()
        await message.bot.download_file(file.file_path, buffer)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            text = await transcribe_voice(buffer.getvalue(), file_name)
    except TranscriptionError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось распознать: {exc}")
        return
    except TelegramAPIError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось скачать файл: {exc}")
        return
    await status_msg.edit_text("✅ Готово.")
    await _send_long_message(message, f"📝 Расшифровка:\n{text}")


async def _process_audio(
    message: Message, mode: str, info: tuple[str, int | None, str]
) -> None:
    if mode == "voice":
        await _convert_and_send_voice(message, info[0], info[1], info[2])
    else:
        await _transcribe_and_reply(message, info[0], info[1], info[2])


@router.callback_query(F.data == "audio:menu")
async def cb_audio_menu(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🎙 Аудио и голос\n\nВыберите действие:", reply_markup=audio_menu_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("audio:mode:"))
async def cb_audio_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not isinstance(callback.message, Message):
        await callback.answer()
        return
    mode = callback.data.split(":")[2]
    await state.set_state(AudioStates.waiting_for_file)
    await state.update_data(audio_mode=mode)
    if mode == "voice":
        text = "🔊 Отправьте аудиофайл (mp3, wav, m4a, ogg, aac) — верну настоящее голосовое сообщение. /cancel — отмена."
    else:
        text = "🎤 Отправьте голосовое сообщение или аудиофайл — распознаю речь в текст. /cancel — отмена."
    await callback.message.answer(text, reply_markup=cancel_keyboard("audio:cancel"))
    await callback.answer()


@router.callback_query(F.data == "audio:cancel")
async def cb_audio_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("audio:do:"))
async def cb_audio_do(callback: CallbackQuery) -> None:
    if callback.from_user is None or not callback.data or not isinstance(callback.message, Message):
        await callback.answer()
        return
    mode = callback.data.split(":")[2]
    pending = _PENDING.pop((callback.from_user.id, "audio"), None)
    if pending is None:
        await callback.answer("⚠️ Аудио не найдено. Отправьте файл заново.", show_alert=True)
        return
    await callback.answer()
    await _process_audio(
        callback.message, mode, (pending["file_id"], pending["size"], pending["name"])
    )


@router.message(StateFilter(AudioStates.waiting_for_file), F.text == "/cancel")
async def cancel_audio_mode(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(AudioStates.waiting_for_file), F.voice | F.audio | F.document)
async def handle_audio_in_mode(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("audio_mode", "stt")
    info = _extract_audio_info(message)
    if info is None:
        await message.answer("🎤 Отправьте голосовое сообщение или аудиофайл — распознаю речь в текст. /cancel — отмена.")
        return
    await state.clear()
    await _process_audio(message, mode, info)


# ---------------------------------------------------------------------------
# ВИДЕО ПО ТЕКСТУ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "video:start")
async def cb_video_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(VideoStates.waiting_for_prompt)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🎬 Опишите видео, например: «собака на луне». /cancel — отмена.",
            reply_markup=cancel_keyboard("video:cancel"),
        )
    await callback.answer()


@router.callback_query(F.data == "video:cancel")
async def cb_video_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(StateFilter(VideoStates.waiting_for_prompt), F.text == "/cancel")
async def cancel_video(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(VideoStates.waiting_for_prompt), F.text)
async def handle_video_prompt(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    prompt = message.text.strip()[:500]
    if not prompt:
        return
    user_id = message.from_user.id
    await state.clear()
    if is_job_active(user_id, "video"):
        await message.answer("⏳ Предыдущая задача ещё выполняется. Дождитесь результата.")
        return
    allowed, _remaining = await db.consume_video_request(user_id)
    if not allowed:
        await message.answer(
            f"⛔ Лимит видео на сегодня исчерпан ({config.DAILY_VIDEO_LIMIT} в сутки). Попробуйте завтра."
        )
        return
    start_video_job(message.bot, message.chat.id, user_id, prompt, current_lang.get())
    await message.answer(
        "🎬 Видео создаётся, отправлю прямо в чат, как только будет готово (обычно 1–3 минуты).",
        reply_markup=main_menu_keyboard(),
    )
    await message.answer(
        _t("Можно остановить генерацию:", "You can stop the generation:"),
        reply_markup=_stop_kb("video"),
    )


# ---------------------------------------------------------------------------
# ВИДЕО -> КРУЖОК
# ---------------------------------------------------------------------------
def _extract_video_info(message: Message) -> tuple[str, int | None] | None:
    if message.video is not None:
        return message.video.file_id, message.video.file_size
    if message.animation is not None:
        return message.animation.file_id, message.animation.file_size
    document = message.document
    if document is not None and (document.mime_type or "").startswith("video/"):
        return document.file_id, document.file_size
    return None


async def _start_note(message: Message, user_id: int, file_id: str, file_size: int | None) -> None:
    if message.bot is None:
        return
    if (file_size or 0) > 20 * 1024 * 1024:
        await message.answer("⚠️ Видео слишком большое для бота. Максимум 20 МБ.")
        return
    if is_job_active(user_id, "note"):
        await message.answer("⏳ Предыдущая задача ещё выполняется. Дождитесь результата.")
        return
    status_msg = await message.answer(
        "⭕ Делаю кружок… Это займёт несколько секунд.", reply_markup=_stop_kb("note")
    )
    tmp_dir = make_temp_dir("note")
    src_path = os.path.join(tmp_dir, "source.mp4")
    try:
        file = await message.bot.get_file(file_id)
        if not file.file_path:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await status_msg.edit_text("⚠️ Не удалось получить файл от Telegram.")
            return
        await message.bot.download_file(file.file_path, destination=src_path)
    except TelegramAPIError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await status_msg.edit_text(f"⚠️ Не удалось скачать файл: {exc}")
        return
    if not start_note_job(message.bot, message.chat.id, user_id, tmp_dir, src_path, current_lang.get()):
        await message.answer("⏳ Предыдущая задача ещё выполняется. Дождитесь результата.")


@router.callback_query(F.data == "note:start")
async def cb_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NoteStates.waiting_for_video)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "⭕ Пришлите видео (до 20 МБ) — сделаю из него кружок до 59 секунд. /cancel — отмена.",
            reply_markup=cancel_keyboard("note:cancel"),
        )
    await callback.answer()


@router.callback_query(F.data == "note:cancel")
async def cb_note_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "note:do")
async def cb_note_do(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    pending = _PENDING.pop((callback.from_user.id, "video"), None)
    if pending is None:
        await callback.answer("⚠️ Видео не найдено. Отправьте файл заново.", show_alert=True)
        return
    await callback.answer()
    await _start_note(callback.message, callback.from_user.id, pending["file_id"], pending["size"])


@router.message(StateFilter(NoteStates.waiting_for_video), F.text == "/cancel")
async def cancel_note(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(NoteStates.waiting_for_video), F.video | F.animation | F.document)
async def handle_note_video(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    info = _extract_video_info(message)
    if info is None:
        await message.answer(
            "⭕ Пришлите видео (до 20 МБ) — сделаю из него кружок до 59 секунд. /cancel — отмена."
        )
        return
    await state.clear()
    await _start_note(message, message.from_user.id, info[0], info[1])


async def _offer_note_choice(message: Message, info: tuple[str, int | None]) -> None:
    if message.from_user is None:
        return
    _remember_pending(message.from_user.id, "video", file_id=info[0], size=info[1])
    await message.answer("⭕ Что сделать с этим видео?", reply_markup=note_choice_keyboard())


@router.message(F.video | F.animation)
async def handle_video_without_mode(message: Message) -> None:
    info = _extract_video_info(message)
    if info is not None:
        await _offer_note_choice(message, info)


# ---------------------------------------------------------------------------
# АНАЛИЗ ФОТО (Vision)
# ---------------------------------------------------------------------------
@router.message(F.photo)
async def handle_photo_vision(message: Message) -> None:
    """Фото с вопросом — ответ на вопрос; без подписи — разбор и 3–5 вариантов действий."""
    if message.from_user is None or not message.photo or message.bot is None:
        return
    photo = message.photo[-1]
    max_size = config.MAX_IMAGE_FILE_SIZE_BYTES
    if (photo.file_size or 0) > max_size:
        await message.answer(f"⚠️ Фото слишком большое. Максимум {max_size // (1024 * 1024)} МБ.")
        return

    question = (message.caption or "").strip()[:1000]
    user_id = message.from_user.id
    if _is_running(user_id, "vision"):
        await message.answer(_t("⏳ Предыдущее фото ещё анализируется.", "⏳ The previous photo is still being analyzed."))
        return
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        return

    status_msg = await message.answer("📷 Анализирую фото…", reply_markup=_stop_kb("vision"))
    bot = message.bot
    lang = current_lang.get()

    async def _job() -> tuple[str, str]:
        file = await bot.get_file(photo.file_id)
        if not file.file_path:
            raise AIServiceError("Не удалось получить файл от Telegram.")
        buffer = io.BytesIO()
        await bot.download_file(file.file_path, buffer)
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            return await analyze_image(buffer.getvalue(), question, lang)

    try:
        stopped, result = await _run_stoppable(user_id, "vision", _job())
    except AIServiceError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось проанализировать фото: {exc}"))
        return
    except TelegramAPIError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось скачать файл: {exc}"))
        return
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return

    answer, model_used = result
    answer = clean_response_text(answer)
    await db.add_history_message(user_id, "user", f"[Фото] {question}".strip())
    await db.add_history_message(user_id, "assistant", answer, model_used)
    await award_xp(user_id, XP_VISION)
    await _deliver_answer(message, status_msg, answer, _answer_kb())


# ---------------------------------------------------------------------------
# ГОЛОС
# ---------------------------------------------------------------------------
@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    if message.from_user is None or message.voice is None or message.bot is None:
        return
    user_id = message.from_user.id
    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        return

    file = await message.bot.get_file(message.voice.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            text = await transcribe_voice(buffer.getvalue())
    except TranscriptionError as exc:
        await refund_text_attempt(user_id)
        await message.answer(f"⚠️ Не удалось распознать: {exc}")
        return

    await message.answer(f"🎙 Вы сказали: «{html.escape(text)}»")

    user = await db.get_user(user_id)
    if user is None:
        user = await db.get_or_create_user(
            user_id, message.from_user.username, message.from_user.first_name
        )
    await _respond_streaming(message, user, text, user_id)


# ---------------------------------------------------------------------------
# АУДИО -> ГОЛОСОВОЕ СООБЩЕНИЕ
# ---------------------------------------------------------------------------
async def _convert_and_send_voice(
    message: Message,
    file_id: str,
    file_size: int | None,
    file_name: str | None,
) -> None:
    if message.bot is None:
        return
    max_size = config.MAX_AUDIO_FILE_SIZE_BYTES
    if (file_size or 0) > max_size:
        await message.answer(f"⚠️ Аудио слишком большое. Максимум {max_size // (1024 * 1024)} МБ.")
        return

    status_msg = await message.answer("🎙 Конвертирую аудио в голосовое сообщение…")
    try:
        file = await message.bot.get_file(file_id)
        if not file.file_path:
            await status_msg.edit_text("⚠️ Не удалось получить файл от Telegram.")
            return
        buffer = io.BytesIO()
        await message.bot.download_file(file.file_path, buffer)
        async with ChatActionSender.record_voice(bot=message.bot, chat_id=message.chat.id):
            ogg_bytes = await convert_audio_to_voice(buffer.getvalue(), file_name)
    except AudioConversionError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        return
    except TelegramAPIError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось скачать файл: {exc}")
        return

    try:
        await message.answer_voice(BufferedInputFile(ogg_bytes, filename="voice.ogg"))
        await status_msg.edit_text("✅ Готово.")
    except TelegramAPIError:
        await status_msg.edit_text(
            "✅ Готово, но Telegram не разрешил голосовое (проверьте приватность). Отправляю файлом."
        )
        await message.answer_document(BufferedInputFile(ogg_bytes, filename="voice.ogg"))


@router.message(F.audio)
async def handle_audio(message: Message) -> None:
    if message.audio is None:
        return
    audio = message.audio
    await _offer_audio_choice(
        message, (audio.file_id, audio.file_size, audio.file_name or "audio.mp3")
    )


# ---------------------------------------------------------------------------
# ФАЙЛЫ
# ---------------------------------------------------------------------------
@router.message(F.document)
async def handle_document(message: Message) -> None:
    if message.from_user is None or message.document is None or message.bot is None:
        return
    document = message.document
    lower = (document.file_name or "").lower()
    if lower.endswith(AUDIO_EXTENSIONS) or (document.mime_type or "").startswith("audio/"):
        await _offer_audio_choice(
            message, (document.file_id, document.file_size, document.file_name or "audio")
        )
        return
    if (document.mime_type or "").startswith("video/"):
        await _offer_note_choice(message, (document.file_id, document.file_size))
        return
    if lower.endswith(".txt"):
        max_size = config.MAX_TXT_FILE_SIZE_BYTES
    elif lower.endswith(".pdf"):
        max_size = config.MAX_PDF_FILE_SIZE_BYTES
    else:
        await message.answer("Поддерживаются только .txt и .pdf.")
        return
    if (document.file_size or 0) > max_size:
        await message.answer(f"⚠️ Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ.")
        return
    allowed, remaining = await consume_text_attempt(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        return

    file = await message.bot.get_file(document.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    raw = buffer.getvalue()
    try:
        if lower.endswith(".txt"):
            text = raw.decode("utf-8", errors="replace")
        else:
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > config.MAX_PDF_PAGES:
                await message.answer(f"⚠️ PDF длиннее {config.MAX_PDF_PAGES} страниц.")
                return
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось прочитать: {exc}")
        return
    text = text.strip()
    if not text:
        await message.answer("⚠️ Файл пустой.")
        return

    truncated = text[:12000]
    prompt = (
        "Ниже содержимое файла. Ответь по нему или сделай резюме, если вопросов нет.\n\n"
        f"---\n{truncated}\n---"
    )
    user = await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
    messages = await _prepare_messages(message.from_user.id, prompt, include_history=False)
    estimated = estimate_processing_seconds(len(truncated), user.get("current_model"))
    status_msg = await message.answer(f"📄 Примерно {format_seconds(estimated)}.")

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            answer, model_used, elapsed = await request_ai(
                messages, preferred_model=user.get("current_model")
            )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Модели недоступны: {exc}")
        return

    answer = clean_response_text(answer)
    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(f"✅ За {elapsed:.1f} сек.")
    await _send_long_message(message, answer, reply_markup=_answer_kb())


# ---------------------------------------------------------------------------
# ОБЫЧНЫЙ ТЕКСТ
# ---------------------------------------------------------------------------
@router.message(StateFilter(None), F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    user_id = message.from_user.id
    user = await db.get_or_create_user(
        user_id, message.from_user.username, message.from_user.first_name
    )
    try:
        await game_checkin(user_id)
    except Exception:
        logger.exception("Не удалось обновить streak пользователя %s", user_id)

    # --- Распознавание запроса на презентацию ---
    pptx_topic = _is_presentation_request(text)
    if pptx_topic:
        await _ask_slide_count(message, state, pptx_topic)
        return

    data = await state.get_data()
    mode = data.get("mode")

    allowed, remaining = await consume_text_attempt(user_id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан (осталось: {remaining}).")
        return

    if mode == "battle":
        await state.update_data(mode=None)
        await _run_battle(message, user, text)
        return

    await _respond_streaming(message, user, text, user_id)


async def _run_battle(message: Message, user: dict[str, Any], text: str) -> None:
    """⚔️ Битва: один запрос двум моделям (Gemma 3 и Qwen 3) параллельно, с кнопкой «⏹» и голосованием."""
    if message.bot is None or message.from_user is None:
        return
    user_id = message.from_user.id
    model_a, model_b = BATTLE_MODELS
    messages = await _prepare_messages(user_id, text, include_history=True)
    status_msg = await message.answer(
        f"⚔️ {_model_label(model_a)} vs {_model_label(model_b)}…", reply_markup=_stop_kb("battle")
    )
    bot = message.bot
    lang = current_lang.get()

    async def _job() -> Any:
        model_text, _has_url = await build_url_context(text, lang)
        if model_text != text:
            messages[-1] = {"role": "user", "content": model_text}
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            return await request_two_models(messages, model_a, model_b, max_tokens=1400)

    try:
        stopped, result = await _run_stoppable(user_id, "battle", _job())
    except AIServiceError as exc:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, html.escape(f"⚠️ Не удалось: {exc}"))
        return
    if stopped:
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _stopped_text())
        return

    (text_a, used_a, elapsed_a), (text_b, used_b, elapsed_b) = result
    if text_a.startswith("⚠️") and text_b.startswith("⚠️"):
        await refund_text_attempt(user_id)
        await _safe_edit(status_msg, _t("⚠️ Обе модели сейчас недоступны.", "⚠️ Both models are unavailable right now."))
        return
    await _safe_edit(
        status_msg,
        html.escape(
            f"⚔️ Готово.\n1: {_model_label(used_a)} — {elapsed_a:.1f} сек\n"
            f"2: {_model_label(used_b)} — {elapsed_b:.1f} сек"
        ),
    )
    await _send_long_message(message, f"1️⃣ {_model_label(used_a)}\n\n{text_a}")
    await _send_long_message(
        message, f"2️⃣ {_model_label(used_b)}\n\n{text_b}", reply_markup=_battle_vote_kb()
    )
    await award_xp(user_id, XP_BATTLE)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    cleanup_stale_temp_dirs()
    logger.info("Инициализация базы данных…")
    await db.init_db()

    # Проверка шрифта для мемов: ./fonts, системные шрифты или автоматическая загрузка.
    if not await ensure_meme_font():
        logger.error("Шрифт для мемов недоступен: мемы будут возвращать ошибку.")

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(TranslateRequestMiddleware())

    if config.WEBAPP_URL:
        asyncio.create_task(run_web_server(bot))
        logger.info("Веб-сервер запущен на порту %s", config.WEBAPP_PORT)
    else:
        logger.warning("WEBAPP_URL не задан — Mini App отключён.")

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserContextMiddleware())
    router.message.outer_middleware(IncomingButtonMiddleware())
    dp.include_router(router)
    logger.info("Klyx AI запускается…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Klyx AI остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
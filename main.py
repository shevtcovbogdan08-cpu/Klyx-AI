# main.py
"""
Klyx AI — главный файл бота с запуском веб-сервера для Mini App.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from pypdf import PdfReader

import config
import database as db
from ai_service import (
    AUDIO_EXTENSIONS,
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
    edit_image_img2img,
    estimate_processing_seconds,
    format_seconds,
    generate_image_txt2img,
    generate_meme,
    generate_pptx_presentation,
    generate_word_report,
    get_system_prompt,
    request_ai,
    request_two_models,
    transcribe_voice,
)
from keyboards import (
    BTN_IMAGE,
    BTN_MEME,
    BTN_PPTX,
    BTN_TASKS,
    BTN_WORD,
    ROLES,
    answer_actions_keyboard,
    back_keyboard,
    battle_keyboard,
    confirm_keyboard,
    image_cancel_keyboard,
    image_menu_keyboard,
    inline_main_menu,
    main_menu_keyboard,
    meme_cancel_keyboard,
    models_keyboard,
    pptx_cancel_keyboard,
    profile_keyboard,
    roles_keyboard,
    style_keyboard,
    tasks_cancel_keyboard,
    tasks_keyboard,
    webapp_keyboard,
    word_cancel_keyboard,
)
from web_server import run_web_server

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


HELP_TEXT = (
    "❓ Klyx AI — возможности\n\n"
    "💚 Бот полностью бесплатный. Без рекламы и подписок.\n\n"
    "💬 Чат — просто напишите сообщение.\n"
    "🤖 Модель — выберите бесплатную модель.\n"
    "🎭 Роль — задайте характер ассистента.\n"
    "🎨 Генерация фото — по тексту или по фото с подписью.\n"
    "😂 Мемы — тема, картинка и подписи.\n"
    "✅ Задачи — умный To-Do трекер.\n"
    "📄 Word-отчет — структурированный .docx файл.\n"
    "📊 Презентация — .pptx файл по теме.\n"
    "⚔️ Битва ИИ — один запрос, две модели.\n"
    "🧐 Второе мнение — проверка ответа другой моделью.\n"
    "📋 В шпаргалку — сжатие диалога.\n"
    "🧠 Память — факты о вас.\n"
    "🎤 Голосовые — расшифровка через Whisper.\n"
    "🎙 Аудио в голосовое — пришлите аудиофайл (mp3, wav, m4a, ogg, aac), верну голосовое.\n"
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
        {"role": "system", "content": get_system_prompt()},
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
    reset_text = _format_reset_time(status["reset_at"])
    text = (
        "📊 Лимиты и статус\n\n"
        "💚 Klyx AI — полностью бесплатен. Без рекламы и подписок.\n\n"
        f"📈 Текстовые запросы: {status['used']} / {status['limit']}\n"
        f"🟢 Осталось: {status['remaining']}\n"
        f"⏳ Сброс: {reset_text}\n\n"
        f"🎨 Редактирование фото: {img_status['used']} / {img_status['limit']}\n"
        f"🟢 Осталось: {img_status['remaining']}\n\n"
        f"🌐 Глобальный лимит API: до {config.GLOBAL_REQUESTS_PER_MINUTE} запросов в минуту."
    )
    await message.answer(text)


async def _send_long_message(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    """Разбивает длинный текст на части по лимиту Telegram (4096 символов)."""
    if not text:
        text = "(пустой ответ)"
    LIMIT = 4000
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= LIMIT:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, LIMIT)
        if cut == -1 or cut < LIMIT // 2:
            cut = LIMIT
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await message.answer(part, reply_markup=reply_markup if is_last else None)


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
# КОМАНДЫ
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return
    await db.get_or_create_user(user.id, user.username, user.first_name)

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
    text = (
        "👤 Профиль и память\n\n"
        f"🆔 ID: {profile['telegram_id']}\n"
        f"🎭 Роль: {ROLES.get(profile.get('current_role') or 'default', 'Без роли')}\n"
        f"✍️ Стиль: {profile.get('style')}, {profile.get('expertise')}, {profile.get('tone')}\n"
        f"🤖 Модель: {profile.get('current_model') or 'по умолчанию'}\n\n"
        f"🧠 Память:\n{facts_text}"
    )
    await message.answer(text, reply_markup=profile_keyboard(facts))


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
    text = (
        "👤 Профиль\n\n"
        f"🎭 Роль: {ROLES.get(profile.get('current_role') or 'default', 'Без роли')}\n"
        f"✍️ Стиль: {profile.get('style')}, {profile.get('expertise')}, {profile.get('tone')}\n"
        f"🤖 Модель: {profile.get('current_model') or 'по умолчанию'}\n\n"
        f"🧠 Память:\n{facts_text}"
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
    reset_text = _format_reset_time(status["reset_at"])
    text = (
        "📊 Лимиты\n\n"
        f"📈 Текст: {status['used']} / {status['limit']} (осталось {status['remaining']})\n"
        f"⏳ Сброс: {reset_text}\n\n"
        f"🎨 Фото: {img_status['used']} / {img_status['limit']} (осталось {img_status['remaining']})"
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
        {"role": "system", "content": get_system_prompt()},
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
        {"role": "system", "content": get_system_prompt()},
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
    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer("🎨 Генерирую… Улучшаю промт и рисую картинку (бесплатно, до минуты).")
    try:
        async with ChatActionSender.upload_photo(bot=message.bot, chat_id=message.chat.id):
            image_bytes = await generate_image_txt2img(prompt)
    except ImageGenerationError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_photo(io.BytesIO(image_bytes))
    await state.clear()


@router.message(StateFilter(ImageStates.waiting_for_photo), F.photo)
async def handle_img_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo or message.bot is None:
        return
    prompt = (message.caption or "").strip()
    if not prompt:
        await message.answer("⚠️ Пришлите фото с подписью, что изменить.")
        return
    allowed, remaining = await db.consume_img2img_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит редактирования исчерпан (10/сутки). Осталось: {remaining}.")
        await state.clear()
        return

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    image_bytes = buffer.getvalue()

    status_msg = await message.answer("✏️ Создаю новую сцену по вашему описанию (бесплатно, до минуты)…")
    try:
        async with ChatActionSender.upload_photo(bot=message.bot, chat_id=message.chat.id):
            result_bytes = await edit_image_img2img(prompt, image_bytes)
    except ImageEditingError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_photo(io.BytesIO(result_bytes))
    await state.clear()


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
    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer("😂 Придумываю мем… Это может занять до 30 секунд.")
    try:
        async with ChatActionSender.upload_photo(bot=message.bot, chat_id=message.chat.id):
            meme_bytes = await generate_meme(topic)
    except MemeGenerationError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_photo(io.BytesIO(meme_bytes), caption=f"😂 {topic}")
    await state.clear()


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
    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer("📄 Генерирую отчёт и упаковываю в .docx…")
    try:
        async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
            docx_bytes = await generate_word_report(topic)
    except WordReportError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_document(
        BufferedInputFile(docx_bytes, filename=f"klyx_report_{message.from_user.id}.docx")
    )
    await state.clear()


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
    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return

    status_msg = await message.answer("📊 Генерирую презентацию и собираю .pptx…")
    try:
        async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
            pptx_bytes = await generate_pptx_presentation(topic)
    except PptxGenerationError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_document(
        BufferedInputFile(
            pptx_bytes,
            filename=f"Презентация_Klyx_AI_{message.from_user.id}.pptx",
        )
    )
    await state.clear()


@router.callback_query(F.data == "pptx:cancel")
async def cb_pptx_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


# ---------------------------------------------------------------------------
# ГОЛОС
# ---------------------------------------------------------------------------
@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    if message.from_user is None or message.voice is None or message.bot is None:
        return
    allowed, remaining = await db.consume_request(message.from_user.id)
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
        await message.answer(f"⚠️ Не удалось распознать: {exc}")
        return

    await message.answer(f"🎙 Вы сказали: «{text}»")

    user = await db.get_user(message.from_user.id)
    if user is None:
        user = await db.get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
    await db.add_history_message(message.from_user.id, "user", text)
    messages = await _prepare_messages(message.from_user.id, text, include_history=True)
    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(f"⏳ Примерно {format_seconds(estimated)}.")

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
    await _send_long_message(message, answer, reply_markup=answer_actions_keyboard())


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
    await _convert_and_send_voice(message, audio.file_id, audio.file_size, audio.file_name)


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
        await _convert_and_send_voice(
            message, document.file_id, document.file_size, document.file_name
        )
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
    allowed, remaining = await db.consume_request(message.from_user.id)
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
    await _send_long_message(message, answer, reply_markup=answer_actions_keyboard())


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

    user = await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )

    # --- Распознавание запроса на презентацию ---
    pptx_topic = _is_presentation_request(text)
    if pptx_topic:
        allowed, remaining = await db.consume_request(message.from_user.id)
        if not allowed:
            await message.answer(f"⛔ Лимит исчерпан (осталось: {remaining}).")
            return
        status_msg = await message.answer("📊 Генерирую презентацию и собираю .pptx…")
        try:
            async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
                pptx_bytes = await generate_pptx_presentation(pptx_topic)
        except PptxGenerationError as exc:
            await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
            return
        await status_msg.edit_text("✅ Готово.")
        await message.answer_document(
            BufferedInputFile(
                pptx_bytes,
                filename=f"Презентация_Klyx_AI_{message.from_user.id}.pptx",
            )
        )
        return

    data = await state.get_data()
    mode = data.get("mode")

    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан (осталось: {remaining}).")
        return

    await db.add_history_message(message.from_user.id, "user", text)
    messages = await _prepare_messages(message.from_user.id, text, include_history=True)

    if mode == "battle":
        await state.update_data(mode=None)
        models = config.FREE_MODELS
        if len(models) < 2:
            await message.answer("⚠️ Нужно минимум две модели.")
            return
        model_a = user.get("current_model") or models[0]
        model_b = next((m for m in models if m != model_a), models[1])
        status_msg = await message.answer(f"⚔️ {model_a} vs {model_b}…")
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            result_a, result_b = await request_two_models(messages, model_a, model_b)
        text_a, used_a, elapsed_a = result_a
        text_b, used_b, elapsed_b = result_b
        await status_msg.edit_text(
            f"⚔️ Готово.\nA: {used_a} — {elapsed_a:.1f} сек\nB: {used_b} — {elapsed_b:.1f} сек"
        )
        await _send_long_message(message, f"🅰️ {used_a}\n\n{text_a}")
        await _send_long_message(message, f"🅱️ {used_b}\n\n{text_b}")
        return

    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(f"⏳ Примерно {format_seconds(estimated)}…")

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            answer, model_used, elapsed = await request_ai(
                messages, preferred_model=user.get("current_model")
            )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        return
    except AIServiceError as exc:
        await status_msg.edit_text(f"⚠️ Ошибка сервиса: {exc}")
        return

    answer = clean_response_text(answer)
    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(f"✅ За {elapsed:.1f} сек.")
    await _send_long_message(message, answer, reply_markup=answer_actions_keyboard())


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
    logger.info("Инициализация базы данных…")
    await db.init_db()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    if config.WEBAPP_URL:
        asyncio.create_task(run_web_server(bot))
        logger.info("Веб-сервер запущен на порту %s", config.WEBAPP_PORT)
    else:
        logger.warning("WEBAPP_URL не задан — Mini App отключён.")

    dp = Dispatcher(storage=MemoryStorage())
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
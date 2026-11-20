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
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from pypdf import PdfReader

import config
import database as db
from ai_service import (
    AIServiceError,
    AllModelsFailedError,
    ImageEditingError,
    ImageGenerationError,
    TranscriptionError,
    clean_response_text,
    edit_image_img2img,
    estimate_processing_seconds,
    format_seconds,
    generate_image_txt2img,
    request_ai,
    request_two_models,
    transcribe_voice,
)
from keyboards import (
    BTN_IMAGE,
    ROLES,
    answer_actions_keyboard,
    back_keyboard,
    battle_keyboard,
    confirm_keyboard,
    image_cancel_keyboard,
    image_menu_keyboard,
    inline_main_menu,
    main_menu_keyboard,
    models_keyboard,
    profile_keyboard,
    roles_keyboard,
    style_keyboard,
    webapp_keyboard,
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


HELP_TEXT = (
    "❓ Klyx AI — возможности\n\n"
    "💚 Бот полностью бесплатный. Без рекламы, подписок и скрытых платежей.\n\n"
    "💬 Обычный чат\nПросто напишите сообщение.\n\n"
    "🤖 Модель\nВыберите бесплатную модель.\n\n"
    "🎭 Роль\nЗадайте характер ассистента.\n\n"
    "🎨 Генерация фото\nПо тексту или по присланному фото с подписью.\n\n"
    "⚔️ Битва ИИ\nОдин запрос — две модели.\n\n"
    "🧐 Второе мнение\nКнопка под ответом.\n\n"
    "📋 В шпаргалку\nКнопка под ответом.\n\n"
    "🧠 Долгосрочная память\nВ профиле можно добавить факты о себе.\n\n"
    "🎤 Голосовые\nОтправьте голосовое — расшифрую.\n\n"
    "📄 Файлы\n.txt (до 2 МБ) или .pdf (до 10 МБ, до 50 страниц).\n\n"
    "📊 Лимиты\nСколько запросов потрачено и когда сброс.\n\n"
    "🚀 Mini App\nКнопка «Открыть Klyx AI App».\n\n"
    "Команда /menu переотправляет клавиатуру, если кнопки пропали."
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
        {"role": "system", "content": config.SYSTEM_PROMPT_CLEAN_RUSSIAN},
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


# ---------------------------------------------------------------------------
# КОМАНДЫ (высший приоритет)
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
        "💚 Важно: я полностью бесплатен. Без рекламы, без подписок.\n\n"
        "Выбери действие в меню ниже или просто напиши сообщение.\n\n"
        "❓ Все подробности — /help.\n"
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
# REPLY-КНОПКИ МЕНЮ (сразу после команд, до FSM и handle_text)
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
# INLINE: навигация и настройки
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Главное меню:", reply_markup=back_keyboard("menu:main"))
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
        {"role": "system", "content": config.SYSTEM_PROMPT_CLEAN_RUSSIAN},
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
        text, model_used, elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось: {exc}")
        return
    await callback.message.answer(f"🧐 Второе мнение ({model_used}, {elapsed:.1f} сек)\n\n{text}")


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
        {"role": "system", "content": config.SYSTEM_PROMPT_CLEAN_RUSSIAN},
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
        text, model_used, elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось: {exc}")
        return
    await callback.message.answer(f"📋 Шпаргалка ({model_used}, {elapsed:.1f} сек)\n\n{text}")


# ---------------------------------------------------------------------------
# ИЗОБРАЖЕНИЯ: inline-переходы
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
            "Фото будет загружено на временный хостинг, а промт переведён на английский.\n"
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


# ---------------------------------------------------------------------------
# ИЗОБРАЖЕНИЯ: FSM
# ---------------------------------------------------------------------------
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
    if message.from_user is None or not message.text:
        return
    prompt = message.text.strip()
    if not prompt:
        return
    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Лимит исчерпан. Осталось: {remaining}.")
        await state.clear()
        return
    status_msg = await message.answer("🎨 Генерирую… Перевожу промт на английский и запрашиваю картинку.")
    try:
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

    status_msg = await message.answer("✏️ Загружаю фото на хостинг, перевожу промт и редактирую…")
    try:
        result_bytes = await edit_image_img2img(prompt, image_bytes)
    except ImageEditingError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось: {exc}")
        await state.clear()
        return
    await status_msg.edit_text("✅ Готово.")
    await message.answer_photo(io.BytesIO(result_bytes))
    await state.clear()


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
        text = await transcribe_voice(buffer.getvalue())
    except TranscriptionError as exc:
        await message.answer(f"⚠️ Не удалось распознать: {exc}")
        return
    await message.answer(f"🎤 Распознано:\n{text}")
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
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Модели недоступны: {exc}")
        return
    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(f"✅ Готово за {elapsed:.1f} сек ({model_used}).")
    await message.answer(answer, reply_markup=answer_actions_keyboard())


# ---------------------------------------------------------------------------
# ФАЙЛЫ
# ---------------------------------------------------------------------------
@router.message(F.document)
async def handle_document(message: Message) -> None:
    if message.from_user is None or message.document is None or message.bot is None:
        return
    document = message.document
    lower = (document.file_name or "").lower()
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
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Модели недоступны: {exc}")
        return
    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(f"✅ Готово за {elapsed:.1f} сек ({model_used}).")
    await message.answer(answer, reply_markup=answer_actions_keyboard())


# ---------------------------------------------------------------------------
# ОБЫЧНЫЙ ТЕКСТ (в самом низу, только вне FSM)
# ---------------------------------------------------------------------------
@router.message(StateFilter(None), F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    user = await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
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
        result_a, result_b = await request_two_models(messages, model_a, model_b)
        text_a, used_a, elapsed_a = result_a
        text_b, used_b, elapsed_b = result_b
        await status_msg.edit_text(
            f"⚔️ Готово.\nA: {used_a} — {elapsed_a:.1f} сек\nB: {used_b} — {elapsed_b:.1f} сек"
        )
        await message.answer(f"🅰️ {used_a}\n\n{text_a}")
        await message.answer(f"🅱️ {used_b}\n\n{text_b}")
        return

    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(f"⏳ Примерно {format_seconds(estimated)}…")
    try:
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
    await status_msg.edit_text(f"✅ За {elapsed:.1f} сек ({model_used}).")
    await message.answer(answer, reply_markup=answer_actions_keyboard())


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
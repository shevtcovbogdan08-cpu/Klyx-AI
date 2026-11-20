# main.py
"""
Klyx AI — главный файл бота.

Здесь регистрируются все хендлеры и запускается polling.
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
    TranscriptionError,
    estimate_processing_seconds,
    format_seconds,
    request_ai,
    request_two_models,
    transcribe_voice,
)
from keyboards import (
    ROLES,
    answer_actions_keyboard,
    back_keyboard,
    battle_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    models_keyboard,
    profile_keyboard,
    roles_keyboard,
    style_keyboard,
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


# ---------------------------------------------------------------------------
# Текст справки
# ---------------------------------------------------------------------------
HELP_TEXT = (
    "❓ **Klyx AI — возможности**\n\n"
    "💚 Бот полностью бесплатный. Без рекламы, подписок и скрытых платежей.\n\n"
    "💬 **Обычный чат**\n"
    "Просто напишите сообщение — бот ответит через одну из бесплатных нейросетей "
    "(DeepSeek, Llama, Qwen, Gemma).\n\n"
    "🤖 **Модель**\n"
    "Выберите, какая бесплатная модель отвечает. Если она недоступна, бот автоматически "
    "переключится на другую.\n\n"
    "🎭 **Роль**\n"
    "Задайте характер ассистента: программист, учитель, копирайтер, переводчик, "
    "Сократ или критик.\n\n"
    "🎛 **Стиль общения** (в профиле)\n"
    "• Длина ответа: кратко / подробно\n"
    "• Уровень: для новичка / для эксперта\n"
    "• Тон: дружелюбно / строго\n\n"
    "⚔️ **Битва ИИ**\n"
    "Один запрос уходит сразу двум моделям — можно сравнить, кто ответил лучше.\n\n"
    "🧐 **Второе мнение**\n"
    "Кнопка под ответом. Другая модель проверяет предыдущий ответ: ищет ошибки, "
    "спорные места, галлюцинации.\n\n"
    "📋 **В шпаргалку**\n"
    "Кнопка под ответом. Сжимает текущий диалог в структурированную шпаргалку: "
    "резюме, ключевые идеи, решения, открытые вопросы, TODO.\n\n"
    "🧠 **Долгосрочная память**\n"
    "В профиле можно добавить факты о себе (имя, интересы, стиль общения). "
    "Бот будет их учитывать и не забудет даже после «Нового диалога».\n\n"
    "🎤 **Голосовые сообщения**\n"
    "Отправьте голосовое — бот расшифрует его через Whisper и ответит текстом.\n\n"
    "📄 **Файлы**\n"
    "Пришлите `.txt` (до 2 МБ) или `.pdf` (до 10 МБ, до 50 страниц) — бот прочитает "
    "и ответит по содержимому.\n\n"
    "📊 **Лимиты**\n"
    "В разделе «Лимиты и статус» видно: сколько запросов потрачено сегодня, "
    "сколько осталось и когда сброс (раз в 24 часа).\n\n"
    "🔄 **Новый диалог**\n"
    "Очищает историю текущего разговора. Долгосрочная память при этом сохраняется."
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_reset_time(reset_at: datetime) -> str:
    """Форматирует время сброса лимита в человекочитаемый вид."""
    delta = reset_at - _now_utc()
    total_seconds = max(0, int(delta.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"через {hours} ч {minutes} мин"
    return f"через {minutes} мин"


def _user_style_hint(user: dict[str, Any]) -> str:
    """Собирает системную подсказку о стиле общения пользователя."""
    parts: list[str] = []
    length = user.get("style") or "detailed"
    expertise = user.get("expertise") or "novice"
    tone = user.get("tone") or "friendly"

    if length == "short":
        parts.append("Отвечай кратко, по существу, без лишних вступлений.")
    else:
        parts.append("Отвечай подробно, с пояснениями и примерами, где уместно.")

    if expertise == "novice":
        parts.append("Объясняй простыми словами, избегай излишнего жаргона.")
    else:
        parts.append("Можно использовать профессиональные термины и глубину.")

    if tone == "friendly":
        parts.append("Тон дружелюбный и поддерживающий.")
    else:
        parts.append("Тон строгий, деловой, без эмоциональных отступлений.")

    return " ".join(parts)


def _role_hint(role_key: str | None) -> str | None:
    if not role_key or role_key == "default":
        return None
    prompts = {
        "programmer": "Ты опытный Senior Python-разработчик. Давай практичные советы и код.",
        "teacher": "Ты терпеливый учитель. Объясняй пошагово и проверяй понимание.",
        "copywriter": "Ты профессиональный копирайтер. Пиши ярко, структурированно и по делу.",
        "translator": "Ты профессиональный переводчик. Переводи точно и естественно.",
        "socrates": "Ты Сократ. Веди диалог через уточняющие вопросы, помогая собеседнику самому найти ответ.",
        "critic": "Ты строгий, но конструктивный критик. Указывай на слабые места и предлагай улучшения.",
    }
    return prompts.get(role_key)


def _build_system_messages(user: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Формирует системные сообщения для модели на основе профиля, роли и памяти."""
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Ты — Klyx AI, бесплатный и дружелюбный ИИ-ассистент. "
                "Ты не упоминаешь платные подписки, рекламу или ограничения, "
                "кроме честного отображения суточного лимита запросов. "
                "Твоя цель — помогать пользователю полезно и прозрачно."
            ),
        }
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
    """Собирает полный список сообщений: системные + история + новый запрос."""
    user = await db.get_user(telegram_id)
    if user is None:
        user = await db.get_or_create_user(telegram_id, None, None)

    facts = await db.get_memory_facts(telegram_id)
    messages = _build_system_messages(user, facts)

    if include_history:
        history = await db.get_history(telegram_id)
        for item in history:
            role = item["role"]
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": item["content"]})

    messages.append({"role": "user", "content": user_prompt})
    return messages


async def _send_limits_status(message: Message, telegram_id: int) -> None:
    """Отправляет сообщение со статусом лимитов."""
    status = await db.get_user_daily_status(telegram_id)
    reset_text = _format_reset_time(status["reset_at"])
    text = (
        "📊 **Лимиты и статус**\n\n"
        "💚 **Klyx AI — полностью бесплатен.** Без рекламы, подписок и скрытых платежей.\n\n"
        f"📈 Ваш суточный лимит: **{status['limit']} запросов**\n"
        f"✅ Использовано сегодня: **{status['used']}**\n"
        f"🟢 Осталось: **{status['remaining']}**\n"
        f"⏳ Сброс: {reset_text}\n\n"
        f"🌐 Глобальный лимит API: до **{config.GLOBAL_REQUESTS_PER_MINUTE}** запросов в минуту."
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    await db.get_or_create_user(user.id, user.username, user.first_name)

    text = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        "Я — **Klyx AI**, твой бесплатный нейросетевой помощник.\n\n"
        "💚 **Важно:** я полностью бесплатен. Без рекламы, без подписок, без скрытых платежей.\n\n"
        "Что я умею:\n"
        "• Отвечать на вопросы с помощью бесплатных нейросетей\n"
        "• Сравнивать две модели в режиме «Битва ИИ»\n"
        "• Запоминать факты о тебе (долгосрочная память)\n"
        "• Расшифровывать голосовые сообщения\n"
        "• Читать `.txt` и `.pdf` файлы\n"
        "• Давать «второе мнение» и сжимать диалог в шпаргалку\n\n"
        "Выбери действие в меню ниже или просто напиши сообщение.\n\n"
        "❓ Все подробности — кнопка «Возможности» или команда /help."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /help и кнопка «❓ Возможности»
# ---------------------------------------------------------------------------
@router.message(Command("help"))
@router.message(F.text == "❓ Возможности")
async def cmd_help(message: Message) -> None:
    await message.answer(
        HELP_TEXT,
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Нижнее меню (Reply-кнопки)
# ---------------------------------------------------------------------------
@router.message(F.text == "🔄 Новый диалог")
async def new_dialog(message: Message) -> None:
    if message.from_user is None:
        return
    await db.clear_history(message.from_user.id)
    await message.answer(
        "🔄 История диалога очищена. Долгосрочная память сохранена.\n"
        "О чём поговорим?",
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "🤖 Модель")
async def show_models(message: Message) -> None:
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    current = user.get("current_model") if user else None
    await message.answer(
        "Выберите бесплатную модель для ответов:",
        reply_markup=models_keyboard(current),
    )


@router.message(F.text == "🎭 Роль")
async def show_roles(message: Message) -> None:
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    current = user.get("current_role") if user else None
    await message.answer(
        "Выберите роль ассистента:",
        reply_markup=roles_keyboard(current),
    )


@router.message(F.text == "⚔️ Битва ИИ")
async def show_battle(message: Message) -> None:
    await message.answer(
        "⚔️ **Битва нейросетей**\n\n"
        "Отправьте запрос, и я параллельно спрошу две разные бесплатные модели. "
        "Вы сможете сравнить ответы.\n\n"
        "Нажмите кнопку ниже, чтобы активировать режим.",
        reply_markup=battle_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(F.text == "📊 Лимиты и статус")
async def show_status(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_limits_status(message, message.from_user.id)


@router.message(F.text == "👤 Профиль и память")
async def show_profile(message: Message) -> None:
    if message.from_user is None:
        return
    profile = await db.get_full_profile(message.from_user.id)
    if profile is None:
        await message.answer("Не удалось загрузить профиль. Попробуйте /start.")
        return

    facts = profile.get("facts", [])
    facts_text = "\n".join(f"• {f['fact']}" for f in facts) if facts else "— пока пусто —"

    text = (
        "👤 **Профиль и память**\n\n"
        f"🆔 ID: `{profile['telegram_id']}`\n"
        f"🎭 Роль: **{ROLES.get(profile.get('current_role') or 'default', 'Без роли')}**\n"
        f"✍️ Стиль: длина — **{profile.get('style')}**, уровень — **{profile.get('expertise')}**, тон — **{profile.get('tone')}**\n"
        f"🤖 Модель: `{profile.get('current_model') or 'по умолчанию'}`\n\n"
        f"🧠 **Долгосрочная память:**\n{facts_text}\n\n"
        "Нажмите ❌ рядом с фактом, чтобы удалить его."
    )
    await message.answer(
        text,
        reply_markup=profile_keyboard(facts),
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Inline: навигация и настройки
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Главное меню. Выберите действие:",
            reply_markup=back_keyboard("menu:main"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def cb_choose_model(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    model = callback.data.split(":", 1)[1]
    if model not in config.FREE_MODELS:
        await callback.answer("Эта модель недоступна.", show_alert=True)
        return
    await db.update_user_field(callback.from_user.id, "current_model", model)
    await callback.answer("Модель обновлена ✅")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"✅ Текущая модель: `{model}`",
            reply_markup=models_keyboard(model),
            parse_mode=ParseMode.MARKDOWN,
        )


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
        await callback.message.edit_text(
            f"🎭 Текущая роль: **{ROLES[role]}**",
            reply_markup=roles_keyboard(role),
            parse_mode=ParseMode.MARKDOWN,
        )


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
    field_map = {
        "length": "style",
        "expertise": "expertise",
        "tone": "tone",
    }
    field = field_map.get(dimension)
    if field is None:
        await callback.answer("Неизвестное измерение стиля.", show_alert=True)
        return

    await db.update_user_field(callback.from_user.id, field, value)
    user = await db.get_user(callback.from_user.id)
    await callback.answer("Стиль обновлён ✅")
    if isinstance(callback.message, Message) and user is not None:
        await callback.message.edit_text(
            "🎛 Настройте стиль общения:",
            reply_markup=style_keyboard(
                current_length=user.get("style"),
                current_expertise=user.get("expertise"),
                current_tone=user.get("tone"),
            ),
        )


# ---------------------------------------------------------------------------
# Inline: память
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "memory:add")
async def cb_memory_add(callback: CallbackQuery, state: FSMContext) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🧠 Напишите факт, который нужно запомнить (например: «Меня зовут Алекс, я изучаю Python»).\n"
            "Отправьте /cancel, чтобы отменить."
        )
    await state.set_state(MemoryStates.waiting_for_fact)
    await callback.answer()


@router.message(StateFilter(MemoryStates.waiting_for_fact), F.text == "/cancel")
async def cancel_memory_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление факта отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(MemoryStates.waiting_for_fact))
async def save_memory_fact(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    ok, reason = await db.add_memory_fact(message.from_user.id, message.text)
    if ok:
        await message.answer("✅ Факт сохранён в долгосрочную память.")
    elif reason == "limit":
        await message.answer(
            f"⚠️ Достигнут лимит фактов ({config.MAX_MEMORY_FACTS}). "
            "Удалите что-нибудь в профиле."
        )
    elif reason == "too_long":
        await message.answer("⚠️ Факт слишком длинный (максимум 500 символов).")
    else:
        await message.answer("⚠️ Не удалось сохранить факт.")
    await state.clear()


@router.callback_query(F.data.startswith("memory:del:"))
async def cb_memory_delete(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    try:
        fact_id = int(callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID факта.", show_alert=True)
        return

    deleted = await db.delete_memory_fact(callback.from_user.id, fact_id)
    if deleted:
        await callback.answer("Факт удалён 🗑")
    else:
        await callback.answer("Факт не найден.", show_alert=True)

    profile = await db.get_full_profile(callback.from_user.id)
    if profile and isinstance(callback.message, Message):
        facts = profile.get("facts", [])
        facts_text = "\n".join(f"• {f['fact']}" for f in facts) if facts else "— пока пусто —"
        await callback.message.edit_text(
            f"👤 **Профиль и память**\n\n🧠 Факты:\n{facts_text}",
            reply_markup=profile_keyboard(facts),
            parse_mode=ParseMode.MARKDOWN,
        )


@router.callback_query(F.data == "confirm:clear_memory")
async def cb_confirm_clear_memory(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🗑 Вы уверены, что хотите удалить все факты из долгосрочной памяти?",
            reply_markup=confirm_keyboard("clear_memory"),
        )
    await callback.answer()


@router.callback_query(F.data == "confirm:clear_memory:yes")
async def cb_do_clear_memory(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    removed = await db.clear_all_memory(callback.from_user.id)
    await callback.answer(f"Удалено фактов: {removed}")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🧠 Долгосрочная память очищена.",
            reply_markup=back_keyboard("menu:main"),
        )


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Действие отменено.",
            reply_markup=back_keyboard("menu:main"),
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Битва ИИ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "battle:start")
async def cb_battle_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChatStates.chatting)
    await state.update_data(mode="battle")
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "⚔️ Режим **Битва ИИ** активирован. Отправьте запрос — я спрошу две модели."
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# AI-Критик
# ---------------------------------------------------------------------------
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

    critic_prompt = (
        "Ты — AI-Критик. Проанализируй ответ другой модели и оцени его. "
        "Найди возможные ошибки, спорные места, галлюцинации. "
        "Дай краткий вердикт: что хорошо, что плохо, что исправить.\n\n"
        f"Ответ модели:\n---\n{last_answer}\n---"
    )
    messages = [
        {"role": "system", "content": "Ты строгий, но конструктивный AI-Критик."},
        {"role": "user", "content": critic_prompt},
    ]

    try:
        text, model_used, elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось получить второе мнение: {exc}")
        return

    await callback.message.answer(
        f"🧐 **Второе мнение** (модель: `{model_used}`, {elapsed:.1f} сек)\n\n{text}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Шпаргалка
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "digest:run")
async def cb_digest_run(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    history = await db.get_history(callback.from_user.id, limit=20)
    if len(history) < 2:
        await callback.answer("Слишком короткий диалог для шпаргалки.", show_alert=True)
        return

    dialog_text = "\n".join(
        f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
        for m in history
        if m["role"] in ("user", "assistant")
    )
    prompt = (
        "Сожми этот диалог в структурированную шпаргалку. "
        "Формат: краткое резюме, ключевые идеи, принятые решения, открытые вопросы, TODO (если есть). "
        "Пиши по-русски, структурированно, без воды.\n\n"
        f"Диалог:\n---\n{dialog_text}\n---"
    )

    await callback.answer("Сжимаю диалог…")
    messages = [
        {"role": "system", "content": "Ты — редактор, который делает краткие и полезные шпаргалки."},
        {"role": "user", "content": prompt},
    ]

    try:
        text, model_used, elapsed = await request_ai(messages)
    except AllModelsFailedError as exc:
        await callback.message.answer(f"⚠️ Не удалось создать шпаргалку: {exc}")
        return

    await callback.message.answer(
        f"📋 **Шпаргалка по диалогу** (модель: `{model_used}`, {elapsed:.1f} сек)\n\n{text}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Голосовые сообщения
# ---------------------------------------------------------------------------
@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    if message.from_user is None or message.voice is None or message.bot is None:
        return

    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(
            f"⛔ Суточный лимит исчерпан. Осталось: {remaining}. "
            "Лимит сбросится автоматически."
        )
        return

    file = await message.bot.get_file(message.voice.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    audio_bytes = buffer.getvalue()

    try:
        text = await transcribe_voice(audio_bytes)
    except TranscriptionError as exc:
        await message.answer(f"⚠️ Не удалось распознать голос: {exc}")
        return

    await message.answer(f"🎤 Распознано:\n_{text}_", parse_mode=ParseMode.MARKDOWN)

    user = await db.get_user(message.from_user.id)
    if user is None:
        user = await db.get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )

    await db.add_history_message(message.from_user.id, "user", text)
    messages = await _prepare_messages(message.from_user.id, text, include_history=True)
    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(
        f"⏳ Обрабатываю… Примерно {format_seconds(estimated)}."
    )

    try:
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Все модели недоступны: {exc}")
        return

    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(
        f"✅ Готово за **{elapsed:.1f} сек** (модель: `{model_used}`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    await message.answer(answer, reply_markup=answer_actions_keyboard())


# ---------------------------------------------------------------------------
# Файлы .txt / .pdf
# ---------------------------------------------------------------------------
@router.message(F.document)
async def handle_document(message: Message) -> None:
    if message.from_user is None or message.document is None or message.bot is None:
        return

    document = message.document
    filename = document.file_name or ""
    lower = filename.lower()

    if lower.endswith(".txt"):
        max_size = config.MAX_TXT_FILE_SIZE_BYTES
    elif lower.endswith(".pdf"):
        max_size = config.MAX_PDF_FILE_SIZE_BYTES
    else:
        await message.answer("Поддерживаются только `.txt` и `.pdf`.")
        return

    if (document.file_size or 0) > max_size:
        await message.answer(
            f"⚠️ Файл слишком большой. Максимум: {max_size // (1024 * 1024)} МБ."
        )
        return

    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(f"⛔ Суточный лимит исчерпан. Осталось: {remaining}.")
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
                await message.answer(
                    f"⚠️ PDF слишком длинный. Максимум страниц: {config.MAX_PDF_PAGES}."
                )
                return
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 — парсинг файла может падать по-разному
        await message.answer(f"⚠️ Не удалось прочитать файл: {exc}")
        return

    text = text.strip()
    if not text:
        await message.answer("⚠️ Файл пустой или текст не извлекается.")
        return

    truncated = text[:12000]
    prompt = (
        "Ниже — содержимое файла. Ответь на вопросы пользователя по этому файлу. "
        "Если вопросов нет, сделай краткое резюме и выдели ключевые пункты.\n\n"
        f"Содержимое:\n---\n{truncated}\n---"
    )

    user = await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
    messages = await _prepare_messages(message.from_user.id, prompt, include_history=False)
    estimated = estimate_processing_seconds(len(truncated), user.get("current_model"))
    status_msg = await message.answer(
        f"📄 Файл получен. Примерное время обработки: {format_seconds(estimated)}."
    )

    try:
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Все модели недоступны: {exc}")
        return

    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(
        f"✅ Готово за **{elapsed:.1f} сек** (модель: `{model_used}`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    await message.answer(answer, reply_markup=answer_actions_keyboard())


# ---------------------------------------------------------------------------
# Обычный текстовый чат (в самом конце — ловит всё остальное)
# ---------------------------------------------------------------------------
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return

    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    user = await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )

    # Режим битвы?
    data = await state.get_data()
    mode = data.get("mode")

    allowed, remaining = await db.consume_request(message.from_user.id)
    if not allowed:
        await message.answer(
            f"⛔ Суточный лимит исчерпан (осталось: {remaining}). "
            "Лимит сбросится автоматически."
        )
        return

    await db.add_history_message(message.from_user.id, "user", text)
    messages = await _prepare_messages(message.from_user.id, text, include_history=True)

    if mode == "battle":
        await state.update_data(mode=None)
        models = config.FREE_MODELS
        if len(models) < 2:
            await message.answer("⚠️ Для битвы нужно минимум две модели.")
            return

        model_a = user.get("current_model") or models[0]
        model_b = next((m for m in models if m != model_a), models[1])
        status_msg = await message.answer(
            f"⚔️ Битва: `{model_a}` vs `{model_b}`…"
        )
        result_a, result_b = await request_two_models(messages, model_a, model_b)

        text_a, used_a, elapsed_a = result_a
        text_b, used_b, elapsed_b = result_b

        await status_msg.edit_text(
            f"⚔️ **Битва завершена.**\n"
            f"Модель A: `{used_a}` — {elapsed_a:.1f} сек\n"
            f"Модель B: `{used_b}` — {elapsed_b:.1f} сек",
            parse_mode=ParseMode.MARKDOWN,
        )
        await message.answer(
            f"🅰️ **{used_a}**\n\n{text_a}", parse_mode=ParseMode.MARKDOWN
        )
        await message.answer(
            f"🅱️ **{used_b}**\n\n{text_b}", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Обычный ответ
    estimated = estimate_processing_seconds(len(text), user.get("current_model"))
    status_msg = await message.answer(
        f"⏳ Примерное время обработки: {format_seconds(estimated)}…"
    )

    try:
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        await status_msg.edit_text(f"⚠️ Не удалось получить ответ: {exc}")
        return
    except AIServiceError as exc:
        await status_msg.edit_text(f"⚠️ Ошибка ИИ-сервиса: {exc}")
        return

    await db.add_history_message(message.from_user.id, "assistant", answer, model_used)
    await status_msg.edit_text(
        f"✅ Готово за **{elapsed:.1f} сек** (модель: `{model_used}`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    await message.answer(answer, reply_markup=answer_actions_keyboard())


# ---------------------------------------------------------------------------
# /cancel (глобальный)
# ---------------------------------------------------------------------------
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Запуск
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

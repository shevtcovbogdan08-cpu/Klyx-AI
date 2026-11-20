# keyboards.py
"""
Все клавиатуры Klyx AI.

Соглашение по callback_data:
  model:<model_id>          — выбор модели
  role:<role_key>           — выбор роли
  style:<dimension>:<value> — стиль (length/expertise/tone)
  memory:add                — добавить факт
  memory:list               — показать факты
  memory:del:<fact_id>      — удалить факт
  confirm:<action>          — подтверждение
  cancel                    — отмена
  menu:<section>            — переход в раздел
  battle:start              — запуск битвы
  critic:run                — запуск AI-критика
  digest:run                — сжатие диалога в шпаргалку
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import FREE_MODELS


# ---------------------------------------------------------------------------
# Reply-клавиатура (нижнее меню)
# ---------------------------------------------------------------------------
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Нижняя Reply-клавиатура с основными разделами."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="🔄 Новый диалог")
    builder.button(text="🤖 Модель")
    builder.button(text="🎭 Роль")
    builder.button(text="⚔️ Битва ИИ")
    builder.button(text="📊 Лимиты и статус")
    builder.button(text="👤 Профиль и память")
    builder.button(text="❓ Возможности")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


# ---------------------------------------------------------------------------
# Inline: модели
# ---------------------------------------------------------------------------
def models_keyboard(current_model: str | None = None) -> InlineKeyboardMarkup:
    """Список бесплатных моделей. Текущая отмечается галочкой."""
    builder = InlineKeyboardBuilder()
    for model in FREE_MODELS:
        prefix = "✅ " if model == current_model else ""
        # Короткая подпись: убираем суффикс :free для читаемости
        label = model.split("/")[-1].replace(":free", "")
        builder.button(text=f"{prefix}{label}", callback_data=f"model:{model}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: роли
# ---------------------------------------------------------------------------
ROLES: dict[str, str] = {
    "default": "Без роли",
    "programmer": "Программист",
    "teacher": "Учитель",
    "copywriter": "Копирайтер",
    "translator": "Переводчик",
    "socrates": "Сократ (вопросы)",
    "critic": "Критик",
}


def roles_keyboard(current_role: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in ROLES.items():
        prefix = "✅ " if key == current_role else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"role:{key}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: стиль общения
# ---------------------------------------------------------------------------
LENGTH_OPTIONS = {"short": "Кратко", "detailed": "Подробно"}
EXPERTISE_OPTIONS = {"novice": "Для новичка", "expert": "Для эксперта"}
TONE_OPTIONS = {"friendly": "Дружелюбно", "strict": "Строго"}


def style_keyboard(
    current_length: str | None = None,
    current_expertise: str | None = None,
    current_tone: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in LENGTH_OPTIONS.items():
        prefix = "✅ " if value == current_length else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"style:length:{value}")
    for value, label in EXPERTISE_OPTIONS.items():
        prefix = "✅ " if value == current_expertise else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"style:expertise:{value}")
    for value, label in TONE_OPTIONS.items():
        prefix = "✅ " if value == current_tone else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"style:tone:{value}")
    builder.adjust(2, 2, 2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: профиль и память
# ---------------------------------------------------------------------------
def profile_keyboard(facts: list[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура профиля:
      - список фактов с кнопками удаления;
      - добавить факт;
      - очистить всю память;
      - назад.
    """
    builder = InlineKeyboardBuilder()
    for fact in facts:
        # Ограничиваем длину подписи, чтобы кнопка не разъехалась
        label = fact["fact"] if len(fact["fact"]) <= 40 else fact["fact"][:37] + "…"
        builder.button(text=f"❌ {label}", callback_data=f"memory:del:{fact['id']}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить факт", callback_data="memory:add"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Очистить память", callback_data="confirm:clear_memory"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: подтверждение опасных действий
# ---------------------------------------------------------------------------
def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{action}:yes")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: универсальная «назад»
# ---------------------------------------------------------------------------
def back_keyboard(target: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=target)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: битва моделей
# ---------------------------------------------------------------------------
def battle_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Запустить битву", callback_data="battle:start")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: пост-обработка ответа (Критик / Шпаргалка)
# ---------------------------------------------------------------------------
def answer_actions_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧐 Второе мнение", callback_data="critic:run")
    builder.button(text="📋 В шпаргалку", callback_data="digest:run")
    builder.adjust(2)
    return builder.as_markup()
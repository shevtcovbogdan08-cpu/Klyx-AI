# keyboards.py
"""
Все клавиатуры Klyx AI.

Тексты Reply-кнопок вынесены в константы BTN_*, чтобы main.py
использовал те же строки в фильтрах — это исключает рассинхрон.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import FREE_MODELS, WEBAPP_URL


# ---------------------------------------------------------------------------
# Константы текстов Reply-кнопок (единый источник правды)
# ---------------------------------------------------------------------------
BTN_NEW_DIALOG = "🔄 Новый диалог"
BTN_MODEL = "🤖 Модель"
BTN_ROLE = "🎭 Роль"
BTN_BATTLE = "⚔️ Битва ИИ"
BTN_STATUS = "📊 Лимиты и статус"
BTN_PROFILE = "👤 Профиль и память"
BTN_HELP = "❓ Возможности"
BTN_IMAGE = "🎨 Генерация фото"


# ---------------------------------------------------------------------------
# Reply-клавиатура (нижнее меню)
# ---------------------------------------------------------------------------
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Нижняя Reply-клавиатура с основными разделами."""
    builder = ReplyKeyboardBuilder()
    builder.button(text=BTN_NEW_DIALOG)
    builder.button(text=BTN_MODEL)
    builder.button(text=BTN_ROLE)
    builder.button(text=BTN_BATTLE)
    builder.button(text=BTN_IMAGE)
    builder.button(text=BTN_STATUS)
    builder.button(text=BTN_PROFILE)
    builder.button(text=BTN_HELP)
    builder.adjust(2, 2, 2, 2)
    return builder.as_markup(resize_keyboard=True)


# ---------------------------------------------------------------------------
# Inline: кнопка запуска Mini App
# ---------------------------------------------------------------------------
def webapp_keyboard() -> InlineKeyboardMarkup | None:
    """
    Кнопка открытия Mini App.
    Если WEBAPP_URL не задан — возвращает None, чтобы main.py не падал.
    """
    if not WEBAPP_URL:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Открыть Klyx AI App", web_app=WebAppInfo(url=WEBAPP_URL))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: модели
# ---------------------------------------------------------------------------
def models_keyboard(current_model: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model in FREE_MODELS:
        prefix = "✅ " if model == current_model else ""
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
    builder = InlineKeyboardBuilder()
    for fact in facts:
        label = fact["fact"] if len(fact["fact"]) <= 40 else fact["fact"][:37] + "…"
        builder.button(text=f"❌ {label}", callback_data=f"memory:del:{fact['id']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="➕ Добавить факт", callback_data="memory:add"))
    builder.row(InlineKeyboardButton(text="🗑 Очистить память", callback_data="confirm:clear_memory"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: подтверждение
# ---------------------------------------------------------------------------
def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{action}:yes")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: назад
# ---------------------------------------------------------------------------
def back_keyboard(target: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=target)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: битва
# ---------------------------------------------------------------------------
def battle_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Запустить битву", callback_data="battle:start")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: пост-обработка ответа
# ---------------------------------------------------------------------------
def answer_actions_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧐 Второе мнение", callback_data="critic:run")
    builder.button(text="📋 В шпаргалку", callback_data="digest:run")
    builder.adjust(2)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Inline: генерация изображений
# ---------------------------------------------------------------------------
def image_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🖼 По тексту", callback_data="img:txt2img")
    builder.button(text="✏️ По фото", callback_data="img:img2img")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def image_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="img:cancel")
    return builder.as_markup()
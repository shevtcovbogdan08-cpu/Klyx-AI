# keyboards.py
"""
Все клавиатуры Klyx AI.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import FREE_MODELS, WEBAPP_URL

BTN_NEW_DIALOG = "🔄 Новый диалог"
BTN_MODEL = "🤖 Модель"
BTN_ROLE = "🎭 Роль"
BTN_BATTLE = "⚔️ Битва ИИ"
BTN_STATUS = "📊 Лимиты и статус"
BTN_PROFILE = "👤 Профиль и память"
BTN_HELP = "❓ Возможности"
BTN_IMAGE = "🎨 Генерация фото"
BTN_MEME = "😂 Мемы"
BTN_TASKS = "✅ Список задач"
BTN_WORD = "📄 Создать Word-отчет"
BTN_PPTX = "📊 Презентация"
BTN_VIDEO = "🎬 Генерация видео"
BTN_NOTE = "⭕ Видео в кружок"
BTN_AUDIO = "🎙 Аудио и голос"
BTN_LANG = "🌐 Язык / Language"
BTN_ADMIN = "🛠 Админ-панель"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=BTN_NEW_DIALOG)
    builder.button(text=BTN_MODEL)
    builder.button(text=BTN_ROLE)
    builder.button(text=BTN_BATTLE)
    builder.button(text=BTN_IMAGE)
    builder.button(text=BTN_MEME)
    builder.button(text=BTN_TASKS)
    builder.button(text=BTN_WORD)
    builder.button(text=BTN_PPTX)
    builder.button(text=BTN_VIDEO)
    builder.button(text=BTN_NOTE)
    builder.button(text=BTN_AUDIO)
    builder.button(text=BTN_STATUS)
    builder.button(text=BTN_PROFILE)
    builder.button(text=BTN_LANG)
    builder.button(text=BTN_HELP)
    builder.adjust(2, 2, 2, 2, 2, 2, 2, 2)
    return builder.as_markup(resize_keyboard=True)


def inline_main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Модель", callback_data="menu:models")
    builder.button(text="🎭 Роль", callback_data="menu:roles")
    builder.button(text="🎨 Генерация фото", callback_data="img:txt2img")
    builder.button(text="😂 Мемы", callback_data="meme:start")
    builder.button(text="✅ Задачи", callback_data="tasks:list")
    builder.button(text="📄 Word-отчет", callback_data="word:start")
    builder.button(text="📊 Презентация", callback_data="pptx:start")
    builder.button(text="🎬 Видео", callback_data="video:start")
    builder.button(text="⭕ Кружок", callback_data="note:start")
    builder.button(text="🎙 Аудио", callback_data="audio:menu")
    builder.button(text=BTN_LANG, callback_data="lang:menu")
    builder.button(text="👤 Профиль", callback_data="menu:profile")
    builder.button(text="📊 Лимиты", callback_data="menu:limits")
    builder.adjust(2, 2, 2, 2, 2, 2, 2)
    return builder.as_markup()


def webapp_keyboard() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Открыть Klyx AI App", web_app=WebAppInfo(url=WEBAPP_URL))
    return builder.as_markup()


def models_keyboard(current_model: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model in FREE_MODELS:
        prefix = "✅ " if model == current_model else ""
        label = model.split("/")[-1].replace(":free", "")
        builder.button(text=f"{prefix}{label}", callback_data=f"model:{model}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


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


def profile_keyboard(facts: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fact in facts:
        label = fact["fact"] if len(fact["fact"]) <= 40 else fact["fact"][:37] + "…"
        builder.button(text=f"❌ {label}", callback_data=f"memory:del:{fact['id']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=BTN_LANG, callback_data="lang:menu"))
    builder.row(InlineKeyboardButton(text="➕ Добавить факт", callback_data="memory:add"))
    builder.row(InlineKeyboardButton(text="🗑 Очистить память", callback_data="confirm:clear_memory"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{action}:yes")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()


def back_keyboard(target: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=target)
    return builder.as_markup()


def battle_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Запустить битву", callback_data="battle:start")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def answer_actions_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧐 Второе мнение", callback_data="critic:run")
    builder.button(text="📋 В шпаргалку", callback_data="digest:run")
    builder.adjust(2)
    return builder.as_markup()


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


def tasks_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for task in tasks:
        mark = "✅" if int(task["is_done"]) == 1 else "⬜️"
        text = task["text"] if len(task["text"]) <= 35 else task["text"][:32] + "…"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {text}",
                callback_data=f"tasks:toggle:{task['id']}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"tasks:del:{task['id']}",
            ),
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить задачу", callback_data="tasks:add"))
    builder.row(
        InlineKeyboardButton(
            text="🧹 Удалить выполненные", callback_data="tasks:clear_done"
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def tasks_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="tasks:cancel")
    return builder.as_markup()


def meme_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="meme:cancel")
    return builder.as_markup()


def word_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="word:cancel")
    return builder.as_markup()


def pptx_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="pptx:cancel")
    return builder.as_markup()


def cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=callback_data)
    return builder.as_markup()


def lang_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🇷🇺 Русский", callback_data="lang:set:ru")
    builder.button(text="🇬🇧 English", callback_data="lang:set:en")
    builder.adjust(2)
    return builder.as_markup()


def audio_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Распознать речь", callback_data="audio:mode:stt")
    builder.button(text="🔊 В голосовое", callback_data="audio:mode:voice")
    builder.adjust(1)
    return builder.as_markup()


def audio_choice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Распознать речь", callback_data="audio:do:stt")
    builder.button(text="🔊 В голосовое", callback_data="audio:do:voice")
    builder.adjust(1)
    return builder.as_markup()


def note_choice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭕ Сделать кружок", callback_data="note:do")
    return builder.as_markup()


def pptx_count_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for count in (3, 5, 7, 10):
        builder.button(text=str(count), callback_data=f"pptx:count:{count}")
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="✏️ Своё число", callback_data="pptx:count:custom"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="pptx:cancel"))
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Выгрузить всех пользователей", callback_data="admin:export")
    builder.button(text="🔄 Обновить", callback_data="admin:refresh")
    builder.adjust(1)
    return builder.as_markup()

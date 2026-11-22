# i18n.py
"""
Локализация Klyx AI (RU / EN) для бота и Mini App.

Как это работает:
  - язык пользователя хранится в БД (users.lang) и кладётся в contextvar current_lang
    на время обработки каждого апдейта / запроса Mini App;
  - все исходящие сообщения бота проходят через TranslateRequestMiddleware: если язык
    английский, тексты, подписи и кнопки переводятся по словарю STRINGS_EN
    (точное совпадение строки или шаблон с {}). Непереведённые строки остаются русскими,
    поэтому пропуск в словаре никогда не ломает бота;
  - входящие нажатия английских кнопок reply-клавиатуры приводятся к русским подписям
    (IncomingButtonMiddleware), поэтому обработчики работают на любом языке.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
import database as db

logger = logging.getLogger(__name__)

current_lang: ContextVar[str] = ContextVar("current_lang", default="ru")
current_user_id: ContextVar[int] = ContextVar("current_user_id", default=0)

BTN_ADMIN = "🛠 Админ-панель"

# ---------------------------------------------------------------------------
# Словарь RU -> EN (строки и шаблоны; {} — подставляемое значение)
# ---------------------------------------------------------------------------
STRINGS_EN: dict[str, str] = {
    # --- Кнопки reply-меню ---
    "🔄 Новый диалог": "🔄 New chat",
    "🤖 Модель": "🤖 Model",
    "🎭 Роль": "🎭 Role",
    "⚔️ Битва ИИ": "⚔️ AI Battle",
    "🎨 Генерация фото": "🎨 Photo generation",
    "😂 Мемы": "😂 Memes",
    "✅ Список задач": "✅ Task list",
    "📄 Создать Word-отчет": "📄 Create Word report",
    "📊 Презентация": "📊 Presentation",
    "📊 Лимиты и статус": "📊 Limits & status",
    "👤 Профиль и память": "👤 Profile & memory",
    "❓ Возможности": "❓ Features",
    "🎬 Генерация видео": "🎬 Video generation",
    "⭕ Видео в кружок": "⭕ Video to circle",
    "🎙 Аудио и голос": "🎙 Audio & voice",
    BTN_ADMIN: "🛠 Admin panel",
    # --- Inline-кнопки ---
    "✅ Задачи": "✅ Tasks",
    "📄 Word-отчет": "📄 Word report",
    "👤 Профиль": "👤 Profile",
    "📊 Лимиты": "📊 Limits",
    "🎬 Видео": "🎬 Video",
    "⭕ Кружок": "⭕ Circle",
    "🎙 Аудио": "🎙 Audio",
    "🚀 Открыть Klyx AI App": "🚀 Open Klyx AI App",
    "⬅️ Назад": "⬅️ Back",
    "✅ Подтвердить": "✅ Confirm",
    "❌ Отмена": "❌ Cancel",
    "⚔️ Запустить битву": "⚔️ Start battle",
    "🧐 Второе мнение": "🧐 Second opinion",
    "📋 В шпаргалку": "📋 To cheat sheet",
    "🖼 По тексту": "🖼 From text",
    "✏️ По фото": "✏️ From photo",
    "➕ Добавить факт": "➕ Add fact",
    "🗑 Очистить память": "🗑 Clear memory",
    "➕ Добавить задачу": "➕ Add task",
    "🧹 Удалить выполненные": "🧹 Delete completed",
    "📝 Распознать речь": "📝 Speech to text",
    "🔊 В голосовое": "🔊 To voice message",
    "⭕ Сделать кружок": "⭕ Make a circle",
    "✏️ Своё число": "✏️ Custom number",
    "👥 Выгрузить всех пользователей": "👥 Export all users",
    "🔄 Обновить": "🔄 Refresh",
    # --- Роли и стиль ---
    "Без роли": "No role",
    "Программист": "Programmer",
    "Учитель": "Teacher",
    "Копирайтер": "Copywriter",
    "Переводчик": "Translator",
    "Сократ (вопросы)": "Socrates (questions)",
    "Критик": "Critic",
    "Кратко": "Brief",
    "Подробно": "Detailed",
    "Для новичка": "For beginners",
    "Для эксперта": "For experts",
    "Дружелюбно": "Friendly",
    "Строго": "Strict",
    # --- Справка ---
    "❓ Klyx AI — возможности": "❓ Klyx AI — features",
    "💚 Бот полностью бесплатный. Без рекламы и подписок.": "💚 The bot is completely free. No ads, no subscriptions.",
    "💬 Чат — просто напишите сообщение.": "💬 Chat — just write a message.",
    "🤖 Модель — выберите бесплатную модель.": "🤖 Model — choose a free model.",
    "🎭 Роль — задайте характер ассистента.": "🎭 Role — set the assistant's character.",
    "🎨 Генерация фото — по тексту или по фото с подписью.": "🎨 Photo generation — from text or from a photo with a caption.",
    "😂 Мемы — тема, картинка и подписи.": "😂 Memes — a topic, a picture and captions.",
    "✅ Задачи — умный To-Do трекер.": "✅ Tasks — a smart to-do tracker.",
    "📄 Word-отчет — структурированный .docx файл.": "📄 Word report — a structured .docx file.",
    "📊 Презентация — .pptx файл по теме с иллюстрациями.": "📊 Presentation — a .pptx file on your topic, with pictures.",
    "⚔️ Битва ИИ — один запрос, две модели.": "⚔️ AI Battle — one request, two models.",
    "🧐 Второе мнение — проверка ответа другой моделью.": "🧐 Second opinion — another model checks the answer.",
    "📋 В шпаргалку — сжатие диалога.": "📋 Cheat sheet — dialog summary.",
    "🧠 Память — факты о вас.": "🧠 Memory — facts about you.",
    "🎤 Голосовые — расшифровка через Whisper.": "🎤 Voice messages — transcription via Whisper.",
    "🎙 Аудио и голос — распознавание речи и конвертация аудиофайла (mp3, wav, m4a, ogg, aac) в голосовое.": "🎙 Audio & voice — speech to text, or convert an audio file (mp3, wav, m4a, ogg, aac) to a voice message.",
    "🎬 Видео по тексту — короткий ролик по описанию (1–2 в сутки).": "🎬 Text to video — a short clip from a description (1–2 per day).",
    "⭕ Видео в кружок — обычное видео превращается в видеосообщение.": "⭕ Video to circle — turns a regular video into a video note.",
    "🌐 /lang — язык интерфейса (RU / EN).": "🌐 /lang — interface language (RU / EN).",
    "📄 Файлы — .txt и .pdf.": "📄 Files — .txt and .pdf.",
    "📊 Лимиты — сколько запросов потрачено.": "📊 Limits — how many requests are used.",
    "Команда /menu переотправляет клавиатуру.": "The /menu command resends the keyboard.",
    # --- Время / лимиты ---
    "через {} ч {} мин": "in {} h {} min",
    "через {} мин": "in {} min",
    "📊 Лимиты и статус": "📊 Limits & status",
    "💚 Klyx AI — полностью бесплатен. Без рекламы и подписок.": "💚 Klyx AI is completely free. No ads, no subscriptions.",
    "📈 Текстовые запросы: {} / {}": "📈 Text requests: {} / {}",
    "🟢 Осталось: {}": "🟢 Remaining: {}",
    "⏳ Сброс: {}": "⏳ Reset: {}",
    "🎨 Редактирование фото: {} / {}": "🎨 Photo editing: {} / {}",
    "🎬 Видео: {} / {}": "🎬 Video: {} / {}",
    "🌐 Глобальный лимит API: до {} запросов в минуту.": "🌐 Global API limit: up to {} requests per minute.",
    "📊 Лимиты": "📊 Limits",
    "📈 Текст: {} / {} (осталось {})": "📈 Text: {} / {} ({} left)",
    "🎨 Фото: {} / {} (осталось {})": "🎨 Photo: {} / {} ({} left)",
    "🎬 Видео: {} / {} (осталось {})": "🎬 Video: {} / {} ({} left)",
    "(пустой ответ)": "(empty answer)",
    # --- Старт и меню ---
    "👋 Привет, {}!": "👋 Hello, {}!",
    "Я — Klyx AI, твой бесплатный нейросетевой помощник.": "I'm Klyx AI, your free AI assistant.",
    "💚 Важно: я полностью бесплатен. Без рекламы и подписок.": "💚 Important: I'm completely free. No ads, no subscriptions.",
    "Выбери действие в меню ниже или просто напиши сообщение.": "Choose an action from the menu below or just write a message.",
    "❓ Подробности — /help.": "❓ Details — /help.",
    "🔄 Если кнопки пропали — /menu.": "🔄 If the buttons disappeared — /menu.",
    "🚀 Открой Mini App для премиального интерфейса:": "🚀 Open the Mini App for the premium interface:",
    "🔄 Клавиатура обновлена. Если кнопки не появились — закройте и снова откройте чат.": "🔄 Keyboard updated. If the buttons did not appear, close and reopen the chat.",
    "Дублирующее меню:": "Duplicate menu:",
    "🔄 История диалога очищена. Память сохранена.": "🔄 Dialog history cleared. Memory is kept.",
    "Выберите бесплатную модель:": "Choose a free model:",
    "Выберите роль ассистента:": "Choose the assistant's role:",
    "⚔️ Битва нейросетей": "⚔️ AI Battle",
    "Отправьте запрос — спрошу две модели параллельно.": "Send a request and I'll ask two models in parallel.",
    "🎨 Генерация фото": "🎨 Photo generation",
    "• По тексту — сгенерирую изображение по описанию.": "• From text — I'll generate an image from a description.",
    "• По фото — пришлите фото с подписью, что изменить.": "• From photo — send a photo with a caption of what to change.",
    "😂 Генератор мемов": "😂 Meme generator",
    "Напишите тему, и я придумаю подписи и картинку.": "Write a topic and I'll come up with the picture and captions.",
    "📄 Создание Word-отчета": "📄 Word report",
    "Напишите тему отчёта.": "Write the report topic.",
    "📊 Создание презентации .pptx": "📊 Presentation (.pptx)",
    "Напишите тему презентации.": "Write the presentation topic.",
    "Выберите модель:": "Choose a model:",
    "Выберите роль:": "Choose a role:",
    "Главное меню:": "Main menu:",
    "Профиль не найден. Попробуйте /start.": "Profile not found. Try /start.",
    "Профиль не найден.": "Profile not found.",
    "Модель обновлена ✅": "Model updated ✅",
    "Роль обновлена ✅": "Role updated ✅",
    "Стиль обновлён ✅": "Style updated ✅",
    "Отменено.": "Cancelled.",
    "✅ Модель: {}": "✅ Model: {}",
    "Модель недоступна.": "Model unavailable.",
    "Неизвестная роль.": "Unknown role.",
    "Неизвестный стиль.": "Unknown style.",
    "Некорректный запрос.": "Invalid request.",
    # --- Профиль и память ---
    "✅ Список задач": "✅ Task list",
    "Всего: {} · Выполнено: {} · Активно: {}": "Total: {} · Done: {} · Active: {}",
    "👤 Профиль и память": "👤 Profile & memory",
    "🎭 Роль: {}": "🎭 Role: {}",
    "✍️ Стиль: {}, {}, {}": "✍️ Style: {}, {}, {}",
    "🤖 Модель: {}": "🤖 Model: {}",
    "🌐 Язык: {}": "🌐 Language: {}",
    "🧠 Память:": "🧠 Memory:",
    "— пока пусто —": "— empty so far —",
    "— пусто —": "— empty —",
    "по умолчанию": "default",
    "🎛 Стиль:": "🎛 Style:",
    "🧠 Напишите факт для запоминания. /cancel — отмена.": "🧠 Write a fact to remember. /cancel to cancel.",
    "✅ Факт сохранён.": "✅ Fact saved.",
    "Удалено 🗑": "Deleted 🗑",
    "Не найдено.": "Not found.",
    "🧠 Факты:": "🧠 Facts:",
    "🗑 Удалить все факты из памяти?": "🗑 Delete all facts from memory?",
    "Удалено: {}": "Deleted: {}",
    "🧠 Память очищена.": "🧠 Memory cleared.",
    "⚠️ Лимит фактов ({}).": "⚠️ Fact limit reached ({}).",
    "⚠️ Факт длиннее 500 символов.": "⚠️ The fact is longer than 500 characters.",
    "⚠️ Не удалось сохранить.": "⚠️ Could not save.",
    "Некорректный ID.": "Invalid ID.",
    # --- Битва / второе мнение / шпаргалка ---
    "⚔️ Режим Битва активирован. Отправьте запрос.": "⚔️ Battle mode is on. Send your request.",
    "⚔️ Готово.": "⚔️ Done.",
    "A: {} — {} сек": "A: {} — {} sec",
    "B: {} — {} сек": "B: {} — {} sec",
    "⚠️ Нужно минимум две модели.": "⚠️ At least two models are required.",
    "Нет ответа для проверки.": "No answer to check.",
    "Запускаю второе мнение…": "Getting a second opinion…",
    "🧐 Второе мнение": "🧐 Second opinion",
    "Сжимаю…": "Summarizing…",
    "Слишком короткий диалог.": "The dialog is too short.",
    "📋 Шпаргалка": "📋 Cheat sheet",
    # --- Фото ---
    "🖼 Опишите изображение. /cancel — отмена.": "🖼 Describe the image. /cancel to cancel.",
    "✏️ Пришлите фото с подписью, что изменить.": "✏️ Send a photo with a caption of what to change.",
    "Например: фото + «добавь ананас».": "For example: a photo + “add a pineapple”.",
    "/cancel — отмена.": "/cancel to cancel.",
    "Генерация отменена.": "Generation cancelled.",
    "Редактирование отменено.": "Editing cancelled.",
    "🎨 Генерирую… Улучшаю промт и рисую картинку (бесплатно, до минуты).": "🎨 Generating… Improving the prompt and drawing the picture (free, up to a minute).",
    "✏️ Создаю новую сцену по вашему описанию (бесплатно, до минуты)…": "✏️ Creating a new scene from your description (free, up to a minute)…",
    "⚠️ Пришлите фото с подписью, что изменить.": "⚠️ Send a photo with a caption of what to change.",
    "⛔ Лимит редактирования исчерпан (10/сутки). Осталось: {}.": "⛔ Editing limit reached (10/day). Remaining: {}.",
    "✅ Готово.": "✅ Done.",
    # --- Мемы ---
    "😂 Напишите тему мема. /cancel — отмена.": "😂 Write the meme topic. /cancel to cancel.",
    "😂 Придумываю мем… Это может занять до 30 секунд.": "😂 Creating a meme… This can take up to 30 seconds.",
    # --- Задачи ---
    "➕ Напишите задачу. /cancel — отмена.": "➕ Write a task. /cancel to cancel.",
    "✅ Задача добавлена.": "✅ Task added.",
    "✅ Задачи: всего {}, выполнено {}.": "✅ Tasks: {} total, {} done.",
    "✅ Задачи": "✅ Tasks",
    "Задача не найдена.": "Task not found.",
    "Выполнено ✅": "Done ✅",
    "Возвращено в работу ⬜️": "Back to work ⬜️",
    "Удалено выполненных: {}": "Completed tasks deleted: {}",
    "⚠️ Лимит задач ({}).": "⚠️ Task limit reached ({}).",
    "⚠️ Задача длиннее 300 символов.": "⚠️ The task is longer than 300 characters.",
    "⚠️ Не удалось добавить.": "⚠️ Could not add.",
    # --- Word / PPTX ---
    "📄 Напишите тему отчёта. /cancel — отмена.": "📄 Write the report topic. /cancel to cancel.",
    "📄 Генерирую отчёт и упаковываю в .docx…": "📄 Generating the report and packing it into .docx…",
    "📊 Напишите тему презентации. /cancel — отмена.": "📊 Write the presentation topic. /cancel to cancel.",
    "📊 Сколько слайдов сделать? Выберите 3, 5, 7 или 10 либо отправьте своё число от 3 до 15.": "📊 How many slides? Pick 3, 5, 7 or 10, or send your own number from 3 to 15.",
    "✏️ Отправьте число слайдов от 3 до 15.": "✏️ Send the number of slides from 3 to 15.",
    "⚠️ Нужно число от 3 до 15.": "⚠️ A number from 3 to 15 is required.",
    "📊 Создаю презентацию на {} слайдов с иллюстрациями. Это займёт несколько минут, пришлю файл прямо в чат.": "📊 Creating a {}-slide presentation with illustrations. It will take a few minutes, I'll send the file right to this chat.",
    "✅ Презентация готова: {}": "✅ Presentation ready: {}",
    "Презентация: {}": "Presentation: {}",
    "Word-отчёт: {}": "Word report: {}",
    "⚠️ Не удалось создать презентацию: {}": "⚠️ Could not create the presentation: {}",
    "⏳ Предыдущая задача ещё выполняется. Дождитесь результата.": "⏳ The previous task is still running. Please wait for the result.",
    # --- Голос / аудио ---
    "🎙 Вы сказали: «{}»": "🎙 You said: “{}”",
    "⏳ Примерно {}.": "⏳ About {}.",
    "⏳ Примерно {}…": "⏳ About {}…",
    "📄 Примерно {}.": "📄 About {}.",
    "✅ За {} сек.": "✅ In {} sec.",
    "🎙 Конвертирую аудио в голосовое сообщение…": "🎙 Converting the audio to a voice message…",
    "⚠️ Не удалось распознать: {}": "⚠️ Could not recognize: {}",
    "⚠️ Модели недоступны: {}": "⚠️ Models unavailable: {}",
    "⚠️ Ошибка сервиса: {}": "⚠️ Service error: {}",
    "⚠️ Не удалось: {}": "⚠️ Failed: {}",
    "⚠️ Аудио слишком большое. Максимум {} МБ.": "⚠️ The audio is too large. Maximum {} MB.",
    "⚠️ Не удалось получить файл от Telegram.": "⚠️ Could not get the file from Telegram.",
    "⚠️ Не удалось скачать файл: {}": "⚠️ Could not download the file: {}",
    "✅ Готово, но Telegram не разрешил голосовое (проверьте приватность). Отправляю файлом.": "✅ Done, but Telegram did not allow a voice message (check privacy settings). Sending as a file.",
    "🎙 Аудио и голос\n\nВыберите действие:": "🎙 Audio & voice\n\nChoose an action:",
    "🎙 Аудио и голос": "🎙 Audio & voice",
    "Выберите действие:": "Choose an action:",
    "🎤 Отправьте голосовое сообщение или аудиофайл — распознаю речь в текст. /cancel — отмена.": "🎤 Send a voice message or an audio file and I'll turn the speech into text. /cancel to cancel.",
    "🔊 Отправьте аудиофайл (mp3, wav, m4a, ogg, aac) — верну настоящее голосовое сообщение. /cancel — отмена.": "🔊 Send an audio file (mp3, wav, m4a, ogg, aac) and I'll return a real voice message. /cancel to cancel.",
    "🎙 Что сделать с этим аудио?": "🎙 What should I do with this audio?",
    "📝 Расшифровка:": "📝 Transcript:",
    "🎙 Распознаю речь…": "🎙 Recognizing speech…",
    "⚠️ Аудио не найдено. Отправьте файл заново.": "⚠️ Audio not found. Please send the file again.",
    # --- Файлы ---
    "Поддерживаются только .txt и .pdf.": "Only .txt and .pdf are supported.",
    "⚠️ Файл слишком большой. Максимум {} МБ.": "⚠️ The file is too large. Maximum {} MB.",
    "⚠️ Файл пустой.": "⚠️ The file is empty.",
    "⚠️ Не удалось прочитать: {}": "⚠️ Could not read: {}",
    "⚠️ PDF длиннее {} страниц.": "⚠️ The PDF has more than {} pages.",
    "Ниже содержимое файла. Ответь по нему или сделай резюме, если вопросов нет.": "Below is the file content. Answer based on it or write a summary if there are no questions.",
    # --- Лимит ---
    "⛔ Лимит исчерпан. Осталось: {}.": "⛔ Limit reached. Remaining: {}.",
    "⛔ Лимит исчерпан (осталось: {}).": "⛔ Limit reached (remaining: {}).",
    # --- Язык ---
    "🌐 Выберите язык / Choose language:": "🌐 Выберите язык / Choose language:",
    "✅ Язык переключён на русский.": "✅ Language switched to English.",
    "Меню обновлено.": "Menu updated.",
    "Русский": "Russian",
    # --- Видео ---
    "🎬 Опишите видео, например: «собака на луне». /cancel — отмена.": "🎬 Describe the video, for example: “a dog on the moon”. /cancel to cancel.",
    "🎬 Видео создаётся, отправлю прямо в чат, как только будет готово (обычно 1–3 минуты).": "🎬 Your video is being created, I'll send it right to this chat as soon as it's ready (usually 1–3 minutes).",
    "⛔ Лимит видео на сегодня исчерпан ({} в сутки). Попробуйте завтра.": "⛔ Today's video limit is reached ({} per day). Try again tomorrow.",
    "⚠️ Не удалось создать видео: {}": "⚠️ Could not create the video: {}",
    "Попытка возвращена, можно повторить.": "The attempt was refunded, you can try again.",
    "🎬 {}": "🎬 {}",
    "Не удалось создать видео ни одним из способов.": "The video could not be created by any method.",
    # --- Кружок ---
    "⭕ Пришлите видео (до 20 МБ) — сделаю из него кружок до 59 секунд. /cancel — отмена.": "⭕ Send a video (up to 20 MB) and I'll turn it into a circle of up to 59 seconds. /cancel to cancel.",
    "⭕ Что сделать с этим видео?": "⭕ What should I do with this video?",
    "⭕ Делаю кружок… Это займёт несколько секунд.": "⭕ Making the circle… It will take a few seconds.",
    "⚠️ Видео слишком большое для бота. Максимум 20 МБ.": "⚠️ The video is too large for the bot. Maximum 20 MB.",
    "⚠️ Не удалось сделать кружок: {}": "⚠️ Could not make the circle: {}",
    "⚠️ Видео не найдено. Отправьте файл заново.": "⚠️ Video not found. Please send the file again.",
    # --- Админ ---
    "🛠 Админ-панель": "🛠 Admin panel",
    "👥 Всего пользователей: {}": "👥 Total users: {}",
    "🕒 Были активны за 24 часа: {}": "🕒 Active in the last 24 hours: {}",
    "🆕 Новых за 24 часа: {}": "🆕 New in the last 24 hours: {}",
    "Последние 20 пользователей (Имя | @username | ID | Дата):": "Last 20 users (Name | @username | ID | Date):",
    "Пользователей пока нет.": "No users yet.",
    "Полная база пользователей Klyx AI.": "Full Klyx AI user database.",
    # --- Ошибки сервисов (ai_service / web_server) ---
    "Пустой промт.": "Empty prompt.",
    "Не переданы байты изображения.": "No image bytes provided.",
    "Бесплатный сервис картинок сейчас перегружен. Попробуйте ещё раз через минуту.": "The free image service is overloaded right now. Please try again in a minute.",
    "Пустая тема для мема.": "Empty meme topic.",
    "Не удалось придумать мем. Попробуйте другую тему.": "Could not come up with a meme. Try another topic.",
    "Не удалось открыть изображение: {}": "Could not open the image: {}",
    "Не найден шрифт с кириллицей. Положите файл DejaVuSans-Bold.ttf в папку fonts или задайте MEME_FONT_PATH.": "No font with Cyrillic support found. Put DejaVuSans-Bold.ttf into the fonts folder or set MEME_FONT_PATH.",
    "Пустой аудиофайл.": "Empty audio file.",
    "Не удалось получить голосовое сообщение из этого файла.": "Could not get a voice message from this file.",
    "Не удалось конвертировать аудио: {}": "Could not convert the audio: {}",
    "Groq Whisper вернул пустой текст.": "Groq Whisper returned empty text.",
    "Сетевая ошибка Groq: {}": "Groq network error: {}",
    "Не удалось создать Word-документ: {}": "Could not create the Word document: {}",
    "Не удалось собрать .pptx: {}": "Could not build the .pptx: {}",
    "Пустая тема для презентации.": "Empty presentation topic.",
    "Нейросеть вернула JSON не в виде объекта.": "The model returned JSON that is not an object.",
    "Не удалось получить структуру презентации от нейросети. Попробуйте ещё раз.": "Could not get the presentation structure from the model. Please try again.",
    "Не удалось обработать видео: {}": "Could not process the video: {}",
    "Видео слишком длинное или повреждено.": "The video is too long or damaged.",
    "Все бесплатные модели недоступны: {}": "All free models are unavailable: {}",
    "Ошибка ИИ-сервиса: {}": "AI service error: {}",
    "Пустое сообщение.": "Empty message.",
    "Суточный лимит исчерпан. Осталось: {}.": "Daily limit reached. Remaining: {}.",
    "Достигнут лимит фактов ({}).": "Fact limit reached ({}).",
    "Факт слишком длинный (максимум 500 символов).": "The fact is too long (500 characters max).",
    "Пустой факт.": "Empty fact.",
    "Не удалось сохранить факт.": "Could not save the fact.",
    "Факт не найден.": "Fact not found.",
    "Некорректный fact_id.": "Invalid fact_id.",
    "Не передано изображение.": "No image provided.",
    "Лимит редактирования фото исчерпан (10 в сутки). Осталось: {}.": "Photo editing limit reached (10 per day). Remaining: {}.",
    "Пустая тема.": "Empty topic.",
    "Не передан аудиофайл.": "No audio file provided.",
    "Некорректные данные аудио.": "Invalid audio data.",
    "Файл слишком большой. Максимум {} МБ.": "The file is too large. Maximum {} MB.",
    "Бот не может написать вам. Откройте чат с ботом и нажмите /start.": "The bot cannot message you. Open the chat with the bot and press /start.",
    "Telegram не принял голосовое сообщение. Проверьте настройки приватности голосовых.": "Telegram did not accept the voice message. Check your voice message privacy settings.",
    "Достигнут лимит задач ({}).": "Task limit reached ({}).",
    "Задача длиннее 300 символов.": "The task is longer than 300 characters.",
    "Пустая задача.": "Empty task.",
    "Не удалось добавить задачу.": "Could not add the task.",
    "Некорректный task_id.": "Invalid task_id.",
    "Некорректный язык.": "Invalid language.",
    "Лимит видео на сегодня исчерпан ({} в сутки).": "Today's video limit is reached ({} per day).",
    "Не передано видео.": "No video provided.",
    "Не передан промт.": "No prompt provided.",
    "Некорректное число слайдов (нужно от 3 до 15).": "Invalid number of slides (3 to 15 required).",
    "Предыдущая задача ещё выполняется. Дождитесь результата.": "The previous task is still running. Please wait for the result.",
    "Видео создаётся, отправлю в чат, как только будет готово.": "Your video is being created, I'll send it to the chat as soon as it's ready.",
}

# Подписи кнопок reply-меню, для которых нужна обратная карта EN -> RU (нажатия)
_REPLY_BUTTON_KEYS = (
    "🔄 Новый диалог", "🤖 Модель", "🎭 Роль", "⚔️ Битва ИИ", "🎨 Генерация фото",
    "😂 Мемы", "✅ Список задач", "📄 Создать Word-отчет", "📊 Презентация",
    "📊 Лимиты и статус", "👤 Профиль и память", "❓ Возможности", "🎬 Генерация видео",
    "⭕ Видео в кружок", "🎙 Аудио и голос", BTN_ADMIN,
)
EN_TO_RU_BUTTONS: dict[str, str] = {STRINGS_EN[k]: k for k in _REPLY_BUTTON_KEYS}

_PLACEHOLDER = re.compile(r"\{\}")
_TEMPLATES: list[tuple[re.Pattern[str], str]] = []
for _ru, _en in STRINGS_EN.items():
    if "{}" in _ru and "\n" not in _ru:
        _pattern = "^" + _PLACEHOLDER.sub("(.+?)", re.escape(_ru).replace(r"\{\}", "{}")) + "$"
        _TEMPLATES.append((re.compile(_pattern, re.DOTALL), _en))
_TEMPLATES.sort(key=lambda item: -len(item[0].pattern))
_PREFIXES = ("✅ ", "⬜️ ", "❌ ")


def _translate_line(line: str, depth: int = 0) -> str:
    stripped = line.strip()
    if not stripped:
        return line
    direct = STRINGS_EN.get(stripped)
    if direct is not None:
        return line.replace(stripped, direct)
    if depth < 3:
        for pattern, en_template in _TEMPLATES:
            match = pattern.match(stripped)
            if match:
                parts = [_translate_line(group, depth + 1) for group in match.groups()]
                result = en_template
                for part in parts:
                    result = result.replace("{}", part, 1)
                return line.replace(stripped, result)
        for prefix in _PREFIXES:
            if stripped.startswith(prefix):
                rest = stripped[len(prefix):]
                translated = _translate_line(rest, depth + 1)
                if translated != rest:
                    return line.replace(stripped, prefix + translated)
    return line


def translate_text(text: str) -> str:
    """Переводит RU -> EN по словарю. Неизвестные строки остаются без изменений."""
    if not text:
        return text
    whole = STRINGS_EN.get(text.strip())
    if whole is not None:
        return text.replace(text.strip(), whole)
    return "\n".join(_translate_line(line) for line in text.split("\n"))


def localize(text: str, lang: str | None = None) -> str:
    return translate_text(text) if (lang or current_lang.get()) == "en" else text


# ---------------------------------------------------------------------------
# Переводчик исходящих запросов Bot API
# ---------------------------------------------------------------------------
def _translate_markup(markup: Any) -> Any:
    if isinstance(markup, InlineKeyboardMarkup):
        rows = [
            [button.model_copy(update={"text": translate_text(button.text)}) for button in row]
            for row in markup.inline_keyboard
        ]
        return markup.model_copy(update={"inline_keyboard": rows})
    if isinstance(markup, ReplyKeyboardMarkup):
        rows = [
            [button.model_copy(update={"text": translate_text(button.text)}) for button in row]
            for row in markup.keyboard
        ]
        return markup.model_copy(update={"keyboard": rows})
    return markup


def _inject_admin_button(markup: Any) -> Any:
    """Добавляет кнопку админ-панели в главное меню (только для ADMIN_ID)."""
    if not isinstance(markup, ReplyKeyboardMarkup):
        return markup
    flat = [button.text for row in markup.keyboard for button in row if hasattr(button, "text")]
    if "🔄 Новый диалог" not in flat or BTN_ADMIN in flat:
        return markup
    rows = [list(row) for row in markup.keyboard] + [[KeyboardButton(text=BTN_ADMIN)]]
    return markup.model_copy(update={"keyboard": rows})


class TranslateRequestMiddleware(BaseRequestMiddleware):
    """Переводит тексты, подписи и кнопки всех исходящих запросов для EN-пользователей."""

    async def __call__(self, make_request, bot, method):  # type: ignore[no-untyped-def]
        try:
            markup = getattr(method, "reply_markup", None)
            if markup is not None and config.ADMIN_ID and current_user_id.get() == config.ADMIN_ID:
                markup = _inject_admin_button(markup)
                method.reply_markup = markup
            if current_lang.get() == "en":
                for attr in ("text", "caption"):
                    value = getattr(method, attr, None)
                    if isinstance(value, str) and value:
                        setattr(method, attr, translate_text(value))
                if markup is not None:
                    method.reply_markup = _translate_markup(markup)
        except Exception:
            logger.exception("Не удалось локализовать исходящий запрос")
        return await make_request(bot, method)


# ---------------------------------------------------------------------------
# Входящие апдейты
# ---------------------------------------------------------------------------
class UserContextMiddleware(BaseMiddleware):
    """Фиксирует пользователя (регистрация, последний вход) и выставляет язык для апдейта."""

    async def __call__(self, handler, event, data):  # type: ignore[no-untyped-def]
        user = data.get("event_from_user")
        if user is not None and not getattr(user, "is_bot", False):
            try:
                lang = await db.touch_user(user.id, user.username, user.first_name)
            except Exception:
                logger.exception("touch_user не удался")
                lang = db.cached_lang(user.id)
            current_lang.set(lang)
            current_user_id.set(user.id)
        return await handler(event, data)


class IncomingButtonMiddleware(BaseMiddleware):
    """Приводит нажатия английских кнопок reply-меню к русским подписям для обработчиков."""

    async def __call__(self, handler, event, data):  # type: ignore[no-untyped-def]
        if isinstance(event, Message) and event.text:
            ru = EN_TO_RU_BUTTONS.get(event.text.strip())
            if ru is not None:
                event = event.model_copy(update={"text": ru})
        return await handler(event, data)


def language_name(lang: str) -> str:
    return "English" if lang == "en" else "Русский"

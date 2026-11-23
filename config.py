# config.py
"""
Конфигурация проекта Klyx AI.
Все секреты и изменяемые параметры читаются из переменных окружения (.env).
"""

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value.strip() == ""):
        raise RuntimeError(
            f"Не задана обязательная переменная окружения: {name}. "
            f"Добавьте её в .env или в панели Bothost."
        )
    return value.strip() if isinstance(value, str) else value


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть числом, получено: {raw!r}") from exc


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
BOT_TOKEN: Final[str] = _get_env("BOT_TOKEN", required=True)
# Telegram ID администратора (команда /admin). 0 — админ-панель отключена.
ADMIN_ID: Final[int] = _get_int("ADMIN_ID", default=0)

# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY: Final[str] = _get_env("OPENROUTER_API_KEY", required=True)
OPENROUTER_BASE_URL: Final[str] = _get_env(
    "OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1"
)
OPENROUTER_SITE_URL: Final[str] = _get_env("OPENROUTER_SITE_URL", default="https://github.com/klyx-ai")
OPENROUTER_APP_NAME: Final[str] = _get_env("OPENROUTER_APP_NAME", default="Klyx AI")

_DEFAULT_FREE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "inclusionai/ling-3.0-flash:free",
    "poolside/laguna-m.1:free",
    "poolside/laguna-s-2.1:free",
)
_raw_models = os.getenv("OPENROUTER_FREE_MODELS", "")
if _raw_models.strip():
    FREE_MODELS: Final[tuple[str, ...]] = tuple(
        model.strip() for model in _raw_models.split(",") if model.strip()
    )
else:
    FREE_MODELS: Final[tuple[str, ...]] = _DEFAULT_FREE_MODELS

# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
GROQ_API_KEY: Final[str] = _get_env("GROQ_API_KEY", required=True)
GROQ_BASE_URL: Final[str] = _get_env("GROQ_BASE_URL", default="https://api.groq.com/openai/v1")
GROQ_WHISPER_MODEL: Final[str] = _get_env("GROQ_WHISPER_MODEL", default="whisper-large-v3-turbo")

# ---------------------------------------------------------------------------
# Pollinations.ai
# ---------------------------------------------------------------------------
POLLINATIONS_IMAGE_URL: Final[str] = "https://image.pollinations.ai/prompt/"
POLLINATIONS_MODEL_TXT2IMG: Final[str] = _get_env("POLLINATIONS_MODEL_TXT2IMG", default="flux")
POLLINATIONS_MODEL_IMG2IMG: Final[str] = _get_env("POLLINATIONS_MODEL_IMG2IMG", default="kontext")
POLLINATIONS_TIMEOUT: Final[float] = _get_float("POLLINATIONS_TIMEOUT", default=120.0)

# ---------------------------------------------------------------------------
# Hugging Face (бесплатный Inference API): видео, картинки, Vision
# ---------------------------------------------------------------------------
HF_TOKEN: Final[str] = _get_env("HF_TOKEN", default="")


def _get_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


# Модели text-to-video через InferenceClient.text_to_video (перебираются по очереди)
HF_VIDEO_MODELS: Final[tuple[str, ...]] = _get_list(
    "HF_VIDEO_MODELS",
    (
        "Wan-AI/Wan2.1-T2V-1.3B",
        "Lightricks/LTX-Video",
        "ali-vilab/text-to-video-ms-1.7b",
    ),
)
# Второй эшелон: ZeroGPU Spaces через gradio_client (перебираются по очереди)
HF_VIDEO_SPACES: Final[tuple[str, ...]] = _get_list(
    "HF_VIDEO_SPACES",
    ("Lightricks/ltx-video-distilled", "KingNish/Instant-Video"),
)
# Если пусто — эндпоинт Space подбирается автоматически по описанию его API
HF_VIDEO_API_NAME: Final[str] = _get_env("HF_VIDEO_API_NAME", default="")
HF_VIDEO_TIMEOUT: Final[float] = _get_float("HF_VIDEO_TIMEOUT", default=150.0)
HF_VIDEO_TOTAL_TIMEOUT: Final[float] = _get_float("HF_VIDEO_TOTAL_TIMEOUT", default=420.0)
DAILY_VIDEO_LIMIT: Final[int] = _get_int("DAILY_VIDEO_LIMIT", default=2)

# Картинки (слайды, мемы, фото по тексту): HF FLUX -> Wikimedia / Pollinations
HF_IMAGE_MODEL: Final[str] = _get_env("HF_IMAGE_MODEL", default="black-forest-labs/FLUX.1-schnell")

# Анализ фото (Vision): бесплатные :free модели OpenRouter, запасной путь — HF InferenceClient
VISION_MODELS: Final[tuple[str, ...]] = _get_list(
    "VISION_MODELS",
    (
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.2-11b-vision-instruct:free",
        "qwen/qwen2.5-vl-72b-instruct:free",
    ),
)
HF_VISION_MODEL: Final[str] = _get_env("HF_VISION_MODEL", default="meta-llama/Llama-3.2-11B-Vision-Instruct")
VISION_MAX_SIDE: Final[int] = _get_int("VISION_MAX_SIDE", default=1280)

# ---------------------------------------------------------------------------
# Временный хостинг изображений (для img2img)
# ---------------------------------------------------------------------------
CATBOX_API_URL: Final[str] = "https://catbox.moe/user/api.php"
TMPFILES_API_URL: Final[str] = "https://tmpfiles.org/api/v1/upload"

# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------
DAILY_USER_LIMIT: Final[int] = _get_int("DAILY_USER_LIMIT", default=50)
DAILY_IMG2IMG_LIMIT: Final[int] = _get_int("DAILY_IMG2IMG_LIMIT", default=10)
GLOBAL_REQUESTS_PER_MINUTE: Final[int] = _get_int("GLOBAL_REQUESTS_PER_MINUTE", default=20)
MAX_CONCURRENT_AI_REQUESTS: Final[int] = _get_int("MAX_CONCURRENT_AI_REQUESTS", default=3)
MAX_TASKS_PER_USER: Final[int] = _get_int("MAX_TASKS_PER_USER", default=50)

# ---------------------------------------------------------------------------
# Таймауты
# ---------------------------------------------------------------------------
OPENROUTER_TIMEOUT: Final[float] = _get_float("OPENROUTER_TIMEOUT", default=90.0)
GROQ_TIMEOUT: Final[float] = _get_float("GROQ_TIMEOUT", default=60.0)
TELEGRAM_REQUEST_TIMEOUT: Final[float] = _get_float("TELEGRAM_REQUEST_TIMEOUT", default=60.0)

# ---------------------------------------------------------------------------
# Ограничения на файлы
# ---------------------------------------------------------------------------
MAX_TXT_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_TXT_FILE_SIZE_BYTES", default=2 * 1024 * 1024)
MAX_PDF_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_PDF_FILE_SIZE_BYTES", default=10 * 1024 * 1024)
MAX_PDF_PAGES: Final[int] = _get_int("MAX_PDF_PAGES", default=50)
MAX_HISTORY_MESSAGES: Final[int] = _get_int("MAX_HISTORY_MESSAGES", default=20)
MAX_MEMORY_FACTS: Final[int] = _get_int("MAX_MEMORY_FACTS", default=20)
MAX_IMAGE_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_IMAGE_FILE_SIZE_BYTES", default=10 * 1024 * 1024)
# Telegram позволяет боту скачивать файлы размером до 20 МБ
MAX_AUDIO_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_AUDIO_FILE_SIZE_BYTES", default=20 * 1024 * 1024)

# Видео для кружка (Mini App принимает файлы большего размера, чем бот: бот ограничен Telegram 20 МБ)
MAX_VIDEO_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_VIDEO_FILE_SIZE_BYTES", default=50 * 1024 * 1024)
VIDEO_NOTE_SIZE: Final[int] = _get_int("VIDEO_NOTE_SIZE", default=384)

# ---------------------------------------------------------------------------
# Презентации
# ---------------------------------------------------------------------------
PPTX_MIN_SLIDES: Final[int] = 3
PPTX_MAX_SLIDES: Final[int] = 15

# ---------------------------------------------------------------------------
# Конвертер аудио в голосовое (OGG / Opus)
# ---------------------------------------------------------------------------
VOICE_BITRATE: Final[str] = _get_env("VOICE_BITRATE", default="64k")

# ---------------------------------------------------------------------------
# Mini App / веб-сервер
# ---------------------------------------------------------------------------
WEBAPP_PORT: Final[int] = _get_int("WEBAPP_PORT", default=8080)
WEBAPP_URL: Final[str] = _get_env("WEBAPP_URL", default="").rstrip("/")

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
_DEFAULT_DB_PATH = BASE_DIR / "database.db"
DATABASE_PATH: Final[str] = _get_env("DATABASE_PATH", default=str(_DEFAULT_DB_PATH))

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
LOG_LEVEL: Final[str] = _get_env("LOG_LEVEL", default="INFO").upper()

# ---------------------------------------------------------------------------
# Мемы
# ---------------------------------------------------------------------------
FONTS_DIR: Final[Path] = BASE_DIR / "fonts"
MEME_FONT_PATH: Final[str] = _get_env("MEME_FONT_PATH", default="")
MEME_FONT_SIZE: Final[int] = _get_int("MEME_FONT_SIZE", default=64)
MEME_IMAGE_SIZE: Final[int] = _get_int("MEME_IMAGE_SIZE", default=1024)

# ---------------------------------------------------------------------------
# Word-отчеты
# ---------------------------------------------------------------------------
WORD_REPORT_MAX_TOKENS: Final[int] = _get_int("WORD_REPORT_MAX_TOKENS", default=3000)

# ---------------------------------------------------------------------------
# Системный промт: чистый русский без мусора
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_CLEAN_RUSSIAN: Final[str] = (
    "Отвечай строго на чистом русском языке. "
    "Не вставляй случайные английские слова, кроме названий брендов, "
    "общепринятых технических терминов или кода, если пользователь явно просит код. "
    "ЗАПРЕЩЕНО использовать символы * и # для оформления: никаких звёздочек для выделения "
    "и списков, никаких решёток для заголовков. "
    "Заголовки пиши обычной строкой (можно с двоеточием на конце), "
    "списки оформляй символом «•» или «—», а нумерованные — как «1.», «2.». "
    "Блоки кода в тройных обратных кавычках (```) используй только если пользователь "
    "просит написать код."
)

SYSTEM_PROMPT_CLEAN_ENGLISH: Final[str] = (
    "Always answer strictly in clear English. "
    "It is FORBIDDEN to use the characters * and # for formatting: no asterisks for emphasis "
    "or bullet lists, no hash signs for headings. "
    "Write headings as a plain line (optionally ending with a colon), "
    "format bullet lists with the symbol \"•\" or \"—\" and numbered lists as \"1.\", \"2.\". "
    "Use triple-backtick code blocks (```) only when the user asks you to write code."
)

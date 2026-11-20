# config.py
"""
Конфигурация проекта Klyx AI.
Все секреты и изменяемые параметры читаются из переменных окружения (.env).
"""

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Загружаем .env из корня проекта (если файл существует)
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    """Безопасное чтение переменной окружения с понятной ошибкой."""
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

# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY: Final[str] = _get_env("OPENROUTER_API_KEY", required=True)
OPENROUTER_BASE_URL: Final[str] = _get_env(
    "OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1"
)
OPENROUTER_SITE_URL: Final[str] = _get_env("OPENROUTER_SITE_URL", default="https://github.com/klyx-ai")
OPENROUTER_APP_NAME: Final[str] = _get_env("OPENROUTER_APP_NAME", default="Klyx AI")

# Список бесплатных моделей OpenRouter (можно переопределить через env, но по умолчанию — эти)
# config.py
_DEFAULT_FREE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "baidu/ernie-5.5-flash-preview:free",
)
_raw_models = os.getenv("OPENROUTER_FREE_MODELS", "")
if _raw_models.strip():
    FREE_MODELS: Final[tuple[str, ...]] = tuple(
        model.strip() for model in _raw_models.split(",") if model.strip()
    )
else:
    FREE_MODELS: Final[tuple[str, ...]] = _DEFAULT_FREE_MODELS

# ---------------------------------------------------------------------------
# Groq (Whisper и быстрые ответы)
# ---------------------------------------------------------------------------
GROQ_API_KEY: Final[str] = _get_env("GROQ_API_KEY", required=True)
GROQ_BASE_URL: Final[str] = _get_env("GROQ_BASE_URL", default="https://api.groq.com/openai/v1")
GROQ_WHISPER_MODEL: Final[str] = _get_env("GROQ_WHISPER_MODEL", default="whisper-large-v3-turbo")

# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------
# Суточный лимит на одного пользователя (сбрасывается раз в 24 часа от первого запроса)
DAILY_USER_LIMIT: Final[int] = _get_int("DAILY_USER_LIMIT", default=50)

# Глобальный лимит запросов к API в минуту (защита бесплатного ключа)
GLOBAL_REQUESTS_PER_MINUTE: Final[int] = _get_int("GLOBAL_REQUESTS_PER_MINUTE", default=20)

# Максимальное количество одновременных запросов к ИИ (семафор)
MAX_CONCURRENT_AI_REQUESTS: Final[int] = _get_int("MAX_CONCURRENT_AI_REQUESTS", default=3)

# ---------------------------------------------------------------------------
# Таймауты (секунды)
# ---------------------------------------------------------------------------
OPENROUTER_TIMEOUT: Final[float] = _get_float("OPENROUTER_TIMEOUT", default=90.0)
GROQ_TIMEOUT: Final[float] = _get_float("GROQ_TIMEOUT", default=60.0)
TELEGRAM_REQUEST_TIMEOUT: Final[float] = _get_float("TELEGRAM_REQUEST_TIMEOUT", default=60.0)

# ---------------------------------------------------------------------------
# Ограничения на файлы
# ---------------------------------------------------------------------------
MAX_TXT_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_TXT_FILE_SIZE_BYTES", default=2 * 1024 * 1024)  # 2 MB
MAX_PDF_FILE_SIZE_BYTES: Final[int] = _get_int("MAX_PDF_FILE_SIZE_BYTES", default=10 * 1024 * 1024)  # 10 MB
MAX_PDF_PAGES: Final[int] = _get_int("MAX_PDF_PAGES", default=50)
MAX_HISTORY_MESSAGES: Final[int] = _get_int("MAX_HISTORY_MESSAGES", default=20)
MAX_MEMORY_FACTS: Final[int] = _get_int("MAX_MEMORY_FACTS", default=20)

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
_DEFAULT_DB_PATH = BASE_DIR / "database.db"
DATABASE_PATH: Final[str] = _get_env("DATABASE_PATH", default=str(_DEFAULT_DB_PATH))

# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------
DEFAULT_MODEL: Final[str] = FREE_MODELS[0]
LOG_LEVEL: Final[str] = _get_env("LOG_LEVEL", default="INFO").upper()

# ai_service.py
"""
Сервис взаимодействия с ИИ-провайдерами.

Реализует:
  - запросы к OpenRouter с fallback между бесплатными моделями;
  - транскрипцию голосовых через Groq Whisper;
  - оценку и фактический замер времени обработки;
  - защиту от параллельной перегрузки через семафор.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from config import (
    FREE_MODELS,
    GLOBAL_REQUESTS_PER_MINUTE,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_TIMEOUT,
    GROQ_WHISPER_MODEL,
    MAX_CONCURRENT_AI_REQUESTS,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_NAME,
    OPENROUTER_BASE_URL,
    OPENROUTER_SITE_URL,
    OPENROUTER_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Глобальный семафор: ограничивает число одновременных тяжёлых запросов к ИИ.
_AI_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_AI_REQUESTS)

# Простой «мягкий» троттлинг: не более GLOBAL_REQUESTS_PER_MINUTE запросов в минуту.
_RATE_LOCK = asyncio.Lock()
_RATE_TIMESTAMPS: list[float] = []


class AIServiceError(RuntimeError):
    """Базовая ошибка сервиса ИИ."""


class AllModelsFailedError(AIServiceError):
    """Все бесплатные модели OpenRouter вернули ошибку."""


class TranscriptionError(AIServiceError):
    """Не удалось расшифровать аудио через Groq."""


# ---------------------------------------------------------------------------
# Вспомогательное: мягкий глобальный rate-limit
# ---------------------------------------------------------------------------
async def _acquire_rate_slot() -> None:
    """
    Не даёт превысить GLOBAL_REQUESTS_PER_MINUTE запросов в минуту.
    Работает в пределах одного процесса (для одного инстанса бота этого достаточно).
    """
    async with _RATE_LOCK:
        now = time.monotonic()
        # Оставляем только отметки за последние 60 секунд
        while _RATE_TIMESTAMPS and now - _RATE_TIMESTAMPS[0] > 60.0:
            _RATE_TIMESTAMPS.pop(0)

        if len(_RATE_TIMESTAMPS) >= GLOBAL_REQUESTS_PER_MINUTE:
            wait_for = 60.0 - (now - _RATE_TIMESTAMPS[0])
            if wait_for > 0:
                logger.info("Глобальный rate-limit: пауза %.2f сек", wait_for)
                await asyncio.sleep(wait_for)

        _RATE_TIMESTAMPS.append(time.monotonic())


# ---------------------------------------------------------------------------
# Оценка времени
# ---------------------------------------------------------------------------
def estimate_processing_seconds(prompt_chars: int, model: str | None = None) -> float:
    """
    Грубая эвристика для оценки времени генерации.
    Возвращает секунды (минимум 2, максимум 60).
    """
    base = 2.0
    # Чем больше промпт — тем дольше обработка. 0.005 сек на символ — усреднённо.
    estimated = base + prompt_chars * 0.005
    # Некоторые модели традиционно медленнее; слегка увеличиваем оценку.
    if model and ("70b" in model.lower() or "72b" in model.lower()):
        estimated *= 1.15
    return round(min(max(estimated, 2.0), 60.0), 1)


def format_seconds(seconds: float) -> str:
    """Человекочитаемое время: '~9 сек' или '~1.2 мин'."""
    if seconds < 60:
        return f"~{int(round(seconds))} сек"
    return f"~{seconds / 60:.1f} мин"


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------
async def _call_openrouter_model(
    session: aiohttp.ClientSession,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    """Один вызов конкретной модели OpenRouter. Возвращает текст ответа."""
    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with session.post(
        url, json=payload, headers=headers, timeout=OPENROUTER_TIMEOUT
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise AIServiceError(
                f"OpenRouter модель {model} вернула HTTP {resp.status}: {body[:300]}"
            )
        data = await resp.json()

    try:
        choices = data["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIServiceError(
            f"Неожиданный формат ответа OpenRouter для модели {model}: {data}"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise AIServiceError(f"Модель {model} вернула пустой ответ.")

    return content.strip()


async def request_ai(
    messages: list[dict[str, str]],
    preferred_model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[str, str, float]:
    """
    Отправляет запрос в OpenRouter с fallback между бесплатными моделями.

    Возвращает:
        (текст_ответа, фактическая_модель, время_выполнения_сек)

    Порядок моделей: сначала preferred_model (если задана и есть в FREE_MODELS),
    затем остальные из FREE_MODELS.
    """
    if not FREE_MODELS:
        raise AIServiceError("Список FREE_MODELS пуст. Проверьте конфигурацию.")

    # Строим порядок перебора моделей
    ordered_models: list[str] = []
    if preferred_model and preferred_model in FREE_MODELS:
        ordered_models.append(preferred_model)
    for model in FREE_MODELS:
        if model not in ordered_models:
            ordered_models.append(model)

    last_error: Exception | None = None
    start = time.monotonic()

    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        async with aiohttp.ClientSession() as session:
            for model in ordered_models:
                try:
                    logger.info("Запрос к OpenRouter: %s", model)
                    text = await _call_openrouter_model(
                        session, model, messages, temperature, max_tokens
                    )
                    elapsed = time.monotonic() - start
                    return text, model, round(elapsed, 2)
                except (aiohttp.ClientError, asyncio.TimeoutError, AIServiceError) as exc:
                    last_error = exc
                    logger.warning("Модель %s недоступна: %s", model, exc)
                    continue

    raise AllModelsFailedError(
        f"Все бесплатные модели недоступны. Последняя ошибка: {last_error}"
    )


# ---------------------------------------------------------------------------
# Groq Whisper
# ---------------------------------------------------------------------------
async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    """
    Расшифровывает аудио через Groq Whisper.
    Возвращает распознанный текст.
    """
    url = f"{GROQ_BASE_URL.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    form = aiohttp.FormData()
    form.add_field("file", file_bytes, filename=filename, content_type="application/octet-stream")
    form.add_field("model", GROQ_WHISPER_MODEL)
    form.add_field("response_format", "json")

    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=form, headers=headers, timeout=GROQ_TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise TranscriptionError(
                            f"Groq Whisper вернул HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TranscriptionError(f"Сетевая ошибка при обращении к Groq: {exc}") from exc

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("Groq Whisper вернул пустой текст.")

    return text.strip()


# ---------------------------------------------------------------------------
# Обёртка для «битвы» и «второго мнения»
# ---------------------------------------------------------------------------
async def request_two_models(
    messages: list[dict[str, str]],
    model_a: str,
    model_b: str,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[tuple[str, str, float], tuple[str, str, float]]:
    """
    Параллельно запрашивает две разные модели (для режима «Битва»).
    Если одна из моделей падает — возвращает ответ второй и текст ошибки для первой.
    """
    async def _safe(model: str) -> tuple[str, str, float]:
        try:
            return await request_ai(
                messages, preferred_model=model, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 — нужно вернуть текст ошибки пользователю
            return f"⚠️ Модель `{model}` недоступна: {exc}", model, 0.0

    result_a, result_b = await asyncio.gather(_safe(model_a), _safe(model_b))
    return result_a, result_b
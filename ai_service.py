# ai_service.py
"""
Сервис взаимодействия с ИИ-провайдерами.

- OpenRouter: текстовые ответы (с переводом промтов для картинок)
- Groq Whisper: транскрипция голоса
- Pollinations.ai: txt2img и img2img через kontext (с upload_temp_image)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.parse
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
    POLLINATIONS_IMAGE_URL,
    POLLINATIONS_MODEL_IMG2IMG,
    POLLINATIONS_MODEL_TXT2IMG,
    POLLINATIONS_TIMEOUT,
    SYSTEM_PROMPT_CLEAN_RUSSIAN,
)

logger = logging.getLogger(__name__)

_AI_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_AI_REQUESTS)

_RATE_LOCK = asyncio.Lock()
_RATE_TIMESTAMPS: list[float] = []


class AIServiceError(RuntimeError):
    pass


class AllModelsFailedError(AIServiceError):
    pass


class TranscriptionError(AIServiceError):
    pass


class ImageGenerationError(AIServiceError):
    pass


class ImageEditingError(AIServiceError):
    pass


def clean_response_text(text: str) -> str:
    """Убирает двойные звёздочки ** из ответа модели."""
    return text.replace("**", "")


async def _acquire_rate_slot() -> None:
    async with _RATE_LOCK:
        now = time.monotonic()
        while _RATE_TIMESTAMPS and now - _RATE_TIMESTAMPS[0] > 60.0:
            _RATE_TIMESTAMPS.pop(0)
        if len(_RATE_TIMESTAMPS) >= GLOBAL_REQUESTS_PER_MINUTE:
            wait_for = 60.0 - (now - _RATE_TIMESTAMPS[0])
            if wait_for > 0:
                logger.info("Глобальный rate-limit: пауза %.2f сек", wait_for)
                await asyncio.sleep(wait_for)
        _RATE_TIMESTAMPS.append(time.monotonic())


def estimate_processing_seconds(prompt_chars: int, model: str | None = None) -> float:
    base = 2.0
    estimated = base + prompt_chars * 0.005
    if model and ("70b" in model.lower() or "72b" in model.lower()):
        estimated *= 1.15
    return round(min(max(estimated, 2.0), 60.0), 1)


def format_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"~{int(round(seconds))} сек"
    return f"~{seconds / 60:.1f} мин"


async def _call_openrouter_model(
    session: aiohttp.ClientSession,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
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
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIServiceError(
            f"Неожиданный формат ответа OpenRouter для модели {model}: {data}"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise AIServiceError(f"Модель {model} вернула пустой ответ.")

    return clean_response_text(content.strip())


async def request_ai(
    messages: list[dict[str, str]],
    preferred_model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[str, str, float]:
    if not FREE_MODELS:
        raise AIServiceError("Список FREE_MODELS пуст. Проверьте конфигурацию.")

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


async def request_two_models(
    messages: list[dict[str, str]],
    model_a: str,
    model_b: str,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[tuple[str, str, float], tuple[str, str, float]]:
    async def _safe(model: str) -> tuple[str, str, float]:
        try:
            return await request_ai(
                messages, preferred_model=model, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            return f"⚠️ Модель `{model}` недоступна: {exc}", model, 0.0

    return await asyncio.gather(_safe(model_a), _safe(model_b))


# ---------------------------------------------------------------------------
# Перевод промта на английский (для Pollinations)
# ---------------------------------------------------------------------------
async def translate_prompt_to_english(user_prompt: str) -> str:
    """
    Переводит русский промт в детальный английский через OpenRouter.
    Если перевод не удался — возвращает исходный текст (чтобы не блокировать генерацию).
    """
    system = (
        "You are a prompt translator for an image generation model. "
        "Translate the user's request into a detailed English image prompt. "
        "Return ONLY the English prompt, without quotes, explanations or extra text. "
        "Keep all visual details, style hints and composition instructions."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    try:
        translated, _model, _elapsed = await request_ai(
            messages, temperature=0.2, max_tokens=400
        )
        translated = translated.strip().strip('"').strip("'")
        if translated:
            return translated
    except Exception as exc:
        logger.warning("Не удалось перевести промт для картинки: %s", exc)
    return user_prompt


# ---------------------------------------------------------------------------
# Загрузка временного изображения на публичный хостинг
# ---------------------------------------------------------------------------
async def _upload_to_catbox(image_bytes: bytes, ext: str = "jpg") -> str:
    url = "https://catbox.moe/user/api.php"
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field(
        "fileToUpload",
        image_bytes,
        filename=f"klyx_input.{ext}",
        content_type=f"image/{ext}",
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=60) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ImageEditingError(
                    f"Catbox вернул HTTP {resp.status}: {body[:200]}"
                )
            link = (await resp.text()).strip()
    if not link.startswith("http"):
        raise ImageEditingError(f"Catbox вернул некорректную ссылку: {link[:200]}")
    return link


async def _upload_to_tmpfiles(image_bytes: bytes, ext: str = "jpg") -> str:
    url = "https://tmpfiles.org/api/v1/upload"
    form = aiohttp.FormData()
    form.add_field(
        "file",
        image_bytes,
        filename=f"klyx_input.{ext}",
        content_type=f"image/{ext}",
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=60) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ImageEditingError(
                    f"Tmpfiles вернул HTTP {resp.status}: {body[:200]}"
                )
            data = await resp.json()

    # Tmpfiles возвращает {"data": {"url": "https://tmpfiles.org/XXXX/filename"}}
    try:
        raw_url = data["data"]["url"]
    except (KeyError, TypeError) as exc:
        raise ImageEditingError(f"Tmpfiles вернул неожиданный ответ: {data}") from exc

    # Прямая ссылка: /dl/ вместо /
    if "/dl/" not in raw_url:
        raw_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    return raw_url


async def upload_temp_image(image_bytes: bytes, ext: str = "jpg") -> str:
    """
    Загружает изображение на бесплатный анонимный хостинг.
    Сначала Catbox, при ошибке — Tmpfiles.
    Возвращает короткую публичную HTTPS-ссылку.
    """
    if not image_bytes:
        raise ImageEditingError("Пустые байты изображения.")

    try:
        return await _upload_to_catbox(image_bytes, ext)
    except Exception as exc:
        logger.warning("Catbox не сработал (%s), пробуем Tmpfiles", exc)

    try:
        return await _upload_to_tmpfiles(image_bytes, ext)
    except Exception as exc:
        logger.error("Tmpfiles тоже не сработал: %s", exc)
        raise ImageEditingError(
            "Не удалось загрузить изображение на временный хостинг. Попробуйте позже."
        ) from exc


# ---------------------------------------------------------------------------
# Pollinations: txt2img
# ---------------------------------------------------------------------------
async def generate_image_txt2img(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
) -> bytes:
    if not prompt.strip():
        raise ImageGenerationError("Пустой промт для генерации изображения.")

    en_prompt = await translate_prompt_to_english(prompt)
    encoded = urllib.parse.quote(en_prompt, safe="")
    seed = random.randint(1, 999_999_999)

    params: dict[str, Any] = {
        "model": POLLINATIONS_MODEL_TXT2IMG,
        "width": width,
        "height": height,
        "nologo": "true",
        "seed": seed,
        "private": "true",
    }
    url = f"{POLLINATIONS_IMAGE_URL}{encoded}"

    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=POLLINATIONS_TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise ImageGenerationError(
                            f"Pollinations вернул HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ImageGenerationError(f"Сетевая ошибка Pollinations: {exc}") from exc

    if not data or len(data) < 1024:
        raise ImageGenerationError("Pollinations вернул пустое или слишком маленькое изображение.")
    return data


# ---------------------------------------------------------------------------
# Pollinations: img2img (kontext) с загрузкой фото на хостинг
# ---------------------------------------------------------------------------
async def edit_image_img2img(
    prompt: str,
    image_bytes: bytes,
    width: int = 1024,
    height: int = 1024,
) -> bytes:
    """
    Редактирует изображение по промту через Pollinations kontext.
    Сначала загружает байты на публичный хостинг, чтобы не упереться
    в лимит длины URL Cloudflare (HTTP 414).
    """
    if not prompt.strip():
        raise ImageEditingError("Пустой промт для редактирования.")
    if not image_bytes:
        raise ImageEditingError("Не переданы байты изображения.")

    # 1) Загружаем фото на короткую HTTPS-ссылку
    public_url = await upload_temp_image(image_bytes)

    # 2) Переводим инструкцию пользователя на английский
    en_prompt = await translate_prompt_to_english(prompt)
    encoded = urllib.parse.quote(en_prompt, safe="")
    seed = random.randint(1, 999_999_999)

    params: dict[str, Any] = {
        "model": POLLINATIONS_MODEL_IMG2IMG,
        "image": public_url,
        "width": width,
        "height": height,
        "nologo": "true",
        "seed": seed,
        "private": "true",
    }
    url = f"{POLLINATIONS_IMAGE_URL}{encoded}"

    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=POLLINATIONS_TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise ImageEditingError(
                            f"Pollinations kontext вернул HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ImageEditingError(f"Сетевая ошибка Pollinations kontext: {exc}") from exc

    if not data or len(data) < 1024:
        raise ImageEditingError("Pollinations kontext вернул пустое изображение.")
    return data


# ---------------------------------------------------------------------------
# Groq Whisper
# ---------------------------------------------------------------------------
async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    url = f"{GROQ_BASE_URL.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = aiohttp.FormData()
    form.add_field(
        "file", file_bytes, filename=filename, content_type="application/octet-stream"
    )
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
            raise TranscriptionError(f"Сетевая ошибка Groq: {exc}") from exc

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("Groq Whisper вернул пустой текст.")
    return text.strip()


def get_system_prompt() -> str:
    return SYSTEM_PROMPT_CLEAN_RUSSIAN
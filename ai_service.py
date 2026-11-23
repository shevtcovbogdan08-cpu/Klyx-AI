# ai_service.py
"""
Сервис взаимодействия с ИИ-провайдерами.

- OpenRouter (бесплатные :free модели): ответы, улучшение промтов, идеи мемов, отчёты, презентации
- Pollinations.ai (Flux, без ключа): txt2img, img2img (перегенерация сцены) и мемы (+ Pillow)
- Groq Whisper: транскрипция голоса
- pydub + imageio-ffmpeg: конвертация аудио в голосовое сообщение (OGG / Opus)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from config import (
    BASE_DIR,
    FONTS_DIR,
    FREE_MODELS,
    GLOBAL_REQUESTS_PER_MINUTE,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_TIMEOUT,
    GROQ_WHISPER_MODEL,
    HF_IMAGE_MODEL,
    HF_TOKEN,
    HF_VISION_MODEL,
    MAX_CONCURRENT_AI_REQUESTS,
    MEME_FONT_PATH,
    MEME_IMAGE_SIZE,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_NAME,
    OPENROUTER_BASE_URL,
    OPENROUTER_SITE_URL,
    OPENROUTER_TIMEOUT,
    PPTX_MAX_SLIDES,
    PPTX_MIN_SLIDES,
    POLLINATIONS_IMAGE_URL,
    POLLINATIONS_MODEL_TXT2IMG,
    POLLINATIONS_TIMEOUT,
    SYSTEM_PROMPT_CLEAN_ENGLISH,
    SYSTEM_PROMPT_CLEAN_RUSSIAN,
    VISION_MAX_SIDE,
    VOICE_BITRATE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vision: бесплатные модели OpenRouter + запасные модели Hugging Face
# (список OpenRouter можно переопределить переменной окружения VISION_MODELS)
# ---------------------------------------------------------------------------
_DEFAULT_VISION_MODELS: tuple[str, ...] = (
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen-2-vl-7b-instruct:free",
    "google/gemma-3-27b-it:free",
)
VISION_MODELS: tuple[str, ...] = tuple(
    item.strip() for item in os.getenv("VISION_MODELS", "").split(",") if item.strip()
) or _DEFAULT_VISION_MODELS

HF_VISION_MODELS: tuple[str, ...] = tuple(
    dict.fromkeys(
        ("Qwen/Qwen2.5-VL-7B-Instruct", HF_VISION_MODEL, "meta-llama/Llama-3.2-11B-Vision-Instruct")
    )
)


def call_guarded(function: Any, *args: Any) -> Any:
    """
    Вызывает синхронную функцию (InferenceClient / gradio_client) внутри asyncio.to_thread.
    StopIteration нельзя пробрасывать в Future (Python 3.11: TypeError), поэтому он
    превращается в обычную RuntimeError.
    """
    try:
        return function(*args)
    except StopIteration as exc:
        raise RuntimeError("No inference provider available") from exc

_AI_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_AI_REQUESTS)
_AUDIO_SEMAPHORE = asyncio.Semaphore(2)

AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".aac", ".flac",
)

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


class MemeGenerationError(AIServiceError):
    pass


class WordReportError(AIServiceError):
    pass


class PptxGenerationError(AIServiceError):
    pass


class AudioConversionError(AIServiceError):
    pass


class VisionError(AIServiceError):
    pass


_FENCE_RE = re.compile(r"(```[\s\S]*?(?:```|\Z))")
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+(.*?))?\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[*+]\s+(?=\S)")
_RULE_RE = re.compile(r"^\s*(?:\*{3,}|_{3,}|(?:\*\s+){2,}\*)\s*$")
_BOLD_RE = re.compile(r"(\*{2,3})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")


def _clean_prose(chunk: str) -> str:
    lines: list[str] = []
    for line in chunk.split("\n"):
        if _RULE_RE.match(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            line = (heading.group(1) or "").strip()
        else:
            line = _BULLET_RE.sub(r"\1• ", line)
        lines.append(line)
    text = "\n".join(lines)

    pieces = _INLINE_CODE_RE.split(text)
    for index in range(0, len(pieces), 2):  # чётные — не код, нечётные — `inline code`
        piece = _BOLD_RE.sub(r"\2", pieces[index])
        piece = _ITALIC_STAR_RE.sub(r"\1", piece)
        pieces[index] = piece.replace("**", "")
    text = "".join(pieces)
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_markdown(text: str) -> str:
    """Убирает Markdown-разметку (*, **, #, ##, ###), сохраняя блоки кода ``` как есть."""
    if not text:
        return text
    parts = _FENCE_RE.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _clean_prose(parts[index])
    return "".join(parts).strip("\n") if text.strip() else text


def clean_response_text(text: str) -> str:
    return clean_markdown(text)


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
# Перевод промта
# ---------------------------------------------------------------------------
async def translate_prompt_to_english(user_prompt: str) -> str:
    system = (
        "You are a prompt translator for an image generation model. "
        "Translate the user's request into a detailed English image prompt. "
        "Return ONLY the English prompt, without quotes, explanations or extra text."
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
        logger.warning("Не удалось перевести промт: %s", exc)
    return user_prompt


# ---------------------------------------------------------------------------
# Бесплатные изображения: улучшение промта (бесплатная текстовая модель) + Pollinations Flux
# ---------------------------------------------------------------------------
_IMAGE_SIZE = 1024
_IMAGE_MIN_BYTES = 1024
_MAX_PROMPT_CHARS = 900
_QUALITY_TAGS = (
    "masterpiece, ultra detailed, 8k resolution, sharp focus, "
    "vivid lighting, high contrast, no blur"
)
_POLLINATIONS_LOCK = asyncio.Lock()
_POLLINATIONS_RETRY_DELAYS = (15.0, 20.0)

_ENHANCER_SYSTEM = (
    "You are an expert prompt engineer for the Flux image generation model. "
    "Rewrite the user's request (it may be in Russian) into ONE detailed artistic image prompt "
    "in English: subject, composition, style, colors, lighting, camera and lens details. "
    "Keep the user's intent exactly. Do not put any text, letters or captions into the image. "
    f"End the prompt with these tags: {_QUALITY_TAGS}. "
    "Return ONLY the prompt as a single paragraph, without quotes, markdown or explanations."
)


def guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def image_to_data_url(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{guess_image_mime(data)};base64,{encoded}"


def _looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data[:3] == b"\xff\xd8\xff"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _with_quality_tags(prompt: str) -> str:
    """Гарантирует, что промт заканчивается тегами резкости и не превышает лимит длины URL."""
    base = " ".join(prompt.split()).strip().strip("\"'").strip()
    if base.lower().endswith(_QUALITY_TAGS):
        base = base[: -len(_QUALITY_TAGS)]
    base = base.rstrip(" ,.")
    limit = _MAX_PROMPT_CHARS - len(_QUALITY_TAGS) - 2
    if len(base) > limit:
        base = base[:limit].rsplit(" ", 1)[0].rstrip(" ,.")
    return f"{base}, {_QUALITY_TAGS}" if base else _QUALITY_TAGS


async def enhance_image_prompt(user_request: str, context: str = "") -> str:
    """Превращает запрос пользователя в детальный английский промт через бесплатную модель."""
    system = _ENHANCER_SYSTEM if not context else f"{_ENHANCER_SYSTEM} {context}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_request},
    ]
    try:
        text, _model, _elapsed = await request_ai(messages, temperature=0.7, max_tokens=300)
        if len(text.strip()) >= 10:
            return _with_quality_tags(text)
    except Exception as exc:
        logger.warning("Не удалось улучшить промт, используем исходный: %s", exc)
    return _with_quality_tags(user_request)


async def _fetch_pollinations_image(
    prompt: str,
    error_cls: type[AIServiceError],
    size: int = _IMAGE_SIZE,
    retry_delays: tuple[float, ...] = _POLLINATIONS_RETRY_DELAYS,
) -> bytes:
    url = f"{POLLINATIONS_IMAGE_URL}{urllib.parse.quote(prompt, safe='')}"
    timeout = aiohttp.ClientTimeout(total=POLLINATIONS_TIMEOUT)
    attempts = len(retry_delays) + 1
    last_error = "неизвестная ошибка"

    for attempt in range(attempts):
        params: dict[str, Any] = {
            "width": size,
            "height": size,
            "model": POLLINATIONS_MODEL_TXT2IMG,
            "nologo": "true",
            "seed": random.randint(1, 999_999_999),
            "private": "true",
        }
        fatal = False
        try:
            # Анонимный тариф Pollinations ограничен по частоте — запросы идут строго по очереди.
            async with _POLLINATIONS_LOCK:
                await _acquire_rate_slot()
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        url, params=params, headers={"User-Agent": "KlyxAI/1.0"}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if len(data) >= _IMAGE_MIN_BYTES and _looks_like_image(data):
                                return data
                            last_error = "пустой или некорректный ответ"
                        else:
                            body = await resp.text()
                            last_error = f"HTTP {resp.status}: {body[:200]}"
                            fatal = resp.status not in (402, 429, 500, 502, 503, 504)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = f"сетевая ошибка: {exc}"

        logger.warning(
            "Pollinations (попытка %s/%s): %s", attempt + 1, attempts, last_error
        )
        if fatal or attempt == attempts - 1:
            break
        await asyncio.sleep(retry_delays[attempt])

    raise error_cls(
        "Бесплатный сервис картинок сейчас перегружен. Попробуйте ещё раз через минуту."
    )


# ---------------------------------------------------------------------------
# Каскад получения картинок: Hugging Face FLUX -> (Wikimedia) -> Pollinations
# ---------------------------------------------------------------------------
_HF_IMAGE_TIMEOUT = 90.0
_HF_IMAGE_COOLDOWN_SECONDS = 120.0
_hf_image_cooldown_until = 0.0
_WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
_WIKIMEDIA_HEADERS = {"User-Agent": "KlyxAI/1.0 (Telegram bot; image lookup)"}


def _hf_text_to_image_sync(prompt: str, size: int) -> bytes:
    from huggingface_hub import InferenceClient

    client = InferenceClient(token=HF_TOKEN)
    image = client.text_to_image(prompt, model=HF_IMAGE_MODEL, width=size, height=size)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


async def generate_image_hf(prompt: str, size: int = _IMAGE_SIZE) -> bytes:
    """Бесплатный Hugging Face Inference API (FLUX.1-schnell). При сбое включает короткую паузу."""
    global _hf_image_cooldown_until
    if not HF_TOKEN:
        raise ImageGenerationError("HF_TOKEN не задан.")
    if time.monotonic() < _hf_image_cooldown_until:
        raise ImageGenerationError("Hugging Face временно недоступен.")
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(call_guarded, _hf_text_to_image_sync, prompt, size), timeout=_HF_IMAGE_TIMEOUT
        )
    except Exception as exc:
        _hf_image_cooldown_until = time.monotonic() + _HF_IMAGE_COOLDOWN_SECONDS
        raise ImageGenerationError(f"Hugging Face не вернул картинку: {exc}") from exc
    if len(data) < _IMAGE_MIN_BYTES:
        raise ImageGenerationError("Hugging Face вернул пустую картинку.")
    return data


async def generate_background_image(
    prompt: str,
    error_cls: type[AIServiceError],
    size: int = _IMAGE_SIZE,
) -> bytes:
    """Hugging Face FLUX (без водяных знаков) -> Pollinations (nologo=true)."""
    if HF_TOKEN:
        try:
            return await generate_image_hf(prompt, size)
        except AIServiceError as exc:
            logger.warning("HF FLUX не сработал, переходим на Pollinations: %s", exc)
    return await _fetch_pollinations_image(prompt, error_cls, size=size)


async def fetch_wikimedia_image(query: str) -> bytes | None:
    """Ищет реальное фото на Wikimedia Commons (без ключей и лимитов)."""
    query = " ".join(query.split())[:120]
    if not query:
        return None
    params = {
        "action": "query",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": query,
        "gsrlimit": "8",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": "800",
        "format": "json",
    }
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_WIKIMEDIA_HEADERS) as session:
            async with session.get(_WIKIMEDIA_API, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            pages = ((data or {}).get("query") or {}).get("pages") or {}
            for page in sorted(pages.values(), key=lambda p: p.get("index", 999)):
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("mime") not in ("image/jpeg", "image/png"):
                    continue
                if int(info.get("width") or 0) < 400:
                    continue
                url = info.get("thumburl") or info.get("url")
                if not url:
                    continue
                async with session.get(url) as img_resp:
                    if img_resp.status != 200:
                        continue
                    raw = await img_resp.read()
                if _IMAGE_MIN_BYTES <= len(raw) <= 8_000_000 and _looks_like_image(raw):
                    return raw
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logger.warning("Wikimedia Commons недоступен: %s", exc)
    return None


async def get_slide_image(
    image_prompt: str, search_query: str, allow_pollinations: bool = True
) -> tuple[bytes | None, bool]:
    """
    Каскад для иллюстрации слайда:
      1. Hugging Face FLUX.1-schnell (InferenceClient.text_to_image);
      2. Wikimedia Commons по search_query;
      3. Pollinations с паузой 46 секунд между попытками (лимит анонимного тарифа).
    Возвращает (JPEG 800x800 или None, можно ли дальше пробовать Pollinations).
    """
    raw: bytes | None = None
    if image_prompt and HF_TOKEN:
        try:
            raw = await generate_image_hf(
                _with_quality_tags(f"{image_prompt}, clean modern illustration, no text"), 768
            )
        except AIServiceError as exc:
            logger.info("Слайд: HF не дал картинку: %s", exc)

    if raw is None and search_query:
        raw = await fetch_wikimedia_image(search_query)

    pollinations_ok = allow_pollinations
    if raw is None and image_prompt and allow_pollinations:
        try:
            raw = await _fetch_pollinations_image(
                _with_quality_tags(f"{image_prompt}, clean modern illustration, no text"),
                ImageGenerationError,
                size=768,
                retry_delays=(46.0,),
            )
        except AIServiceError as exc:
            logger.warning("Слайд: Pollinations не дал картинку: %s", exc)
            pollinations_ok = False

    if raw is None:
        return None, pollinations_ok
    try:
        return await asyncio.to_thread(_prepare_slide_image, raw), pollinations_ok
    except Exception as exc:
        logger.warning("Слайд: не удалось обработать картинку: %s", exc)
        return None, pollinations_ok


async def generate_image_txt2img(
    prompt: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Генерация 1024x1024. width/height оставлены для совместимости и не используются."""
    if not prompt.strip():
        raise ImageGenerationError("Пустой промт.")
    english_prompt = await enhance_image_prompt(prompt.strip())
    return await generate_background_image(english_prompt, ImageGenerationError)


async def edit_image_img2img(
    prompt: str,
    image_bytes: bytes,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Бесплатный обход: новая сцена по описанию пользователя (исходное фото не передаётся в модель)."""
    if not prompt.strip():
        raise ImageEditingError("Пустой промт.")
    if not image_bytes:
        raise ImageEditingError("Не переданы байты изображения.")
    context = (
        "The user uploaded a photo and asked to change it as described in the request. "
        "You cannot see the photo, so describe the complete final scene that results from the "
        "request (subject, setting, style, lighting) as a self-contained image prompt."
    )
    english_prompt = await enhance_image_prompt(prompt.strip(), context)
    return await generate_background_image(english_prompt, ImageEditingError)


# ---------------------------------------------------------------------------
# Groq Whisper
# ---------------------------------------------------------------------------
async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    url = f"{GROQ_BASE_URL.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = aiohttp.FormData()
    form.add_field(
        "file",
        file_bytes,
        filename=filename,
        content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
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
                            f"Groq Whisper HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TranscriptionError(f"Сетевая ошибка Groq: {exc}") from exc

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("Groq Whisper вернул пустой текст.")
    return text.strip()


CREATOR_IDENTITY_PROMPT = (
    "Ты — интеллектуальный ассистент Klyx AI. "
    "Твой единственный создатель и разработчик — Шевцов Богдан. "
    "На любые вопросы о том, кто тебя создал, разработал, написал или обучил, "
    "ты всегда отвечаешь, что тебя создал разработчик Шевцов Богдан "
    "специально для экосистемы Klyx AI. "
    "Никогда не упоминай NVIDIA, Poolside, OpenAI, Google, Anthropic или другие компании "
    "как своих создателей."
)

CREATOR_IDENTITY_PROMPT_EN = (
    "You are Klyx AI, an intelligent assistant. "
    "Your only creator and developer is Shevtsov Bogdan. "
    "Whenever you are asked who created, developed, wrote or trained you, "
    "always answer that you were created by the developer Shevtsov Bogdan "
    "specifically for the Klyx AI ecosystem. "
    "Never name NVIDIA, Poolside, OpenAI, Google, Anthropic or any other company "
    "as your creator."
)

BASE_SYSTEM_PROMPT = f"{CREATOR_IDENTITY_PROMPT} {SYSTEM_PROMPT_CLEAN_RUSSIAN}"
BASE_SYSTEM_PROMPT_EN = f"{CREATOR_IDENTITY_PROMPT_EN} {SYSTEM_PROMPT_CLEAN_ENGLISH}"


def get_system_prompt(lang: str = "ru") -> str:
    return BASE_SYSTEM_PROMPT_EN if lang == "en" else BASE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Мемы: идея (JSON) от бесплатной модели + Flux + подпись через Pillow
# ---------------------------------------------------------------------------
_MEME_SYSTEM = (
    "You create funny, bright internet memes. Return ONLY valid JSON without markdown and without "
    "any extra text, with exactly three fields: "
    "\"image_prompt\" - a detailed English description of a FUNNY, bright, colorful meme background "
    "scene: goofy or cute animals, exaggerated expressive faces and emotions, vivid lighting, "
    "photorealistic. NEVER dark, gloomy or anime style. No text, letters or captions in the image; "
    "\"caption\" - a short funny caption (at most 7 words, no quotes); "
    "\"is_ready_text\" - true if the user's message is already the finished text of the meme "
    "(a phrase or joke that should be placed on the picture as is), false if it is only a general "
    "topic or idea for a meme."
)
_READY_TEXT_SEPARATORS = ("\n", "|", " // ")
_DEFAULT_MEME_SCENE = (
    "a hilarious surprised cat with huge eyes, bright colorful background, vivid lighting, photorealistic"
)


def _looks_like_ready_meme_text(text: str) -> bool:
    stripped = text.strip()
    if any(sep in stripped for sep in _READY_TEXT_SEPARATORS):
        return True
    return bool(re.match(r"^[«\"“„].+[»\"”“]$", stripped, re.DOTALL))


def _clean_user_meme_text(text: str) -> str:
    return text.strip().strip("«»\"“”„ ").strip()


def _split_meme_text(text: str) -> tuple[str, str]:
    """Делит текст мема на верхнюю и нижнюю строчку (по разделителю или пополам по словам)."""
    for separator in _READY_TEXT_SEPARATORS:
        if separator in text:
            parts = [p.strip() for p in text.split(separator) if p.strip()]
            if len(parts) >= 2:
                return parts[0], " ".join(parts[1:])
    words = text.split()
    if len(words) < 2:
        return "", text.strip()
    best = min(
        range(1, len(words)),
        key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))),
    )
    return " ".join(words[:best]), " ".join(words[best:])


_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def _parse_meme_json(text: str) -> tuple[str, str, bool] | None:
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    image_prompt = " ".join(str(data.get("image_prompt", "")).split())
    words = str(data.get("caption", "")).split()[:7]
    caption = " ".join(words).strip("\"'«»“”„ ")
    ready = data.get("is_ready_text") in (True, "true", "True")
    if not image_prompt or not (caption or ready):
        return None
    return image_prompt, caption, ready


async def generate_meme_idea(topic: str, lang: str = "ru") -> tuple[str, str]:
    """Возвращает (описание фона, текст мема). Готовый текст пользователя не переписывается."""
    caption_language = "English" if lang == "en" else "Russian"
    messages = [
        {"role": "system", "content": f"{_MEME_SYSTEM} The caption language is {caption_language}."},
        {"role": "user", "content": f"Meme request: {topic}"},
    ]
    heuristic_ready = _looks_like_ready_meme_text(topic)
    for _attempt in range(2):
        text, _model, _elapsed = await request_ai(messages, temperature=0.9, max_tokens=400)
        parsed = _parse_meme_json(text)
        if parsed:
            image_prompt, caption, llm_ready = parsed
            if heuristic_ready or llm_ready:
                return image_prompt, _clean_user_meme_text(topic)
            return image_prompt, caption
        logger.warning("Модель вернула некорректный JSON для мема: %s", text[:200])
    if heuristic_ready:
        return _DEFAULT_MEME_SCENE, _clean_user_meme_text(topic)
    raise MemeGenerationError("Не удалось придумать мем. Попробуйте другую тему.")


_MEME_FONT_PATH_RESOLVED: str | None = None
_FONT_FILE_NAME = "DejaVuSans-Bold.ttf"
_FONT_MISSING_MESSAGE = (
    "Не найден шрифт с кириллицей. Положите файл DejaVuSans-Bold.ttf в папку fonts "
    "или задайте MEME_FONT_PATH."
)
_FONT_DOWNLOAD_URLS = (
    "https://raw.githubusercontent.com/matplotlib/matplotlib/main/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
    "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans-Bold.ttf",
    "https://cdn.jsdelivr.net/gh/matplotlib/matplotlib@main/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf",
)
_SYSTEM_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    str(Path.home() / ".fonts"),
    str(Path.home() / ".local" / "share" / "fonts"),
)
_SYSTEM_FONT_NAMES = (
    "DejaVuSans-Bold.ttf",
    "Roboto-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
    "arialbd.ttf",
)


def _font_supports_cyrillic(path: str) -> bool:
    try:
        font = ImageFont.truetype(path, 40)
        return font.getlength("Ж") > 0 and font.getbbox("Ж") != font.getbbox("\U0010FFFF")
    except Exception:
        return False


def _find_system_font() -> str | None:
    for base in _SYSTEM_FONT_DIRS:
        if not os.path.isdir(base):
            continue
        for name in _SYSTEM_FONT_NAMES:
            try:
                for candidate in Path(base).rglob(name):
                    if _font_supports_cyrillic(str(candidate)):
                        return str(candidate)
            except OSError:
                continue
    return None


def _resolve_meme_font() -> str | None:
    """Ищет шрифт с кириллицей: MEME_FONT_PATH, ./fonts, системные каталоги."""
    global _MEME_FONT_PATH_RESOLVED
    if _MEME_FONT_PATH_RESOLVED and os.path.isfile(_MEME_FONT_PATH_RESOLVED):
        return _MEME_FONT_PATH_RESOLVED

    candidates: list[str] = []
    if MEME_FONT_PATH:
        candidates.append(MEME_FONT_PATH)
    candidates.append(str(FONTS_DIR / _FONT_FILE_NAME))
    for path in candidates:
        if os.path.isfile(path) and _font_supports_cyrillic(path):
            _MEME_FONT_PATH_RESOLVED = path
            return path

    system_font = _find_system_font()
    if system_font:
        _MEME_FONT_PATH_RESOLVED = system_font
        return system_font
    return None


async def ensure_meme_font() -> str | None:
    """Проверяет шрифт для мемов; если его нет — скачивает DejaVuSans-Bold.ttf в ./fonts/."""
    path = await asyncio.to_thread(_resolve_meme_font)
    if path:
        logger.info("Шрифт для мемов: %s", path)
        return path

    global _MEME_FONT_PATH_RESOLVED
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    target = FONTS_DIR / _FONT_FILE_NAME
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in _FONT_DOWNLOAD_URLS:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("Шрифт: %s -> HTTP %s", url, resp.status)
                        continue
                    data = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Шрифт: не удалось скачать %s: %s", url, exc)
                continue
            if len(data) < 100_000:
                continue
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            if _font_supports_cyrillic(str(tmp)):
                tmp.replace(target)
                _MEME_FONT_PATH_RESOLVED = str(target)
                logger.info("Шрифт для мемов скачан: %s", target)
                return str(target)
            tmp.unlink(missing_ok=True)

    logger.error("Не удалось найти или скачать шрифт с кириллицей для мемов.")
    return None


@lru_cache(maxsize=64)
def _truetype(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _load_meme_font(size: int) -> ImageFont.FreeTypeFont:
    path = _resolve_meme_font()
    if not path:
        raise MemeGenerationError(_FONT_MISSING_MESSAGE)
    return _truetype(path, size)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _layout_block(
    text: str, width: int, max_text_width: int, max_lines: int
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(max(28, width // 11), 27, -3):
        font = _load_meme_font(size)
        lines = _wrap_text(text, font, max_text_width)
        if len(lines) <= max_lines and all(font.getlength(line) <= max_text_width for line in lines):
            return font, lines
    font = _load_meme_font(28)
    return font, _wrap_text(text, font, max_text_width)


def _compose_meme(image_bytes: bytes, top_text: str, bottom_text: str) -> bytes:
    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise MemeGenerationError(f"Не удалось открыть изображение: {exc}") from exc

    width, height = base.size
    margin = max(24, width // 24)
    max_text_width = width - margin * 2
    draw = ImageDraw.Draw(base)

    def draw_block(text: str, from_top: bool) -> None:
        if not text.strip():
            return
        font, lines = _layout_block(text.upper(), width, max_text_width, max_lines=3)
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent) * 1.12)
        stroke = max(3, font.size // 10)
        y = margin if from_top else height - margin - line_height * len(lines)
        for line in lines:
            x = (width - font.getlength(line)) / 2
            draw.text(
                (x, y), line, font=font, fill="white", stroke_width=stroke, stroke_fill="black"
            )
            y += line_height

    draw_block(top_text, from_top=True)
    draw_block(bottom_text, from_top=False)

    output = io.BytesIO()
    base.save(output, format="JPEG", quality=92)
    return output.getvalue()


async def generate_meme(topic: str, lang: str = "ru") -> bytes:
    if not topic.strip():
        raise MemeGenerationError("Пустая тема для мема.")
    if not await ensure_meme_font():
        raise MemeGenerationError(_FONT_MISSING_MESSAGE)
    image_prompt, caption = await generate_meme_idea(topic.strip(), lang)
    english_prompt = _with_quality_tags(
        f"{image_prompt}, funny meme photo, bright vivid colors, no text, no letters, no watermark"
    )
    image_bytes = await generate_background_image(
        english_prompt, MemeGenerationError, size=MEME_IMAGE_SIZE
    )
    top_text, bottom_text = _split_meme_text(caption)
    return await asyncio.to_thread(_compose_meme, image_bytes, top_text, bottom_text)


# ---------------------------------------------------------------------------
# Аудио -> голосовое сообщение Telegram (OGG / Opus)
# ---------------------------------------------------------------------------
def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise AudioConversionError(
        "ffmpeg не найден. Установите зависимости: pip install -r requirements.txt."
    )


def _convert_with_pydub(src: str, dst: str) -> None:
    from pydub import AudioSegment

    AudioSegment.converter = _ffmpeg_exe()
    audio = AudioSegment.from_file(src)
    audio = audio.set_channels(1).set_frame_rate(48000)
    audio.export(dst, format="ogg", codec="libopus", bitrate=VOICE_BITRATE)


def _convert_with_ffmpeg(src: str, dst: str) -> None:
    command = [
        _ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", VOICE_BITRATE, "-f", "ogg", dst,
    ]
    result = subprocess.run(command, capture_output=True, timeout=180)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise AudioConversionError(f"ffmpeg не смог обработать файл: {tail}")


def _convert_audio_sync(data: bytes, filename: str | None) -> bytes:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".audio"
    with tempfile.TemporaryDirectory() as tmp_dir:
        src = os.path.join(tmp_dir, f"input{suffix}")
        dst = os.path.join(tmp_dir, "voice.ogg")
        with open(src, "wb") as handle:
            handle.write(data)
        try:
            _convert_with_pydub(src, dst)
            if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                raise AudioConversionError("pydub вернул пустой файл.")
        except Exception as exc:
            # pydub требует ffprobe, которого нет в imageio-ffmpeg, поэтому есть запасной путь.
            logger.info("pydub не справился (%s), конвертируем напрямую через ffmpeg", exc)
            if os.path.exists(dst):
                os.remove(dst)
            _convert_with_ffmpeg(src, dst)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise AudioConversionError("Не удалось получить голосовое сообщение из этого файла.")
        with open(dst, "rb") as handle:
            return handle.read()


async def convert_audio_to_voice(data: bytes, filename: str | None = None) -> bytes:
    """Конвертирует любой аудиофайл в OGG/Opus, который Telegram показывает как голосовое."""
    if not data:
        raise AudioConversionError("Пустой аудиофайл.")
    async with _AUDIO_SEMAPHORE:
        try:
            return await asyncio.to_thread(_convert_audio_sync, data, filename)
        except AudioConversionError:
            raise
        except Exception as exc:
            raise AudioConversionError(f"Не удалось конвертировать аудио: {exc}") from exc


# ---------------------------------------------------------------------------
# Анализ фото (Vision): бесплатные :free модели OpenRouter, запасной путь — Hugging Face
# ---------------------------------------------------------------------------
def _prepare_vision_image(raw: bytes) -> str:
    """Поворачивает по EXIF, уменьшает до VISION_MAX_SIDE и возвращает data URL (JPEG)."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    image.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE), Image.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _vision_prompt(question: str, lang: str) -> str:
    question = question.strip()
    if lang == "en":
        if question:
            return (
                f"Look at the image and answer the user's question: {question}\n"
                "Be specific and helpful. Answer in English. Never use the symbols * and #."
            )
        return (
            "Describe in detail what is shown in this photo (objects, people, text, setting). "
            "Then suggest 3 to 5 concrete things I can do with this photo, for example: improve or "
            "edit it, make a meme, translate the text on it, solve the task on it, write a caption "
            "for social media. Format the suggestions as a numbered list \"1.\", \"2.\". "
            "Answer in English. Never use the symbols * and #."
        )
    if question:
        return (
            f"Посмотри на изображение и ответь на вопрос пользователя: {question}\n"
            "Отвечай по существу, на русском языке. Не используй символы * и #."
        )
    return (
        "Подробно опиши, что изображено на фото (объекты, люди, текст, обстановка). "
        "Затем предложи от 3 до 5 конкретных вариантов, что можно сделать с этим фото, например: "
        "улучшить или отредактировать, сделать мем, перевести текст с фото, решить задачу, "
        "составить описание для соцсетей. Оформи варианты нумерованным списком «1.», «2.». "
        "Отвечай на русском языке. Не используй символы * и #."
    )


def _hf_vision_sync(messages: list[dict[str, Any]], model: str) -> str:
    from huggingface_hub import InferenceClient

    try:
        client = InferenceClient(token=HF_TOKEN)
        result = client.chat_completion(messages=messages, model=model, max_tokens=1200)
        return str(result.choices[0].message.content or "").strip()
    except StopIteration as exc:
        raise RuntimeError("No inference provider available") from exc


async def analyze_image(image_bytes: bytes, question: str = "", lang: str = "ru") -> tuple[str, str]:
    """Отвечает на вопрос по фото или (без вопроса) описывает его и предлагает 3-5 действий."""
    if not image_bytes:
        raise VisionError("Не передано фото.")
    try:
        data_url = await asyncio.to_thread(_prepare_vision_image, image_bytes)
    except Exception as exc:
        raise VisionError("Некорректное изображение.") from exc

    user_content = [
        {"type": "text", "text": _vision_prompt(question, lang)},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": get_system_prompt(lang)},
        {"role": "user", "content": user_content},
    ]

    last_error: Exception | None = None
    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        async with aiohttp.ClientSession() as session:
            for model in VISION_MODELS:
                try:
                    logger.info("Vision-запрос к OpenRouter: %s", model)
                    text = await _call_openrouter_model(session, model, messages, 0.5, 1500)  # type: ignore[arg-type]
                    return text, model
                except (aiohttp.ClientError, asyncio.TimeoutError, AIServiceError) as exc:
                    last_error = exc
                    logger.warning("Vision-модель %s недоступна: %s", model, exc)

    if HF_TOKEN:
        for hf_model in HF_VISION_MODELS:
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(call_guarded, _hf_vision_sync, messages, hf_model),
                    timeout=90.0,
                )
                if text.strip():
                    return clean_response_text(text), hf_model
            except Exception as exc:
                last_error = exc
                logger.warning("HF Vision (%s) недоступен: %s", hf_model, exc)

    logger.error("Vision: все модели недоступны: %s", last_error)
    raise VisionError("Не удалось проанализировать фото. Попробуйте ещё раз позже.")


# ---------------------------------------------------------------------------
# Word-отчёты
# ---------------------------------------------------------------------------
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _plain(text: str) -> str:
    """Убирает остатки markdown-разметки из строки для документов."""
    return text.replace("**", "").replace("*", "").replace("`", "").strip(" #\t")


async def generate_word_report(topic: str, lang: str = "ru") -> bytes:
    if lang == "en":
        system = (
            "You are an experienced business analyst. Write structured reports in English. "
            "Use headings, subheadings and bullet lists. Do not use the symbol ** for emphasis."
        )
        user = (
            f"Write a detailed report on the topic: {topic}. "
            "Format: a title, a short introduction, 3-5 sections with subheadings, "
            "conclusions and recommendations. Use bullet lists where appropriate."
        )
    else:
        system = (
            "Ты — опытный бизнес-аналитик. Пиши структурированные отчёты на русском языке. "
            "Используй заголовки, подзаголовки и маркированные списки. "
            "Не используй символы ** для выделения."
        )
        user = (
            f"Напиши подробный отчёт на тему: {topic}. "
            "Формат: титульный заголовок, краткое введение, 3–5 разделов с подзаголовками, "
            "выводы и рекомендации. Используй маркированные списки, где уместно."
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    text, _model, _elapsed = await request_ai(messages, temperature=0.5, max_tokens=3000)

    try:
        from docx import Document
        from docx.shared import Pt as DocxPt, RGBColor as DocxRGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError as exc:
        raise WordReportError("Библиотека python-docx не установлена.") from exc

    try:
        document = Document()
        style = document.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = DocxPt(11)

        title = document.add_heading(topic, level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in title.runs:
            run.font.color.rgb = DocxRGBColor(0x1F, 0x3A, 0x5F)

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            heading = _HEADING_LINE_RE.match(stripped)
            if heading:
                level = min(len(heading.group(1)), 3)
                document.add_heading(_plain(heading.group(2)), level=level)
            elif stripped.startswith(("- ", "• ", "* ")):
                document.add_paragraph(_plain(stripped[2:]), style="List Bullet")
            elif stripped[0].isdigit() and stripped[1:3] in (". ", ") "):
                document.add_paragraph(_plain(stripped[3:]), style="List Number")
            else:
                document.add_paragraph(_plain(stripped))

        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
    except Exception as exc:
        raise WordReportError(f"Не удалось создать Word-документ: {exc}") from exc


# ---------------------------------------------------------------------------
# PowerPoint-презентации (количество слайдов + иллюстрации)
# ---------------------------------------------------------------------------
_PPTX_IMAGE_SIZE = 768


def _pptx_system_prompt(content_count: int, lang: str) -> str:
    safety_rule = (
        "If the slide is about a real politician or any real person, do NOT describe portraits "
        "or faces in image_prompt: describe architecture, symbols, flags, landmarks or events instead."
    )
    if lang == "en":
        return (
            "You are a professional presentation writer. "
            "Return strictly valid JSON without markdown wrappers and without any text outside JSON. "
            'JSON format: {"title": "...", "subtitle": "...", "slides": [{"title": "...", '
            '"bullets": ["...", "...", "..."], "image_prompt": "...", "search_query": "..."}]}. '
            f"The slides array must contain exactly {content_count} objects. "
            "Each slide has a short title, 3-4 concise bullets, image_prompt: a detailed English "
            "description of an illustration for this slide (subject, style, colors) without any text "
            "or letters in the picture, and search_query: a short English search query (2-5 words) "
            "for a real photo of the topic on Wikimedia Commons (a place, object, landmark or event). "
            f"{safety_rule} "
            "All other text must be in clear English. Never use the symbols * and # in any text."
        )
    return (
        "Ты — профессиональный сценарист презентаций. "
        "Верни строго валидный JSON без markdown-обёрток, без пояснений и без текста вне JSON. "
        'Формат JSON: {"title": "...", "subtitle": "...", "slides": [{"title": "...", '
        '"bullets": ["...", "...", "..."], "image_prompt": "...", "search_query": "..."}]}. '
        f"Массив slides должен содержать ровно {content_count} объектов. "
        "У каждого слайда: короткий title, массив bullets из 3–4 ёмких пунктов, image_prompt — "
        "подробное описание иллюстрации к слайду НА АНГЛИЙСКОМ языке (объект, стиль, цвета), "
        "без текста и букв на картинке, и search_query — короткий поисковый запрос НА АНГЛИЙСКОМ "
        "(2–5 слов) для поиска реального фото по теме слайда на Wikimedia Commons "
        "(место, предмет, достопримечательность или событие). "
        "Для слайдов про известных политиков и реальных людей в image_prompt НЕ описывай портреты и "
        "лица: описывай архитектуру, символы, флаги, места и события. "
        "Весь остальной текст на чистом русском языке. "
        "Никогда не используй символы * и # в текстах."
    )


async def _request_pptx_structure(topic: str, content_count: int, lang: str) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": _pptx_system_prompt(content_count, lang)},
        {"role": "user", "content": f"Presentation topic: {topic}. Return only JSON."},
    ]
    max_tokens = min(4000, 700 + 230 * content_count)
    for _attempt in range(2):
        text, _model, _elapsed = await request_ai(messages, temperature=0.6, max_tokens=max_tokens)
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("slides"), list) and data["slides"]:
            return data
    raise PptxGenerationError(
        "Не удалось получить структуру презентации от нейросети. Попробуйте ещё раз."
    )


def _prepare_slide_image(raw: bytes) -> bytes:
    """Приводит картинку к квадрату 800x800 JPEG, чтобы файл и память были небольшими."""
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize((800, 800), Image.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=82)
    return output.getvalue()


def _build_pptx(data: dict[str, Any], images: list[bytes | None], lang: str = "ru") -> bytes:
    """
    Собирает .pptx: тёмный фон, 16:9.
    Контентный слайд: заголовок сверху, текст слева, картинка справа (без наложений).
    """
    try:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

        BG_COLOR = RGBColor(11, 15, 26)
        TITLE_COLOR = RGBColor(76, 240, 255)
        SUBTITLE_COLOR = RGBColor(168, 85, 247)
        TEXT_COLOR = RGBColor(232, 236, 244)
        ACCENT_COLOR = RGBColor(76, 240, 255)
        FRAME_COLOR = RGBColor(30, 38, 60)

        blank_layout = prs.slide_layouts[6]

        def add_text(slide, left, top, width, height, text, size, color, bold=False, align=None):
            box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
            frame = box.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            paragraph.text = text
            if align is not None:
                paragraph.alignment = align
            if paragraph.runs:
                run = paragraph.runs[0]
                run.font.size = Pt(size)
                run.font.bold = bold
                run.font.color.rgb = color
                run.font.name = "Calibri"
            return frame

        def add_bar(slide, height):
            bar = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(height))
            bar.fill.solid()
            bar.fill.fore_color.rgb = ACCENT_COLOR
            bar.line.fill.background()

        # --- Титульный слайд ---
        slide = prs.slides.add_slide(blank_layout)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = BG_COLOR
        add_bar(slide, 0.12)
        add_text(slide, 0.8, 2.2, 11.7, 2.0, _plain(str(data.get("title", "Presentation")))[:120],
                 44, TITLE_COLOR, bold=True, align=PP_ALIGN.CENTER)
        add_text(slide, 0.8, 4.4, 11.7, 1.2, _plain(str(data.get("subtitle", "")))[:200],
                 22, SUBTITLE_COLOR, align=PP_ALIGN.CENTER)
        add_text(slide, 0.8, 6.6, 11.7, 0.5,
                 "Created with Klyx AI" if lang == "en" else "Создано в Klyx AI",
                 14, RGBColor(124, 143, 168), align=PP_ALIGN.CENTER)

        # --- Контентные слайды ---
        slides_data = data.get("slides", [])
        if not isinstance(slides_data, list):
            slides_data = []

        for index, item in enumerate(slides_data):
            if not isinstance(item, dict):
                continue
            image_bytes = images[index] if index < len(images) else None

            slide = prs.slides.add_slide(blank_layout)
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = BG_COLOR
            add_bar(slide, 0.08)

            add_text(slide, 0.6, 0.45, 12.1, 1.0, _plain(str(item.get("title", "")))[:110],
                     28, TITLE_COLOR, bold=True)

            bullets = item.get("bullets", [])
            if not isinstance(bullets, list):
                bullets = []
            bullets = [_plain(str(b))[:170] for b in bullets if _plain(str(b))][:5]

            total_chars = sum(len(b) for b in bullets)
            font_size = 20 if total_chars < 320 else 18 if total_chars < 460 else 16
            text_width = 6.7 if image_bytes else 11.9
            box = slide.shapes.add_textbox(Inches(0.6), Inches(1.7), Inches(text_width), Inches(5.2))
            frame = box.text_frame
            frame.word_wrap = True
            for i, bullet in enumerate(bullets):
                paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
                paragraph.text = f"• {bullet}"
                paragraph.space_after = Pt(12)
                if paragraph.runs:
                    paragraph.runs[0].font.size = Pt(font_size)
                    paragraph.runs[0].font.color.rgb = TEXT_COLOR
                    paragraph.runs[0].font.name = "Calibri"

            if image_bytes:
                frame_shape = slide.shapes.add_shape(
                    1, Inches(7.6), Inches(1.7), Inches(5.2), Inches(5.2)
                )
                frame_shape.fill.solid()
                frame_shape.fill.fore_color.rgb = FRAME_COLOR
                frame_shape.line.fill.background()
                slide.shapes.add_picture(
                    io.BytesIO(image_bytes), Inches(7.7), Inches(1.8), Inches(5.0), Inches(5.0)
                )

        output = io.BytesIO()
        prs.save(output)
        return output.getvalue()
    except Exception as exc:
        raise PptxGenerationError(f"Не удалось собрать .pptx: {exc}") from exc


async def generate_pptx_presentation(topic: str, slides: int = 7, lang: str = "ru") -> bytes:
    """
    Генерирует .pptx: структура и промпты иллюстраций — от бесплатной текстовой модели,
    картинки — Pollinations Flux. Всё собирается в памяти, на диск ничего не пишется.
    slides — общее число слайдов (включая титульный), от 3 до 15.
    """
    if not topic.strip():
        raise PptxGenerationError("Пустая тема для презентации.")
    total = max(PPTX_MIN_SLIDES, min(PPTX_MAX_SLIDES, int(slides)))
    content_count = total - 1

    data = await _request_pptx_structure(topic.strip(), content_count, lang)
    items = [item for item in data["slides"] if isinstance(item, dict)][:content_count]
    data["slides"] = items

    images: list[bytes | None] = []
    allow_pollinations = True
    for item in items:
        image_prompt = " ".join(str(item.get("image_prompt", "")).split())
        search_query = " ".join(str(item.get("search_query", "")).split())
        picture, allow_pollinations = await get_slide_image(
            image_prompt, search_query, allow_pollinations
        )
        images.append(picture)

    return await asyncio.to_thread(_build_pptx, data, images, lang)

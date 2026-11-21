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
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from config import (
    BASE_DIR,
    FREE_MODELS,
    GLOBAL_REQUESTS_PER_MINUTE,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_TIMEOUT,
    GROQ_WHISPER_MODEL,
    MAX_CONCURRENT_AI_REQUESTS,
    MEME_FONT_PATH,
    MEME_IMAGE_SIZE,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_NAME,
    OPENROUTER_BASE_URL,
    OPENROUTER_SITE_URL,
    OPENROUTER_TIMEOUT,
    POLLINATIONS_IMAGE_URL,
    POLLINATIONS_MODEL_TXT2IMG,
    POLLINATIONS_TIMEOUT,
    SYSTEM_PROMPT_CLEAN_RUSSIAN,
    VOICE_BITRATE,
)

logger = logging.getLogger(__name__)

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


def clean_response_text(text: str) -> str:
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
) -> bytes:
    url = f"{POLLINATIONS_IMAGE_URL}{urllib.parse.quote(prompt, safe='')}"
    timeout = aiohttp.ClientTimeout(total=POLLINATIONS_TIMEOUT)
    attempts = len(_POLLINATIONS_RETRY_DELAYS) + 1
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
                            fatal = resp.status not in (429, 500, 502, 503, 504)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = f"сетевая ошибка: {exc}"

        logger.warning(
            "Pollinations (попытка %s/%s): %s", attempt + 1, attempts, last_error
        )
        if fatal or attempt == attempts - 1:
            break
        await asyncio.sleep(_POLLINATIONS_RETRY_DELAYS[attempt])

    raise error_cls(
        "Бесплатный сервис картинок сейчас перегружен. Попробуйте ещё раз через минуту."
    )


async def generate_image_txt2img(
    prompt: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Генерация 1024x1024. width/height оставлены для совместимости и не используются."""
    if not prompt.strip():
        raise ImageGenerationError("Пустой промт.")
    english_prompt = await enhance_image_prompt(prompt.strip())
    return await _fetch_pollinations_image(english_prompt, ImageGenerationError)


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
    return await _fetch_pollinations_image(english_prompt, ImageEditingError)


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

BASE_SYSTEM_PROMPT = f"{CREATOR_IDENTITY_PROMPT} {SYSTEM_PROMPT_CLEAN_RUSSIAN}"


def get_system_prompt() -> str:
    return BASE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Мемы: идея (JSON) от бесплатной модели + Flux + подпись через Pillow
# ---------------------------------------------------------------------------
_MEME_SYSTEM = (
    "You create meme ideas. Return ONLY valid JSON without markdown and without any extra text, "
    "with exactly two fields: "
    "\"image_prompt\" - a detailed English description of a funny meme visual scene, "
    "WITHOUT any text, letters or captions in the image; "
    "\"caption\" - a short funny caption in Russian, at most 7 words, without quotes."
)

_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def _parse_meme_json(text: str) -> tuple[str, str] | None:
    cleaned = text.strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
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
    if not image_prompt or not caption:
        return None
    return image_prompt, caption


async def generate_meme_idea(topic: str) -> tuple[str, str]:
    messages = [
        {"role": "system", "content": _MEME_SYSTEM},
        {"role": "user", "content": f"Тема мема: {topic}"},
    ]
    for _attempt in range(2):
        text, _model, _elapsed = await request_ai(messages, temperature=0.9, max_tokens=400)
        parsed = _parse_meme_json(text)
        if parsed:
            return parsed
        logger.warning("Модель вернула некорректный JSON для мема: %s", text[:200])
    raise MemeGenerationError("Не удалось придумать мем. Попробуйте другую тему.")


@lru_cache(maxsize=32)
def _load_meme_font(size: int) -> ImageFont.FreeTypeFont:
    candidates: list[str] = []
    if MEME_FONT_PATH:
        candidates.append(MEME_FONT_PATH)
    candidates.append(str(BASE_DIR / "fonts" / "DejaVuSans-Bold.ttf"))
    candidates.extend(_FONT_CANDIDATES)
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise MemeGenerationError(
        "Не найден шрифт с кириллицей DejaVuSans-Bold.ttf. Положите файл в папку fonts "
        "рядом с main.py или укажите путь в переменной MEME_FONT_PATH."
    )


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


def _compose_meme(image_bytes: bytes, caption: str) -> bytes:
    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise MemeGenerationError(f"Не удалось открыть изображение: {exc}") from exc

    width, height = base.size
    margin = max(24, width // 24)
    max_text_width = width - margin * 2
    text = caption.upper()

    font = _load_meme_font(max(24, width // 10))
    lines: list[str] = [text]
    for size in range(max(24, width // 10), 35, -4):
        font = _load_meme_font(size)
        lines = _wrap_text(text, font, max_text_width)
        if len(lines) <= 3 and all(font.getlength(line) <= max_text_width for line in lines):
            break

    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * 1.12)
    stroke = max(3, font.size // 10)
    y = height - margin - line_height * len(lines)

    draw = ImageDraw.Draw(base)
    for line in lines:
        x = (width - font.getlength(line)) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=stroke,
            stroke_fill="black",
        )
        y += line_height

    output = io.BytesIO()
    base.save(output, format="JPEG", quality=92)
    return output.getvalue()


async def generate_meme(topic: str) -> bytes:
    if not topic.strip():
        raise MemeGenerationError("Пустая тема для мема.")
    image_prompt, caption = await generate_meme_idea(topic.strip())
    english_prompt = _with_quality_tags(
        f"{image_prompt}, funny meme photo, no text, no letters, no watermark"
    )
    image_bytes = await _fetch_pollinations_image(
        english_prompt, MemeGenerationError, size=MEME_IMAGE_SIZE
    )
    return await asyncio.to_thread(_compose_meme, image_bytes, caption)


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
# Word-отчёты
# ---------------------------------------------------------------------------
async def generate_word_report(topic: str) -> bytes:
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
            if stripped.startswith("# "):
                document.add_heading(stripped[2:].strip(), level=1)
            elif stripped.startswith("## "):
                document.add_heading(stripped[3:].strip(), level=2)
            elif stripped.startswith(("- ", "• ", "* ")):
                document.add_paragraph(stripped[2:].strip(), style="List Bullet")
            elif stripped[0].isdigit() and stripped[1:3] in (". ", ") "):
                document.add_paragraph(stripped[3:].strip(), style="List Number")
            else:
                document.add_paragraph(stripped)

        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
    except Exception as exc:
        raise WordReportError(f"Не удалось создать Word-документ: {exc}") from exc


# ---------------------------------------------------------------------------
# PowerPoint-презентации
# ---------------------------------------------------------------------------
_PPTX_JSON_SYSTEM = (
    "Ты — профессиональный сценарист презентаций. "
    "Верни строго валидный JSON без markdown-обёрток, без пояснений и без текста вне JSON. "
    "Формат JSON: {\"title\": \"...\", \"subtitle\": \"...\", \"slides\": [{\"title\": \"...\", \"bullets\": [\"...\", \"...\", \"...\"]}]}. "
    "Требования: title — заголовок презентации; subtitle — краткий подзаголовок; "
    "slides — массив из 6–8 объектов; у каждого slide title — заголовок слайда, "
    "bullets — массив из 3–4 ёмких пунктов. "
    "Весь текст на чистом русском языке, без символов **, без случайных английских слов. "
    "Пункты bullets должны быть короткими и содержательными."
)


def _build_pptx(data: dict[str, Any]) -> bytes:
    """
    Собирает .pptx из распарсенного JSON.
    Тёмный фон, акцентный текст, 16:9, титульный слайд + контентные.
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

        blank_layout = prs.slide_layouts[6]

        # --- Титульный слайд ---
        slide = prs.slides.add_slide(blank_layout)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = BG_COLOR

        # Акцентная полоса сверху
        bar = slide.shapes.add_shape(
            1, Inches(0), Inches(0), prs.slide_width, Inches(0.12)
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = ACCENT_COLOR
        bar.line.fill.background()

        title_box = slide.shapes.add_textbox(
            Inches(0.8), Inches(2.2), Inches(11.7), Inches(2.0)
        )
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = str(data.get("title", "Презентация"))[:120]
        p.alignment = PP_ALIGN.CENTER
        if p.runs:
            p.runs[0].font.size = Pt(44)
            p.runs[0].font.bold = True
            p.runs[0].font.color.rgb = TITLE_COLOR
            p.runs[0].font.name = "Calibri"

        subtitle_box = slide.shapes.add_textbox(
            Inches(0.8), Inches(4.4), Inches(11.7), Inches(1.2)
        )
        tf2 = subtitle_box.text_frame
        tf2.word_wrap = True
        p2 = tf2.paragraphs[0]
        p2.text = str(data.get("subtitle", ""))[:200]
        p2.alignment = PP_ALIGN.CENTER
        if p2.runs:
            p2.runs[0].font.size = Pt(22)
            p2.runs[0].font.color.rgb = SUBTITLE_COLOR
            p2.runs[0].font.name = "Calibri"

        footer_box = slide.shapes.add_textbox(
            Inches(0.8), Inches(6.6), Inches(11.7), Inches(0.5)
        )
        tf3 = footer_box.text_frame
        p3 = tf3.paragraphs[0]
        p3.text = "Создано в Klyx AI"
        p3.alignment = PP_ALIGN.CENTER
        if p3.runs:
            p3.runs[0].font.size = Pt(14)
            p3.runs[0].font.color.rgb = RGBColor(124, 143, 168)
            p3.runs[0].font.name = "Calibri"

        # --- Контентные слайды ---
        slides_data = data.get("slides", [])
        if not isinstance(slides_data, list):
            slides_data = []

        for slide_item in slides_data:
            if not isinstance(slide_item, dict):
                continue
            slide = prs.slides.add_slide(blank_layout)
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = BG_COLOR

            bar = slide.shapes.add_shape(
                1, Inches(0), Inches(0), prs.slide_width, Inches(0.08)
            )
            bar.fill.solid()
            bar.fill.fore_color.rgb = ACCENT_COLOR
            bar.line.fill.background()

            title_box = slide.shapes.add_textbox(
                Inches(0.6), Inches(0.5), Inches(12.1), Inches(0.9)
            )
            tf = title_box.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = str(slide_item.get("title", ""))[:150]
            if p.runs:
                p.runs[0].font.size = Pt(30)
                p.runs[0].font.bold = True
                p.runs[0].font.color.rgb = TITLE_COLOR
                p.runs[0].font.name = "Calibri"

            content_box = slide.shapes.add_textbox(
                Inches(0.8), Inches(1.6), Inches(11.7), Inches(5.4)
            )
            tf_c = content_box.text_frame
            tf_c.word_wrap = True
            bullets = slide_item.get("bullets", [])
            if not isinstance(bullets, list):
                bullets = []
            for i, bullet in enumerate(bullets):
                if i == 0:
                    p = tf_c.paragraphs[0]
                else:
                    p = tf_c.add_paragraph()
                p.text = str(bullet)[:200]
                p.level = 0
                p.space_after = Pt(14)
                if p.runs:
                    p.runs[0].font.size = Pt(20)
                    p.runs[0].font.color.rgb = TEXT_COLOR
                    p.runs[0].font.name = "Calibri"

        output = io.BytesIO()
        prs.save(output)
        return output.getvalue()
    except Exception as exc:
        raise PptxGenerationError(f"Не удалось собрать .pptx: {exc}") from exc


async def generate_pptx_presentation(topic: str) -> bytes:
    """
    Генерирует презентацию .pptx по теме.
    Сначала получает JSON-структуру через OpenRouter, затем собирает файл.
    """
    if not topic.strip():
        raise PptxGenerationError("Пустая тема для презентации.")

    user_prompt = (
        f"Создай структуру презентации на тему: {topic}. "
        "Верни только JSON в указанном формате."
    )
    messages = [
        {"role": "system", "content": _PPTX_JSON_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    text, _model, _elapsed = await request_ai(
        messages, temperature=0.6, max_tokens=2500
    )

    # Убираем возможные markdown-обёртки ```json ... ```
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PptxGenerationError(
            f"Не удалось разобрать JSON от нейросети: {exc}. Ответ: {cleaned[:300]}"
        ) from exc

    if not isinstance(data, dict):
        raise PptxGenerationError("Нейросеть вернула JSON не в виде объекта.")

    return _build_pptx(data)
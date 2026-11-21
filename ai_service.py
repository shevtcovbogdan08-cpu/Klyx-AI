# ai_service.py
"""
Сервис взаимодействия с ИИ-провайдерами.

- OpenRouter (текстовые модели): ответы, Word-отчёты, презентации
- OpenRouter (Google Gemini, google/gemini-2.5-flash-image): txt2img, img2img и мемы
- Groq Whisper: транскрипция голоса
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import time
from typing import Any

import aiohttp
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

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
    OPENROUTER_IMAGE_MODEL,
    OPENROUTER_IMAGE_TIMEOUT,
    OPENROUTER_SITE_URL,
    OPENROUTER_TIMEOUT,
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


class MemeGenerationError(AIServiceError):
    pass


class WordReportError(AIServiceError):
    pass


class PptxGenerationError(AIServiceError):
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
# Google Gemini (google/gemini-2.5-flash-image через OpenRouter): txt2img / img2img / мемы
# ---------------------------------------------------------------------------
_IMAGE_MIN_BYTES = 1024

_TXT2IMG_INSTRUCTION = (
    "Generate one ultra-sharp, high-resolution, high-quality image with crisp focus, "
    "fine detail, natural lighting and accurate anatomy and perspective. "
    "Unless the request explicitly asks for another style (anime, painting, synthwave, "
    "illustration, 3D and so on), make it photorealistic. "
    "No blur, no haze, no watermark, and no text on the image unless the request asks for it. "
    "Image request (it may be written in Russian): "
)

_EDIT_INSTRUCTION = (
    "Edit the attached photo exactly as instructed below. "
    "Keep everything that is not mentioned (people, identity, composition, background, "
    "lighting) unchanged. The result must be sharp, high-resolution and photorealistic, "
    "without blur, artifacts or watermarks. "
    "Instruction (it may be written in Russian): "
)

_MEME_INSTRUCTION = (
    "Create a complete, genuinely funny internet meme image about the topic below. "
    "It needs a clear visual joke (a recognizable meme-style scene or character composition) "
    "and the caption written directly on the image in RUSSIAN (Cyrillic). "
    "Caption requirements: short punchy captions at the top and/or bottom, at most about "
    "8 words each; huge bold white sans-serif letters with a thick black outline "
    "(classic Impact meme style); perfectly legible and high contrast; fully inside the "
    "frame and not cut off; correct Russian spelling and grammar, no typos, no invented "
    "or mirrored letters. No watermark and no other text. Sharp, high-resolution image. "
    "Topic (in Russian): "
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


def _extract_image_data_url(data: dict[str, Any]) -> str | None:
    """Достаёт data:image/...;base64 из ответа OpenRouter.

    Основной путь: choices[0].message.images[0].image_url.url.
    Запасной: image_url-блоки внутри message.content.
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict):
        return None

    candidates: list[Any] = []
    images = message.get("images")
    if isinstance(images, list):
        candidates.extend(images)
    content = message.get("content")
    if isinstance(content, list):
        candidates.extend(
            item for item in content if isinstance(item, dict) and item.get("type") == "image_url"
        )

    for item in candidates:
        if not isinstance(item, dict):
            continue
        image_url = item.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.startswith("data:image"):
            return url
    return None


def _decode_data_url(url: str, error_cls: type[AIServiceError]) -> bytes:
    try:
        _, encoded = url.split(",", 1)
        raw = base64.b64decode(encoded)
    except (ValueError, binascii.Error) as exc:
        raise error_cls("Gemini вернула повреждённое изображение.") from exc
    if len(raw) < _IMAGE_MIN_BYTES:
        raise error_cls("Gemini вернула пустое изображение.")
    return raw


async def _request_gemini_image(
    content: list[dict[str, Any]],
    error_cls: type[AIServiceError],
) -> bytes:
    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }
    payload: dict[str, Any] = {
        "model": OPENROUTER_IMAGE_MODEL,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }
    timeout = aiohttp.ClientTimeout(total=OPENROUTER_IMAGE_TIMEOUT)

    status = 0
    body = ""
    data: dict[str, Any] = {}
    async with _AI_SEMAPHORE:
        await _acquire_rate_slot()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    status = resp.status
                    if status != 200:
                        body = await resp.text()
                    else:
                        data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise error_cls(f"Сетевая ошибка при обращении к Gemini: {exc}") from exc

    if status != 200:
        logger.warning("Gemini (OpenRouter) HTTP %s: %s", status, body[:500])
        if status == 402:
            raise error_cls("Недостаточно средств на OpenRouter для генерации изображений.")
        if status == 429:
            raise error_cls("Сервис изображений перегружен. Попробуйте через минуту.")
        raise error_cls(f"Не удалось получить изображение от Gemini (HTTP {status}).")

    if not isinstance(data, dict):
        raise error_cls("Gemini вернула некорректный ответ.")
    if data.get("error"):
        logger.warning("Gemini (OpenRouter) вернула ошибку: %s", data["error"])
        raise error_cls("Gemini не смогла обработать запрос. Попробуйте ещё раз.")

    data_url = _extract_image_data_url(data)
    if data_url is None:
        try:
            reply = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            reply = None
        logger.warning("Gemini не вернула изображение. Ответ модели: %s", str(reply)[:300])
        raise error_cls(
            "Gemini не смогла создать изображение по этому запросу. Попробуйте переформулировать."
        )

    return _decode_data_url(data_url, error_cls)


async def generate_image_txt2img(
    prompt: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Генерация изображения по тексту. width/height оставлены для совместимости и не используются."""
    if not prompt.strip():
        raise ImageGenerationError("Пустой промт.")
    content = [{"type": "text", "text": f"{_TXT2IMG_INSTRUCTION}{prompt.strip()}"}]
    return await _request_gemini_image(content, ImageGenerationError)


async def edit_image_img2img(
    prompt: str,
    image_bytes: bytes,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Редактирование загруженного фото. width/height оставлены для совместимости и не используются."""
    if not prompt.strip():
        raise ImageEditingError("Пустой промт.")
    if not image_bytes:
        raise ImageEditingError("Не переданы байты изображения.")
    content = [
        {"type": "text", "text": f"{_EDIT_INSTRUCTION}{prompt.strip()}"},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_bytes)}},
    ]
    return await _request_gemini_image(content, ImageEditingError)


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


def get_system_prompt() -> str:
    return SYSTEM_PROMPT_CLEAN_RUSSIAN


# ---------------------------------------------------------------------------
# Мемы (картинку и русский текст на ней целиком рисует Gemini)
# ---------------------------------------------------------------------------
async def generate_meme(topic: str) -> bytes:
    if not topic.strip():
        raise MemeGenerationError("Пустая тема для мема.")
    content = [{"type": "text", "text": f"{_MEME_INSTRUCTION}{topic.strip()}"}]
    return await _request_gemini_image(content, MemeGenerationError)


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
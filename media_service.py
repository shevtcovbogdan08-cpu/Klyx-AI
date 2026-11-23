# media_service.py
"""
Медиа-сервис Klyx AI: ИИ-видео по тексту (Hugging Face), видео в кружок, презентации в фоне.

Принципы (Bothost, 2 ГБ RAM):
  - тяжёлые задачи выполняются в фоновых asyncio-задачах, бот отвечает сразу;
  - FFmpeg запускается как асинхронный подпроцесс и только по одному за раз;
  - временные файлы лежат в каталогах klyx_* внутри системного tmp и удаляются сразу
    после отправки (плюс очистка «осиротевших» каталогов при старте);
  - презентации и видео из Hugging Face читаются в память и отдаются Telegram оттуда.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, FSInputFile

import config
import database as db
from ai_service import (
    AIServiceError,
    _ffmpeg_exe,
    generate_pptx_presentation,
    request_ai,
)
from i18n import current_lang

logger = logging.getLogger(__name__)

TEMP_PREFIX = "klyx_"


class MediaError(AIServiceError):
    pass


class VideoGenerationError(MediaError):
    pass


_FFMPEG_SEMAPHORE = asyncio.Semaphore(1)
_VIDEO_SEMAPHORE = asyncio.Semaphore(1)
_PPTX_SEMAPHORE = asyncio.Semaphore(1)
_ACTIVE_JOBS: set[tuple[int, str]] = set()
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------
def make_temp_dir(kind: str = "tmp") -> str:
    return tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{kind}_")


def cleanup_stale_temp_dirs(max_age_seconds: float = 3600.0) -> int:
    """Удаляет каталоги klyx_*, оставшиеся после аварийного завершения."""
    removed = 0
    base = Path(tempfile.gettempdir())
    now = time.time()
    try:
        for path in base.glob(f"{TEMP_PREFIX}*"):
            try:
                if path.is_dir() and now - path.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    if removed:
        logger.info("Удалено устаревших временных каталогов: %s", removed)
    return removed


def is_job_active(user_id: int, kind: str) -> bool:
    return (user_id, kind) in _ACTIVE_JOBS


def _spawn(coro: Any, key: tuple[int, str]) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(finished: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(finished)
        _ACTIVE_JOBS.discard(key)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error("Фоновая задача завершилась ошибкой: %r", finished.exception())

    task.add_done_callback(_done)


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)


async def _run_ffmpeg(args: list[str], timeout: float = 300.0) -> None:
    process = await asyncio.create_subprocess_exec(
        _ffmpeg_exe(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaError("Видео слишком длинное или повреждено.") from exc
    if process.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace").strip()[-300:]
        logger.warning("ffmpeg завершился с кодом %s: %s", process.returncode, tail)
        raise MediaError(f"Не удалось обработать видео: {tail or 'ошибка ffmpeg'}")


# ---------------------------------------------------------------------------
# Видео -> кружок (Telegram video note)
# ---------------------------------------------------------------------------
async def convert_to_video_note(src: str, dst: str) -> None:
    """Квадрат 1:1 по центру, 384x384, libx264, максимум 59 секунд, малый битрейт."""
    size = config.VIDEO_NOTE_SIZE
    video_filter = (
        f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}:flags=lanczos,setsar=1"
    )
    args = [
        "-y", "-hide_banner", "-loglevel", "error",
        "-t", "59", "-i", src,
        "-vf", video_filter,
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-maxrate", "800k", "-bufsize", "1600k",
        "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart",
        "-t", "59", "-threads", "2",
        dst,
    ]
    async with _FFMPEG_SEMAPHORE:
        await _run_ffmpeg(args)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise MediaError("Не удалось обработать видео: пустой результат.")


def start_note_job(bot: Bot, chat_id: int, user_id: int, tmp_dir: str, src_path: str, lang: str) -> bool:
    """Запускает конвертацию в фоне. Если у пользователя уже идёт такая задача — False."""
    key = (user_id, "note")
    if key in _ACTIVE_JOBS:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    _ACTIVE_JOBS.add(key)
    _spawn(run_note_job(bot, chat_id, tmp_dir, src_path, lang), key)
    return True


async def run_note_job(bot: Bot, chat_id: int, tmp_dir: str, src_path: str, lang: str) -> None:
    current_lang.set(lang)
    dst = os.path.join(tmp_dir, "note.mp4")
    try:
        await convert_to_video_note(src_path, dst)
        await bot.send_video_note(
            chat_id, FSInputFile(dst), length=config.VIDEO_NOTE_SIZE
        )
    except AIServiceError as exc:
        await _safe_send(bot, chat_id, f"⚠️ Не удалось сделать кружок: {exc}")
    except TelegramAPIError as exc:
        logger.warning("send_video_note не удался: %s", exc)
        await _safe_send(bot, chat_id, f"⚠️ Не удалось сделать кружок: {exc}")
    except Exception:
        logger.exception("Ошибка при создании кружка")
        await _safe_send(bot, chat_id, "⚠️ Не удалось сделать кружок: ошибка обработки.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # исходное и готовое видео удаляются сразу


# ---------------------------------------------------------------------------
# Видео по тексту: только настоящее ИИ-видео через Hugging Face (без склейки картинок)
# ---------------------------------------------------------------------------
VIDEO_BUSY_MESSAGE = "Видеосерверы сейчас заняты, попробуйте через пару минут"
_MIN_VIDEO_BYTES = 10_000


def _find_video_path(result: Any) -> str | None:
    if isinstance(result, str):
        if result.lower().endswith((".mp4", ".webm", ".mov")) and os.path.isfile(result):
            return result
        return None
    if isinstance(result, dict):
        for key in ("video", "path", "value", "name"):
            if key in result:
                found = _find_video_path(result[key])
                if found:
                    return found
        for value in result.values():
            found = _find_video_path(value)
            if found:
                return found
        return None
    if isinstance(result, (list, tuple)):
        for item in result:
            found = _find_video_path(item)
            if found:
                return found
    return None


def _pick_hf_call(client: Any, prompt: str) -> tuple[str, dict[str, Any]]:
    """Подбирает эндпоинт и аргументы Space по его описанию API."""
    info = client.view_api(return_format="dict", print_info=False)
    named: dict[str, Any] = info.get("named_endpoints", {}) if isinstance(info, dict) else {}
    if not named:
        raise VideoGenerationError("У Space нет именованных эндпоинтов.")

    api_name = config.HF_VIDEO_API_NAME
    if not api_name or api_name not in named:
        api_name = next((name for name in named if "video" in name.lower()), next(iter(named)))

    kwargs: dict[str, Any] = {}
    for param in named[api_name].get("parameters", []):
        name = str(param.get("parameter_name", ""))
        lowered = name.lower()
        if lowered in ("prompt", "text", "text_prompt", "caption", "description") or (
            "prompt" in lowered and "negative" not in lowered
        ):
            kwargs[name] = prompt
        elif not param.get("parameter_has_default", False):
            raise VideoGenerationError(f"Не удалось заполнить параметр {name} у Space.")
    if not any(value == prompt for value in kwargs.values()):
        raise VideoGenerationError("У Space не найден параметр для текстового запроса.")
    return api_name, kwargs


def _hf_space_generate_sync(prompt: str, space: str) -> bytes:
    """Второй эшелон: ZeroGPU Space через gradio_client."""
    from gradio_client import Client

    try:
        client = Client(space, hf_token=config.HF_TOKEN, verbose=False)
    except TypeError:
        client = Client(space, token=config.HF_TOKEN, verbose=False)

    api_name, kwargs = _pick_hf_call(client, prompt)
    result = client.predict(api_name=api_name, **kwargs)
    path = _find_video_path(result)
    if not path:
        raise VideoGenerationError("Space не вернул видеофайл.")
    try:
        return Path(path).read_bytes()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _hf_inference_video_sync(prompt: str, model: str) -> bytes:
    """Первый эшелон: официальный huggingface_hub.InferenceClient.text_to_video."""
    from huggingface_hub import InferenceClient

    client = InferenceClient(token=config.HF_TOKEN)
    video = client.text_to_video(prompt, model=model)
    if isinstance(video, (bytes, bytearray)):
        return bytes(video)
    raise VideoGenerationError("Модель вернула неожиданный формат видео.")


_VIDEO_TRANSLATE_SYSTEM = (
    "You write prompts for a text-to-video model. Translate the user's idea into ONE vivid English "
    "prompt of at most 40 words: the main subject, the action and the setting, then add "
    "'cinematic, realistic, natural motion'. Return ONLY the prompt, without quotes or comments."
)


async def translate_video_prompt(prompt: str) -> str:
    """Переводит запрос на английский и дополняет деталями (бесплатной текстовой моделью)."""
    messages = [
        {"role": "system", "content": _VIDEO_TRANSLATE_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        text, _model, _elapsed = await request_ai(messages, temperature=0.5, max_tokens=150)
        cleaned = " ".join(text.split()).strip("\"'`")
        if len(cleaned) >= 5:
            return cleaned[:400]
    except Exception as exc:
        logger.warning("Не удалось перевести запрос для видео: %s", exc)
    return prompt


async def generate_video(prompt: str) -> bytes:
    """
    Настоящее ИИ-видео: перебор моделей InferenceClient.text_to_video, затем ZeroGPU Spaces.
    Если всё занято — честная ошибка (без склейки картинок), попытка возвращается пользователю.
    """
    if not config.HF_TOKEN:
        raise VideoGenerationError("Генерация видео не настроена: не задан HF_TOKEN.")

    english_prompt = await translate_video_prompt(prompt)
    logger.info("Промпт для видеомодели: %s", english_prompt)
    deadline = time.monotonic() + config.HF_VIDEO_TOTAL_TIMEOUT

    attempts: list[tuple[str, Any, tuple[str, str]]] = []
    for model in config.HF_VIDEO_MODELS:
        attempts.append((f"model {model}", _hf_inference_video_sync, (english_prompt, model)))
    for space in config.HF_VIDEO_SPACES:
        attempts.append((f"space {space}", _hf_space_generate_sync, (english_prompt, space)))

    for label, function, args in attempts:
        remaining = deadline - time.monotonic()
        if remaining < 15:
            break
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(function, *args), timeout=min(config.HF_VIDEO_TIMEOUT, remaining)
            )
            if len(data) >= _MIN_VIDEO_BYTES:
                logger.info("Видео получено: %s (%s КБ)", label, len(data) // 1024)
                return data
            logger.warning("Видеосервер %s вернул слишком маленький файл", label)
        except Exception as exc:
            logger.warning("Видеосервер %s недоступен: %s", label, exc)

    raise VideoGenerationError(VIDEO_BUSY_MESSAGE)


def start_video_job(bot: Bot, chat_id: int, user_id: int, prompt: str, lang: str) -> bool:
    key = (user_id, "video")
    if key in _ACTIVE_JOBS:
        return False
    _ACTIVE_JOBS.add(key)
    _spawn(run_video_job(bot, chat_id, user_id, prompt, lang), key)
    return True


async def run_video_job(bot: Bot, chat_id: int, user_id: int, prompt: str, lang: str) -> None:
    current_lang.set(lang)
    try:
        async with _VIDEO_SEMAPHORE:
            video = await generate_video(prompt)
        await bot.send_video(
            chat_id,
            BufferedInputFile(video, filename="klyx_video.mp4"),
            caption=f"🎬 {html.escape(prompt)[:200]}",
            supports_streaming=True,
        )
    except Exception as exc:
        logger.warning("Генерация видео не удалась: %s", exc)
        await db.refund_video_request(user_id)
        reason = str(exc) if isinstance(exc, AIServiceError) else "ошибка отправки"
        await _safe_send(
            bot,
            chat_id,
            f"⚠️ Не удалось создать видео: {reason}\nПопытка возвращена, можно повторить.",
        )


# ---------------------------------------------------------------------------
# Презентации в фоне
# ---------------------------------------------------------------------------
def start_pptx_job(
    bot: Bot, chat_id: int, user_id: int, topic: str, slides: int, lang: str
) -> bool:
    key = (user_id, "pptx")
    if key in _ACTIVE_JOBS:
        return False
    _ACTIVE_JOBS.add(key)
    _spawn(run_pptx_job(bot, chat_id, topic, slides, lang), key)
    return True


async def run_pptx_job(bot: Bot, chat_id: int, topic: str, slides: int, lang: str) -> None:
    current_lang.set(lang)
    try:
        async with _PPTX_SEMAPHORE:
            data = await generate_pptx_presentation(topic, slides, lang)
        filename = "Presentation_Klyx_AI.pptx" if lang == "en" else "Презентация_Klyx_AI.pptx"
        await bot.send_document(
            chat_id,
            BufferedInputFile(data, filename=filename),
            caption=f"Презентация: {html.escape(topic.strip())[:200]}",
        )
    except Exception as exc:
        logger.warning("Генерация презентации не удалась: %s", exc)
        reason = str(exc) if isinstance(exc, AIServiceError) else "ошибка отправки"
        await _safe_send(bot, chat_id, f"⚠️ Не удалось создать презентацию: {reason}")

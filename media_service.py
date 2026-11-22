# media_service.py
"""
Медиа-сервис Klyx AI: видео по тексту, видео в кружок, презентации в фоне.

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
import json
import logging
import os
import re
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
    ImageGenerationError,
    _ffmpeg_exe,
    _fetch_pollinations_image,
    _with_quality_tags,
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
# Видео по тексту: Hugging Face Space (gradio_client) + резервное слайд-шоу
# ---------------------------------------------------------------------------
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


def _hf_generate_sync(prompt: str) -> bytes:
    from gradio_client import Client

    try:
        client = Client(config.HF_VIDEO_SPACE, hf_token=config.HF_TOKEN, verbose=False)
    except TypeError:
        client = Client(config.HF_VIDEO_SPACE, token=config.HF_TOKEN, verbose=False)

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


async def _generate_with_hf(prompt: str) -> bytes:
    data = await asyncio.wait_for(
        asyncio.to_thread(_hf_generate_sync, prompt), timeout=config.HF_VIDEO_TIMEOUT
    )
    if len(data) < 10_000:
        raise VideoGenerationError("Space вернул слишком маленький файл.")
    return data


_STORYBOARD_SYSTEM = (
    "You are a storyboard artist. Return ONLY valid JSON without markdown: "
    '{"scenes": ["...", "...", "...", "..."]}. '
    "Write exactly 4 consecutive scenes for a short video about the user's idea. "
    "Each scene is one detailed English image description (subject, camera angle, lighting, style). "
    "Keep the same main subject and the same visual style in all scenes. "
    "No text or letters in the images."
)


async def _storyboard(prompt: str) -> list[str]:
    messages = [
        {"role": "system", "content": _STORYBOARD_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        text, _model, _elapsed = await request_ai(messages, temperature=0.7, max_tokens=700)
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            scenes = json.loads(match.group(0)).get("scenes", [])
            cleaned = [" ".join(str(s).split()) for s in scenes if str(s).strip()]
            if len(cleaned) >= 3:
                return cleaned[:4]
    except Exception as exc:
        logger.warning("Не удалось составить раскадровку: %s", exc)
    suffixes = ("wide establishing shot", "medium shot", "close-up, dramatic light", "final cinematic shot")
    return [f"{prompt}, {suffix}" for suffix in suffixes]


async def _render_slideshow(image_paths: list[str], out_path: str) -> None:
    """Склеивает картинки в mp4: эффект движения камеры (zoompan) + плавные переходы."""
    count = len(image_paths)
    seconds, fade, fps, size = 3.0, 0.6, 25, 720
    frames = int(seconds * fps)

    args = ["-y", "-hide_banner", "-loglevel", "error"]
    for path in image_paths:
        args += ["-i", path]

    motions = (
        f"z='1+0.25*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        f"z='1.25-0.25*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        f"z='1.2':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'",
        f"z='1.2':x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'",
    )
    filters: list[str] = []
    for index in range(count):
        filters.append(
            f"[{index}:v]scale={size * 2}:{size * 2},"
            f"zoompan={motions[index % len(motions)]}:d={frames}:s={size}x{size}:fps={fps},"
            f"setsar=1,format=yuv420p[v{index}]"
        )
    last = "v0"
    for index in range(1, count):
        offset = round(index * (seconds - fade), 3)
        label = f"x{index}"
        filters.append(
            f"[{last}][v{index}]xfade=transition=fade:duration={fade}:offset={offset}[{label}]"
        )
        last = label

    args += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{last}]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-movflags", "+faststart", "-threads", "2",
        out_path,
    ]
    async with _FFMPEG_SEMAPHORE:
        await _run_ffmpeg(args, timeout=240.0)


async def _generate_slideshow_video(prompt: str) -> bytes:
    scenes = await _storyboard(prompt)
    tmp_dir = make_temp_dir("video")
    try:
        paths: list[str] = []
        for index, scene in enumerate(scenes):
            try:
                raw = await _fetch_pollinations_image(
                    _with_quality_tags(scene), ImageGenerationError, size=768
                )
            except AIServiceError as exc:
                logger.warning("Кадр %s не получен: %s", index + 1, exc)
                continue
            path = os.path.join(tmp_dir, f"scene{index}.jpg")
            Path(path).write_bytes(raw)
            paths.append(path)
        if len(paths) < 2:
            raise VideoGenerationError(
                "Бесплатный сервис картинок сейчас перегружен. Попробуйте ещё раз через минуту."
            )
        out_path = os.path.join(tmp_dir, "video.mp4")
        await _render_slideshow(paths, out_path)
        return Path(out_path).read_bytes()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def generate_video(prompt: str) -> bytes:
    """Hugging Face (если задан HF_TOKEN), иначе или при сбое — слайд-шоу через FFmpeg."""
    if config.HF_TOKEN:
        try:
            return await _generate_with_hf(prompt)
        except Exception as exc:
            logger.warning("Hugging Face не справился (%s), переходим на резервный метод", exc)
    try:
        return await _generate_slideshow_video(prompt)
    except MediaError:
        raise
    except AIServiceError as exc:
        raise VideoGenerationError(str(exc)) from exc


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

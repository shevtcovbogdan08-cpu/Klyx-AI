# media_service.py
"""
Медиа-сервис Klyx AI: ИИ-видео по тексту (бесплатные Gradio Spaces), видео в кружок, презентации в фоне.

Принципы (Bothost, 2 ГБ RAM):
  - тяжёлые задачи выполняются в фоновых asyncio-задачах, бот отвечает сразу;
  - FFmpeg запускается как асинхронный подпроцесс и только по одному за раз;
  - временные файлы лежат в каталогах klyx_* внутри системного tmp и удаляются сразу
    после отправки (плюс очистка «осиротевших» каталогов при старте);
  - презентации и видео читаются в память и отдаются Telegram оттуда;
  - любую фоновую задачу можно мгновенно остановить через cancel_job() (кнопка «⏹ Остановить»).
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
    call_guarded,
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
_JOB_TASKS: dict[tuple[int, str], asyncio.Task[Any]] = {}


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


def cancel_job(user_id: int, kind: str) -> bool:
    """
    Мгновенно останавливает фоновую задачу пользователя (кнопка «⏹ Остановить»).
    Возврат попытки выполняют сами задачи в обработчике CancelledError.
    Возвращает True, если задача была найдена и отменена.
    """
    task = _JOB_TASKS.get((user_id, kind))
    if task is None or task.done():
        return False
    task.cancel()
    return True


def cancel_all_jobs(user_id: int) -> int:
    """Останавливает все фоновые задачи пользователя. Возвращает число отменённых."""
    count = 0
    for (uid, kind) in list(_JOB_TASKS):
        if uid == user_id and cancel_job(uid, kind):
            count += 1
    return count


def _spawn(coro: Any, key: tuple[int, str]) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    _JOB_TASKS[key] = task

    def _done(finished: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(finished)
        _ACTIVE_JOBS.discard(key)
        if _JOB_TASKS.get(key) is finished:
            _JOB_TASKS.pop(key, None)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error("Фоновая задача завершилась ошибкой: %r", finished.exception())

    task.add_done_callback(_done)


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)


def _stopped_text(lang: str) -> str:
    if lang == "en":
        return "⏹ Stopped. The attempt has been refunded."
    return "⏹ Остановлено. Попытка возвращена."


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
    except asyncio.CancelledError:
        # Пользователь нажал «Стоп»: гасим ffmpeg, чтобы не держать процессор.
        process.kill()
        await process.wait()
        raise
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
    except asyncio.CancelledError:
        await _safe_send(bot, chat_id, _stopped_text(lang))
        raise
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
# Видео по тексту: только настоящее ИИ-видео через БЕСПЛАТНЫЕ Gradio Spaces
# ---------------------------------------------------------------------------
VIDEO_BUSY_MESSAGE = "Видеосерверы сейчас заняты, попробуйте через пару минут"
_MIN_VIDEO_BYTES = 10_000

# Платные модели (Wan-AI/..., tencent/HunyuanVideo) убраны из InferenceClient.text_to_video:
# они отвечают HTTP 402 Payment Required. Используем только бесплатные Spaces.
# Порядок: основной живой Space, затем резервные. Переопределяется переменной HF_VIDEO_SPACES.
_DEFAULT_VIDEO_SPACES: tuple[str, ...] = (
    "Lightricks/ltx-video-distilled",
    "linoyts/ltx-video-distilled",
    "Heartsync/WAN2.1-T2V-1.3B-Fast",
)


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    items = tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())
    return items or default


def _video_spaces() -> tuple[str, ...]:
    return _env_list("HF_VIDEO_SPACES", _DEFAULT_VIDEO_SPACES)


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
    """Автоматически определяет эндпоинт и аргументы Space по client.view_api(return_format="dict")."""
    info = client.view_api(return_format="dict", print_info=False)
    named: dict[str, Any] = info.get("named_endpoints", {}) if isinstance(info, dict) else {}
    if not named:
        raise VideoGenerationError("У Space нет именованных эндпоинтов.")

    api_name = getattr(config, "HF_VIDEO_API_NAME", "") or ""
    if not api_name or api_name not in named:
        # Предпочитаем эндпоинты с text-to-video / generate / video в названии.
        preferred = next(
            (
                name
                for name in named
                if any(word in name.lower() for word in ("text_to_video", "text-to-video", "t2v"))
            ),
            None,
        )
        if preferred is None:
            preferred = next(
                (name for name in named if any(w in name.lower() for w in ("generate", "video"))),
                next(iter(named)),
            )
        api_name = preferred

    kwargs: dict[str, Any] = {}
    prompt_filled = False
    for param in named[api_name].get("parameters", []):
        name = str(param.get("parameter_name", ""))
        lowered = name.lower()
        is_prompt = lowered in ("prompt", "text", "text_prompt", "caption", "description") or (
            "prompt" in lowered and "negative" not in lowered
        )
        if is_prompt and not prompt_filled:
            kwargs[name] = prompt
            prompt_filled = True
        elif not param.get("parameter_has_default", False):
            # Обязательный параметр без значения по умолчанию: пробуем безопасные значения.
            lowered_label = lowered
            if "negative" in lowered_label:
                kwargs[name] = "worst quality, inconsistent motion, blurry, jittery, distorted"
            elif "mode" in lowered_label:
                kwargs[name] = "text-to-video"
            elif "seed" in lowered_label:
                kwargs[name] = 42
            else:
                raise VideoGenerationError(f"Не удалось заполнить параметр {name} у Space.")
    if not prompt_filled:
        raise VideoGenerationError("У Space не найден параметр для текстового запроса.")
    return api_name, kwargs


def _hf_space_generate_sync(prompt: str, space: str) -> bytes:
    """Бесплатный Gradio Space через gradio_client (авторизация именно hf_token=HF_TOKEN)."""
    from gradio_client import Client

    client = Client(space, hf_token=config.HF_TOKEN, verbose=False)
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Не удалось перевести запрос для видео: %s", exc)
    return prompt


async def generate_video(prompt: str) -> bytes:
    """
    Настоящее ИИ-видео: перебор бесплатных Gradio Spaces
    (Lightricks/ltx-video-distilled -> linoyts/ltx-video-distilled -> Heartsync/WAN2.1-T2V-1.3B-Fast).
    Если всё занято — честная ошибка (без склейки картинок), попытка возвращается пользователю.
    """
    if not config.HF_TOKEN:
        raise VideoGenerationError("Генерация видео не настроена: не задан HF_TOKEN.")

    english_prompt = await translate_video_prompt(prompt)
    logger.info("Промпт для видеомодели: %s", english_prompt)
    deadline = time.monotonic() + config.HF_VIDEO_TOTAL_TIMEOUT

    for space in _video_spaces():
        remaining = deadline - time.monotonic()
        if remaining < 15:
            break
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(call_guarded, _hf_space_generate_sync, english_prompt, space),
                timeout=min(config.HF_VIDEO_TIMEOUT, remaining),
            )
            if len(data) >= _MIN_VIDEO_BYTES:
                logger.info("Видео получено: space %s (%s КБ)", space, len(data) // 1024)
                return data
            logger.warning("Видеосервер %s вернул слишком маленький файл", space)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Видеосервер %s недоступен: %s", space, exc)

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
    except asyncio.CancelledError:
        # «⏹ Остановить»: возвращаем потраченную попытку и сообщаем об остановке.
        try:
            await asyncio.shield(db.refund_video_request(user_id))
        except Exception:
            logger.exception("Не удалось вернуть попытку после остановки видео")
        await _safe_send(bot, chat_id, _stopped_text(lang))
        raise
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
    except asyncio.CancelledError:
        await _safe_send(bot, chat_id, _stopped_text(lang))
        raise
    except Exception as exc:
        logger.warning("Генерация презентации не удалась: %s", exc)
        reason = str(exc) if isinstance(exc, AIServiceError) else "ошибка отправки"
        await _safe_send(bot, chat_id, f"⚠️ Не удалось создать презентацию: {reason}")

# web_server.py
"""
Веб-сервер Klyx AI для Telegram Mini App.
"""

from __future__ import annotations

import base64
import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import config
import database as db
from ai_service import (
    AIServiceError,
    AllModelsFailedError,
    AudioConversionError,
    ImageEditingError,
    ImageGenerationError,
    MemeGenerationError,
    PptxGenerationError,
    WordReportError,
    clean_response_text,
    convert_audio_to_voice,
    edit_image_img2img,
    generate_image_txt2img,
    generate_meme,
    generate_pptx_presentation,
    generate_word_report,
    get_system_prompt,
    image_to_data_url,
    request_ai,
)

logger = logging.getLogger(__name__)


class _UploadError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


async def _read_json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Тело запроса должно быть JSON-объектом.")
    return data


def _extract_init_data(payload: dict[str, Any]) -> str:
    init_data = payload.get("initData")
    if not isinstance(init_data, str) or not init_data.strip():
        raise web.HTTPUnauthorized(text="Отсутствует initData.")
    return init_data


def _validate_init_data(bot: Bot, init_data: str) -> int:
    try:
        parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    except ValueError as exc:
        logger.warning("Невалидный initData: %s", exc)
        raise web.HTTPUnauthorized(text="Невалидный initData.") from exc
    if parsed.user is None:
        raise web.HTTPUnauthorized(text="В initData отсутствует пользователь.")
    return int(parsed.user.id)


async def _ensure_user(telegram_id: int, parsed_user: Any) -> dict[str, Any]:
    user = await db.get_user(telegram_id)
    if user is not None:
        return user
    return await db.get_or_create_user(
        telegram_id,
        getattr(parsed_user, "username", None),
        getattr(parsed_user, "first_name", None),
    )


def _build_system_messages(user: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": get_system_prompt()},
        {
            "role": "system",
            "content": (
                "Ты — Klyx AI, бесплатный и дружелюбный ИИ-ассистент. "
                "Отвечай полезно и прозрачно. Не упоминай платные подписки или рекламу."
            ),
        },
    ]

    length = user.get("style") or "detailed"
    expertise = user.get("expertise") or "novice"
    tone = user.get("tone") or "friendly"

    style_parts: list[str] = []
    if length == "short":
        style_parts.append("Отвечай кратко, по существу.")
    else:
        style_parts.append("Отвечай подробно, с пояснениями и примерами.")
    if expertise == "novice":
        style_parts.append("Объясняй простыми словами.")
    else:
        style_parts.append("Можно использовать профессиональные термины.")
    if tone == "friendly":
        style_parts.append("Тон дружелюбный и поддерживающий.")
    else:
        style_parts.append("Тон строгий и деловой.")

    messages.append({"role": "system", "content": " ".join(style_parts)})

    role_key = user.get("current_role")
    role_prompts = {
        "programmer": "Ты опытный Senior Python-разработчик.",
        "teacher": "Ты терпеливый учитель.",
        "copywriter": "Ты профессиональный копирайтер.",
        "translator": "Ты профессиональный переводчик.",
        "socrates": "Ты Сократ, веди диалог через вопросы.",
        "critic": "Ты строгий, но конструктивный критик.",
    }
    if role_key and role_key in role_prompts:
        messages.append({"role": "system", "content": role_prompts[role_key]})

    if facts:
        facts_text = "\n".join(f"- {fact['fact']}" for fact in facts)
        messages.append(
            {"role": "system", "content": f"Что ты знаешь о пользователе:\n{facts_text}"}
        )

    return messages


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(config.BASE_DIR / "webapp" / "index.html")


async def handle_static(request: web.Request) -> web.FileResponse:
    filename = request.match_info.get("filename", "")
    if not filename or "/" in filename or ".." in filename:
        raise web.HTTPNotFound()
    return web.FileResponse(config.BASE_DIR / "webapp" / filename)


async def api_limits(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    text_status = await db.get_user_daily_status(telegram_id)
    img_status = await db.get_img2img_status(telegram_id)

    return web.json_response(
        {
            "ok": True,
            "limits": {
                "text": {
                    "used": text_status["used"],
                    "limit": text_status["limit"],
                    "remaining": text_status["remaining"],
                },
                "img2img": {
                    "used": img_status["used"],
                    "limit": img_status["limit"],
                    "remaining": img_status["remaining"],
                },
            },
        }
    )


async def api_models(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    _validate_init_data(bot, init_data)
    return web.json_response({"ok": True, "models": list(config.FREE_MODELS)})


async def api_profile(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    profile = await db.get_full_profile(telegram_id)
    if profile is None:
        return web.json_response({"ok": False, "error": "Профиль не найден."}, status=404)

    return web.json_response(
        {
            "ok": True,
            "profile": {
                "telegram_id": profile["telegram_id"],
                "first_name": profile.get("first_name"),
                "style": profile.get("style"),
                "expertise": profile.get("expertise"),
                "tone": profile.get("tone"),
                "current_model": profile.get("current_model"),
                "current_role": profile.get("current_role"),
                "facts": [
                    {"id": f["id"], "fact": f["fact"]} for f in profile.get("facts", [])
                ],
            },
        }
    )


async def api_chat(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    message_text = payload.get("message")
    if not isinstance(message_text, str) or not message_text.strip():
        return web.json_response({"ok": False, "error": "Пустое сообщение."}, status=400)
    message_text = message_text.strip()[:4000]

    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    user = await _ensure_user(telegram_id, parsed.user)

    allowed, remaining = await db.consume_request(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    await db.add_history_message(telegram_id, "user", message_text)
    facts = await db.get_memory_facts(telegram_id)
    messages = _build_system_messages(user, facts)
    history = await db.get_history(telegram_id)
    for item in history:
        if item["role"] in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": message_text})

    try:
        answer, model_used, elapsed = await request_ai(
            messages, preferred_model=user.get("current_model")
        )
    except AllModelsFailedError as exc:
        return web.json_response(
            {"ok": False, "error": f"Все бесплатные модели недоступны: {exc}"}, status=502
        )
    except AIServiceError as exc:
        return web.json_response(
            {"ok": False, "error": f"Ошибка ИИ-сервиса: {exc}"}, status=502
        )

    answer = clean_response_text(answer)
    await db.add_history_message(telegram_id, "assistant", answer, model_used)

    return web.json_response(
        {
            "ok": True,
            "answer": answer,
            "elapsed": elapsed,
            "remaining": remaining,
        }
    )


async def api_memory_add(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    fact = payload.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        return web.json_response({"ok": False, "error": "Пустой факт."}, status=400)

    ok, reason = await db.add_memory_fact(telegram_id, fact.strip())
    if not ok:
        messages = {
            "limit": f"Достигнут лимит фактов ({config.MAX_MEMORY_FACTS}).",
            "too_long": "Факт слишком длинный (максимум 500 символов).",
            "empty": "Пустой факт.",
        }
        return web.json_response(
            {"ok": False, "error": messages.get(reason, "Не удалось сохранить факт.")},
            status=400,
        )
    return web.json_response({"ok": True})


async def api_memory_delete(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        fact_id = int(payload.get("fact_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный fact_id."}, status=400)

    deleted = await db.delete_memory_fact(telegram_id, fact_id)
    if not deleted:
        return web.json_response({"ok": False, "error": "Факт не найден."}, status=404)
    return web.json_response({"ok": True})


async def api_memory_clear(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    removed = await db.clear_all_memory(telegram_id)
    return web.json_response({"ok": True, "removed": removed})


async def api_image_txt2img(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return web.json_response({"ok": False, "error": "Пустой промт."}, status=400)

    try:
        image_bytes = await generate_image_txt2img(prompt.strip())
    except ImageGenerationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "image": image_to_data_url(image_bytes)})


async def api_image_img2img(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    prompt = payload.get("prompt")
    image_url = payload.get("image_url")

    if not isinstance(prompt, str) or not prompt.strip():
        return web.json_response({"ok": False, "error": "Пустой промт."}, status=400)
    if not isinstance(image_url, str) or not image_url.strip():
        return web.json_response({"ok": False, "error": "Не передано изображение."}, status=400)

    allowed, remaining = await db.consume_img2img_request(telegram_id)
    if not allowed:
        return web.json_response(
            {
                "ok": False,
                "error": f"Лимит редактирования фото исчерпан (10 в сутки). Осталось: {remaining}.",
            },
            status=429,
        )

    try:
        if image_url.startswith("data:"):
            _, encoded = image_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
        else:
            image_bytes = base64.b64decode(image_url)
        result_bytes = await edit_image_img2img(prompt.strip(), image_bytes)
    except (ImageEditingError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "image": image_to_data_url(result_bytes)})


async def api_image_meme(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    try:
        meme_bytes = await generate_meme(topic.strip())
    except MemeGenerationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "image": image_to_data_url(meme_bytes)})


async def api_word_report(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    allowed, remaining = await db.consume_request(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    try:
        docx_bytes = await generate_word_report(topic.strip())
    except WordReportError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    filename = "klyx_report.docx"
    sent = await _send_document_to_user(
        bot, telegram_id, docx_bytes, filename, f"Word-отчёт: {html.escape(topic.strip())[:200]}"
    )
    response: dict[str, Any] = {"ok": True, "sent": sent, "filename": filename}
    if not sent:
        response["file"] = base64.b64encode(docx_bytes).decode("ascii")
    return web.json_response(response)


async def api_presentation(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return web.json_response({"ok": False, "error": "Пустая тема."}, status=400)

    allowed, remaining = await db.consume_request(telegram_id)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"Суточный лимит исчерпан. Осталось: {remaining}."},
            status=429,
        )

    try:
        pptx_bytes = await generate_pptx_presentation(topic.strip())
    except PptxGenerationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    filename = "Презентация_Klyx_AI.pptx"
    sent = await _send_document_to_user(
        bot, telegram_id, pptx_bytes, filename, f"Презентация: {html.escape(topic.strip())[:200]}"
    )
    response: dict[str, Any] = {"ok": True, "sent": sent, "filename": filename}
    if not sent:
        response["file"] = base64.b64encode(pptx_bytes).decode("ascii")
    return web.json_response(response)


async def _send_document_to_user(
    bot: Bot, telegram_id: int, data: bytes, filename: str, caption: str
) -> bool:
    """Отправляет файл пользователю в личные сообщения от имени бота (Bot API sendDocument)."""
    try:
        await bot.send_document(
            chat_id=telegram_id,
            document=BufferedInputFile(data, filename=filename),
            caption=caption,
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить документ пользователю %s: %s", telegram_id, exc)
        return False


async def _read_audio_upload(bot: Bot, request: web.Request) -> tuple[int, bytes, str | None]:
    """Читает аудио из multipart/form-data (initData + file) или из JSON (initData + audio в Base64)."""
    max_size = config.MAX_AUDIO_FILE_SIZE_BYTES
    too_large = _UploadError(
        f"Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ.", status=413
    )

    if (request.content_type or "").startswith("multipart/"):
        telegram_id: int | None = None
        audio: bytes | None = None
        filename: str | None = None
        reader = await request.multipart()
        async for part in reader:
            if part.name == "initData":
                telegram_id = _validate_init_data(bot, (await part.text()).strip())
            elif part.name == "file":
                if telegram_id is None:
                    raise web.HTTPUnauthorized(text="Отсутствует initData.")
                filename = part.filename
                buffer = bytearray()
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > max_size:
                        raise too_large
                audio = bytes(buffer)
        if telegram_id is None:
            raise web.HTTPUnauthorized(text="Отсутствует initData.")
        if not audio:
            raise _UploadError("Не передан аудиофайл.")
        return telegram_id, audio, filename

    payload = await _read_json(request)
    telegram_id = _validate_init_data(bot, _extract_init_data(payload))
    raw = payload.get("audio")
    if not isinstance(raw, str) or not raw.strip():
        raise _UploadError("Не передан аудиофайл.")
    encoded = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        audio = base64.b64decode(encoded)
    except ValueError as exc:
        raise _UploadError("Некорректные данные аудио.") from exc
    if len(audio) > max_size:
        raise too_large
    name = payload.get("filename")
    return telegram_id, audio, name if isinstance(name, str) else None


async def api_audio_to_voice(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    try:
        telegram_id, audio_bytes, filename = await _read_audio_upload(bot, request)
    except _UploadError as exc:
        return web.json_response({"ok": False, "error": exc.message}, status=exc.status)

    try:
        ogg_bytes = await convert_audio_to_voice(audio_bytes, filename)
    except AudioConversionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    try:
        await bot.send_voice(
            chat_id=telegram_id,
            voice=BufferedInputFile(ogg_bytes, filename="voice.ogg"),
        )
    except TelegramForbiddenError:
        return web.json_response(
            {"ok": False, "error": "Бот не может написать вам. Откройте чат с ботом и нажмите /start."},
            status=403,
        )
    except TelegramAPIError as exc:
        logger.warning("sendVoice не удался для %s: %s", telegram_id, exc)
        return web.json_response(
            {
                "ok": False,
                "error": "Telegram не принял голосовое сообщение. Проверьте настройки приватности голосовых.",
            },
            status=502,
        )

    return web.json_response({"ok": True, "sent": True, "filename": filename or "audio"})


async def api_tasks_list(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    tasks = await db.get_tasks(telegram_id)
    summary = await db.get_tasks_summary(telegram_id)
    return web.json_response(
        {
            "ok": True,
            "tasks": [
                {"id": t["id"], "text": t["text"], "is_done": int(t["is_done"])}
                for t in tasks
            ],
            "summary": summary,
        }
    )


async def api_tasks_add(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)
    parsed = safe_parse_webapp_init_data(token=bot.token, init_data=init_data)
    await _ensure_user(telegram_id, parsed.user)

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"ok": False, "error": "Пустая задача."}, status=400)

    ok, reason, task_id = await db.add_task(telegram_id, text.strip())
    if not ok:
        messages = {
            "limit": f"Достигнут лимит задач ({config.MAX_TASKS_PER_USER}).",
            "too_long": "Задача длиннее 300 символов.",
            "empty": "Пустая задача.",
        }
        return web.json_response(
            {"ok": False, "error": messages.get(reason, "Не удалось добавить задачу.")},
            status=400,
        )
    return web.json_response({"ok": True, "task_id": task_id})


async def api_tasks_toggle(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        task_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный task_id."}, status=400)

    new_state = await db.toggle_task(telegram_id, task_id)
    if new_state is None:
        return web.json_response({"ok": False, "error": "Задача не найдена."}, status=404)
    return web.json_response({"ok": True, "is_done": int(new_state)})


async def api_tasks_delete(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    try:
        task_id = int(payload.get("task_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректный task_id."}, status=400)

    deleted = await db.delete_task(telegram_id, task_id)
    if not deleted:
        return web.json_response({"ok": False, "error": "Задача не найдена."}, status=404)
    return web.json_response({"ok": True})


async def api_tasks_clear_done(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]
    payload = await _read_json(request)
    init_data = _extract_init_data(payload)
    telegram_id = _validate_init_data(bot, init_data)

    removed = await db.clear_completed_tasks(telegram_id)
    return web.json_response({"ok": True, "removed": removed})


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["bot"] = bot

    app.router.add_get("/", handle_index)
    app.router.add_get("/webapp/{filename}", handle_static)

    app.router.add_post("/api/limits", api_limits)
    app.router.add_post("/api/models", api_models)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/memory/add", api_memory_add)
    app.router.add_post("/api/memory/delete", api_memory_delete)
    app.router.add_post("/api/memory/clear", api_memory_clear)
    app.router.add_post("/api/image/txt2img", api_image_txt2img)
    app.router.add_post("/api/image/img2img", api_image_img2img)
    app.router.add_post("/api/image/meme", api_image_meme)
    app.router.add_post("/api/audio/to_voice", api_audio_to_voice)
    app.router.add_post("/api/word_report", api_word_report)
    app.router.add_post("/api/presentation", api_presentation)
    app.router.add_post("/api/tasks/list", api_tasks_list)
    app.router.add_post("/api/tasks/add", api_tasks_add)
    app.router.add_post("/api/tasks/toggle", api_tasks_toggle)
    app.router.add_post("/api/tasks/delete", api_tasks_delete)
    app.router.add_post("/api/tasks/clear_done", api_tasks_clear_done)

    return app


async def run_web_server(bot: Bot) -> None:
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.WEBAPP_PORT)
    await site.start()
    logger.info("Веб-сервер Klyx AI запущен на порту %s", config.WEBAPP_PORT)
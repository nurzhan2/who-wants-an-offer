"""Messages to the owner's own Telegram chat, through the owner's bot.

``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are notification settings, not
the Telegram source: a bot the owner created writes to the owner's chat, and to
nobody else. The bot token is part of every Bot API URL, so no URL here is ever
logged; a failure is logged by status code only, and never raised — a missed
notification must not stop the worker that found the vacancy.
"""

from typing import Any, Final

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TELEGRAM_API: Final[str] = "https://api.telegram.org"
#: Telegram refuses a message longer than this, and a caption longer than 1024.
MESSAGE_LIMIT: Final[int] = 4096
CAPTION_LIMIT: Final[int] = 1024


def configured() -> bool:
    """Whether a bot and a chat are set."""
    return settings.telegram_bot_token is not None and bool(settings.telegram_chat_id)


def _url(method: str) -> str:
    token = settings.telegram_bot_token
    secret = token.get_secret_value() if token is not None else ""
    return f"{TELEGRAM_API}/bot{secret}/{method}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: One row of inline buttons: (label, callback data). Telegram allows 64 bytes
#: of callback data, which a prefix and a UUID fit in.
Buttons = list[list[tuple[str, str]]]


def keyboard(buttons: Buttons) -> dict[str, Any]:
    """Inline buttons as the Bot API wants them."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]
    }


async def send_text(
    text: str,
    *,
    buttons: Buttons | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """One message to the owner's chat. False when not set up or refused."""
    if not configured():
        return False
    payload: dict[str, object] = {
        "chat_id": settings.telegram_chat_id,
        "text": _cut(text, MESSAGE_LIMIT),
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = keyboard(buttons)
    return await _post("sendMessage", client=client, json=payload)


async def edit_text(message_id: int, text: str, *, buttons: Buttons | None = None) -> bool:
    """Replace a message the bot sent, e.g. the filter menu after a tap."""
    if not configured():
        return False
    payload: dict[str, object] = {
        "chat_id": settings.telegram_chat_id,
        "message_id": message_id,
        "text": _cut(text, MESSAGE_LIMIT),
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = keyboard(buttons)
    return await _post("editMessageText", client=None, json=payload)


async def answer_callback(callback_id: str, text: str = "") -> bool:
    """Stop the spinner on a tapped button, optionally with a short toast."""
    if not configured():
        return False
    return await _post(
        "answerCallbackQuery",
        client=None,
        json={"callback_query_id": callback_id, "text": _cut(text, 200)},
    )


async def get_updates(offset: int, *, timeout: int = 25) -> list[dict[str, Any]]:
    """Long-poll the bot's updates. Empty on any failure; never logs the URL."""
    if not configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout + 10) as http:
            response = await http.post(
                _url("getUpdates"),
                json={
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("notify.updates_failed", error=type(exc).__name__)
        return []
    if response.status_code != httpx.codes.OK:
        logger.warning("notify.updates_refused", status=response.status_code)
        return []
    result = response.json().get("result")
    return result if isinstance(result, list) else []


async def send_document(
    filename: str,
    content: bytes,
    *,
    caption: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """One file to the owner's chat, with a short caption."""
    if not configured():
        return False
    return await _post(
        "sendDocument",
        client=client,
        data={"chat_id": str(settings.telegram_chat_id), "caption": _cut(caption, CAPTION_LIMIT)},
        files={"document": (filename, content)},
    )


async def _post(
    method: str,
    *,
    client: httpx.AsyncClient | None,
    json: dict[str, object] | None = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes]] | None = None,
) -> bool:
    own = client is None
    http = client or httpx.AsyncClient(timeout=settings.http_timeout)
    try:
        response = await http.post(_url(method), json=json, data=data, files=files)
    except httpx.HTTPError as exc:
        logger.warning("notify.failed", method=method, error=type(exc).__name__)
        return False
    finally:
        if own:
            await http.aclose()
    if response.status_code != httpx.codes.OK:
        logger.warning("notify.refused", method=method, status=response.status_code)
        return False
    return True


async def set_menu_button(url: str, text: str = "Открыть") -> bool:
    """Put the Mini App on the bot's menu button in the owner's chat."""
    if not configured():
        return False
    return await _post(
        "setChatMenuButton",
        client=None,
        json={
            "chat_id": settings.telegram_chat_id,
            "menu_button": {"type": "web_app", "text": text, "web_app": {"url": url}},
        },
    )

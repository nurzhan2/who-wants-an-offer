"""Messages to the owner's own Telegram chat, through the owner's bot.

``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are notification settings, not
the Telegram source: a bot the owner created writes to the owner's chat, and to
nobody else. The bot token is part of every Bot API URL, so no URL here is ever
logged; a failure is logged by status code only, and never raised — a missed
notification must not stop the worker that found the vacancy.
"""

from typing import Final

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


async def send_text(text: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """One message to the owner's chat. False when not set up or refused."""
    if not configured():
        return False
    return await _post(
        "sendMessage",
        client=client,
        json={
            "chat_id": settings.telegram_chat_id,
            "text": _cut(text, MESSAGE_LIMIT),
            "disable_web_page_preview": True,
        },
    )


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

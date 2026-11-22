"""Who is calling the Mini App's API: Telegram's signed ``initData``.

The Mini App opens inside Telegram, where a browser password prompt is not an
option, so it does not sit behind ``DASHBOARD_PASSWORD``. Instead every request
carries the ``initData`` string Telegram hands the page, and this module checks
it the way Telegram documents (core.telegram.org/bots/webapps, "Validating data
received via the Mini App"):

    secret = HMAC_SHA256(key="WebAppData", msg=<bot token>)
    hash   = hex(HMAC_SHA256(key=secret, msg=<the other fields, sorted, "k=v" joined by \\n>))

A valid signature proves Telegram issued the data to *this* bot; it does not
prove who the user is allowed to be. That second check is the owner's id: in a
private chat with the bot, ``TELEGRAM_CHAT_ID`` is the owner's user id, and any
other user is refused. Data older than a day is refused too, so a leaked string
does not work forever.
"""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status

from app.core.config import settings

#: How long a signed ``initData`` is accepted after Telegram issued it.
MAX_AGE_SECONDS = 24 * 3600


def verify(init_data: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The user Telegram vouches for, or None when the data is not genuine."""
    token = settings.telegram_bot_token
    if token is None or not init_data:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.get_secret_value().encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        issued = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        return None
    if (now or time.time()) - issued > MAX_AGE_SECONDS or not isinstance(user, dict):
        return None
    return user


def is_owner(user: dict[str, Any]) -> bool:
    return bool(settings.telegram_chat_id) and str(user.get("id")) == str(settings.telegram_chat_id)


async def require_owner(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
) -> dict[str, Any]:
    """FastAPI dependency: the Mini App's caller, who must be the owner."""
    user = verify(x_telegram_init_data)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Откройте приложение из Telegram-бота.")
    if not is_owner(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Это приложение только для владельца.")
    return user

"""Reading public Telegram channels through a user session, and nothing else.

The one place in ``backend/`` that holds an account's session (see
docs/ARCHITECTURE.md, «Исключение: Telegram»). What this module may do is
deliberately narrow, and the narrowness is the safety argument:

* **read only** — ``get_entity`` to resolve a public ``@username`` and
  ``get_messages`` to read its history. No ``JoinChannelRequest``, no
  ``send_message``, no reactions, no read receipts: a public channel's history
  is readable by username without joining it;
* **no flood handling of its own** — ``flood_sleep_threshold=0`` makes Telethon
  raise every ``FloodWaitError`` instead of sleeping on it silently, so the
  connector can wait exactly what Telegram asked and write it down;
* **never logs the session, the phone or a message** — only usernames, ids and
  counts.

Telethon is imported lazily inside :class:`TelethonReader`, so a test of the
connector never needs it and a missing install is a configuration answer rather
than an import error for every source.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ChannelPost:
    """One message of a public channel, as much of it as the connector reads."""

    id: int
    date: datetime
    text: str


class FloodWaitRequired(Exception):  # noqa: N818 - Telegram's own name for the event
    """Telegram asked us to wait this many seconds before the next request."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"FloodWait {seconds}s")
        self.seconds = seconds


class ChannelMissingError(Exception):
    """The username names no public channel (renamed, deleted or private)."""


class SessionExpiredError(Exception):
    """The stored session is no longer authorised; a person must log in again."""


class ChannelReader(Protocol):
    """What the connector needs from Telegram. A fake implements it in tests."""

    async def recent(self, username: str, *, after_id: int, limit: int) -> Sequence[ChannelPost]:
        """Posts newer than ``after_id``, newest first, at most ``limit``."""
        ...

    async def close(self) -> None:
        """Release the connection."""
        ...


class TelethonReader:
    """The real reader: one Telethon client on the owner's stored session."""

    def __init__(self, *, api_id: int, api_hash: str, session_path: Path) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        # Any: Telethon ships no type information, and the client is used only
        # through the three calls wrapped below.
        self._client: Any = None

    async def _connected(self) -> Any:
        if self._client is not None:
            return self._client
        from telethon import TelegramClient

        client = TelegramClient(
            str(self._session_path),
            self._api_id,
            self._api_hash,
            flood_sleep_threshold=0,
            receive_updates=False,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SessionExpiredError(
                "сессия Telegram не авторизована — войдите заново: "
                "uv run python -m app.sources.telegram_login"
            )
        self._client = client
        return client

    async def recent(self, username: str, *, after_id: int, limit: int) -> Sequence[ChannelPost]:
        from telethon import errors

        client = await self._connected()
        try:
            entity = await client.get_entity(username)
            messages = await client.get_messages(entity, limit=limit, min_id=after_id)
        except errors.FloodWaitError as exc:
            raise FloodWaitRequired(int(exc.seconds)) from exc
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError) as exc:
            raise ChannelMissingError(username) from exc
        except errors.AuthKeyError as exc:
            raise SessionExpiredError("сессия Telegram отозвана — войдите заново") from exc
        return [
            ChannelPost(id=int(message.id), date=message.date, text=message.message or "")
            for message in messages
            if getattr(message, "message", None)
        ]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

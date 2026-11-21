"""Log in to Telegram once, by hand, and leave a session file for the source.

    uv run python -m app.sources.telegram_login            # session file
    uv run python -m app.sources.telegram_login --string   # also print it for a server

Needs ``TELEGRAM_API_ID``, ``TELEGRAM_API_HASH`` (from https://my.telegram.org,
«API development tools») and ``TELEGRAM_SESSION_PATH`` — a path OUTSIDE this
repository, e.g. ``C:\\Users\\<you>\\.wwao\\telegram.session``.

It asks for the phone number, then the code Telegram sends, then the two-factor
password if the account has one. **None of the three is printed, logged or
stored here**: the phone and the code are read with ``input``/``getpass`` and
handed straight to Telethon, and only the session file Telethon writes remains.
The file is made readable by its owner only (600) where the system supports it.

A session is a login. Whoever has the file reads Telegram as you, so it must
never be committed, copied into a fixture or pasted into a bug report.
``.gitignore`` already refuses ``*.session``.

The connector itself never logs in: without this file it reports
«сессии Telegram ещё нет» and stays inactive.
"""

import asyncio
import getpass
import os
import stat
import sys
from typing import Any

from app.core.config import settings
from app.sources.telegram import session_file


async def login() -> int:
    """Interactive first login. Returns a process exit code."""
    api_id, api_hash = settings.telegram_api_id, settings.telegram_api_hash
    path = settings.telegram_session_path
    if api_id is None or api_hash is None or path is None:
        print(
            "Задайте в .env TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_SESSION_PATH "
            "(путь вне репозитория). Ключи — https://my.telegram.org → API development tools.",
            file=sys.stderr,
        )
        return 2
    target = session_file(path)
    if _inside_repository(target):
        print(
            f"TELEGRAM_SESSION_PATH указывает внутрь репозитория ({target}). "
            "Сессия — это вход в ваш аккаунт; положите её вне проекта.",
            file=sys.stderr,
        )
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)

    from telethon import TelegramClient

    client: Any = TelegramClient(str(target), api_id, api_hash.get_secret_value())
    await client.start(
        phone=lambda: input("Номер телефона в формате +7…: "),
        code_callback=lambda: getpass.getpass("Код из Telegram (не отображается): "),
        password=lambda: getpass.getpass("Пароль двухфакторной защиты (если есть): "),
    )
    me = await client.get_me()
    if "--string" in sys.argv:
        # For a server: the same login as a string, to paste into the platform's
        # secret variable TELEGRAM_SESSION_STRING. Printed once, to this
        # terminal only; never logged.
        from telethon.sessions import StringSession

        print("\nTELEGRAM_SESSION_STRING (вставьте в переменные Railway, никому не показывайте):")
        print(StringSession.save(client.session))
    await client.disconnect()
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - Windows keeps ACLs, not modes
        print(f"Права 600 не выставлены ({exc}); файл всё равно вне репозитория.")
    # The account's own id only: no phone, no name.
    print(f"Готово: сессия сохранена в {target} (аккаунт id {getattr(me, 'id', '?')}).")
    return 0


def _inside_repository(path: object) -> bool:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    try:
        Path(str(path)).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def main() -> int:  # pragma: no cover - interactive
    """Entry point for ``python -m app.sources.telegram_login``."""
    return asyncio.run(login())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Writing to the recruiter a Telegram vacancy names — the owner's explicit choice.

Until 21 Nov 2026 the project's rule was that nothing writes to a recruiter, ever.
The owner changed it for Telegram vacancies, knowing that Telegram restricts
accounts that message strangers, and asked for it with limits. Every limit is
here, in one function, so none can be skipped by a caller:

* off unless ``TELEGRAM_OUTREACH_ENABLED`` is set;
* at most ``TELEGRAM_OUTREACH_MAX_PER_DAY`` messages per UTC day (10);
* only to a contact the post itself names, and only an ``@username`` — never a
  phone number, an e-mail, a link or a bot (``…bot``);
* one message per contact, ever, and one per vacancy;
* only for a vacancy whose best profile cleared the queue's floor;
* a FloodWait or a refusal ends outreach for the day.

hh and LinkedIn are not here: hh goes through the local agent with the owner's
confirmation, and LinkedIn is applied to by hand.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories.source_state import SourceStateRepository
from app.letters.context import text_language

logger = get_logger(__name__)

STATE_SLUG = "outreach"
#: A Telegram username as a post writes it. 5-32 characters, letters first.
HANDLE = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{4,31})$")


#: The line after the letter that says which post this answers and that the
#: CV is attached, per language. After, not before: the letter already opens
#: with a greeting and the role, and a second «Здравствуйте!» above it was
#: the first thing a recruiter read (22 Nov 2026).
FOOTER: dict[str, dict[str, str]] = {
    "ru": {
        "post": "Вакансия: {url}",
        "channel": " (канал @{channel})",
        "cv": " Резюме во вложении.",
    },
    "en": {"post": "The post: {url}", "channel": " (@{channel})", "cv": " My CV is attached."},
}

#: With no letter to carry the greeting, the message opens with one.
BARE_OPENING: dict[str, str] = {
    "ru": "Здравствуйте! Пишу по вакансии «{title}»{channel}: {url}",
    "en": "Hello! I'm writing about the {title} role{channel}: {url}",
}


def compose_message(
    letter: str | None,
    *,
    title: str,
    post_url: str,
    channel: str | None = None,
    with_cv: bool = False,
) -> str:
    """The message to a recruiter: the letter, then which post and the CV.

    In the letter's own language, so an English letter is not followed by a
    Russian line. ``with_cv`` says the file goes with the message — true for
    the worker, which attaches it; the message a person copies by hand does
    not promise a file they may not attach.
    """
    language = text_language(letter or title, default="ru")
    language = language if language in FOOTER else "en"
    words = FOOTER[language]
    if not letter:
        where = words["channel"].format(channel=channel) if channel else ""
        return BARE_OPENING[language].format(title=title, channel=where, url=post_url)
    footer = words["post"].format(url=post_url)
    if channel:
        footer += words["channel"].format(channel=channel)
    footer += "." + (words["cv"] if with_cv else "")
    return f"{letter.strip()}\n\n{footer}"


class Sender(Protocol):
    """What outreach needs from Telegram: one message, one optional file."""

    async def send(self, contact: str, text: str, *, file: tuple[str, bytes] | None) -> None: ...


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a message may go, and the reason when it may not."""

    allowed: bool
    reason: str
    handle: str | None = None


def handle_of(contact: str | None) -> str | None:
    """The bare username a post's contact names, or None if it is not one we write to."""
    if not contact:
        return None
    match = HANDLE.match(contact.strip())
    if match is None or match.group(1).casefold().endswith("bot"):
        return None
    return match.group(1)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


async def decide(
    session: AsyncSession, *, contact: str | None, vacancy_key: str, score: float
) -> Decision:
    """Every rule above, read against what has been sent so far."""
    if not settings.telegram_outreach_enabled:
        return Decision(False, "автоотправка выключена (TELEGRAM_OUTREACH_ENABLED)")
    if score < settings.agent_queue_min_score:
        return Decision(False, f"оценка ниже порога {settings.agent_queue_min_score}")
    handle = handle_of(contact)
    if handle is None:
        return Decision(False, "в посте нет @username рекрутера")
    states = SourceStateRepository(session)
    day = await states.get(STATE_SLUG, f"day:{_today()}") or {}
    if day.get("stopped"):
        return Decision(False, f"на сегодня остановлено: {day['stopped']}", handle)
    if int(day.get("sent", 0)) >= settings.telegram_outreach_max_per_day:
        return Decision(False, f"дневной лимит {settings.telegram_outreach_max_per_day} исчерпан")
    if await states.get(STATE_SLUG, f"contact:{handle.casefold()}") is not None:
        return Decision(False, f"@{handle} уже писали", handle)
    if await states.get(STATE_SLUG, f"vacancy:{vacancy_key}") is not None:
        return Decision(False, "по этой вакансии уже писали", handle)
    return Decision(True, "можно", handle)


async def send(
    session: AsyncSession,
    sender: Sender,
    *,
    decision: Decision,
    vacancy_key: str,
    text: str,
    file: tuple[str, bytes] | None,
) -> str:
    """Send one allowed message and record it. Returns a line for the owner."""
    if not decision.allowed or decision.handle is None:
        return f"Не отправлено: {decision.reason}."
    states = SourceStateRepository(session)
    key = f"day:{_today()}"
    day = await states.get(STATE_SLUG, key) or {}
    try:
        await sender.send(f"@{decision.handle}", text, file=file)
    except Exception as exc:  # recorded and surfaced, never swallowed
        logger.warning("outreach.failed", error=type(exc).__name__)
        await states.set(STATE_SLUG, key, {**day, "stopped": type(exc).__name__})
        return (
            f"Не отправлено @{decision.handle}: {type(exc).__name__}. "
            "Автоотправка на сегодня остановлена."
        )
    now = datetime.now(UTC).isoformat()
    await states.set(STATE_SLUG, key, {**day, "sent": int(day.get("sent", 0)) + 1})
    await states.set(STATE_SLUG, f"contact:{decision.handle.casefold()}", {"at": now})
    await states.set(STATE_SLUG, f"vacancy:{vacancy_key}", {"at": now, "to": decision.handle})
    logger.info("outreach.sent", vacancy=vacancy_key)
    return f"Отправлено @{decision.handle}: письмо и резюме."

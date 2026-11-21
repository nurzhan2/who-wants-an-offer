"""The owner's bot: filters chosen with buttons, and the buttons on each card.

Phase 22b, after the first evening on the server (21 Nov 2026): every match of
three days arrived at once, each with a letter pasted in, and the chat became
unreadable. Since then a vacancy arrives as a short card, the letter and the CV
come only when a button asks for them, and what arrives at all is decided by
the owner's filters, set from the chat:

* ``/filters`` — which tracks, how fresh, from what score, whether Telegram
  recruiters are written to automatically, and a pause;
* the card's buttons — ``✉️ Письмо``, ``📄 Резюме`` and, for hh, ``➕ В очередь
  hh``, which writes the letter and so puts the vacancy in the agent's queue.

Only updates from ``TELEGRAM_CHAT_ID`` are acted on: the bot's username is
public, and anyone can write to it.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.source_state import SourceStateRepository
from app.services import notify
from app.services.notify import Buttons

logger = get_logger(__name__)

STATE_SLUG = "bot"
PREFS_KEY = "prefs"
OFFSET_KEY = "offset"
AGES_HOURS = (24, 72, 168)
SCORES = (70, 80, 90)


@dataclass(slots=True)
class Prefs:
    """What the owner wants to hear about. Stored as JSON in ``source_state``."""

    paused: bool = False
    max_age_hours: int = 24
    min_score: int | None = None
    tracks_off: list[str] = field(default_factory=list)
    #: Writing to Telegram recruiters automatically. Off until the owner turns
    #: it on in /filters: a contact can be written to only once.
    outreach: bool = False

    @property
    def floor(self) -> Decimal:
        return Decimal(max(settings.agent_queue_min_score, self.min_score or 0))

    def to_json(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "max_age_hours": self.max_age_hours,
            "min_score": self.min_score,
            "tracks_off": list(self.tracks_off),
            "outreach": self.outreach,
        }


async def load_prefs(session: AsyncSession) -> Prefs:
    stored = await SourceStateRepository(session).get(STATE_SLUG, PREFS_KEY) or {}
    return Prefs(
        paused=bool(stored.get("paused", False)),
        max_age_hours=int(stored.get("max_age_hours", 24)),
        min_score=stored.get("min_score"),
        tracks_off=[str(slug) for slug in stored.get("tracks_off", [])],
        outreach=bool(stored.get("outreach", False)),
    )


async def save_prefs(session: AsyncSession, prefs: Prefs) -> None:
    await SourceStateRepository(session).set(STATE_SLUG, PREFS_KEY, prefs.to_json())


def why_not(prefs: Prefs, *, slug: str, score: Decimal, published: datetime | None) -> str | None:
    """Why a match is not announced, or None when it is."""
    if prefs.paused:
        return "paused"
    if slug in prefs.tracks_off:
        return "track_off"
    if score < prefs.floor:
        return "below_floor"
    if published is not None:
        moment = published if published.tzinfo else published.replace(tzinfo=UTC)
        if datetime.now(UTC) - moment > timedelta(hours=prefs.max_age_hours):
            return "too_old"
    return None


def age_label(published: datetime | None) -> str:
    """«2 ч назад», «3 дн назад», or «дата неизвестна»."""
    if published is None:
        return "дата неизвестна"
    moment = published if published.tzinfo else published.replace(tzinfo=UTC)
    hours = int((datetime.now(UTC) - moment).total_seconds() // 3600)
    if hours < 1:
        return "только что"
    if hours < 48:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


async def menu(session: AsyncSession, prefs: Prefs) -> tuple[str, Buttons]:
    """The /filters message and its buttons."""
    slugs = [profile.slug for profile in await ProfileRepository(session).list_active()]
    age_names = {24: "24 ч", 72: "3 дня", 168: "7 дней"}
    lines = [
        "Фильтры уведомлений",
        "",
        "Пауза: да" if prefs.paused else "Уведомления: включены",
        f"Свежесть: не старше {age_names.get(prefs.max_age_hours, f'{prefs.max_age_hours} ч')}",
        f"Оценка: от {prefs.floor}",
        "Автоотклик рекрутерам в Telegram: " + ("вкл" if prefs.outreach else "выкл"),
        "Треки: нажмите, чтобы выключить или включить.",
    ]
    buttons: Buttons = [
        [
            (("⬜ " if slug in prefs.tracks_off else "✅ ") + slug, f"T:{slug}")
            for slug in slugs[i : i + 2]
        ]
        for i in range(0, len(slugs), 2)
    ]
    buttons.append(
        [
            (("● " if prefs.max_age_hours == hours else "") + age_names[hours], f"A:{hours}")
            for hours in AGES_HOURS
        ]
    )
    buttons.append(
        [
            (("● " if prefs.min_score == score else "") + f"от {score}", f"S:{score}")
            for score in SCORES
        ]
    )
    buttons.append(
        [
            ("🤖 Автоотклик TG: " + ("вкл" if prefs.outreach else "выкл"), "O"),
            ("▶️ Включить" if prefs.paused else "⏸ Пауза", "P"),
        ]
    )
    return "\n".join(lines), buttons


def apply_setting(prefs: Prefs, data: str) -> bool:
    """Change ``prefs`` by one filter button. False for data that is not one."""
    kind, _, value = data.partition(":")
    if kind == "T" and value:
        if value in prefs.tracks_off:
            prefs.tracks_off.remove(value)
        else:
            prefs.tracks_off.append(value)
    elif kind == "A" and value.isdigit() and int(value) in AGES_HOURS:
        prefs.max_age_hours = int(value)
    elif kind == "S" and value.isdigit() and int(value) in SCORES:
        prefs.min_score = None if prefs.min_score == int(value) else int(value)
    elif kind == "O":
        prefs.outreach = not prefs.outreach
    elif kind == "P":
        prefs.paused = not prefs.paused
    else:
        return False
    return True


def _from_owner(chat: Any) -> bool:
    return isinstance(chat, dict) and str(chat.get("id")) == str(settings.telegram_chat_id)


async def handle(session: AsyncSession, update: dict[str, Any], *, sender: Any = None) -> None:
    """Act on one update from the owner's chat; ignore everything else."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        if not _from_owner(message.get("chat")):
            return
        data = str(callback.get("data") or "")
        prefs = await load_prefs(session)
        if apply_setting(prefs, data):
            await save_prefs(session, prefs)
            await session.commit()
            text, buttons = await menu(session, prefs)
            await notify.edit_text(int(message.get("message_id", 0)), text, buttons=buttons)
            await notify.answer_callback(str(callback.get("id")), "Сохранено")
            return
        from app.services import react

        toast = await react.on_button(session, data, sender=sender)
        await notify.answer_callback(str(callback.get("id")), toast)
        return

    message = update.get("message")
    if not isinstance(message, dict) or not _from_owner(message.get("chat")):
        return
    command = str(message.get("text") or "").split("@")[0].strip().lower()
    prefs = await load_prefs(session)
    if command in {"/pause", "/stop"}:
        prefs.paused = True
    elif command in {"/resume", "/go"}:
        prefs.paused = False
    elif command not in {"/start", "/filters", "/settings", "/menu"}:
        return
    await save_prefs(session, prefs)
    await session.commit()
    text, buttons = await menu(session, prefs)
    await notify.send_text(text, buttons=buttons)


async def poll_once(session: AsyncSession, *, sender: Any = None) -> int:
    """Read the waiting updates once and act on each. Returns how many."""
    states = SourceStateRepository(session)
    stored = await states.get(STATE_SLUG, OFFSET_KEY) or {}
    offset = int(stored.get("offset", 0))
    updates = await notify.get_updates(offset)
    for update in updates:
        try:
            await handle(session, update, sender=sender)
        except Exception as exc:  # one bad tap must not stop the bot
            logger.warning("bot.update_failed", error=type(exc).__name__)
            await session.rollback()
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        await states.set(STATE_SLUG, OFFSET_KEY, {"offset": offset})
        await session.commit()
    return len(updates)

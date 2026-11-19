"""The day's autopilot: the chain, then a summary in the owner's Telegram.

``python -m wwao daily`` → ``scripts/daily.py`` → :func:`run_daily`. Phase 22.

**One routine, not two.** The work is :func:`app.services.autopilot.run_chain`
— the same function the dashboard's «Цепочка» button runs: crawl every enabled
source (each one's own ``min_interval`` still applies), embed the backlog,
score every active profile, re-read stale pages, write letters for what passed,
build the queue. This module adds only what a morning run needs on top: a
summary, and a message with it.

**It sends no application, and cannot.** The chain ends with a ready queue;
applications leave only after the owner confirms a batch on the dashboard and
the local agent sends it within ``AGENT_DAILY_CAP`` and working hours. Nothing
here imports the agent or mints a mandate.

**The message goes to the owner, from the owner's bot.** ``TELEGRAM_BOT_TOKEN``
and ``TELEGRAM_CHAT_ID`` are notification settings, not the Telegram source: a
bot writing to its owner's chat. Nothing is written to anyone else, and no
Telegram user session is used for it. Without both settings the summary is only
printed.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.enums import MatchBucket, PipelineRunStatus
from app.db.models import CandidateProfile, Match, PipelineRun, SourceState, Vacancy, VacancySource
from app.db.profile_order import best_profile_id
from app.db.session import session_factory
from app.services import agent_queue, autopilot

logger = get_logger(__name__)

#: Telegram refuses a message longer than this many characters.
TELEGRAM_LIMIT: Final[int] = 4096
TOP: Final[int] = 15
TELEGRAM_API: Final[str] = "https://api.telegram.org"


@dataclass(slots=True)
class DailySummary:
    """What a morning reads, before it is rendered."""

    started_at: datetime
    new_by_source: dict[str, int] = field(default_factory=dict)
    top: list[dict[str, Any]] = field(default_factory=list)
    awaiting_confirmation: int = 0
    set_aside: int = 0
    broken: list[str] = field(default_factory=list)
    chain_lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The message, in Russian, short enough for one Telegram message."""
        lines = [f"Who Wants an Offer — сводка за {self.started_at:%d.%m.%Y}"]
        total = sum(self.new_by_source.values())
        lines.append(f"\nНовых вакансий: {total}")
        lines += [f"  {slug}: {count}" for slug, count in sorted(self.new_by_source.items())]
        lines.append(f"\nТоп-{len(self.top)} по лучшему резюме:")
        for index, row in enumerate(self.top, start=1):
            company = f" — {row['company']}" if row.get("company") else ""
            lines.append(
                f"{index}. {row['score']:.0f} · {row['profile']} · {row['title']}{company}\n"
                f"   {row['url']}"
            )
        if not self.top:
            lines.append("  пока ничего не оценено")
        lines.append(
            f"\nЖдут подтверждения на дашборде: {self.awaiting_confirmation}"
            f" (посмотреть руками: {self.set_aside})"
        )
        lines.append("\nЧто сломалось:")
        lines += [f"  • {item}" for item in self.broken] or ["  ничего"]
        lines.append("\nОтклики сами не уходят: пачку подтверждаете вы.")
        text = "\n".join(lines)
        return text if len(text) <= TELEGRAM_LIMIT else text[: TELEGRAM_LIMIT - 1] + "…"


async def run_daily(*, crawl: bool = True) -> DailySummary:
    """Run the chain, then read what it left. A failed step is reported, not raised."""
    started = datetime.now(UTC)
    chain_lines: list[str] = []
    broken: list[str] = []
    if crawl:
        try:
            chain_lines = await autopilot.run_chain(autopilot.fresh_steps())
        except AppError as error:
            broken.append(f"цепочка: {error.detail}")
            logger.warning("daily.chain_failed", detail=error.detail)
    async with session_factory() as session:
        summary = await build_summary(session, since=started)
    summary.chain_lines = chain_lines
    summary.broken = broken + summary.broken
    return summary


async def build_summary(session: AsyncSession, *, since: datetime) -> DailySummary:
    """Everything the message says, read from the database."""
    summary = DailySummary(started_at=since)
    rows = await session.execute(
        select(VacancySource.source_slug, func.count())
        .where(VacancySource.created_at >= since)
        .group_by(VacancySource.source_slug)
    )
    summary.new_by_source = {slug: int(count) for slug, count in rows.all()}
    summary.top = await _top(session, since=since)
    selection = await agent_queue.triage(session, limit=autopilot.SELECTION_LIMIT)
    summary.awaiting_confirmation = len(selection.ready)
    summary.set_aside = len(selection.set_aside)
    summary.broken = await _broken(session, since=since - timedelta(hours=1))
    return summary


async def _top(session: AsyncSession, *, since: datetime) -> list[dict[str, Any]]:
    """The best vacancies nobody has acted on, by their best profile's score."""
    url = (
        select(VacancySource.url)
        .where(VacancySource.vacancy_id == Vacancy.id)
        .order_by(VacancySource.created_at)
        .limit(1)
        .scalar_subquery()
    )
    stmt: Select[Any] = (
        select(
            Vacancy.title,
            Vacancy.company,
            Match.score,
            CandidateProfile.slug,
            url.label("url"),
            (Vacancy.first_seen_at >= since).label("is_new"),
        )
        .join(Match, Match.vacancy_id == Vacancy.id)
        .join(CandidateProfile, CandidateProfile.id == Match.profile_id)
        .where(Match.profile_id == best_profile_id(Vacancy.id))
        .where(Vacancy.is_active.is_(True), Vacancy.is_spam.is_(False))
        .where(Match.bucket != MatchBucket.FILTERED)
        .order_by(Match.score.desc())
        .limit(TOP)
    )
    return [
        {
            "title": row.title,
            "company": row.company,
            "score": float(row.score),
            "profile": row.slug,
            "url": row.url or "",
            "is_new": bool(row.is_new),
        }
        for row in (await session.execute(stmt)).all()
    ]


async def _broken(session: AsyncSession, *, since: datetime) -> list[str]:
    """Failed or partial source runs, captchas, FloodWaits and an expired session."""
    found: list[str] = []
    runs = (
        await session.execute(
            select(PipelineRun.source_slug, PipelineRun.status, PipelineRun.errors).where(
                PipelineRun.started_at >= since,
                PipelineRun.status.in_([PipelineRunStatus.FAILED, PipelineRunStatus.PARTIAL]),
            )
        )
    ).all()
    for slug, status, errors in runs:
        reason = _first_error(errors)
        word = "упал" if status == PipelineRunStatus.FAILED else "частично (проверка на робота?)"
        found.append(f"{slug}: {word}" + (f" — {reason}" if reason else ""))
    telegram = await session.scalar(
        select(SourceState.value).where(
            SourceState.source_slug == "telegram",
            SourceState.key == "run:last",
            SourceState.updated_at >= since,
        )
    )
    if isinstance(telegram, dict):
        for wait in telegram.get("flood_waits", []) or []:
            found.append(f"telegram: FloodWait {wait.get('seconds')} с на @{wait.get('channel')}")
        if telegram.get("session_expired"):
            found.append("telegram: сессия протухла — python -m app.sources.telegram_login")
        for name in telegram.get("dead", []) or []:
            found.append(f"telegram: @{name} молчит больше 30 дней")
        for name in telegram.get("missing", []) or []:
            found.append(f"telegram: @{name} не найден")
    return found


def _first_error(errors: Any) -> str | None:
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("detail") or first)[:200]
        return str(first)[:200]
    return None


async def notify(text: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """Send the summary to the owner's own chat through their bot. False if not set up.

    The token is part of the URL Telegram's Bot API asks for, so the URL is never
    logged; a failure is logged by status code only.
    """
    token, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if token is None or not chat:
        return False
    own = client is None
    http = client or httpx.AsyncClient(timeout=settings.http_timeout)
    try:
        response = await http.post(
            f"{TELEGRAM_API}/bot{token.get_secret_value()}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        )
    except httpx.HTTPError as exc:
        logger.warning("daily.notify_failed", error=type(exc).__name__)
        return False
    finally:
        if own:
            await http.aclose()
    if response.status_code != httpx.codes.OK:
        logger.warning("daily.notify_refused", status=response.status_code)
        return False
    return True

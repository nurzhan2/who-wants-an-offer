"""What the Telegram Mini App shows: days, tracks, one vacancy at a time.

The owner's design (22 Nov 2026): statistics first; then vacancies by the day
they were published; a day opens its tracks; a track opens its vacancies one at
a time — where to apply, the letter, the CV — with previous and next, and a
switch to a plain list.

A vacancy belongs to the track whose resume won it (``matching.best``), inside
the owner's bot filters (``services.bot.Prefs``: tracks switched off, the score
floor). Days are calendar days in the owner's time zone, of the posting's own
date where the source gave one and of the day it was found otherwise.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.enums import MatchBucket
from app.db.models import Match, Vacancy, VacancySource
from app.db.repositories.source_state import SourceStateRepository
from app.db.seed_rows import seed_only
from app.matching.best import best_profiles
from app.services import bot, outreach, react

#: Almaty. The owner's days start at local midnight, not at UTC's.
LOCAL = timezone(timedelta(hours=5))
#: How far back the day list goes.
DAYS_BACK = 14
#: The Mini App is for browsing, so its default floor is lower than the
#: notification floor: a 65 that the owner reads and skips costs nothing.
BROWSE_MIN_SCORE = 60


@dataclass(frozen=True, slots=True)
class Item:
    """One vacancy won by one track."""

    vacancy_id: UUID
    slug: str
    profile_id: UUID
    score: Decimal
    title: str
    company: str | None
    source: str
    url: str
    published: datetime
    known_date: bool

    @property
    def day(self) -> date:
        moment = self.published if self.published.tzinfo else self.published.replace(tzinfo=UTC)
        return moment.astimezone(LOCAL).date()

    def brief(self) -> dict[str, Any]:
        return {
            "vacancy_id": str(self.vacancy_id),
            "slug": self.slug,
            "score": float(self.score),
            "title": self.title,
            "company": self.company,
            "source": self.source,
            "url": self.url,
            "published": self.published.isoformat(),
            "known_date": self.known_date,
        }


async def won_items(
    session: AsyncSession,
    *,
    days: int = DAYS_BACK,
    min_score: int = BROWSE_MIN_SCORE,
    with_hh: bool = False,
) -> list[Item]:
    """Every vacancy of the last ``days`` days, with the track that won it, best first.

    hh is left out unless asked for: its vacancies go through the agent's queue
    on the dashboard, and the owner browses everything else here.
    """
    prefs = await bot.load_prefs(session)
    since = datetime.now(UTC) - timedelta(days=days)
    when = func.coalesce(Vacancy.published_at, Vacancy.first_seen_at)
    rows = (
        await session.execute(
            select(
                Match.vacancy_id,
                Match.profile_id,
                Match.score,
                Vacancy.title,
                Vacancy.company,
                when,
                Vacancy.published_at.is_not(None),
            )
            .join(Vacancy, Vacancy.id == Match.vacancy_id)
            .where(
                Match.score >= min_score,
                Match.bucket != MatchBucket.FILTERED,
                Vacancy.is_active.is_(True),
                when >= since,
                not_(seed_only(Vacancy.id)),
            )
            .order_by(Match.score.desc(), when.desc())
        )
    ).all()
    ids = list({row[0] for row in rows})
    winners = await best_profiles(session, ids)
    sources: dict[UUID, tuple[str, str]] = {}
    for vacancy_id, slug, url in (
        await session.execute(
            select(VacancySource.vacancy_id, VacancySource.source_slug, VacancySource.url)
            .where(VacancySource.vacancy_id.in_(ids))
            .order_by(VacancySource.created_at)
        )
    ).all():
        sources.setdefault(vacancy_id, (slug, url))
    items: list[Item] = []
    for vacancy_id, profile_id, score, title, company, published, known in rows:
        winner = winners.get(vacancy_id)
        if winner is None or winner.profile_id != profile_id or winner.slug in prefs.tracks_off:
            continue
        source, url = sources.get(vacancy_id, ("?", ""))
        if not with_hh and source == settings.agent_source_slug:
            continue
        items.append(
            Item(
                vacancy_id,
                winner.slug,
                profile_id,
                Decimal(score),
                title,
                company,
                source,
                url,
                published,
                bool(known),
            )
        )
    return items


def by_day(items: list[Item]) -> list[dict[str, Any]]:
    """``[{date, count, tracks: {slug: n}}]``, newest day first."""
    days: dict[date, dict[str, int]] = {}
    for item in items:
        tracks = days.setdefault(item.day, {})
        tracks[item.slug] = tracks.get(item.slug, 0) + 1
    return [
        {"date": day.isoformat(), "count": sum(tracks.values()), "tracks": tracks}
        for day, tracks in sorted(days.items(), reverse=True)
    ]


def of_day_and_track(items: list[Item], day: date, slug: str) -> list[Item]:
    return [item for item in items if item.day == day and item.slug == slug]


async def detail(session: AsyncSession, vacancy_id: UUID) -> dict[str, Any] | None:
    """Where to apply, the letter if written, and which CV goes with it."""
    winner = (await best_profiles(session, [vacancy_id])).get(vacancy_id)
    if winner is None:
        return None
    facts = await react._facts(session, vacancy_id, winner.profile_id)
    if facts is None:
        return None
    cv = await react.original_cv(session, winner.profile_id)
    letter = await react._letter(session, vacancy_id)
    published = facts.get("published")
    return {
        "vacancy_id": str(vacancy_id),
        "slug": winner.slug,
        "score": float(winner.score),
        "title": facts["title"],
        "company": facts.get("company"),
        "source": facts["slug"],
        "url": facts["url"],
        "contact": facts.get("contact"),
        "channel": facts.get("channel"),
        "published": published.isoformat() if isinstance(published, datetime) else None,
        "known_date": bool(facts.get("known_date")),
        "is_hh": facts["slug"] == settings.agent_source_slug,
        "letter": letter,
        "cv_filename": cv[0] if cv else None,
    }


async def write_letter(session: AsyncSession, vacancy_id: UUID) -> str | None:
    """Write the letter now (within the day's budget). None when it could not be."""
    winner = (await best_profiles(session, [vacancy_id])).get(vacancy_id)
    if winner is None:
        return None
    budget = await react.load_budget(session)
    text = await react._write_letter(session, vacancy_id, winner.profile_id, budget)
    await react.save_budget(session, budget)
    await session.commit()
    return text


async def send_cv(session: AsyncSession, vacancy_id: UUID) -> bool:
    """Send this vacancy's CV file to the owner's chat, ready to forward."""
    return await react.on_button(session, f"C:{vacancy_id}") == "Резюме отправлено"


async def stats(session: AsyncSession, items: list[Item]) -> dict[str, Any]:
    """The numbers on the first screen."""
    today = datetime.now(LOCAL).date()
    week_ago = today - timedelta(days=6)
    now = datetime.now(UTC)
    found_today = await session.scalar(
        select(func.count(Vacancy.id)).where(Vacancy.first_seen_at >= now - timedelta(hours=24))
    )
    found_week = await session.scalar(
        select(func.count(Vacancy.id)).where(Vacancy.first_seen_at >= now - timedelta(days=7))
    )
    tracks: dict[str, int] = {}
    for item in items:
        if item.day >= week_ago:
            tracks[item.slug] = tracks.get(item.slug, 0) + 1
    states = SourceStateRepository(session)
    day_key = now.date().isoformat()
    sent = await states.get(outreach.STATE_SLUG, f"day:{day_key}") or {}
    budget = await react.load_budget(session)
    return {
        "found_today": int(found_today or 0),
        "found_week": int(found_week or 0),
        "matched_today": sum(1 for item in items if item.day == today),
        "matched_week": sum(tracks.values()),
        "tracks_week": tracks,
        "outreach_today": int(sent.get("sent", 0)),
        "outreach_limit": settings.telegram_outreach_max_per_day,
        "letters_today": budget.documents,
        "spent_usd_today": round(budget.usd, 2),
        "budget_usd": settings.llm_max_cost_per_day_usd,
    }

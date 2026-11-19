"""Which of the owner's resumes a vacancy is worked from: its best profile.

Since migration 0018 several profiles are active at once, one per track, and
``match`` already holds one row per (profile, vacancy) pair — its unique key has
always been ``(profile_id, vacancy_id)``, so no schema change was needed for the
scores themselves. What changed is the question every consumer asks: not "what
did the active profile score" but "which active profile scored highest, and by
how much".

**The rule, in one place.** Among the vacancy's ``match`` rows whose profile is
active: the highest ``score``; on a tie, the lower ``priority``; on a tie of
that, the newer profile — :data:`app.db.profile_order.DEFAULT_ORDER` after the
score. Every reader goes through :func:`best_profile_id` (defined in
``app/db/profile_order.py`` so repositories can use it) so the list,
the card, the letter queue and the agent queue cannot disagree about it.

**Why a correlated subquery and not a stored column.** A ``vacancy.best_profile_id``
column would be a second copy of a fact the ``match`` rows already state, and it
would go stale the moment a profile is switched off or rescored. The subquery is
an index lookup on ``match(vacancy_id)`` per vacancy — at six profiles, six rows
— and is always right.

**A filtered match still competes.** A vacancy the backend resume fails on
language may be a fine match for another resume; but the best of all profiles
being ``filtered`` is still filtered. Callers that must not act on a filtered
vacancy keep their own ``bucket != filtered`` condition on the winning row.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateProfile, Match
from app.db.profile_order import (
    DEFAULT_ORDER,
    best_profile_id,
    is_best_match,
    runner_up_score,
)

__all__ = [
    "BestProfile",
    "best_profile_id",
    "best_profiles",
    "is_best_match",
    "runner_up_score",
]


@dataclass(frozen=True, slots=True)
class BestProfile:
    """Which resume a vacancy was won by, and by how much over the next one."""

    vacancy_id: UUID
    profile_id: UUID
    slug: str
    track: str | None
    score: Decimal
    #: The second-best active profile, when there is one.
    runner_up_slug: str | None = None
    runner_up_score: Decimal | None = None

    @property
    def margin(self) -> Decimal | None:
        """How far ahead the winner is. None with a single active profile."""
        if self.runner_up_score is None:
            return None
        return self.score - self.runner_up_score


async def best_profiles(
    session: AsyncSession, vacancy_ids: Sequence[UUID]
) -> dict[UUID, BestProfile]:
    """The winner and the runner-up for each of these vacancies, in one read.

    Read in Python over the (few) match rows of these vacancies rather than as
    two more subqueries, because the card and the summary want the runner-up's
    *name* as well as its score.
    """
    if not vacancy_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Match.vacancy_id,
                Match.profile_id,
                Match.score,
                CandidateProfile.slug,
                CandidateProfile.track,
            )
            .join(CandidateProfile, CandidateProfile.id == Match.profile_id)
            .where(Match.vacancy_id.in_(vacancy_ids), CandidateProfile.is_active.is_(True))
            .order_by(Match.vacancy_id, Match.score.desc(), *DEFAULT_ORDER)
        )
    ).all()
    ranked: dict[UUID, list[Any]] = {}
    for row in rows:
        ranked.setdefault(row.vacancy_id, []).append(row)
    found: dict[UUID, BestProfile] = {}
    for vacancy_id, entries in ranked.items():
        winner = entries[0]
        second = entries[1] if len(entries) > 1 else None
        found[vacancy_id] = BestProfile(
            vacancy_id=vacancy_id,
            profile_id=winner.profile_id,
            slug=winner.slug,
            track=winner.track,
            score=winner.score,
            runner_up_slug=second.slug if second is not None else None,
            runner_up_score=second.score if second is not None else None,
        )
    return found

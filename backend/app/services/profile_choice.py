"""Which resume a vacancy's letter and CV are written from.

The resume that won the vacancy (``matching.best``) — unless it is in the other
language from the posting and the owner has the same track in the posting's
language. The owner keeps a Russian and an English resume per track
(``backend-ru`` / ``backend-en``); on 22 Nov 2026 an English posting got a CV
with English headings over Russian bullets, because the dashboard's button
always took the highest-priority resume and the bullets are quoted verbatim.

A resume's language is read from its own words — headline, summary and job
bullets — with the same Cyrillic-share rule the posting's language is read by
(:func:`app.letters.context.posting_language`), so the two are judged alike.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateProfile, Match, ProfileExperience
from app.db.profile_order import DEFAULT_ORDER
from app.letters import store as letter_store
from app.letters.context import CYRILLIC_SHARE_FOR_RUSSIAN, posting_language
from app.matching.best import best_profiles


def text_language(text: str) -> str | None:
    """``ru``/``en`` by the share of Cyrillic letters, or None for no letters."""
    cyrillic = sum(1 for char in text if "Ѐ" <= char <= "ӿ")
    latin = sum(1 for char in text if "a" <= char.lower() <= "z")
    if cyrillic + latin == 0:
        return None
    return "ru" if cyrillic / (cyrillic + latin) >= CYRILLIC_SHARE_FOR_RUSSIAN else "en"


async def profile_language(session: AsyncSession, profile: CandidateProfile) -> str | None:
    bullets = (
        await session.scalars(
            select(ProfileExperience.achievements).where(ProfileExperience.profile_id == profile.id)
        )
    ).all()
    words = [profile.headline or "", profile.summary or ""]
    words += [str(item) for job in bullets for item in (job or [])]
    return text_language(" ".join(words))


async def for_vacancy(session: AsyncSession, vacancy_id: UUID) -> UUID | None:
    """The profile to write this vacancy's documents from, or None without one."""
    active = list(
        (
            await session.scalars(
                select(CandidateProfile)
                .where(CandidateProfile.is_active.is_(True))
                .order_by(*DEFAULT_ORDER)
            )
        ).all()
    )
    if not active:
        return None
    winner = (await best_profiles(session, [vacancy_id])).get(vacancy_id)
    chosen = next(
        (p for p in active if winner is not None and p.id == winner.profile_id), active[0]
    )
    facts = await letter_store.load_vacancy_facts(session, vacancy_id)
    if facts is None:
        return chosen.id
    wanted = posting_language(facts)
    if await profile_language(session, chosen) in {wanted, None}:
        return chosen.id
    same_track = [
        p for p in active if p.id != chosen.id and chosen.track and p.track == chosen.track
    ]
    matching = [p for p in same_track if await profile_language(session, p) == wanted]
    if not matching:
        return chosen.id
    rows = await session.execute(
        select(Match.profile_id, Match.score).where(
            Match.vacancy_id == vacancy_id,
            Match.profile_id.in_([p.id for p in matching]),
        )
    )
    scores: dict[UUID, float] = {row[0]: float(row[1]) for row in rows.all()}
    # Best score among the right-language resumes; priority order breaks ties
    # and decides when none was scored.
    return max(matching, key=lambda p: (scores.get(p.id, -1), -active.index(p))).id

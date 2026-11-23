"""What a CV change costs now: a bounded, recent set, and nothing re-embedded.

Three rules, each one there because the alternative made a new resume a
recomputation of everything ever crawled:

* the **scoring window** — a pass over "the corpus" scores active vacancies
  published (or first seen) within ``scoring_window_days``; explicit
  ``vacancy_ids`` are scored whatever their age;
* the **archive** — after each crawl, a vacancy nothing vouches for any more is
  made inactive, and drops out of the lists, the scoring and the embedding step;
* **vectors are the vacancy's, not the CV's** — a new headline re-ranks the
  embedding walk but never re-embeds a row whose text has not moved.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import CandidateProfile, Match, Vacancy
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.matching import embeddings
from app.matching.embeddings import FakeEmbeddingProvider
from app.matching.scorer import score_corpus, window_line
from app.pipeline.embedding import embed_pending
from app.pipeline.runner import archive_stale
from app.schemas.vacancy import VacancyFilter
from factories import make_profile, make_upsert_item

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)


async def a_profile(db_session: AsyncSession) -> Any:
    profiles = ProfileRepository(db_session)
    created = await profiles.create(make_profile(skills=("python", "postgresql")))
    await profiles.activate(created.id)
    await db_session.flush()
    return created


async def vacancies_dated(
    db_session: AsyncSession, **published: datetime | None
) -> dict[str, UUID]:
    """One stored vacancy per keyword, its ``published_at`` the value given."""
    result = await VacancyRepository(db_session).bulk_upsert(
        [make_upsert_item(seed, "hh", published_at=when) for seed, when in published.items()]
    )
    await db_session.flush()
    return dict(zip(published, result.vacancy_ids, strict=True))


async def scored(db_session: AsyncSession) -> set[UUID]:
    rows = await db_session.execute(select(Match.vacancy_id))
    return {row[0] for row in rows.all()}


async def set_dates(db_session: AsyncSession, vacancy_id: UUID, **values: datetime) -> None:
    await db_session.execute(sa_update(Vacancy).where(Vacancy.id == vacancy_id).values(**values))
    await db_session.flush()


async def active(db_session: AsyncSession, ids: Sequence[UUID]) -> dict[UUID, bool]:
    rows = await db_session.execute(
        select(Vacancy.id, Vacancy.is_active).where(Vacancy.id.in_(list(ids)))
    )
    return {row.id: row.is_active for row in rows.all()}


# -- the scoring window ------------------------------------------------------


async def test_the_window_leaves_old_vacancies_out_of_a_corpus_pass(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new CV rescores the last month, not every posting ever crawled."""
    monkeypatch.setattr(settings, "scoring_window_days", 30)
    await a_profile(db_session)
    ids = await vacancies_dated(
        db_session, recent=NOW - timedelta(days=2), old=NOW - timedelta(days=60)
    )

    outcome = await score_corpus(db_session)

    assert await scored(db_session) == {ids["recent"]}
    assert outcome.considered == 1
    assert outcome.window_days == 30
    assert outcome.outside_window == 1
    line = window_line(outcome)
    assert line is not None and "30" in line and "1" in line


async def test_named_vacancies_are_scored_whatever_their_age(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker names what a tick brought in; the window does not second-guess it."""
    monkeypatch.setattr(settings, "scoring_window_days", 30)
    await a_profile(db_session)
    ids = await vacancies_dated(db_session, old=NOW - timedelta(days=60))

    outcome = await score_corpus(db_session, vacancy_ids=[ids["old"]])

    assert await scored(db_session) == {ids["old"]}
    assert outcome.window_days is None
    assert window_line(outcome) is None


async def test_a_posting_with_no_date_is_dated_by_when_we_first_saw_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``coalesce(published_at, first_seen_at)``: no employer date is not "old"."""
    monkeypatch.setattr(settings, "scoring_window_days", 30)
    await a_profile(db_session)
    ids = await vacancies_dated(db_session, undated=None, seen_long_ago=None)
    await set_dates(db_session, ids["seen_long_ago"], first_seen_at=NOW - timedelta(days=90))

    await score_corpus(db_session)

    assert await scored(db_session) == {ids["undated"]}


async def test_a_zero_window_scores_every_active_vacancy(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "scoring_window_days", 30)
    await a_profile(db_session)
    ids = await vacancies_dated(
        db_session, recent=NOW - timedelta(days=2), old=NOW - timedelta(days=60)
    )

    outcome = await score_corpus(db_session, window_days=0)

    assert await scored(db_session) == set(ids.values())
    assert outcome.window_days is None


# -- the archive -------------------------------------------------------------


async def test_stale_vacancies_are_archived_and_live_ones_are_not(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Either rule retires a row; a row either date still vouches for stays."""
    monkeypatch.setattr(settings, "vacancy_unseen_days", 30)
    monkeypatch.setattr(settings, "max_vacancy_age_days", 45)
    ids = await vacancies_dated(
        db_session,
        # Neither seen nor republished for forty days: gone.
        forgotten=NOW - timedelta(days=40),
        # Not re-fetched for forty days — hh holds it unchanged — but renewed
        # by its employer two days ago: live.
        renewed=NOW - timedelta(days=2),
        # Still listed by its source yesterday, but published two months ago:
        # older than anything the planner asks for.
        ancient=NOW - timedelta(days=60),
        fresh=NOW - timedelta(days=1),
    )
    for key in ("forgotten", "renewed"):
        await set_dates(db_session, ids[key], last_seen_at=NOW - timedelta(days=40))

    retired = await archive_stale(db_session)

    assert retired == 2
    state = await active(db_session, list(ids.values()))
    assert state == {
        ids["forgotten"]: False,
        ids["renewed"]: True,
        ids["ancient"]: False,
        ids["fresh"]: True,
    }


async def test_an_archived_vacancy_leaves_the_lists_and_the_scoring(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "scoring_window_days", 0)
    monkeypatch.setattr(settings, "max_vacancy_age_days", 45)
    await a_profile(db_session)
    ids = await vacancies_dated(
        db_session, ancient=NOW - timedelta(days=60), fresh=NOW - timedelta(days=1)
    )
    await archive_stale(db_session)

    await score_corpus(db_session)
    listed = await VacancyRepository(db_session).list_filtered(VacancyFilter())

    assert await scored(db_session) == {ids["fresh"]}
    assert {item.id for item in listed.items} == {ids["fresh"]}


async def test_a_vacancy_that_turns_up_again_is_live_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive is the crawl's to reverse: an upsert sets it active."""
    monkeypatch.setattr(settings, "vacancy_unseen_days", 30)
    ids = await vacancies_dated(db_session, back=NOW - timedelta(days=40))
    await set_dates(db_session, ids["back"], last_seen_at=NOW - timedelta(days=40))
    assert await archive_stale(db_session) == 1

    await VacancyRepository(db_session).bulk_upsert(
        [make_upsert_item("back", "hh", published_at=NOW - timedelta(hours=1))]
    )
    await db_session.flush()

    assert (await active(db_session, [ids["back"]]))[ids["back"]] is True


async def test_an_archived_vacancy_is_never_embedded(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight seconds of CPU a description, for a posting nobody will read."""
    monkeypatch.setattr(settings, "max_vacancy_age_days", 45)
    ids = await vacancies_dated(
        db_session, ancient=NOW - timedelta(days=60), fresh=NOW - timedelta(days=1)
    )
    await archive_stale(db_session)
    vacancies = VacancyRepository(db_session)

    offered = {row.id for row in await vacancies.needs_embedding(limit=100)}

    assert offered == {ids["fresh"]}
    assert await vacancies.count_needing_embedding() == 1
    assert await vacancies.count_never_embedded() == 1


# -- vectors are the vacancy's, not the CV's -----------------------------------


class Counting:
    """The deterministic provider, counting every text it is handed."""

    name = "counting"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [FakeEmbeddingProvider.vector_for(text) for text in texts]


async def test_a_new_headline_re_embeds_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline re-ranks the walk; the rows already embedded stay embedded.

    What a CV change costs is therefore the profile's own two vectors and one
    scoring pass — never the corpus's descriptions again.
    """
    monkeypatch.setattr(settings, "embedding_cache_dir", None)
    provider = Counting()
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)
    dim = settings.embedding_dim
    axis0 = [1.0] + [0.0] * (dim - 1)
    axis1 = [0.0, 1.0] + [0.0] * (dim - 2)
    ids = await vacancies_dated(
        db_session, **{f"v{index}": NOW - timedelta(days=1) for index in range(3)}
    )
    await db_session.execute(
        sa_update(Vacancy).where(Vacancy.id.in_(list(ids.values()))).values(title_embedding=axis0)
    )
    profile = CandidateProfile(
        name="Synthetic", headline="Backend", is_active=True, headline_embedding=axis0
    )
    db_session.add(profile)
    await db_session.flush()

    first = await embed_pending(db_session)
    assert first.embedded == 3
    encoded = len(provider.texts)

    await db_session.execute(
        sa_update(CandidateProfile)
        .where(CandidateProfile.id == profile.id)
        .values(headline="Data Engineer", headline_embedding=axis1, updated_at=func.now())
    )
    # And a re-crawl that rewrote every row without changing a word: the rows
    # are offered again ("written since the vector"), and the hash turns them away.
    await db_session.execute(
        sa_update(Vacancy)
        .where(Vacancy.id.in_(list(ids.values())))
        .values(updated_at=func.now() + timedelta(minutes=1))
    )
    await db_session.flush()
    second = await embed_pending(db_session)

    assert len(provider.texts) == encoded, "no description encoded twice"
    assert second.embedded == 0
    assert second.unchanged == 3

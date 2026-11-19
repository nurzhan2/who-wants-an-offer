"""Several resumes at once: every vacancy against every active profile, one winner.

Phase 22. The owner keeps one resume per track on hh; a vacancy is scored
against each, worked from the one it scores best with, and applied to with that
resume's hh id. What these tests pin is that every reader agrees on the winner:

* the batch upload files each resume under its own slug, all or nothing;
* two profiles, two vacancies, each won by a different one — in the list, the
  card, the letter queue and the agent queue alike;
* a tie goes to the lower ``priority``;
* a letter is written from the winner, not from the default profile;
* a winner without ``hh_resume_id`` sends the vacancy to a person.

Every profile here is synthetic. No real CV belongs in this repository.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import uuid7
from app.db.models import Application, CandidateProfile
from app.db.repositories import MatchRepository, ProfileRepository, VacancyRepository
from app.letters import service as letters_service
from app.letters import store as letters_store
from app.letters.service import LetterOutcome
from app.matching.best import best_profiles
from app.schemas.autopilot import SetAsideKind
from app.schemas.profile import CandidateProfileRead
from app.schemas.vacancy import VacancyFilter
from app.services import agent_queue
from app.services import resume as resume_service
from app.services import vacancies as vacancies_service
from app.sources.query_planner import plan_for_profiles, plan_queries
from factories import make_match, make_profile, make_vacancy

pytestmark = pytest.mark.db

RESUMES = Path(__file__).parent / "fixtures" / "resumes"
BATCH_URL = f"{settings.api_v1_prefix}/resume/batch"
UPLOAD_URL = f"{settings.api_v1_prefix}/resume/upload"


async def _posting(vacancies: VacancyRepository, seed: str, external_id: str) -> UUID:
    """One hh vacancy with one source row, as the crawler leaves it."""
    url = f"https://almaty.hh.kz/vacancy/{external_id}"
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed),
        source_slug="hh",
        external_id=external_id,
        url=url,
        raw={"_derived": {"external_id": external_id, "url": url}},
    )
    return result.vacancy_id


def _letter(session: AsyncSession, vacancy_id: UUID) -> None:
    session.add(Application(id=uuid7(), vacancy_id=vacancy_id, cover_letter="Здравствуйте!"))


async def _two_tracks(
    profiles: ProfileRepository,
) -> tuple[CandidateProfile, CandidateProfile]:
    backend = await profiles.create(
        make_profile(name="Backend", slug="backend", track="backend", priority=0)
    )
    llm = await profiles.create(
        make_profile(name="LLM", slug="llm", track="llm", priority=1, hh_resume_id="1" * 38)
    )
    return backend, llm


# ── upload ────────────────────────────────────────────────────────────


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stage under a temp dir and record the background batch instead of parsing."""
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    calls: list[Any] = []

    async def record(jobs: list[Any]) -> None:
        calls.append(jobs)

    async def record_one(profile_id: UUID, path: Path, **kwargs: Any) -> None:
        calls.append([(profile_id, path, kwargs.get("replaces"))])

    monkeypatch.setattr(resume_service, "parse_batch_in_background", record)
    monkeypatch.setattr(resume_service, "parse_in_background", record_one)
    return calls


def _file(name: str) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, (RESUMES / "plain.txt").read_bytes(), "text/plain"))


async def test_a_batch_files_each_resume_under_its_own_slug(
    async_client: AsyncClient, db_session: AsyncSession, staged: list[Any]
) -> None:
    meta = [
        {"slug": "backend", "track": "backend", "hh_resume_id": "a" * 38},
        {"slug": "llm", "track": "llm", "priority": 1},
    ]
    response = await async_client.post(
        BATCH_URL,
        files=[_file("backend.txt"), _file("llm.txt")],
        data={"meta": json.dumps(meta)},
    )

    assert response.status_code == 202, response.text
    items = response.json()["items"]
    assert [(item["slug"], item["filename"]) for item in items] == [
        ("backend", "backend.txt"),
        ("llm", "llm.txt"),
    ]
    rows = {row.slug: row for row in (await db_session.scalars(select(CandidateProfile))).all()}
    assert rows["backend"].hh_resume_id == "a" * 38
    assert (rows["llm"].track, rows["llm"].priority) == ("llm", 1)
    # Staged, not yet live: nothing parsed, nothing active.
    assert not any(row.is_active for row in rows.values())
    assert len(staged) == 1 and len(staged[0]) == 2


async def test_a_batch_with_one_bad_slug_writes_nothing(
    async_client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    staged: list[Any],
) -> None:
    await profiles.create(make_profile(slug="backend"))
    await db_session.flush()
    before = len((await db_session.scalars(select(CandidateProfile))).all())

    response = await async_client.post(
        BATCH_URL,
        files=[_file("a.txt"), _file("b.txt")],
        data={"meta": json.dumps([{"slug": "fresh"}, {"slug": "backend"}])},
    )

    assert response.status_code == 409
    assert len((await db_session.scalars(select(CandidateProfile))).all()) == before
    assert staged == []


async def test_a_batch_naming_one_profile_twice_is_refused(
    async_client: AsyncClient, staged: list[Any]
) -> None:
    response = await async_client.post(
        BATCH_URL,
        files=[_file("a.txt"), _file("b.txt")],
        data={"meta": json.dumps([{"slug": "x"}, {"slug": "x"}])},
    )
    assert response.status_code == 422


async def test_replace_names_a_profile_that_must_exist(
    async_client: AsyncClient, staged: list[Any]
) -> None:
    response = await async_client.post(
        f"{UPLOAD_URL}?replace=nobody",
        files={"file": ("cv.txt", (RESUMES / "plain.txt").read_bytes(), "text/plain")},
    )
    assert response.status_code == 404


async def test_replace_is_remembered_for_the_parse_and_retires_nothing_yet(
    async_client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    staged: list[Any],
) -> None:
    old = await profiles.create(make_profile(slug="backend"))
    old_id = old.id
    await db_session.flush()

    response = await async_client.post(
        f"{UPLOAD_URL}?replace=backend",
        files={"file": ("cv.txt", (RESUMES / "plain.txt").read_bytes(), "text/plain")},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["slug"] == "backend"
    assert body["replaces"] == str(old_id)
    assert staged[0][0][2] == old_id
    db_session.expire_all()
    assert (await profiles.get(old_id)).is_active is True  # type: ignore[union-attr]


async def test_upload_no_longer_retires_the_other_profiles(
    async_client: AsyncClient,
    db_session: AsyncSession,
    profiles: ProfileRepository,
    staged: list[Any],
) -> None:
    old = await profiles.create(make_profile(slug="backend"))
    old_id = old.id
    await db_session.flush()

    response = await async_client.post(
        f"{UPLOAD_URL}?slug=llm&track=llm",
        files={"file": ("cv.txt", (RESUMES / "plain.txt").read_bytes(), "text/plain")},
    )

    assert response.status_code == 202
    db_session.expire_all()
    assert (await profiles.get(old_id)).is_active is True  # type: ignore[union-attr]


async def test_a_profile_can_be_renamed_but_not_onto_another(
    async_client: AsyncClient, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    first = await profiles.create(make_profile(slug="backend"))
    await profiles.create(make_profile(slug="llm"))
    await db_session.flush()

    taken = await async_client.patch(f"/api/v1/profile/{first.id}", json={"slug": "llm"})
    renamed = await async_client.patch(
        f"/api/v1/profile/{first.id}", json={"slug": "fullstack", "hh_resume_id": "b" * 38}
    )
    bad_hash = await async_client.patch(f"/api/v1/profile/{first.id}", json={"hh_resume_id": "x"})

    assert taken.status_code == 409
    assert renamed.status_code == 200
    assert (renamed.json()["slug"], renamed.json()["hh_resume_id"]) == ("fullstack", "b" * 38)
    assert bad_hash.status_code == 422


async def test_the_profile_list_puts_active_profiles_first_in_tie_break_order(
    async_client: AsyncClient, db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    retired = await profiles.create(make_profile(slug="old"))
    retired.is_active = False
    await profiles.create(make_profile(slug="second", priority=5))
    await profiles.create(make_profile(slug="first", priority=0))
    await db_session.flush()

    response = await async_client.get("/api/v1/profile")

    assert [row["slug"] for row in response.json()] == ["first", "second", "old"]


# ── the winner ────────────────────────────────────────────────────────


async def test_two_vacancies_are_won_by_two_different_profiles(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    backend, llm = await _two_tracks(profiles)
    api = await _posting(vacancies, "api-role", "137100001")
    rag = await _posting(vacancies, "rag-role", "137100002")
    await matches.bulk_upsert(
        [
            make_match(backend.id, api, Decimal("88")),
            make_match(llm.id, api, Decimal("71")),
            make_match(backend.id, rag, Decimal("69")),
            make_match(llm.id, rag, Decimal("90")),
        ]
    )
    await db_session.flush()

    best = await best_profiles(db_session, [api, rag])

    assert (best[api].slug, best[api].margin) == ("backend", Decimal("17.00"))
    assert (best[rag].slug, best[rag].runner_up_slug) == ("llm", "backend")

    page = await vacancies_service.list_page(db_session, VacancyFilter())
    shown = {row.id: (row.profile_slug, row.score) for row in page.items}
    assert shown[api] == ("backend", Decimal("88.00"))
    assert shown[rag] == ("llm", Decimal("90.00"))

    narrowed = await vacancies_service.list_page(db_session, VacancyFilter(), profile="backend")
    assert {row.id: row.score for row in narrowed.items}[rag] == Decimal("69.00")

    card = await vacancies_service.card(db_session, rag)
    assert card is not None and card.match is not None
    assert (card.match.profile_slug, card.match.margin) == ("llm", Decimal("21.00"))


async def test_a_tie_goes_to_the_lower_priority(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    # Created in the order that would win if creation time decided.
    low = await profiles.create(make_profile(slug="second", priority=7))
    high = await profiles.create(make_profile(slug="first", priority=2))
    vacancy = await _posting(vacancies, "tie", "137100003")
    await matches.bulk_upsert(
        [make_match(low.id, vacancy, Decimal("80")), make_match(high.id, vacancy, Decimal("80"))]
    )
    await db_session.flush()

    best = (await best_profiles(db_session, [vacancy]))[vacancy]

    assert best.slug == "first"
    assert best.margin == Decimal("0.00")


async def test_an_inactive_profile_never_wins(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    backend, llm = await _two_tracks(profiles)
    llm.is_active = False
    vacancy = await _posting(vacancies, "retired", "137100004")
    await matches.bulk_upsert(
        [make_match(backend.id, vacancy, Decimal("70")), make_match(llm.id, vacancy, Decimal("95"))]
    )
    await db_session.flush()

    assert (await best_profiles(db_session, [vacancy]))[vacancy].slug == "backend"


# ── letters and the agent queue ───────────────────────────────────────


async def test_a_letter_is_written_from_the_profile_that_won(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, llm = await _two_tracks(profiles)
    api = await _posting(vacancies, "api-letter", "137100005")
    rag = await _posting(vacancies, "rag-letter", "137100006")
    await matches.bulk_upsert(
        [
            make_match(backend.id, api, Decimal("88")),
            make_match(llm.id, api, Decimal("80")),
            make_match(llm.id, rag, Decimal("92")),
        ]
    )
    await db_session.flush()
    written: dict[UUID, UUID] = {}

    async def record(_session: Any, vacancy_id: UUID, profile: Any, **_: Any) -> LetterOutcome:
        written[vacancy_id] = profile.profile_id
        return LetterOutcome(vacancy_id=vacancy_id, title="t", company=None)

    monkeypatch.setattr(letters_service, "write_letter", record)

    queued = await letters_store.queue(db_session, limit=10)
    await letters_service.write_batch(db_session, limit=10)

    assert {item.vacancy_id: item.profile_id for item in queued} == {
        api: backend.id,
        rag: llm.id,
    }
    assert written == {api: backend.id, rag: llm.id}


async def test_a_winner_below_the_floor_gets_no_letter_from_anyone(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    backend, llm = await _two_tracks(profiles)
    low = await _posting(vacancies, "low", "137100007")
    await matches.bulk_upsert(
        [make_match(backend.id, low, Decimal("60")), make_match(llm.id, low, Decimal("55"))]
    )
    await db_session.flush()

    assert await letters_store.queue(db_session, limit=10, min_score=Decimal("78")) == []


async def test_the_queue_carries_the_winners_hh_resume_id(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    backend, llm = await _two_tracks(profiles)
    rag = await _posting(vacancies, "rag-queue", "137100008")
    await matches.bulk_upsert(
        [make_match(backend.id, rag, Decimal("80")), make_match(llm.id, rag, Decimal("91"))]
    )
    _letter(db_session, rag)
    await db_session.flush()

    queue = await agent_queue.build_queue(db_session, limit=10)

    assert [(item.profile_slug, item.hh_resume_id) for item in queue.items] == [("llm", "1" * 38)]
    # The resume is part of what the owner confirms.
    other = queue.items[0].model_copy(update={"hh_resume_id": "2" * 38})
    assert agent_queue.card_digest(other) != agent_queue.card_digest(queue.items[0])


async def test_a_winner_without_an_hh_resume_id_goes_to_a_person(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    vacancies: VacancyRepository,
    matches: MatchRepository,
) -> None:
    backend, _ = await _two_tracks(profiles)
    nameless = await profiles.create(make_profile(slug="intern", hh_resume_id=None))
    vacancy = await _posting(vacancies, "intern-role", "137100009")
    await matches.bulk_upsert(
        [
            make_match(backend.id, vacancy, Decimal("79")),
            make_match(nameless.id, vacancy, Decimal("93")),
        ]
    )
    _letter(db_session, vacancy)
    await db_session.flush()

    selection = await agent_queue.triage(db_session, limit=10)

    assert selection.ready == []
    [aside] = selection.set_aside
    assert aside.kind is SetAsideKind.NO_HH_RESUME
    assert "intern" in aside.reason
    # The chain's letter step still sees it: a hand-sent application wants one.
    letterless = await agent_queue.triage(db_session, limit=10, require_letter=False)
    assert [ready.profile_id for ready in letterless.ready] == [nameless.id]


# ── the plan ──────────────────────────────────────────────────────────


async def test_the_plan_looks_for_every_track_default_first(
    db_session: AsyncSession, profiles: ProfileRepository
) -> None:
    backend, llm = await _two_tracks(profiles)
    backend.target_titles = ["Python Developer"]
    llm.target_titles = ["LLM Engineer"]
    await db_session.flush()
    reads = [
        CandidateProfileRead.model_validate(profile) for profile in await profiles.list_active()
    ]

    merged = plan_for_profiles(reads)
    alone = plan_queries(reads[0])

    words = {word for query in merged.queries for word in query.keywords}
    assert {"Python Developer", "LLM Engineer"} <= words
    assert merged.queries[0] == alone.queries[0]
    assert merged.limit == settings.max_queries_per_run * 2
    assert len(set(merged.queries)) == len(merged.queries)

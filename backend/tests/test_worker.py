"""Phase 22b: the server — its password, the worker, and what it does with a vacancy.

Synthetic vacancies, synthetic contacts, no network: the bot API is served by
respx, the Telegram account by a recording fake, the model by stubs that write
a fixed letter. Pinned:

* without ``DASHBOARD_PASSWORD`` nothing changes; with it every request needs
  it, except /health and the agent's token-guarded queue;
* outreach goes only where every limit allows: on, above the floor, a real
  ``@username`` that is not a bot, once per contact, ten a day, and a failure
  stops it for the day;
* a new vacancy above the floor gets a letter and a CV from its best resume and
  a message in the owner's chat; an hh vacancy is left to the agent's queue; a
  Telegram one is written to the recruiter; the day's budget is respected;
* the morning summary goes once a day.
"""

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import password_from
from app.core.config import settings
from app.db.base import uuid7
from app.db.models import Application
from app.db.repositories.source_state import SourceStateRepository
from app.documents.review import ReviewedDocument
from app.services import notify, outreach, react
from factories import make_match, make_profile, make_vacancy

# ── the password ──────────────────────────────────────────────────────


def _basic(password: str) -> dict[str, str]:
    token = base64.b64encode(f"owner:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.mark.unit
def test_the_password_is_read_from_a_basic_header() -> None:
    assert password_from(_basic("s3cret")["Authorization"]) == "s3cret"
    assert password_from("Bearer x") is None
    assert password_from("Basic !!!") is None


@pytest.mark.db
async def test_without_a_password_nothing_changes(async_client: AsyncClient) -> None:
    assert (await async_client.get("/api/v1/profile")).status_code == 200


@pytest.mark.db
async def test_with_a_password_every_request_needs_it_but_health_and_the_agent(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dashboard_password", SecretStr("s3cret"))

    refused = await async_client.get("/api/v1/profile")
    wrong = await async_client.get("/api/v1/profile", headers=_basic("nope"))
    allowed = await async_client.get("/api/v1/profile", headers=_basic("s3cret"))
    health = await async_client.get("/health")
    agent = await async_client.get("/api/v1/applications/queue")

    assert refused.status_code == 401 and "Basic" in refused.headers["www-authenticate"]
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert health.status_code != 401
    # The agent's queue answers with its own token check, not the password.
    assert agent.status_code in {401, 503}
    assert "Basic" not in agent.headers.get("www-authenticate", "")


# ── outreach ──────────────────────────────────────────────────────────


class FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, tuple[str, bytes] | None]] = []

    async def send(self, contact: str, text: str, *, file: tuple[str, bytes] | None) -> None:
        if self.fail:
            raise RuntimeError("PeerFlood")
        self.sent.append((contact, text, file))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contact", "expected"),
    [
        ("@synthetic_hr", "synthetic_hr"),
        ("@hiring_bot", None),
        ("@abc", None),
        ("+77001234567", None),
        ("hr@example.test", None),
        ("https://t.me/x", None),
        (None, None),
    ],
)
def test_only_a_real_username_is_ever_written_to(contact: str | None, expected: str | None) -> None:
    assert outreach.handle_of(contact) == expected


@pytest.mark.db
async def test_outreach_respects_every_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def decide(contact: str, key: str, score: float = 90) -> outreach.Decision:
        return await outreach.decide(db_session, contact=contact, vacancy_key=key, score=score)

    monkeypatch.setattr(settings, "telegram_outreach_enabled", False)
    assert not (await decide("@synthetic_hr", "v1")).allowed

    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    monkeypatch.setattr(settings, "telegram_outreach_max_per_day", 2)
    assert not (await decide("@synthetic_hr", "v1", score=50)).allowed

    sender = FakeSender()
    first = await decide("@synthetic_hr", "v1")
    assert first.allowed
    line = await outreach.send(
        db_session,
        sender,
        decision=first,
        vacancy_key="v1",
        text="Здравствуйте",
        file=("cv.docx", b"x"),
    )
    assert line.startswith("Отправлено @synthetic_hr")
    assert sender.sent == [("@synthetic_hr", "Здравствуйте", ("cv.docx", b"x"))]

    again = await decide("@synthetic_hr", "v2")
    assert not again.allowed and "уже писали" in again.reason

    second = await decide("@other_recruiter", "v3")
    await outreach.send(db_session, sender, decision=second, vacancy_key="v3", text="t", file=None)
    over = await decide("@third_recruiter", "v4")
    assert not over.allowed and "лимит" in over.reason


@pytest.mark.db
async def test_a_failed_send_stops_outreach_for_the_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    decision = await outreach.decide(
        db_session, contact="@synthetic_hr", vacancy_key="v1", score=90
    )

    line = await outreach.send(
        db_session, FakeSender(fail=True), decision=decision, vacancy_key="v1", text="t", file=None
    )

    assert "остановлена" in line
    after = await outreach.decide(db_session, contact="@someone_else", vacancy_key="v2", score=90)
    assert not after.allowed and "остановлено" in after.reason


# ── the reaction to a new vacancy ─────────────────────────────────────


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    async def text(message: str, **_: Any) -> bool:
        sent.append(("text", message))
        return True

    async def document(name: str, content: bytes, **kwargs: Any) -> bool:
        sent.append(("document", name, kwargs.get("caption")))
        return True

    monkeypatch.setattr(notify, "send_text", text)
    monkeypatch.setattr(notify, "send_document", document)
    return sent


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession) -> list[UUID]:
    """The model, stubbed: a fixed letter stored where the letter service stores it."""
    written: list[UUID] = []

    async def letter(session: AsyncSession, vacancy_id: UUID, profile: Any, **_: Any) -> Any:
        written.append(vacancy_id)
        session.add(
            Application(id=uuid7(), vacancy_id=vacancy_id, cover_letter="Синтетическое письмо.")
        )
        await session.flush()

    class Outcome:
        document = ReviewedDocument.model_construct(
            filename="cv.docx", file_format="docx", text="", review=None, content=b"DOCX"
        )

    async def cv(session: AsyncSession, vacancy_id: UUID, profile: Any, **_: Any) -> Any:
        return Outcome()

    monkeypatch.setattr(react.letters, "write_letter", letter)
    monkeypatch.setattr(react.documents, "write_cv", cv)
    return written


async def _posting(
    vacancies: Any, seed: str, slug: str, external: str, raw: dict[str, Any]
) -> UUID:
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed, title=f"Python {seed}"),
        source_slug=slug,
        external_id=external,
        url=f"https://example.test/{slug}/{external}",
        raw=raw,
    )
    return result.vacancy_id


@pytest.mark.db
async def test_new_vacancies_get_documents_a_message_and_the_right_action(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    chat: list[Any],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    tg = await _posting(
        vacancies,
        "tg",
        "telegram",
        "synthetic_jobs/1",
        {"_derived": {"apply_contact": "@synthetic_hr", "channel": "synthetic_jobs"}},
    )
    hh = await _posting(vacancies, "hh", "hh", "137000001", {})
    low = await _posting(vacancies, "low", "remotive", "r-1", {})

    async def scored(session: AsyncSession, **_: Any) -> None:
        await matches.bulk_upsert(
            [
                make_match(backend.id, tg, Decimal("91")),
                make_match(backend.id, hh, Decimal("85")),
                make_match(backend.id, low, Decimal("40")),
            ]
        )

    monkeypatch.setattr(react, "score_corpus", scored)
    sender = FakeSender()

    reactions = await react.react(db_session, [tg, hh, low], sender=sender)

    assert [item.vacancy_id for item in reactions] == [tg, hh], "best first, the low one ignored"
    assert writers == [tg, hh]
    assert sender.sent and sender.sent[0][0] == "@synthetic_hr"
    assert "Синтетическое письмо." in sender.sent[0][1]
    assert sender.sent[0][2] == ("cv.docx", b"DOCX")
    messages = [entry[1] for entry in chat if entry[0] == "text"]
    assert "Оценка 91 · резюме «backend»" in messages[0]
    assert "Отправлено @synthetic_hr" in messages[0]
    assert "в очереди агента" in messages[1]
    assert [entry[1] for entry in chat if entry[0] == "document"] == ["cv.docx", "cv.docx"]

    # A second tick over the same vacancies does nothing again.
    assert await react.react(db_session, [tg, hh], sender=sender) == []


@pytest.mark.db
async def test_a_spent_budget_still_tells_the_owner_but_writes_nothing(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    chat: list[Any],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "worker_max_documents_per_day", 0)
    backend = await profiles.create(make_profile(slug="backend"))
    vacancy = await _posting(vacancies, "x", "remotive", "r-2", {})

    async def scored(session: AsyncSession, **_: Any) -> None:
        await matches.bulk_upsert([make_match(backend.id, vacancy, Decimal("90"))])

    monkeypatch.setattr(react, "score_corpus", scored)

    [reaction] = await react.react(db_session, [vacancy])

    assert writers == []
    assert reaction.notified and not reaction.letter
    assert "дневной бюджет" in chat[0][1]


# ── the worker ────────────────────────────────────────────────────────


@pytest.mark.db
async def test_the_morning_summary_goes_once_a_day(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    sent: list[str] = []

    async def notify_(text: str, **_: Any) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(worker, "session_factory", factory)
    monkeypatch.setattr(worker.daily, "notify", notify_)
    morning = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)

    assert await worker.maybe_summary(morning - timedelta(hours=2)) is False
    assert await worker.maybe_summary(morning) is True
    assert await worker.maybe_summary(morning + timedelta(hours=1)) is False
    assert len(sent) == 1
    assert await SourceStateRepository(db_session).get("worker", "summary:2026-11-22")


@pytest.mark.unit
def test_the_fast_loop_never_includes_hh(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    class S:
        def __init__(self, slug: str) -> None:
            self.slug = slug

    monkeypatch.setattr(worker, "get_enabled_sources", lambda: [S("hh"), S("telegram"), S("lever")])
    assert worker.fast_slugs() == ["telegram", "lever"]


@pytest.mark.unit
async def test_a_bot_document_is_sent_to_the_owners_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:synthetic"))
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bot123:synthetic/sendDocument").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert await notify.send_document("cv.docx", b"DOCX", caption="Резюме") is True
    body = route.calls[0].request.content
    assert b'name="chat_id"' in body and b"42" in body and b"DOCX" in body


@pytest.mark.db
async def test_a_tick_crawls_then_reacts_to_exactly_what_it_brought_in(
    db_session: AsyncSession, vacancies: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    old = await _posting(vacancies, "old", "remotive", "r-old", {})
    # One test transaction means one now(): move the old row back explicitly.
    from sqlalchemy import update

    from app.db.models import Vacancy

    await db_session.execute(
        update(Vacancy)
        .where(Vacancy.id == old)
        .values(first_seen_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db_session.flush()
    seen: list[list[UUID]] = []
    crawled: list[list[str]] = []

    async def crawl(*, source_slugs: list[str]) -> None:
        crawled.append(source_slugs)
        await _posting(vacancies, "fresh", "remotive", "r-new", {})
        await db_session.flush()

    async def react_(session: AsyncSession, ids: list[UUID], **_: Any) -> list[Any]:
        seen.append(ids)
        return []

    monkeypatch.setattr(worker, "session_factory", factory)
    monkeypatch.setattr(worker, "run_pipeline", crawl)
    monkeypatch.setattr(worker.react, "react", react_)

    found = await worker.crawl_and_react(["remotive"], sender=None)

    assert crawled == [["remotive"]]
    assert found == 1 and old not in seen[0]
    assert await worker.crawl_and_react([], sender=None) == 0


@pytest.mark.unit
async def test_a_failed_crawl_is_logged_and_the_tick_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker
    from app.core.exceptions import AppError

    async def broken(**_: Any) -> None:
        raise AppError("нет активного профиля")

    monkeypatch.setattr(worker, "run_pipeline", broken)
    assert await worker.crawl_and_react(["remotive"], sender=None) == 0


@pytest.mark.unit
def test_outreach_needs_its_switch_and_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    monkeypatch.setattr(settings, "telegram_outreach_enabled", False)
    assert worker.outreach_sender() is None
    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", None)
    assert worker.outreach_sender() is None
    monkeypatch.setattr(settings, "telegram_api_id", 1)
    monkeypatch.setattr(settings, "telegram_api_hash", SecretStr("h"))
    monkeypatch.setattr(settings, "telegram_session_string", SecretStr("synthetic"))
    assert worker.outreach_sender() is not None


@pytest.mark.db
async def test_on_start_the_worker_answers_what_a_previous_run_left(
    db_session: AsyncSession, vacancies: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vacancy found while no resume or no vectors existed is not lost."""
    from app import worker

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    recent = await _posting(vacancies, "recent", "remotive", "r-recent", {})
    seen: list[list[UUID]] = []

    async def react_(session: AsyncSession, ids: list[UUID], **_: Any) -> list[Any]:
        seen.append(ids)
        return []

    monkeypatch.setattr(worker, "session_factory", factory)
    monkeypatch.setattr(worker.react, "react", react_)

    assert await worker.catch_up(sender=None) == 1
    assert seen == [[recent]]


@pytest.mark.db
async def test_reacting_computes_the_profile_vectors_first(
    db_session: AsyncSession, profiles: Any, vacancies: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the headline's vector every title scores as unknown."""
    order: list[str] = []

    async def vectors(session: AsyncSession, *, allowed: bool) -> str:
        order.append(f"vectors:{allowed}")
        return "эмбеддинг заголовка посчитан впервые"

    async def scored(session: AsyncSession, **_: Any) -> None:
        order.append("score")

    monkeypatch.setattr(react.profile_vectors, "ensure_profile_embedding", vectors)
    monkeypatch.setattr(react, "score_corpus", scored)
    await profiles.create(make_profile(slug="backend", priority=0))
    one = await _posting(vacancies, "one", "remotive", "r-one", {})

    assert await react.react(db_session, [one]) == []
    assert order == ["vectors:True", "score"]

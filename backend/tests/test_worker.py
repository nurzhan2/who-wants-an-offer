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
    monkeypatch.setattr(settings, "worker_attach_cv", "tailored")
    return written


async def _posting(
    vacancies: Any, seed: str, slug: str, external: str, raw: dict[str, Any]
) -> UUID:
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed, title=f"Python {seed}", published_at=datetime.now(UTC)),
        source_slug=slug,
        external_id=external,
        url=f"https://example.test/{slug}/{external}",
        raw=raw,
    )
    return result.vacancy_id


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


@pytest.mark.unit
def test_a_letter_answers_the_posting_in_its_own_language() -> None:
    """An English posting from the Netherlands got a Russian letter on 21 Nov."""
    from app.letters.context import VacancyFacts, posting_language

    english = VacancyFacts(
        vacancy_id=uuid7(),
        title="Backend Software Developer",
        description="We build remote APIs in TypeScript on AWS.",
    )
    russian = VacancyFacts(
        vacancy_id=uuid7(),
        title="Python-разработчик",
        description="Пишем сервисы на FastAPI и PostgreSQL, нужен опыт с Docker.",
    )
    assert posting_language(english) == "en"
    assert posting_language(russian) == "ru"
    assert posting_language(VacancyFacts(vacancy_id=uuid7(), title="")) == "ru"


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every message the bot sends, with its buttons."""
    sent: list[tuple[str, Any]] = []

    async def text(message: str, *, buttons: Any = None, **_: Any) -> bool:
        sent.append((message, buttons))
        return True

    async def document(name: str, content: bytes, **kwargs: Any) -> bool:
        sent.append((f"file:{name}", kwargs.get("caption")))
        return True

    monkeypatch.setattr(notify, "send_text", text)
    monkeypatch.setattr(notify, "send_document", document)
    return sent


async def _own_file(db_session: AsyncSession, profile_id: UUID, name: str) -> None:
    from sqlalchemy import update

    from app.db.models import CandidateProfile

    await db_session.execute(
        update(CandidateProfile)
        .where(CandidateProfile.id == profile_id)
        .values(resume_file=b"%PDF-synthetic", resume_filename=name)
    )


def _scoring(matches: Any, rows: list[tuple[UUID, UUID, str]]) -> Any:
    async def scored(session: AsyncSession, **_: Any) -> None:
        await matches.bulk_upsert([make_match(p, v, Decimal(s)) for p, v, s in rows])

    return scored


@pytest.mark.db
async def test_a_match_arrives_as_a_short_card_with_buttons_and_nothing_is_written(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """21 Nov: three days of matches, each with a letter pasted in, buried the chat."""
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    tg = await _posting(
        vacancies,
        "tg",
        "telegram",
        "synthetic_jobs/1",
        {"_derived": {"apply_contact": "@synthetic_hr"}},
    )
    hh = await _posting(vacancies, "hh", "hh", "137000001", {})
    low = await _posting(vacancies, "low", "remotive", "r-1", {})
    monkeypatch.setattr(
        react,
        "score_corpus",
        _scoring(
            matches, [(backend.id, tg, "91"), (backend.id, hh, "85"), (backend.id, low, "40")]
        ),
    )
    sender = FakeSender()

    reactions = await react.react(db_session, [tg, hh, low], sender=sender)

    assert [item.vacancy_id for item in reactions] == [tg, hh], "best first, the low one ignored"
    assert writers == [], "no letter until a button asks for one"
    assert sender.sent == [], "outreach is off until the owner turns it on"
    first, second = cards
    assert "⭐ 91 · backend · telegram" in first[0] and "@synthetic_hr" in first[0]
    assert first[1] == [[("✉️ Письмо", f"L:{tg}"), ("📄 Резюме", f"C:{tg}")]]
    assert ("➕ В очередь hh", f"Q:{hh}") in second[1][1]
    assert await react.react(db_session, [tg, hh], sender=sender) == []


@pytest.mark.db
async def test_filters_drop_old_postings_switched_off_tracks_and_the_overflow(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import update

    from app.db.models import Vacancy
    from app.services import bot

    backend = await profiles.create(make_profile(slug="backend", priority=0))
    llm = await profiles.create(make_profile(slug="llm", priority=1))
    old = await _posting(vacancies, "old", "remotive", "r-old", {})
    await db_session.execute(
        update(Vacancy)
        .where(Vacancy.id == old)
        .values(published_at=datetime.now(UTC) - timedelta(days=3))
    )
    off = await _posting(vacancies, "off", "remotive", "r-off", {})
    many = [await _posting(vacancies, f"m{i}", "remotive", f"r-m{i}", {}) for i in range(10)]
    prefs = bot.Prefs(tracks_off=["llm"])
    await bot.save_prefs(db_session, prefs)
    rows = [(backend.id, old, "95"), (llm.id, off, "99")] + [(backend.id, v, "90") for v in many]
    monkeypatch.setattr(react, "score_corpus", _scoring(matches, rows))

    reactions = await react.react(db_session, [old, off, *many])

    assert len(reactions) == react.MAX_CARDS_PER_PASS
    assert old not in {r.vacancy_id for r in reactions} and off not in {
        r.vacancy_id for r in reactions
    }
    assert "Ещё 2 подходящих" in cards[-1][0]


@pytest.mark.db
async def test_outreach_once_turned_on_sends_the_letter_and_the_owner_s_file(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import bot

    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    monkeypatch.setattr(settings, "worker_attach_cv", "original")
    await bot.save_prefs(db_session, bot.Prefs(outreach=True))
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    await _own_file(db_session, backend.id, "backend.pdf")
    tg = await _posting(
        vacancies,
        "own",
        "telegram",
        "synthetic_jobs/3",
        {"_derived": {"apply_contact": "@synthetic_hr3"}},
    )
    monkeypatch.setattr(react, "score_corpus", _scoring(matches, [(backend.id, tg, "90")]))
    sender = FakeSender()

    await react.react(db_session, [tg], sender=sender)

    assert sender.sent[0][0] == "@synthetic_hr3"
    assert "Синтетическое письмо." in sender.sent[0][1]
    assert sender.sent[0][2] == ("backend.pdf", b"%PDF-synthetic")
    assert "Автоотклик: Отправлено @synthetic_hr3" in cards[0][0]


@pytest.mark.db
async def test_no_file_means_no_message_to_the_recruiter(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact is written to once: a letter without the CV would waste it."""
    from app.services import bot

    monkeypatch.setattr(settings, "telegram_outreach_enabled", True)
    monkeypatch.setattr(settings, "worker_attach_cv", "original")
    await bot.save_prefs(db_session, bot.Prefs(outreach=True))
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    tg = await _posting(
        vacancies,
        "nocv",
        "telegram",
        "synthetic_jobs/2",
        {"_derived": {"apply_contact": "@synthetic_hr2"}},
    )
    monkeypatch.setattr(react, "score_corpus", _scoring(matches, [(backend.id, tg, "90")]))
    sender = FakeSender()

    await react.react(db_session, [tg], sender=sender)

    assert sender.sent == []
    assert "нет файла резюме" in cards[0][0]


@pytest.mark.db
async def test_the_buttons_send_the_letter_the_file_and_queue_hh(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "worker_attach_cv", "original")
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    await _own_file(db_session, backend.id, "backend.pdf")
    hh = await _posting(vacancies, "hhq", "hh", "137000009", {})
    monkeypatch.setattr(react, "score_corpus", _scoring(matches, [(backend.id, hh, "90")]))
    await react.react(db_session, [hh])
    cards.clear()

    assert await react.on_button(db_session, f"L:{hh}") == "Письмо отправлено"
    assert "Синтетическое письмо." in cards[-1][0]
    assert await react.on_button(db_session, f"C:{hh}") == "Резюме отправлено"
    assert cards[-1][0] == "file:backend.pdf"
    assert await react.on_button(db_session, f"Q:{hh}") == "В очереди hh"
    assert writers == [hh], "the letter is written once and reused"
    assert await react.on_button(db_session, "L:not-a-uuid") == ""


@pytest.mark.db
async def test_a_spent_budget_writes_no_letter_and_says_so(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    cards: list[tuple[str, Any]],
    writers: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "worker_max_documents_per_day", 0)
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    v = await _posting(vacancies, "budget", "remotive", "r-budget", {})
    monkeypatch.setattr(react, "score_corpus", _scoring(matches, [(backend.id, v, "90")]))
    await react.react(db_session, [v])

    toast = await react.on_button(db_session, f"L:{v}")

    assert "бюджет" in toast and writers == []


@pytest.mark.db
async def test_the_bot_obeys_only_its_owner_and_saves_filters(
    db_session: AsyncSession,
    profiles: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import bot

    monkeypatch.setattr(settings, "telegram_chat_id", "1001")
    edits: list[Any] = []
    toasts: list[str] = []

    async def edit(message_id: int, text: str, **kwargs: Any) -> bool:
        edits.append((message_id, text, kwargs.get("buttons")))
        return True

    async def answer(callback_id: str, text: str = "") -> bool:
        toasts.append(text)
        return True

    monkeypatch.setattr(notify, "edit_text", edit)
    monkeypatch.setattr(notify, "answer_callback", answer)
    await profiles.create(make_profile(slug="backend", priority=0))

    def tap(chat_id: str, data: str) -> dict[str, Any]:
        return {
            "callback_query": {
                "id": "q",
                "data": data,
                "message": {"message_id": 7, "chat": {"id": chat_id}},
            }
        }

    await bot.handle(db_session, tap("666", "P"))
    assert edits == [] and not (await bot.load_prefs(db_session)).paused

    await bot.handle(db_session, tap("1001", "T:backend"))
    await bot.handle(db_session, tap("1001", "A:72"))
    await bot.handle(db_session, tap("1001", "O"))
    prefs = await bot.load_prefs(db_session)
    assert prefs.tracks_off == ["backend"] and prefs.max_age_hours == 72 and prefs.outreach
    assert "⬜ backend" in str(edits[-1][2]) and toasts[-1] == "Сохранено"


@pytest.mark.unit
def test_a_posting_s_age_decides_whether_it_is_announced() -> None:
    from app.services import bot

    prefs = bot.Prefs(max_age_hours=24)
    fresh = datetime.now(UTC) - timedelta(hours=3)
    stale = datetime.now(UTC) - timedelta(days=2)
    assert bot.why_not(prefs, slug="b", score=Decimal(90), published=fresh) is None
    assert bot.why_not(prefs, slug="b", score=Decimal(90), published=stale) == "too_old"
    assert bot.why_not(prefs, slug="b", score=Decimal(50), published=fresh) == "below_floor"
    assert (
        bot.why_not(bot.Prefs(paused=True), slug="b", score=Decimal(90), published=fresh)
        == "paused"
    )
    assert bot.age_label(fresh) == "3 ч назад"

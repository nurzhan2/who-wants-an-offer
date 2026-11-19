"""The Telegram source, against a fake channel reader. No network, no session.

Every post below is synthetic: invented channels, invented employers, invented
handles. What is pinned:

* the free heuristic keeps job posts and drops seekers, digests and chatter,
  and the run reports the share it cut;
* the model reads only what survived, its answer is validated, and any failure
  falls back to the rules instead of losing the post;
* ``last_message_id`` advances only once the pipeline says the postings are
  durable, and the next run asks only for newer posts;
* a FloodWait is waited out exactly, once, and written down;
* a dead or missing channel is reported by name;
* without configuration the source is inactive, not failing.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import ConfigurationError, LLMError, SourceError
from app.llm.base import LLMResult
from app.sources._telegram_client import (
    ChannelMissingError,
    ChannelPost,
    FloodWaitRequired,
    SessionExpiredError,
)
from app.sources._telegram_posts import (
    ParsedPost,
    contact_in,
    fallback_parse,
    looks_like_vacancy,
    parse_post,
)
from app.sources.base import SearchQuery, SourceUnavailable
from app.sources.registry import get_source
from app.sources.telegram import (
    CHANNELS_FILE,
    STATE_CHANNEL,
    STATE_RUN,
    Channel,
    TelegramSource,
    load_channels,
    session_file,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

VACANCY = (
    "🔥 Вакансия: Python Backend Developer (Middle)\n"
    "Компания: Synthetic Labs\nФормат: удалённо\nЗП: 2000–3000 $\n"
    "Стек: Python, FastAPI, PostgreSQL\nОткликаться: @synthetic_hr"
)
ENGLISH = (
    "We're hiring a Senior LLM Engineer, fully remote, $5000-7000.\n"
    "Python, PyTorch. Apply: https://example.test/jobs/42"
)
JAVA = "Ищем Java разработчика в команду, вилка 400 000 ₸, пишите @someone_hr, Алматы, офис."
SEEKER = "#резюме Ищу работу Python разработчиком, опыт 3 года, пишите @me_myself_and_i"
DIGEST = "#дайджест Лучшие вакансии недели по ссылке https://example.test/digest и ещё много"
CHATTER = "Всем привет! Как прошли выходные? Делитесь в комментариях, что читаете сейчас."


def post(number: int, text: str, *, age_days: int = 1) -> ChannelPost:
    return ChannelPost(id=number, date=NOW - timedelta(days=age_days), text=text)


class FakeReader:
    """Serves posts per channel; records what it was asked."""

    def __init__(
        self,
        posts: dict[str, Sequence[ChannelPost]],
        *,
        flood: dict[str, list[int]] | None = None,
        missing: set[str] | None = None,
        expired: bool = False,
    ) -> None:
        self.posts = posts
        self.flood = {name: list(waits) for name, waits in (flood or {}).items()}
        self.missing = missing or set()
        self.expired = expired
        self.asked: list[tuple[str, int, int]] = []
        self.closed = False

    async def recent(self, username: str, *, after_id: int, limit: int) -> Sequence[ChannelPost]:
        self.asked.append((username, after_id, limit))
        if self.expired:
            raise SessionExpiredError("сессия протухла")
        if username in self.missing:
            raise ChannelMissingError(username)
        waits = self.flood.get(username)
        if waits:
            raise FloodWaitRequired(waits.pop(0))
        found = [item for item in self.posts.get(username, ()) if item.id > after_id]
        return sorted(found, key=lambda item: -item.id)[:limit]

    async def close(self) -> None:
        self.closed = True


class Sleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class Store:
    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        return self.saved.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self.saved[key] = value


class FakeRouter:
    """Answers like the model would; ``fail`` makes it raise like the router."""

    def __init__(self, *, fail: bool = False, not_vacancy: set[str] | None = None) -> None:
        self.fail = fail
        self.not_vacancy = not_vacancy or set()
        self.calls = 0

    async def complete_json(self, prompt: str, model: type[ParsedPost], **kwargs: Any) -> Any:
        self.calls += 1
        if self.fail:
            raise LLMError("no provider")
        text = kwargs["variables"]["post"]
        if any(marker in text for marker in self.not_vacancy):
            return LLMResult(value=ParsedPost(is_vacancy=False), usage=None)  # type: ignore[arg-type]
        title = text.splitlines()[0].replace("🔥 Вакансия: ", "")[:80]
        return LLMResult(
            value=ParsedPost(is_vacancy=True, title=title, company="Synthetic Labs", remote="full"),
            usage=None,  # type: ignore[arg-type]
        )


CHANNELS = (
    Channel(username="synthetic_jobs", region="kz", stack=("python",)),
    Channel(username="synthetic_remote", region="world", language="en"),
)


def build(
    reader: FakeReader, store: Store, *, router: Any = None, sleep: Sleeper | None = None
) -> TelegramSource:
    source = TelegramSource(
        reader=reader,
        router=router if router is not None else FakeRouter(),
        channels=CHANNELS,
        sleep=sleep or Sleeper(),
        now=lambda: NOW,
    )
    return source.with_state(store.load, store.save)


async def walk(source: TelegramSource, keywords: tuple[str, ...] = ()) -> list[Any]:
    found = [item async for item in source.search_batch([SearchQuery(keywords=keywords)])]
    await source.record_progress(len(found))
    return found


# ── the free filter ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (VACANCY, True),
        (ENGLISH, True),
        (JAVA, True),
        (SEEKER, False),
        (DIGEST, False),
        (CHATTER, False),
        ("Вакансия", False),
    ],
)
def test_the_heuristic_keeps_job_posts_and_drops_the_rest(text: str, expected: bool) -> None:
    assert looks_like_vacancy(text) is expected


def test_the_contact_is_read_as_written() -> None:
    assert contact_in(VACANCY) == "@synthetic_hr"
    assert contact_in(ENGLISH) == "https://example.test/jobs/42"


def test_the_rules_keep_a_post_the_model_could_not_read() -> None:
    parsed = fallback_parse(VACANCY)
    assert parsed.is_vacancy
    assert parsed.title is not None and "Python Backend Developer" in parsed.title
    assert parsed.remote == "full"
    assert parsed.apply_contact == "@synthetic_hr"
    assert parsed.company is None, "a regular expression does not guess the employer"


async def test_a_failing_model_falls_back_to_the_rules() -> None:
    parsed, how = await parse_post(VACANCY, router=FakeRouter(fail=True))  # type: ignore[arg-type]
    assert (how, parsed.is_vacancy) == ("rules", True)


def test_an_invalid_model_answer_is_refused_by_the_schema() -> None:
    with pytest.raises(ValueError):
        ParsedPost.model_validate({"is_vacancy": True, "currency": "dollars"})


# ── the run ───────────────────────────────────────────────────────────


async def test_a_run_yields_vacancies_and_reports_what_the_filter_cut() -> None:
    reader = FakeReader(
        {
            "synthetic_jobs": [post(1, VACANCY), post(2, SEEKER), post(3, CHATTER), post(4, JAVA)],
            "synthetic_remote": [post(10, ENGLISH), post(11, DIGEST)],
        }
    )
    store = Store()
    router = FakeRouter()

    found = await walk(build(reader, store, router=router), keywords=("python",))

    assert [item.external_id for item in found] == ["synthetic_jobs/1", "synthetic_remote/10"]
    first = found[0]
    assert first.url == "https://t.me/synthetic_jobs/1"
    assert first.raw["_derived"]["post_url"] == first.url
    assert first.raw["_derived"]["region"] == "kz"
    assert first.raw["_derived"]["parsed_by"] == "llm"
    report = store.saved[STATE_RUN]
    assert (report["posts"], report["heuristic_rejected"], report["keyword_rejected"]) == (6, 3, 1)
    assert router.calls == 2, "the model reads only what both filters let through"
    assert reader.closed


async def test_the_model_saying_not_a_vacancy_drops_the_post() -> None:
    reader = FakeReader({"synthetic_jobs": [post(1, VACANCY)], "synthetic_remote": []})
    store = Store()

    router = FakeRouter(not_vacancy={"Synthetic"})
    found = await walk(build(reader, store, router=router))

    assert found == []
    assert store.saved[STATE_RUN]["not_vacancy"] == 1


async def test_the_watermark_moves_only_once_the_postings_are_durable() -> None:
    reader = FakeReader(
        {"synthetic_jobs": [post(5, VACANCY), post(7, VACANCY)], "synthetic_remote": []}
    )
    store = Store()
    source = build(reader, store)

    found = [item async for item in source.search_batch([SearchQuery()])]
    assert f"{STATE_CHANNEL}synthetic_jobs" not in store.saved, "nothing is durable yet"
    await source.record_progress(1)
    assert f"{STATE_CHANNEL}synthetic_jobs" not in store.saved, "one of two is not enough"
    await source.record_progress(len(found))
    assert store.saved[f"{STATE_CHANNEL}synthetic_jobs"]["last_message_id"] == 7


async def test_the_next_run_asks_only_for_newer_posts() -> None:
    posts = {"synthetic_jobs": [post(5, VACANCY)], "synthetic_remote": []}
    store = Store()
    await walk(build(FakeReader(posts), store))
    posts["synthetic_jobs"] = [post(5, VACANCY), post(9, VACANCY)]
    reader = FakeReader(posts)

    found = await walk(build(reader, store))

    assert [item.external_id for item in found] == ["synthetic_jobs/9"]
    assert ("synthetic_jobs", 5, 100) in reader.asked


async def test_a_flood_wait_is_waited_exactly_once_and_reported() -> None:
    reader = FakeReader(
        {"synthetic_jobs": [post(1, VACANCY)], "synthetic_remote": []},
        flood={"synthetic_jobs": [37]},
    )
    store = Store()
    sleeper = Sleeper()

    found = await walk(build(reader, store, sleep=sleeper))

    assert len(found) == 1
    assert 37.0 in sleeper.waits
    assert store.saved[STATE_RUN]["flood_waits"] == [
        {"channel": "synthetic_jobs", "seconds": 37, "attempt": 1}
    ]


async def test_a_second_flood_wait_skips_the_channel_without_more_retries() -> None:
    reader = FakeReader(
        {"synthetic_jobs": [post(1, VACANCY)], "synthetic_remote": [post(2, ENGLISH)]},
        flood={"synthetic_jobs": [5, 5]},
    )
    store = Store()

    found = await walk(build(reader, store))

    assert [item.external_id for item in found] == ["synthetic_remote/2"]
    assert [entry for entry in reader.asked if entry[0] == "synthetic_jobs"] == [
        ("synthetic_jobs", 0, 50),
        ("synthetic_jobs", 0, 50),
    ]


async def test_dead_and_missing_channels_are_named() -> None:
    reader = FakeReader(
        {"synthetic_jobs": [post(1, CHATTER, age_days=45)]}, missing={"synthetic_remote"}
    )
    store = Store()

    await walk(build(reader, store))

    report = store.saved[STATE_RUN]
    assert report["dead"] == ["synthetic_jobs"]
    assert report["missing"] == ["synthetic_remote"]
    assert store.saved[f"{STATE_CHANNEL}synthetic_jobs"]["status"] == "dead"
    assert store.saved[f"{STATE_CHANNEL}synthetic_remote"]["status"] == "missing"


async def test_an_expired_session_fails_the_source_loudly_and_says_so() -> None:
    store = Store()
    with pytest.raises(SourceError):
        await walk(build(FakeReader({}, expired=True), store))
    assert store.saved[STATE_RUN]["session_expired"] is True


# ── configuration ─────────────────────────────────────────────────────


def test_without_configuration_the_source_is_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_api_id", None)
    reason = TelegramSource().unavailable()
    assert reason is not None
    assert reason.code is SourceUnavailable.MISSING_CREDENTIALS


def test_without_a_session_file_the_source_says_how_to_log_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "telegram_api_id", 12345)
    monkeypatch.setattr(settings, "telegram_api_hash", SecretStr("synthetic"))
    monkeypatch.setattr(settings, "telegram_session_path", tmp_path / "tg")
    reason = TelegramSource().unavailable()
    assert reason is not None and "telegram_login" in reason.detail
    (tmp_path / "tg.session").write_bytes(b"")
    assert TelegramSource().unavailable() is None


def test_session_file_follows_telethons_naming() -> None:
    assert session_file(Path("a/tg")) == Path("a/tg.session")
    assert session_file(Path("a/tg.session")) == Path("a/tg.session")


def test_the_shipped_channel_list_parses_and_is_marked_unverified() -> None:
    channels = load_channels(CHANNELS_FILE)
    assert len(channels) >= 10
    assert {channel.region for channel in channels} >= {"kz", "ru", "world"}
    assert not any(channel.verified for channel in channels), (
        "nobody has checked these yet; a run marks dead ones and the prune script removes them"
    )


def test_a_broken_channel_list_is_a_configuration_error(tmp_path: Path) -> None:
    broken = tmp_path / "channels.yaml"
    broken.write_text("channels: [{username: x}]", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_channels(broken)


def test_the_source_registers_with_its_terms() -> None:
    source = get_source("telegram")
    assert source.terms_url == "https://core.telegram.org/api/terms"
    assert source.publisher_of({"_derived": {"channel": "synthetic_jobs"}}) == "@synthetic_jobs"


@pytest.mark.db
async def test_the_card_offers_the_post_the_contact_and_a_message_to_copy(
    db_session: Any, vacancies: Any
) -> None:
    """Applying through Telegram is the owner's job; the card hands them the text."""
    from app.db.base import uuid7
    from app.db.models import Application
    from app.services import vacancies as vacancies_service
    from factories import make_vacancy

    result = await vacancies.upsert_by_external_id(
        make_vacancy("tg-card", title="Python Backend Developer"),
        source_slug="telegram",
        external_id="synthetic_jobs/1",
        url="https://t.me/synthetic_jobs/1",
        raw={
            "_derived": {
                "post_url": "https://t.me/synthetic_jobs/1",
                "apply_contact": "@synthetic_hr",
                "channel": "synthetic_jobs",
            }
        },
    )
    db_session.add(
        Application(id=uuid7(), vacancy_id=result.vacancy_id, cover_letter="Текст письма.")
    )
    await db_session.flush()

    card = await vacancies_service.card(db_session, result.vacancy_id)

    assert card is not None and card.manual_apply is not None
    assert card.manual_apply.contact == "@synthetic_hr"
    assert card.manual_apply.post_url == "https://t.me/synthetic_jobs/1"
    assert "@synthetic_jobs" in card.manual_apply.message
    assert card.manual_apply.message.endswith("Текст письма.")

"""Job-alert letters, parsed from synthetic copies. No mailbox, no network.

Every letter here is invented: invented employers, invented ids, the layout of
an alert card (title link, company, location) and the tracking parameters such
letters carry. What is pinned:

* a job is a link matching the sender's pattern; company and location are the
  two text pieces after it; buttons and boilerplate are not mistaken for them;
* the stored link is rebuilt from the id — the tracking parameters never reach
  the database — and no host is contacted;
* letters are read incrementally by UID, and a UID moves only once the
  postings are durable;
* a letter from a listed sender with no recognisable job is reported;
* a stub from a letter does not erase the description a richer source wrote
  (the fingerprint merge that gives LinkedIn postings their text).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import ConfigurationError
from app.pipeline.runner import to_vacancy
from app.sources.base import SearchQuery, SourceUnavailable
from app.sources.email_alerts import (
    SENDERS_FILE,
    STATE_RUN,
    STATE_SENDER,
    AlertLetter,
    EmailAlertsSource,
    Sender,
    html_of,
    load_senders,
    parse_alert,
)
from app.sources.registry import get_source

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

SENDERS = {sender.key: sender for sender in load_senders()}

LINKEDIN = """
<html><head><style>.x{}</style></head><body>
<table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4100000001/?trackingId=OWNER-SECRET&refId=abc">
    Senior Python Engineer</a>
  <p>Synthetic Labs</p><p>Remote</p>
  <a href="https://www.linkedin.com/comm/jobs/view/4100000001/?trackingId=OWNER-SECRET">View job</a>
</td></tr><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4100000002/?trackingId=OWNER-SECRET">
    Backend Developer (FastAPI)</a>
  <span>Example Fintech</span><span>Almaty, Kazakhstan</span>
  <span>Actively recruiting</span>
</td></tr>
<a href="https://www.linkedin.com/comm/jobs/search?keywords=python">See all jobs</a>
<a href="https://www.linkedin.com/unsubscribe?u=OWNER-SECRET">Unsubscribe</a>
</table></body></html>
"""

INDEED = """
<div><a href="https://www.indeed.com/rc/clk?jk=0a1b2c3d4e5f6789&from=ja&tk=OWNER">ML Engineer</a>
<div>Synthetic AI</div><div>Remote</div><a href="https://www.indeed.com/rc/clk?jk=0a1b2c3d4e5f6789">Apply now</a></div>
"""

HH = """
<p><a href="https://hh.kz/vacancy/137000555?utm_source=email&from=OWNER">Python-разработчик</a></p>
<p>ТОО Синтетика</p><p>Алматы</p>
"""


def letter(uid: int, html: str, *, sender: str = "jobalerts-noreply@linkedin.com") -> AlertLetter:
    return AlertLetter(
        uid=uid,
        message_id=f"<synthetic-{uid}@example.test>",
        received_at=NOW - timedelta(hours=uid),
        sender=sender,
        html=html,
    )


class FakeMailbox:
    def __init__(self, letters: dict[str, list[AlertLetter]], validity: int = 7) -> None:
        self.by_address = letters
        self.validity = validity
        self.asked: list[tuple[tuple[str, ...], int]] = []

    async def letters(
        self, addresses: Sequence[str], *, since: datetime, after_uid: int
    ) -> tuple[int, Sequence[AlertLetter]]:
        self.asked.append((tuple(addresses), after_uid))
        found = [
            item
            for address in addresses
            for item in self.by_address.get(address, [])
            if item.uid > after_uid
        ]
        return self.validity, found


class Store:
    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        return self.saved.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self.saved[key] = value


def build(mailbox: FakeMailbox, store: Store) -> EmailAlertsSource:
    source = EmailAlertsSource(mailbox=mailbox, now=lambda: NOW)
    return source.with_state(store.load, store.save)


async def walk(source: EmailAlertsSource) -> list[Any]:
    found = [item async for item in source.search_batch([SearchQuery()])]
    await source.record_progress(len(found))
    return found


# ── parsing ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_linkedin_card_gives_title_company_and_place_and_drops_the_tracking() -> None:
    jobs = parse_alert(LINKEDIN, SENDERS["linkedin"])

    assert [(job.title, job.company, job.location) for job in jobs] == [
        ("Senior Python Engineer", "Synthetic Labs", "Remote"),
        ("Backend Developer (FastAPI)", "Example Fintech", "Almaty, Kazakhstan"),
    ]
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4100000001/"
    assert not any("OWNER" in job.url for job in jobs)


@pytest.mark.unit
def test_indeed_and_hh_cards_parse_the_same_way() -> None:
    [indeed] = parse_alert(INDEED, SENDERS["indeed"])
    [hh] = parse_alert(HH, SENDERS["hh"])

    assert (indeed.job_id, indeed.title, indeed.company) == (
        "0a1b2c3d4e5f6789",
        "ML Engineer",
        "Synthetic AI",
    )
    assert indeed.url == "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"
    assert (hh.title, hh.company, hh.location) == ("Python-разработчик", "ТОО Синтетика", "Алматы")
    assert hh.url == "https://hh.kz/vacancy/137000555"


@pytest.mark.unit
def test_a_plain_text_letter_is_read_too() -> None:
    message = EmailMessage()
    message["From"] = "alert@indeed.com"
    message.set_content(
        "Data Engineer\nhttps://www.indeed.com/viewjob?jk=1234567890abcdef&tk=OWNER\nSynthetic Data\n"
    )
    [job] = parse_alert(html_of(message), SENDERS["indeed"])
    assert (job.title, job.job_id) == ("Data Engineer", "1234567890abcdef")


@pytest.mark.unit
def test_the_shipped_whitelist_parses_and_names_the_four_services() -> None:
    assert set(SENDERS) == {"linkedin", "indeed", "glassdoor", "hh"}
    assert load_senders(SENDERS_FILE)


@pytest.mark.unit
def test_a_broken_whitelist_is_a_configuration_error(tmp_path: Any) -> None:
    broken = tmp_path / "senders.yaml"
    broken.write_text("senders: [{key: x, from: [], links: ['(']}]", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_senders(broken)


# ── the run ───────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_a_run_yields_stubs_with_the_letter_id_and_never_the_letter() -> None:
    mailbox = FakeMailbox({"jobalerts-noreply@linkedin.com": [letter(10, LINKEDIN)]})
    store = Store()

    found = await walk(build(mailbox, store))

    assert [item.external_id for item in found] == ["linkedin:4100000001", "linkedin:4100000002"]
    first = found[0]
    assert first.description is None
    assert first.raw["email"]["message_id"] == "<synthetic-10@example.test>"
    assert "Senior Python Engineer" not in str(first.raw["email"]), "the letter is not stored"
    assert first.raw["_derived"]["remote"] == "full"
    assert to_vacancy(first).city is None, "a city would split the fingerprint from jsearch's row"
    assert store.saved[STATE_RUN]["jobs"] == 2


@pytest.mark.unit
async def test_letters_are_read_after_the_last_uid_once_the_postings_are_durable() -> None:
    mailbox = FakeMailbox({"jobalerts-noreply@linkedin.com": [letter(10, LINKEDIN)]})
    store = Store()
    source = build(mailbox, store)

    found = [item async for item in source.search_batch([SearchQuery()])]
    assert f"{STATE_SENDER}linkedin" not in store.saved
    await source.record_progress(len(found))
    assert store.saved[f"{STATE_SENDER}linkedin"] == {"uidvalidity": 7, "last_uid": 10}

    await walk(build(mailbox, store))
    linkedin_asks = [
        after for addresses, after in mailbox.asked if "jobalerts-noreply@linkedin.com" in addresses
    ]
    assert linkedin_asks == [0, 10]


@pytest.mark.unit
async def test_a_letter_with_no_recognisable_job_is_reported() -> None:
    mailbox = FakeMailbox(
        {"jobalerts-noreply@linkedin.com": [letter(3, "<p>Your weekly digest</p>")]}
    )
    store = Store()

    assert await walk(build(mailbox, store)) == []
    assert store.saved[STATE_RUN]["letters_without_jobs"] == 1


@pytest.mark.unit
def test_without_credentials_the_source_is_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_alerts_imap_user", None)
    reason = EmailAlertsSource().unavailable()
    assert reason is not None and reason.code is SourceUnavailable.MISSING_CREDENTIALS
    monkeypatch.setattr(settings, "email_alerts_imap_user", "owner@example.test")
    monkeypatch.setattr(settings, "email_alerts_imap_password", SecretStr("app-password"))
    assert EmailAlertsSource().unavailable() is None


@pytest.mark.unit
def test_it_registers_and_names_the_service_on_the_card() -> None:
    source = get_source("email_alerts")
    assert source.terms_url
    assert source.publisher_of({"_derived": {"via": "linkedin"}}) == "LinkedIn"


@pytest.mark.unit
def test_the_module_makes_no_http_request_at_all() -> None:
    """Not even the one page a letter links to: there is no client to make it with."""
    from pathlib import Path

    import app.sources.email_alerts as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    assert "self.http" not in text and "httpx" not in text


@pytest.mark.db
async def test_a_stub_from_a_letter_keeps_the_description_another_source_wrote(
    vacancies: Any, db_session: Any
) -> None:
    from sqlalchemy import select

    from app.db.models import Vacancy, VacancySource
    from app.sources.base import RawPosting

    rich = RawPosting(
        source_slug="jsearch",
        external_id="js-1",
        url="https://example.test/jsearch/1",
        title="Senior Python Engineer",
        company="Synthetic Labs",
        description="Full text of the posting.",
    )
    stub = RawPosting(
        source_slug="email_alerts",
        external_id="linkedin:4100000001",
        url="https://www.linkedin.com/jobs/view/4100000001/",
        title="Senior Python Engineer",
        company="Synthetic Labs",
    )
    for posting in (rich, stub):
        await vacancies.upsert_by_external_id(
            to_vacancy(posting),
            source_slug=posting.source_slug,
            external_id=posting.external_id,
            url=posting.url,
            raw=posting.raw,
        )
    await db_session.flush()

    [row] = (await db_session.scalars(select(Vacancy))).all()
    assert row.description_raw == "Full text of the posting."
    slugs = (await db_session.scalars(select(VacancySource.source_slug))).all()
    assert sorted(slugs) == ["email_alerts", "jsearch"]


@pytest.mark.unit
def test_a_sender_needs_an_id_placeholder() -> None:
    with pytest.raises(ValueError):
        Sender.model_validate(
            {"key": "x1", "from": ["a@b.c"], "links": ["(\\d+)"], "canonical": "x"}
        )

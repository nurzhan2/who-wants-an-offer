"""The keyless remote boards: himalayas, remoteok, jobicy.

Every payload comes from ``fixtures/sources`` and every one of them is
**reconstructed** from the vendor's public documentation, not captured: the
container these connectors were written in had no network. Each fixture says
so itself (a ``_comment`` key, an XML comment, or ``remoteok.README`` beside
the bare-array payload). These tests therefore pin the connectors' own rules —
the legal notice skipped, the malformed entry dropped, the keyword filter
applied here, the HN thread read incrementally — not bytes anyone has seen
from the vendor. Re-record the fixtures on the first live run.

``respx`` serves every request; an unmocked URL fails the test.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.exceptions import SourceError
from app.db.enums import RemoteType
from app.sources import registry
from app.sources._boards import parse_timestamp, positive, strip_tags
from app.sources.base import AccessMode, BaseSource, RawPosting, SearchQuery
from app.sources.himalayas import API_URL as HIMALAYAS_URL
from app.sources.himalayas import PAGE_SIZE, HimalayasSource
from app.sources.http import SourceClient
from app.sources.jobicy import API_URL as JOBICY_URL
from app.sources.jobicy import FEED_COUNT, JobicySource
from app.sources.remoteok import API_URL as REMOTEOK_URL
from app.sources.remoteok import RemoteOkSource

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
KEYLESS_SLUGS = ("himalayas", "remoteok", "jobicy")


def payload(name: str) -> Any:
    """One saved JSON response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``, so a token bucket costs no wall clock."""
    return None


async def drain(source: BaseSource, *queries: SearchQuery) -> list[RawPosting]:
    """Everything one batch yields, drained the way the pipeline drains it."""
    plan = list(queries) or [SearchQuery()]
    return [posting async for posting in source.search_batch(plan)]


def ids(postings: Sequence[RawPosting]) -> list[str]:
    """The external ids, in yield order."""
    return [posting.external_id for posting in postings]


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client with the dev cache off and the waiting removed."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    monkeypatch.setattr(settings, "source_credentials", {})
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


def bound[S: BaseSource](source: S, client: SourceClient) -> S:
    """A connector bound to the mocked client."""
    return source.bind(client.bind(source))


# ── declarations ──────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", KEYLESS_SLUGS)
def test_each_board_is_registered_keyless_and_declares_its_terms(slug: str) -> None:
    """Registered, needing no key, credited, at most one request a second, 6-24 h apart."""
    source = registry.get_source(slug)

    assert source.required_credentials == ()
    assert source.attribution
    assert source.rate_limit.requests_per_second <= 1.0
    assert timedelta(hours=6) <= source.min_interval <= timedelta(hours=24)
    assert source.daily_quota is None
    if source.access_mode is AccessMode.API:
        assert source.terms_url


def test_the_shared_helpers_read_what_boards_send() -> None:
    """Zero and blank salaries are "not stated"; naive dates are UTC; entities decode."""
    assert positive(0) is None
    assert positive("") is None
    assert positive("competitive") is None
    assert positive("85000") == 85000.0
    assert positive(True) is None
    assert parse_timestamp("2026-11-18 10:30:00") == datetime.fromisoformat(
        "2026-11-18T10:30:00+00:00"
    )
    assert parse_timestamp("Wed, 18 Nov 2026 14:00:00 +0000") == datetime.fromisoformat(
        "2026-11-18T14:00:00+00:00"
    )
    assert parse_timestamp(0) == datetime.fromisoformat("1970-01-01T00:00:00+00:00")
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None
    assert strip_tags("<p>R&amp;D <b>Python</b></p>") == "R&D Python"


# ── himalayas ─────────────────────────────────────────────────────────


async def test_himalayas_walks_the_pages_until_total_count(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Offset advances by what each page held, and ``totalCount`` ends the walk."""
    route = http.get(HIMALAYAS_URL).mock(
        side_effect=[
            httpx.Response(200, json=payload("himalayas_page1")),
            httpx.Response(200, json=payload("himalayas_page2")),
        ]
    )

    postings = await drain(bound(HimalayasSource(), client))

    assert [dict(call.request.url.params) for call in route.calls] == [
        {"limit": str(PAGE_SIZE), "offset": "0"},
        {"limit": str(PAGE_SIZE), "offset": "3"},
    ]
    # Four valid entries; the one with no guid is dropped, not raised on.
    assert len(postings) == 4
    assert all("Broken" not in posting.title for posting in postings)


async def test_himalayas_filters_by_keyword_here_and_derives_the_structured_fields(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The feed takes no query, so "python" is applied to title, categories and text."""
    http.get(HIMALAYAS_URL).mock(
        side_effect=[
            httpx.Response(200, json=payload("himalayas_page1")),
            httpx.Response(200, json=payload("himalayas_page2")),
        ]
    )

    postings = await drain(bound(HimalayasSource(), client), SearchQuery(keywords=("python",)))

    assert [posting.title for posting in postings] == [
        "Senior Python Backend Engineer",
        "Data Engineer (Python, Airflow)",
    ]
    first = postings[0]
    # The himalayas.app page, which is the link the terms ask for.
    assert first.url.startswith("https://himalayas.app/")
    assert first.company == "Northwind Labs"
    derived = first.raw["_derived"]
    assert derived["remote"] == RemoteType.FULL.value
    assert derived["location"] == "Germany, Poland"
    assert derived["salary_min"] == 90000
    assert derived["salary_currency"] == "USD"
    assert datetime.fromisoformat(derived["published_at"]).tzinfo is not None
    assert first.raw["applicationLink"] == "https://jobs.northwind.example/apply/123"


async def test_himalayas_stops_paginating_once_the_plan_is_satisfied(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The feed is consumed lazily; a limit of one needs one page."""
    route = http.get(HIMALAYAS_URL).mock(
        return_value=httpx.Response(200, json=payload("himalayas_page1"))
    )

    postings = await drain(bound(HimalayasSource(), client), SearchQuery(limit=1))

    assert len(postings) == 1
    assert route.call_count == 1


async def test_himalayas_without_a_jobs_list_fails_loudly(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """An envelope with no ``jobs`` is a changed API, not an empty market."""
    http.get(HIMALAYAS_URL).mock(return_value=httpx.Response(200, json={"offset": 0}))

    with pytest.raises(SourceError, match="jobs"):
        await drain(bound(HimalayasSource(), client))


# ── remoteok ──────────────────────────────────────────────────────────


async def test_remoteok_skips_the_legal_notice_and_the_malformed_entry(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The first array element is the vendor's terms, not a job."""
    route = http.get(REMOTEOK_URL).mock(return_value=httpx.Response(200, json=payload("remoteok")))

    postings = await drain(bound(RemoteOkSource(), client))

    assert route.call_count == 1
    assert ids(postings) == ["1001", "1002"]
    assert all("legal" not in posting.raw for posting in postings)


async def test_remoteok_normalises_company_url_and_salaries(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Trailing spaces trimmed, the remoteok.com page linked, zero salaries left out."""
    http.get(REMOTEOK_URL).mock(return_value=httpx.Response(200, json=payload("remoteok")))

    acme, brandly = await drain(bound(RemoteOkSource(), client))

    assert acme.company == "Acme"
    assert acme.url == "https://remoteok.com/remote-jobs/remote-senior-python-engineer-acme-1001"
    assert acme.raw["_derived"]["salary_min"] == 100000
    assert acme.raw["_derived"]["remote"] == RemoteType.FULL.value
    assert "salary_min" not in brandly.raw["_derived"]
    assert "salary_max" not in brandly.raw["_derived"]


async def test_remoteok_filters_by_keyword_against_tags_and_text(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """ "django" appears only in one description's markup-free text."""
    http.get(REMOTEOK_URL).mock(return_value=httpx.Response(200, json=payload("remoteok")))

    postings = await drain(bound(RemoteOkSource(), client), SearchQuery(keywords=("Django",)))

    assert ids(postings) == ["1001"]


async def test_remoteok_refuses_a_payload_that_is_not_an_array(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The documented shape is a bare array."""
    http.get(REMOTEOK_URL).mock(return_value=httpx.Response(200, json={"jobs": []}))

    with pytest.raises(SourceError, match="массивом"):
        await drain(bound(RemoteOkSource(), client))


def test_remoteok_credits_remote_ok_as_its_terms_require() -> None:
    """The legal notice makes the link back a condition of access."""
    assert RemoteOkSource.attribution is not None
    assert "Remote OK" in RemoteOkSource.attribution


# ── jobicy ────────────────────────────────────────────────────────────


async def test_jobicy_asks_once_with_only_a_count(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Tags and geo are not sent: the plan is filtered here, in one request."""
    route = http.get(JOBICY_URL).mock(return_value=httpx.Response(200, json=payload("jobicy")))

    postings = await drain(
        bound(JobicySource(), client),
        SearchQuery(keywords=("fastapi",)),
        SearchQuery(keywords=("zendesk",)),
    )

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {"count": str(FEED_COUNT)}
    assert ids(postings) == ["501", "502"]


async def test_jobicy_normalises_dates_and_annual_salaries(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A naive pubDate is read as UTC; the salary is annual in its stated currency."""
    http.get(JOBICY_URL).mock(return_value=httpx.Response(200, json=payload("jobicy")))

    postings = await drain(bound(JobicySource(), client))

    assert ids(postings) == ["501", "502"]
    derived = postings[0].raw["_derived"]
    assert datetime.fromisoformat(derived["published_at"]) == datetime.fromisoformat(
        "2026-11-18T10:30:00+00:00"
    )
    assert derived["salary_min"] == 60000
    assert derived["salary_currency"] == "EUR"
    assert derived["salary_period"] == "year"
    assert derived["location"] == "EMEA"
    assert "salary_period" not in postings[1].raw["_derived"]

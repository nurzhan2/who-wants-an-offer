"""The keyless remote boards: himalayas, remoteok, jobicy, weworkremotely, hn_hiring, freehire.

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
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import SourceError
from app.db.enums import RemoteType
from app.sources import registry
from app.sources._boards import parse_timestamp, positive, strip_tags
from app.sources.base import AccessMode, BaseSource, RawPosting, SearchQuery
from app.sources.freehire import BASE_URL_KEY, FreehireSource, endpoint, jobs_of
from app.sources.himalayas import API_URL as HIMALAYAS_URL
from app.sources.himalayas import PAGE_SIZE, HimalayasSource
from app.sources.hn_hiring import (
    ITEM_URL,
    SEARCH_URL,
    STATE_KEY,
    HNHiringSource,
    StoryHit,
    parse_header,
    pick_story,
    remoteness,
)
from app.sources.http import SourceClient
from app.sources.jobicy import API_URL as JOBICY_URL
from app.sources.jobicy import FEED_COUNT, JobicySource
from app.sources.remoteok import API_URL as REMOTEOK_URL
from app.sources.remoteok import RemoteOkSource
from app.sources.weworkremotely import FEED_URLS, WeWorkRemotelySource, parse_rss, split_title

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
KEYLESS_SLUGS = ("himalayas", "remoteok", "jobicy", "weworkremotely", "hn_hiring", "freehire")

#: An RSS channel with no items, for the category feeds a test does not care about.
EMPTY_RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'


def payload(name: str) -> Any:
    """One saved JSON response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def text_fixture(name: str) -> str:
    """One saved text response."""
    return (FIXTURES / name).read_text(encoding="utf-8")


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


class MemoryState:
    """The pipeline's ``source_state`` hooks, without a database."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        """What was stored under ``key``."""
        return self.rows.get(key)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Store a copy, as the database would."""
        self.rows[key] = json.loads(json.dumps(value))


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


def test_the_rss_board_is_a_crawl_source_so_robots_decides() -> None:
    """A website feed is not an API under contract."""
    assert WeWorkRemotelySource.access_mode is AccessMode.CRAWL


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


# ── weworkremotely ────────────────────────────────────────────────────


def mock_wwr(http: respx.MockRouter) -> dict[str, respx.Route]:
    """robots.txt, the main feed, the back-end feed, and empty other categories."""
    routes = {
        "robots": http.get("https://weworkremotely.com/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        )
    }
    bodies = {
        FEED_URLS[0]: text_fixture("weworkremotely_main.rss"),
        FEED_URLS[1]: text_fixture("weworkremotely_backend.rss"),
    }
    for url in FEED_URLS:
        routes[url] = http.get(url).mock(
            return_value=httpx.Response(200, text=bodies.get(url, EMPTY_RSS))
        )
    return routes


async def test_wwr_reads_every_feed_after_robots_and_dedups_across_them(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The backend posting is in two feeds and is yielded once; the linkless item is dropped."""
    routes = mock_wwr(http)

    postings = await drain(bound(WeWorkRemotelySource(), client))

    assert routes["robots"].called
    assert all(routes[url].call_count == 1 for url in FEED_URLS)
    assert [posting.title for posting in postings] == [
        "Senior Backend Engineer (Python)",
        "Account Executive",
        "Elixir Platform Engineer",
    ]


async def test_wwr_splits_company_from_role_and_keeps_the_html(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """``"Company: Role"``; the escaped description arrives as markup."""
    mock_wwr(http)

    acme, sellwell, elixir = await drain(bound(WeWorkRemotelySource(), client))

    assert acme.company == "Acme Cloud"
    assert acme.description is not None
    assert acme.description.startswith("<p>We are hiring")
    assert sellwell.description == "<p>Close deals with mid-market customers.</p>"
    assert elixir.company is None
    derived = acme.raw["_derived"]
    assert derived["location"] == "Anywhere in the World"
    assert derived["remote"] == RemoteType.FULL.value
    assert datetime.fromisoformat(derived["published_at"]) == datetime.fromisoformat(
        "2026-11-18T14:00:00+00:00"
    )


async def test_wwr_filters_by_keyword(client: SourceClient, http: respx.MockRouter) -> None:
    """ "python" matches the backend posting only, once, whichever feed carried it."""
    mock_wwr(http)

    postings = await drain(bound(WeWorkRemotelySource(), client), SearchQuery(keywords=("python",)))

    assert [posting.company for posting in postings] == ["Acme Cloud"]


async def test_wwr_is_refused_when_robots_disallows_it(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """CRAWL mode: robots.txt is asked first and its answer stands."""
    http.get("https://weworkremotely.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    feed = http.get(FEED_URLS[0]).mock(return_value=httpx.Response(200, text=EMPTY_RSS))

    with pytest.raises(SourceError, match="robots"):
        await drain(bound(WeWorkRemotelySource(), client))
    assert not feed.called


def test_rss_with_a_dtd_is_refused_before_parsing() -> None:
    """No DTD, no entity declarations, nothing to expand."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        "<rss><channel><item><title>&lol;</title></item></channel></rss>"
    )

    with pytest.raises(SourceError, match="DTD"):
        parse_rss(bomb)


def test_rss_that_does_not_parse_or_has_no_channel_is_refused() -> None:
    """A broken document is a failure with a reason, not an empty feed."""
    with pytest.raises(SourceError, match="XML"):
        parse_rss("<rss><channel>")
    with pytest.raises(SourceError, match="channel"):
        parse_rss("<rss></rss>")


def test_a_title_without_a_company_prefix_is_kept_whole() -> None:
    """Only ``"X: Y"`` with both halves is split."""
    assert split_title("Acme: Engineer") == ("Acme", "Engineer")
    assert split_title("Engineer") == (None, "Engineer")
    assert split_title(": Engineer") == (None, ": Engineer")


# ── hn_hiring: the heuristic ──────────────────────────────────────────


def test_the_header_is_company_role_location() -> None:
    """The thread's own convention, with entities decoded."""
    header = parse_header(
        "Acme Robotics | Senior Python Engineer | Remote (EU) | Full-time | &#x2F;careers"
        "<p>We build robots."
    )

    assert header is not None
    assert header.company == "Acme Robotics"
    assert header.title == "Senior Python Engineer"
    assert header.location == "Remote (EU)"
    assert header.remote is RemoteType.FULL


def test_the_role_is_found_wherever_it_sits_in_the_header() -> None:
    """Location second, role third: the role is the segment that names one."""
    header = parse_header("Ledgerly | New York, NY (ONSITE) | Backend Developer, Go | $200k")

    assert header is not None
    assert header.company == "Ledgerly"
    assert header.title == "Backend Developer, Go"
    assert header.location == "New York, NY (ONSITE)"
    assert header.remote is RemoteType.NO


def test_a_header_with_no_role_word_takes_the_second_segment() -> None:
    """Better a plausible title than none."""
    header = parse_header("Initech | TPS Reports | Austin")

    assert header is not None
    assert header.title == "TPS Reports"
    assert header.location is None
    assert header.remote is None


def test_a_comment_with_no_header_is_titled_by_its_first_line() -> None:
    """Prose is not a header: no company, the line cut to a title."""
    line = "We are a small team looking for a data engineer who likes Python " * 4
    header = parse_header(f"{line}<p>Email me.")

    assert header is not None
    assert header.company is None
    assert len(header.title) <= 120
    assert header.title.startswith("We are a small team")


def test_an_empty_comment_has_no_header() -> None:
    """Nothing to title a posting with."""
    assert parse_header("<p>") is None
    assert parse_header("") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Acme | Engineer | REMOTE", RemoteType.FULL),
        ("Acme | Engineer | Onsite or Remote", RemoteType.FULL),
        ("Acme | Engineer | Hybrid, Berlin", RemoteType.HYBRID),
        ("Acme | Engineer | Paris | No remote", RemoteType.NO),
        ("Acme | Engineer | Paris", None),
    ],
)
def test_remoteness_is_read_off_the_header_line(line: str, expected: RemoteType | None) -> None:
    """ "No remote" is not remote; silence is unknown, not onsite."""
    assert remoteness(line) is expected


def test_the_newest_who_is_hiring_story_is_picked_not_the_newest_story() -> None:
    """The same author opens «Who wants to be hired?» seconds later."""
    hits = [StoryHit.model_validate(hit) for hit in payload("hn_hiring_search")["hits"]]

    story = pick_story(hits)

    assert story is not None
    assert story.object_id == "46100000"
    assert pick_story([]) is None


# ── hn_hiring: the connector ──────────────────────────────────────────


def mock_hn(http: respx.MockRouter, thread: dict[str, Any] | None = None) -> respx.Route:
    """The story search and the thread item."""
    http.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload("hn_hiring_search")))
    return http.get(ITEM_URL.format(id="46100000")).mock(
        return_value=httpx.Response(200, json=thread or payload("hn_hiring_thread"))
    )


@pytest.fixture
def state() -> MemoryState:
    """A fresh, empty state store."""
    return MemoryState()


@pytest.fixture
def hn(client: SourceClient, state: MemoryState) -> HNHiringSource:
    """The connector with in-memory state."""
    source = HNHiringSource()
    return source.bind(client.bind(source)).with_state(state.load, state.save)


async def test_each_top_level_comment_is_one_posting(
    hn: HNHiringSource, http: respx.MockRouter
) -> None:
    """Replies are discussion and a deleted comment is nothing."""
    mock_hn(http)

    postings = await drain(hn)

    assert ids(postings) == ["46100101", "46100102", "46100104"]
    first = postings[0]
    assert first.url == "https://news.ycombinator.com/item?id=46100101"
    assert first.company == "Acme Robotics"
    assert first.title == "Senior Python Engineer"
    # The full comment HTML, for the later structuring step.
    assert first.description is not None
    assert "warehouse robots" in first.description
    assert "children" not in first.raw
    assert first.raw["story_id"] == 46100000
    assert first.raw["_derived"]["remote"] == RemoteType.FULL.value
    assert "published_at" in first.raw["_derived"]


async def test_hn_filters_by_keyword(hn: HNHiringSource, http: respx.MockRouter) -> None:
    """ "rust" occurs only in one comment's body, below its header line."""
    mock_hn(http)

    postings = await drain(hn, SearchQuery(keywords=("rust",)))

    assert ids(postings) == ["46100101"]


async def test_a_rerun_yields_only_comments_it_has_not_stored(
    hn: HNHiringSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """The ids written are remembered; the same thread then yields nothing."""
    mock_hn(http)

    first = await drain(hn)
    await hn.record_progress(len(first))
    second = await drain(hn)

    assert len(first) == 3
    assert second == []
    assert state.rows[STATE_KEY] == {
        "story_id": 46100000,
        "seen": [46100101, 46100102, 46100104],
    }


async def test_only_comments_the_pipeline_actually_wrote_are_marked_seen(
    hn: HNHiringSource, http: respx.MockRouter
) -> None:
    """A run that stored one of three must hand the other two over again."""
    mock_hn(http)

    await drain(hn)
    await hn.record_progress(1)
    again = await drain(hn)

    assert ids(again) == ["46100102", "46100104"]


async def test_a_new_months_thread_starts_the_record_over(
    hn: HNHiringSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """Ids from last month's thread say nothing about this one."""
    state.rows[STATE_KEY] = {"story_id": 45700000, "seen": [46100101, 46100102]}
    mock_hn(http)

    postings = await drain(hn)

    assert ids(postings) == ["46100101", "46100102", "46100104"]


async def test_an_unreadable_state_is_a_fresh_start(
    hn: HNHiringSource, http: respx.MockRouter, state: MemoryState
) -> None:
    """A record this class cannot parse costs a re-read, not a failure."""
    state.rows[STATE_KEY] = {"story_id": "not a number", "seen": "nope"}
    mock_hn(http)

    assert len(await drain(hn)) == 3


async def test_no_hiring_thread_is_a_failure_not_an_empty_month(
    hn: HNHiringSource, http: respx.MockRouter
) -> None:
    """Every month has one; not finding it means the search changed."""
    http.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"hits": [{"objectID": "1", "title": "Ask HN: Who wants to be hired?"}]}
        )
    )

    with pytest.raises(SourceError, match="Who is hiring"):
        await drain(hn)


# ── freehire ──────────────────────────────────────────────────────────


async def test_freehire_reads_the_default_endpoint_and_drops_the_malformed_entry(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Nested company and skill objects are read; the entry with no url is dropped."""
    route = http.get("https://freehire.me/api/jobs").mock(
        return_value=httpx.Response(200, json=payload("freehire"))
    )

    postings = await drain(bound(FreehireSource(), client))

    assert route.call_count == 1
    assert ids(postings) == ["9001", "9002"]
    steppe, remote_first = postings
    assert steppe.company == "Steppe Soft"
    assert steppe.raw["_derived"]["remote"] == RemoteType.NO.value
    assert steppe.raw["_derived"]["salary_currency"] == "KZT"
    assert remote_first.company == "Remote First"
    assert remote_first.url == "https://freehire.me/jobs/9002"
    assert remote_first.raw["_derived"]["remote"] == RemoteType.FULL.value
    assert "published_at" in remote_first.raw["_derived"]


async def test_freehire_filters_by_skill(client: SourceClient, http: respx.MockRouter) -> None:
    """Skills are part of what a keyword is matched against."""
    http.get("https://freehire.me/api/jobs").mock(
        return_value=httpx.Response(200, json=payload("freehire"))
    )

    postings = await drain(bound(FreehireSource(), client), SearchQuery(keywords=("typescript",)))

    assert ids(postings) == ["9002"]


async def test_freehire_base_url_comes_from_configuration(
    client: SourceClient, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted instance is one configuration value away, trailing slash and all."""
    monkeypatch.setattr(
        settings, "source_credentials", {BASE_URL_KEY: SecretStr("https://jobs.example.org/")}
    )
    route = http.get("https://jobs.example.org/api/jobs").mock(
        return_value=httpx.Response(200, json=payload("freehire")["jobs"])
    )

    postings = await drain(bound(FreehireSource(), client))

    assert route.called
    assert ids(postings) == ["9001", "9002"]


def test_freehire_refuses_a_base_url_that_is_not_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in configuration is named, not requested."""
    monkeypatch.setattr(settings, "source_credentials", {BASE_URL_KEY: SecretStr("freehire.me")})

    with pytest.raises(SourceError, match=BASE_URL_KEY):
        endpoint()


async def test_freehire_without_a_job_list_names_the_path_to_check(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The path is a guess until a live run; a wrong one must say so."""
    http.get("https://freehire.me/api/jobs").mock(
        return_value=httpx.Response(200, json={"version": "1"})
    )

    with pytest.raises(SourceError, match=r"freehire\.jobs_path"):
        await drain(bound(FreehireSource(), client))


def test_freehire_finds_the_list_under_any_documented_envelope_key() -> None:
    """A bare list, or one under jobs, data, results or items."""
    assert jobs_of([{"id": 1}, "noise"]) == [{"id": 1}]
    assert jobs_of({"data": [{"id": 2}]}) == [{"id": 2}]
    assert jobs_of({"items": [{"id": 3}]}) == [{"id": 3}]
    assert jobs_of({"count": 0}) is None
    assert jobs_of("nope") is None

"""The six ATS connectors, driven against hand-built responses.

**Every fixture here is reconstructed, not captured.** The container these
connectors were written in had its network closed, so each payload under
``fixtures/sources/ats`` was written by hand after the vendor's public
documentation and says so in its own ``_comment`` (or, for Lever's bare array,
in ``lever_board.README``). These tests therefore pin the connectors' rules —
identity, filtering, failure policy — against the documented shapes; they do
not prove those shapes are what the vendors send today. The first live run
re-records them.

Nothing here reaches the network: ``respx`` serves every request, and an
unmocked URL fails the test.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.exceptions import SourceError
from app.sources import registry
from app.sources.ats._common import (
    ATS_KEYS,
    BOARDS_FILE,
    AtsSource,
    CompanyBoard,
    Region,
    load_boards,
    load_boards_file,
    plain_text,
    says_remote,
)
from app.sources.ats.ashby import AshbySource
from app.sources.ats.greenhouse import GreenhouseSource
from app.sources.ats.lever import LeverSource
from app.sources.ats.recruitee import RecruiteeSource
from app.sources.ats.smartrecruiters import SmartRecruitersSource
from app.sources.ats.workable import WorkableSource
from app.sources.base import AccessMode, RawPosting, SearchQuery, SourceUnavailable
from app.sources.http import SourceClient

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "ats"

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{token}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"
WORKABLE_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}"
SR_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
RECRUITEE_URL = "https://{token}.recruitee.com/api/offers/"


def payload(name: str) -> Any:
    """One saved response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def board(slug: str = "exampleco", **overrides: Any) -> CompanyBoard:
    """A board entry as the YAML would give it."""
    fields: dict[str, Any] = {"slug": slug, "company": "Example Co", "region": "world"}
    fields.update(overrides)
    return CompanyBoard.model_validate(fields)


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``, so a token bucket costs no wall clock."""
    return None


async def drain(
    source: AtsSource, queries: Sequence[SearchQuery] | None = None
) -> list[RawPosting]:
    """Everything one run yields, the way the runner drains it."""
    plan = list(queries) if queries is not None else [SearchQuery()]
    return [posting async for posting in source.search_batch(plan)]


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client with the dev cache off and the waiting removed."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


def bound[S: AtsSource](cls: type[S], client: SourceClient, *boards: CompanyBoard) -> S:
    """A connector over the given boards, bound to the mocked client."""
    source = cls(boards=boards or (board(),))
    return source.bind(client.bind(source))


# ── normalisation per vendor ──────────────────────────────────────────


async def test_greenhouse_keeps_the_encoded_html_and_states_no_city(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Greenhouse's content is entity-encoded and stored exactly so.

    The malformed fourth entry is dropped; the other three become postings
    keyed by board token and job id, with the company from the YAML.
    """
    route = http.get(GREENHOUSE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    source = bound(GreenhouseSource, client)

    postings = await drain(source)

    assert [p.external_id for p in postings] == [
        "exampleco:4012345001",
        "exampleco:4012345002",
        "exampleco:4012345003",
    ]
    assert route.calls.last.request.url.params["content"] == "true"
    first = postings[0]
    assert first.company == "Example Co"
    assert first.url == "https://boards.greenhouse.io/exampleco/jobs/4012345001"
    assert first.description is not None and first.description.startswith("&lt;p&gt;")
    derived = first.raw["_derived"]
    assert derived["remote"] is True
    assert derived["location"] == "Remote - EMEA"
    assert "city" not in derived
    # An empty location name falls back to the office's own location line.
    assert postings[2].raw["_derived"]["location"] == "Amsterdam, Netherlands"
    assert postings[2].raw["_derived"]["remote"] is False


async def test_lever_joins_the_lists_into_the_description(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Requirements live in ``lists``; dropping them loses the skills."""
    route = http.get(LEVER_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("lever_board"))
    )
    source = bound(LeverSource, client)

    postings = await drain(source)

    assert route.calls.last.request.url.params["mode"] == "json"
    assert len(postings) == 2  # the third entry has no text and no hostedUrl
    backend = postings[0]
    assert backend.external_id == "exampleco:5b2f6c1e-0001-4a7e-9c11-aaaaaaaaaaaa"
    assert backend.description is not None
    assert "<div>We move money for millions of users.</div>" in backend.description
    assert "<h3>What you&#x27;ll need</h3>" in backend.description
    assert "<ul><li>Python or Go</li><li>PostgreSQL</li></ul>" in backend.description
    assert "equity &amp; a home-office budget" in backend.description
    assert backend.raw["_derived"]["remote"] is True
    assert "city" not in backend.raw["_derived"]
    assert postings[1].raw["_derived"] == {
        "location": "London",
        "remote": False,
        "board": "exampleco",
        "region": "world",
    }


async def test_ashby_states_a_city_and_skips_an_unlisted_posting(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Ashby's postal address is a structured city; isListed=false is not a job."""
    route = http.get(ASHBY_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("ashby_board"))
    )
    source = bound(AshbySource, client)

    postings = await drain(source)

    assert route.calls.last.request.url.params["includeCompensation"] == "true"
    assert [p.title for p in postings] == ["Senior Python Engineer", "Data Engineer"]
    almaty = postings[0]
    assert almaty.raw["_derived"]["city"] == "Almaty"
    assert almaty.raw["_derived"]["remote"] is False
    assert almaty.raw["compensation"]["summaryComponents"][0]["minValue"] == 90000
    remote = postings[1]
    assert remote.raw["_derived"]["remote"] is True
    assert "city" not in remote.raw["_derived"]


async def test_workable_reads_the_widget_endpoint_with_details(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The public widget endpoint, not the token-gated account API."""
    route = http.get(WORKABLE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("workable_account"))
    )
    source = bound(WorkableSource, client)

    postings = await drain(source)

    assert route.calls.last.request.url.params["details"] == "true"
    # The board token is lower-cased in the key; the vendor's id is not touched.
    assert [p.external_id for p in postings] == ["exampleco:A1B2C3D4E5", "exampleco:F6G7H8J9K0"]
    astana = postings[0]
    assert astana.raw["_derived"]["city"] == "Astana"
    assert astana.raw["_derived"]["location"] == "Astana, Kazakhstan"
    assert astana.raw["_derived"]["remote"] is True
    # No top-level city: the first entry of ``locations`` is the stated one.
    tbilisi = postings[1]
    assert tbilisi.raw["_derived"]["city"] == "Tbilisi"
    assert tbilisi.description is None


async def test_smartrecruiters_pages_through_and_fetches_the_detail(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Two pages for a stated total of four; the detail fills the body."""
    route = http.get(SR_URL.format(token="ExampleCo")).mock(
        side_effect=[
            httpx.Response(200, json=payload("smartrecruiters_page1")),
            httpx.Response(200, json=payload("smartrecruiters_page2")),
        ]
    )
    detail = http.get(f"{SR_URL.format(token='ExampleCo')}/744000100000001").mock(
        return_value=httpx.Response(200, json=payload("smartrecruiters_detail"))
    )
    source = bound(SmartRecruitersSource, client, board("ExampleCo"))

    postings = await drain(source)

    assert [call.request.url.params["offset"] for call in route.calls] == ["0", "100"]
    assert [p.external_id for p in postings] == [
        "exampleco:744000100000001",
        "exampleco:744000100000002",
        "exampleco:744000100000003",
    ]
    warsaw = postings[0]
    assert warsaw.url == "https://jobs.smartrecruiters.com/ExampleCo/744000100000001"
    assert warsaw.description is None
    assert warsaw.raw["_derived"]["city"] == "Warsaw"
    assert postings[2].raw["_derived"]["remote"] is True
    assert "city" not in postings[2].raw["_derived"]

    filled = await source.fetch_detail(warsaw)

    assert detail.call_count == 1
    assert filled.description is not None
    assert filled.description.index("Company Description") < filled.description.index(
        "Qualifications"
    )
    assert "<ul><li>Python</li><li>Django</li></ul>" in filled.description
    assert filled.raw["_derived"] == warsaw.raw["_derived"]


async def test_smartrecruiters_keeps_the_stub_when_the_detail_is_gone(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """One vanished ad must not fail the run; the stub still links to the job."""
    http.get(SR_URL.format(token="ExampleCo")).mock(
        side_effect=[
            httpx.Response(200, json=payload("smartrecruiters_page1")),
            httpx.Response(200, json=payload("smartrecruiters_page2")),
        ]
    )
    http.get(f"{SR_URL.format(token='ExampleCo')}/744000100000001").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    source = bound(SmartRecruitersSource, client, board("ExampleCo"))
    first = (await drain(source))[0]

    assert await source.fetch_detail(first) is first


async def test_recruitee_joins_requirements_and_states_a_city(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The board token is the subdomain; requirements join the description."""
    http.get(RECRUITEE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("recruitee_offers"))
    )
    source = bound(RecruiteeSource, client)

    postings = await drain(source)

    assert [p.external_id for p in postings] == ["exampleco:1500001", "exampleco:1500002"]
    riga = postings[0]
    assert riga.description == (
        "<p>High-load services in Go.</p>\n<ul><li>Go</li><li>gRPC</li><li>PostgreSQL</li></ul>"
    )
    assert riga.raw["_derived"]["city"] == "Riga"
    remote = postings[1]
    assert remote.raw["_derived"]["remote"] is True
    # An empty string is not a stated city.
    assert "city" not in remote.raw["_derived"]


async def test_recruitee_refuses_a_token_that_is_not_a_subdomain(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A dot in the token would point the request at another host entirely."""
    http.get(RECRUITEE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("recruitee_offers"))
    )
    source = bound(RecruiteeSource, client, board("evil.example"), board("exampleco"))

    postings = await drain(source)

    assert len(postings) == 2
    assert all(p.external_id.startswith("exampleco:") for p in postings)


# ── the walk ──────────────────────────────────────────────────────────


async def test_keywords_are_unioned_across_the_plan_and_matched_locally(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """One board read for two queries; a match on either keyword is kept.

    "kafka" occurs only inside Greenhouse's encoded content, so it proves the
    filter reads the decoded text rather than the entity soup.
    """
    route = http.get(GREENHOUSE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    source = bound(GreenhouseSource, client)

    postings = await drain(
        source,
        [SearchQuery(keywords=("kafka",)), SearchQuery(keywords=("terraform",))],
    )

    assert route.call_count == 1
    assert [p.title for p in postings] == [
        "Senior Backend Engineer (Python)",
        "Site Reliability Engineer",
    ]


async def test_a_phrase_matches_by_all_of_its_words(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """«Python Engineer» finds «Senior Python Engineer», not «Data Engineer»."""
    http.get(ASHBY_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("ashby_board"))
    )
    source = bound(AshbySource, client)

    postings = await drain(source, [SearchQuery(keywords=("Python Engineer",))])

    assert [p.title for p in postings] == ["Senior Python Engineer"]


async def test_a_board_answering_404_is_skipped_and_the_walk_goes_on(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """An employer that left the vendor costs its own postings, nobody else's."""
    gone = http.get(GREENHOUSE_URL.format(token="movedaway")).mock(
        return_value=httpx.Response(404, json={"status": 404, "error": "Not found"})
    )
    http.get(GREENHOUSE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    source = bound(GreenhouseSource, client, board("movedaway"), board("exampleco"))

    postings = await drain(source)

    assert gone.call_count == 1  # a 404 is not retried
    assert len(postings) == 3


async def test_every_board_missing_is_an_empty_run_not_a_failure(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """404 everywhere means an out-of-date list, which the log already says."""
    http.get(LEVER_URL.format(token="exampleco")).mock(return_value=httpx.Response(404))
    source = bound(LeverSource, client)

    assert await drain(source) == []


async def test_every_board_failing_is_the_sources_failure(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """No board answered and one broke: that is not a quiet day on the market."""
    http.get(LEVER_URL.format(token="exampleco")).mock(return_value=httpx.Response(403))
    source = bound(LeverSource, client)

    with pytest.raises(SourceError, match="не ответила ни одна доска"):
        await drain(source)


async def test_one_broken_board_does_not_cost_the_others(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A changed envelope on one board is that board's problem."""
    http.get(ASHBY_URL.format(token="broken")).mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    http.get(ASHBY_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("ashby_board"))
    )
    source = bound(AshbySource, client, board("broken"), board("exampleco"))

    assert len(await drain(source)) == 2


async def test_being_told_to_slow_down_stops_the_walk(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The next board is on the same host that just asked us to wait."""
    http.get(GREENHOUSE_URL.format(token="first")).mock(
        return_value=httpx.Response(429, headers={"retry-after": "3600"})
    )
    second = http.get(GREENHOUSE_URL.format(token="second")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    source = bound(GreenhouseSource, client, board("first"), board("second"))

    with pytest.raises(SourceError):
        await drain(source)
    assert second.call_count == 0


async def test_the_largest_limit_in_the_plan_caps_the_whole_walk(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A later board is not read once the limit is spent."""
    http.get(GREENHOUSE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    later = http.get(GREENHOUSE_URL.format(token="later")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    source = bound(GreenhouseSource, client, board("exampleco"), board("later"))

    postings = await drain(source, [SearchQuery(limit=1), SearchQuery(limit=2)])

    assert len(postings) == 2
    assert later.call_count == 0


async def test_an_empty_plan_asks_nothing(client: SourceClient, http: respx.MockRouter) -> None:
    """No queries, no requests."""
    route = http.get(GREENHOUSE_URL.format(token="exampleco"))
    source = bound(GreenhouseSource, client)

    assert await drain(source, []) == []
    assert route.call_count == 0


async def test_a_single_search_is_the_same_walk(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """``search`` exists for the contract and delegates to the batch walk."""
    http.get(RECRUITEE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("recruitee_offers"))
    )
    source = bound(RecruiteeSource, client)

    found = [p async for p in source.search(SearchQuery(keywords=("grpc",)))]

    assert [p.title for p in found] == ["Golang Developer"]


async def test_external_ids_stay_unique_across_boards_with_the_same_job_ids(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Two boards serving the same vendor ids must not collapse into one row.

    Vendor job ids are unique per board at best; the board-token prefix is what
    keeps ``(source, external_id)`` a key across boards.
    """
    for token in ("alpha", "Beta"):
        http.get(GREENHOUSE_URL.format(token=token)).mock(
            return_value=httpx.Response(200, json=payload("greenhouse_board"))
        )
    source = bound(GreenhouseSource, client, board("alpha"), board("Beta"))

    postings = await drain(source)

    identifiers = [p.external_id for p in postings]
    assert len(identifiers) == 6
    assert len(set(identifiers)) == 6
    assert {i.split(":")[0] for i in identifiers} == {"alpha", "beta"}
    assert all(len(i) <= 200 for i in identifiers)


async def test_an_identifier_too_long_for_its_column_is_dropped_not_cut(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A truncated id is a different key; the posting is skipped instead."""
    body = payload("ashby_board")
    body["jobs"][0]["id"] = "x" * 250
    http.get(ASHBY_URL.format(token="exampleco")).mock(return_value=httpx.Response(200, json=body))
    source = bound(AshbySource, client)

    assert [p.title for p in await drain(source)] == ["Data Engineer"]


# ── the board list ────────────────────────────────────────────────────


def test_the_shipped_board_list_loads_and_is_marked_unverified() -> None:
    """Seeded with no network: every entry must say it has not been checked."""
    boards = load_boards_file()

    for ats in ATS_KEYS:
        listed = boards.for_ats(ats)
        assert listed, f"{ats} has no boards in {BOARDS_FILE.name}"
        assert all(entry.verified is False for entry in listed)
    assert any(entry.region is Region.KZ for ats in ATS_KEYS for entry in boards.for_ats(ats))


def test_a_board_listed_twice_is_refused(tmp_path: Path) -> None:
    """Twice-listed is twice-fetched, against a rate we promised to keep."""
    path = tmp_path / "boards.yaml"
    path.write_text(
        "lever:\n"
        "  - {slug: acme, company: Acme, region: world}\n"
        "  - {slug: ACME, company: Acme again, region: world}\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceError, match="не описывает"):
        load_boards("lever", path)


def test_a_misspelt_vendor_key_is_refused(tmp_path: Path) -> None:
    """``grenhouse:`` would otherwise be a list nobody ever reads."""
    path = tmp_path / "boards.yaml"
    path.write_text("grenhouse:\n  - {slug: acme, company: Acme, region: world}\n")

    with pytest.raises(SourceError):
        load_boards_file(path)


def test_an_empty_vendor_section_reads_as_no_boards(tmp_path: Path) -> None:
    """``workable:`` with nothing under it is YAML's null, and means none."""
    path = tmp_path / "boards.yaml"
    path.write_text("workable:\n", encoding="utf-8")

    assert load_boards("workable", path) == ()


def test_an_unreadable_board_list_is_a_source_error(tmp_path: Path) -> None:
    """A missing file fails loudly, naming the file."""
    with pytest.raises(SourceError, match="не читается"):
        load_boards("lever", tmp_path / "absent.yaml")


def test_a_connector_with_no_boards_is_off_with_a_reason() -> None:
    """Nothing to walk is configuration, not a failure."""
    reason = LeverSource(boards=()).unavailable()

    assert reason is not None
    assert reason.code is SourceUnavailable.DISABLED_BY_CONFIG


def test_the_preview_names_how_many_boards_it_walks() -> None:
    """The dashboard says what the next run will read, without reading it."""
    source = GreenhouseSource(boards=(board("a"), board("b")))

    preview = source.preview_search([SearchQuery(keywords=("python",))], [])

    assert preview.terms == ["python"]
    assert preview.note is not None and "2" in preview.note


# ── the verification probe ────────────────────────────────────────────


async def test_the_probe_reports_a_live_board_and_a_missing_one(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """What ``scripts/verify_ats_boards.py`` prints, one request per board."""
    http.get(GREENHOUSE_URL.format(token="exampleco")).mock(
        return_value=httpx.Response(200, json=payload("greenhouse_board"))
    )
    http.get(GREENHOUSE_URL.format(token="gone")).mock(return_value=httpx.Response(404))
    source = bound(GreenhouseSource, client)

    live = await source.probe_board(board("exampleco"))
    missing = await source.probe_board(board("gone"))

    assert (live.status, live.jobs, live.entries, live.stated_total) == ("ok", 3, 4, 4)
    assert (missing.status, missing.http_status) == ("missing", 404)


async def test_the_probe_reads_one_smartrecruiters_page_only(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The script promises one request per board, pagination or not."""
    route = http.get(SR_URL.format(token="ExampleCo")).mock(
        return_value=httpx.Response(200, json=payload("smartrecruiters_page1"))
    )
    source = bound(SmartRecruitersSource, client, board("ExampleCo"))

    probe = await source.probe_board(board("ExampleCo"))

    assert route.call_count == 1
    assert (probe.status, probe.jobs, probe.stated_total) == ("ok", 2, 4)


# ── registration ──────────────────────────────────────────────────────


def test_all_six_are_registered_as_api_sources_with_terms() -> None:
    """Discovered through the sub-package, each with its terms declared."""
    registered = {source.slug: source for source in registry.all_sources()}

    assert registry.import_errors() == {}
    for ats in ATS_KEYS:
        source = registered[ats]
        assert source.access_mode is AccessMode.API
        assert source.terms_url and source.terms_url.startswith("https://")
        summary = [line for line in (type(source).__doc__ or "").splitlines() if line.strip()]
        assert len(summary) >= registry.MIN_TERMS_SUMMARY_LINES
        assert source.daily_quota is None
        assert source.min_interval.total_seconds() >= 6 * 3600
        assert source.rate_limit.requests_per_second <= 1.0


def test_the_shared_module_registers_nothing() -> None:
    """``_common`` holds the base class, which has no slug of its own."""
    slugs = {source.slug for source in registry.all_sources()}

    assert not any(slug.startswith("_") or slug == "ats" for slug in slugs)


# ── helpers ───────────────────────────────────────────────────────────


def test_remote_is_read_from_the_words_not_from_a_substring() -> None:
    """«Remoteville» is a place; «Удалённо» is a remote job."""
    assert says_remote("Remote - EMEA")
    assert says_remote("Удалённо")
    assert not says_remote("Remoteville, CA")
    assert not says_remote(None)


def test_plain_text_decodes_before_it_strips() -> None:
    """Greenhouse's ``&lt;p&gt;`` has to become a tag before it can be removed."""
    assert plain_text("&lt;p&gt;Kafka &amp;amp; Go&lt;/p&gt;").split() == ["Kafka", "&", "Go"]

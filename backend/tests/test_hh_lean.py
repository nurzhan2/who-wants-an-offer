"""The hh crawl that buys only what can matter: catalogue only, and nothing stale.

The owner's complaint, 23 Nov 2026: analysing thousands of junk vacancies is not
the way, especially since a new CV means recomputing everything. The junk had
one address — the second half of the walk, "the rest of the corpus newest
first", which a date-ordered walk of Almaty once filled with 294 postings and no
developer among them. These tests hold the two switches that close it:

* ``crawl.catalogue_only`` — only ids a profession catalogue page named are
  bought, and the entries it skips are NOT recorded as covered, so a later
  profile (or the switch turned off) can still reach them;
* ``crawl.max_age_days`` — a sitemap entry hh has not touched for that long is
  never fetched, and is not recorded either;

and the third, older rule they lean on: an entry already covered at the same
``lastmod`` — stored and unchanged — is not bought again.

Every page here is synthetic and minimal; ``test_sources_hh.py`` owns the real
captures.
"""

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import yaml  # type: ignore[import-untyped]
from structlog.testing import capture_logs
from test_hh_catalog import (
    API_ROBOTS_URL,
    CATALOG0_URL,
    DIRECTORY,
    HH_ROBOTS,
    HOST,
    INDEX_URL,
    KEYWORDS,
    ROBOTS_URL,
    VACANCY0_URL,
    StateStore,
    catalog_page,
    catalog_sitemap,
    vacancy_page,
)

from app.core.config import settings
from app.sources.base import RawPosting, SearchQuery
from app.sources.hh import (
    CATALOG_HEAD_PAGES,
    ROLES_URL,
    SITES_FILE,
    CatalogPlan,
    CrawlSettings,
    HHSource,
    load_crawl,
    pages_this_run,
)
from app.sources.http import SourceClient

pytestmark = pytest.mark.unit

#: Relative to the real clock, because freshness is: the filter compares a
#: sitemap date with now, and a fixture dated in September would be stale.
NOW = datetime.now(UTC)


async def _instant(_seconds: float) -> None:
    return None


def sitemap(entries: Sequence[tuple[str, datetime]]) -> str:
    """A vacancy sitemap with an explicit date per entry."""
    body = "".join(
        f"<url><loc>https://{HOST}/vacancy/{vacancy_id}</loc>"
        f"<lastmod>{when.isoformat()}</lastmod></url>"
        for vacancy_id, when in entries
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def fresh(ids: Sequence[str]) -> list[tuple[str, datetime]]:
    """Entries touched within the last hours, newest first as written."""
    return [(vacancy_id, NOW - timedelta(hours=index + 1)) for index, vacancy_id in enumerate(ids)]


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=HH_ROBOTS))
        router.get(API_ROBOTS_URL).mock(return_value=httpx.Response(404))
        router.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<sitemap><loc>{VACANCY0_URL}</loc></sitemap>"
                    f"<sitemap><loc>{CATALOG0_URL}</loc></sitemap>"
                    "</sitemapindex>"
                ),
            )
        )
        router.get(ROLES_URL).mock(return_value=httpx.Response(200, json=DIRECTORY))
        yield router


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


def connector(
    client: SourceClient, store: StateStore, monkeypatch: pytest.MonkeyPatch, **crawl: Any
) -> HHSource:
    """A connector whose ``crawl:`` block is exactly ``crawl``, and nothing shipped."""
    monkeypatch.setattr("app.sources.hh.load_crawl", lambda *_, **__: CrawlSettings(**crawl))
    source = HHSource()
    return source.bind(client.bind(source)).with_state(store.load, store.save)


def serve(
    http: respx.MockRouter,
    *,
    entries: Sequence[tuple[str, datetime]],
    catalog: dict[str, Sequence[str]],
) -> dict[str, respx.Route]:
    """The sitemap, the catalogue and a page per entry; the page routes returned."""
    http.get(VACANCY0_URL).mock(return_value=httpx.Response(200, text=sitemap(entries)))
    http.get(CATALOG0_URL).mock(
        return_value=httpx.Response(200, text=catalog_sitemap(list(catalog)))
    )
    for slug, ids in catalog.items():
        http.get(f"https://{HOST}/vacancies/{slug}").mock(
            return_value=httpx.Response(200, text=catalog_page(ids))
        )
    return {
        vacancy_id: http.get(f"https://{HOST}/vacancy/{vacancy_id}").mock(
            return_value=httpx.Response(200, text=vacancy_page(vacancy_id))
        )
        for vacancy_id, _ in entries
    }


async def collect(source: HHSource) -> list[RawPosting]:
    """One walk, confirmed at the end the way the pipeline confirms it."""
    collected = [posting async for posting in source.search_batch([SearchQuery(keywords=KEYWORDS)])]
    await source.record_progress(len(collected))
    return collected


def ids(postings: Sequence[RawPosting]) -> list[str]:
    return [posting.external_id for posting in postings]


# -- catalogue only ------------------------------------------------------


async def test_catalogue_only_never_buys_a_page_the_catalogue_did_not_name(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corpus half is gone: three newer postings in the sitemap, none bought."""
    routes = serve(
        http,
        entries=fresh(["100", "101", "102", "103"]),
        catalog={"programmist": ["103"]},
    )
    hh = connector(client, store, monkeypatch, catalogue_only=True)

    with capture_logs() as logs:
        postings = await collect(hh)

    assert ids(postings) == ["103"]
    assert [routes[key].call_count for key in ("100", "101", "102")] == [0, 0, 0]
    planned = next(line for line in logs if line["event"] == "sources.hh.role_pass_planned")
    assert planned["catalog_ids"] == 1
    assert planned["due_now"] == 1
    assert planned["corpus_due"] == 3
    # The three the old walk would have bought on this run's budget.
    assert planned["junk_avoided"] == 3
    finished = next(line for line in logs if line["event"] == "sources.hh.site_finished")
    assert finished["fetched"] == 1
    assert finished["junk_avoided"] == 3


async def test_with_the_switch_off_the_corpus_still_follows_the_catalogue(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old walk is still there for whoever turns the switch off."""
    serve(http, entries=fresh(["100", "101", "102"]), catalog={"programmist": ["102"]})
    hh = connector(client, store, monkeypatch, catalogue_only=False)

    assert ids(await collect(hh)) == ["102", "100", "101"]


async def test_what_catalogue_only_skipped_is_not_recorded_as_done(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping is not a filter that sticks: a later run can still reach them.

    The walk records what it covered, so a skipped entry recorded as covered
    would be invisible to every future profile. A run with the switch off after
    a catalogue-only one buys exactly the entries the first one left alone.
    """
    entries = fresh(["100", "101", "102"])
    serve(http, entries=entries, catalog={"programmist": ["101"]})
    assert ids(await collect(connector(client, store, monkeypatch, catalogue_only=True))) == ["101"]

    later = connector(client, store, monkeypatch, catalogue_only=False)
    assert sorted(ids(await collect(later))) == ["100", "102"]


async def test_a_catalogue_id_already_stored_unchanged_is_not_bought_twice(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head pages are re-read every run; their postings are not."""
    routes = serve(http, entries=fresh(["100", "101"]), catalog={"programmist": ["100", "101"]})
    assert sorted(
        ids(await collect(connector(client, store, monkeypatch, catalogue_only=True)))
    ) == [
        "100",
        "101",
    ]

    with capture_logs() as logs:
        second = await collect(connector(client, store, monkeypatch, catalogue_only=True))

    assert second == []
    assert [routes[key].call_count for key in ("100", "101")] == [1, 1]
    planned = next(line for line in logs if line["event"] == "sources.hh.role_pass_planned")
    assert planned["known_skipped"] == 2
    assert planned["due_now"] == 0


async def test_a_posting_hh_renewed_is_bought_again(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged means the same ``lastmod``; a renewal moves it, and is new work."""
    route = serve(
        http, entries=[("100", NOW - timedelta(days=2))], catalog={"programmist": ["100"]}
    )["100"]
    await collect(connector(client, store, monkeypatch, catalogue_only=True))

    serve(http, entries=[("100", NOW - timedelta(hours=1))], catalog={"programmist": ["100"]})
    await collect(connector(client, store, monkeypatch, catalogue_only=True))

    assert route.call_count == 2


# -- freshness before fetching -----------------------------------------------


async def test_a_stale_sitemap_entry_is_never_fetched(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three weeks without an edit or a renewal: no page is bought for it."""
    routes = serve(
        http,
        entries=[
            ("100", NOW - timedelta(days=1)),
            ("101", NOW - timedelta(days=30)),
            ("102", NOW - timedelta(days=90)),
        ],
        catalog={"programmist": ["100", "101", "102"]},
    )
    hh = connector(client, store, monkeypatch, catalogue_only=True, max_age_days=21)

    with capture_logs() as logs:
        postings = await collect(hh)

    assert ids(postings) == ["100"]
    assert routes["101"].call_count == routes["102"].call_count == 0
    planned = next(line for line in logs if line["event"] == "sources.hh.role_pass_planned")
    assert planned["stale_skipped"] == 2
    finished = next(line for line in logs if line["event"] == "sources.hh.site_finished")
    assert finished["stale_skipped"] == 2


async def test_the_freshness_filter_also_holds_for_the_old_walk(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both switches are independent; the age filter needs no catalogue."""
    serve(
        http,
        entries=[("100", NOW - timedelta(days=1)), ("101", NOW - timedelta(days=40))],
        catalog={"programmist": []},
    )
    hh = connector(client, store, monkeypatch, max_age_days=21)

    assert ids(await collect(hh)) == ["100"]


async def test_a_stale_entry_is_not_recorded_as_fetched(
    client: SourceClient,
    http: respx.MockRouter,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning the filter off gets it back, because nothing claimed it was read."""
    serve(
        http,
        entries=[("100", NOW - timedelta(days=1)), ("101", NOW - timedelta(days=40))],
        catalog={"programmist": []},
    )
    await collect(connector(client, store, monkeypatch, max_age_days=21))

    assert ids(await collect(connector(client, store, monkeypatch))) == ["101"]


# -- breadth and the shipped file ------------------------------------------


def test_a_wider_run_rereads_a_wider_head_and_still_rotates() -> None:
    """Thirty pages: the fifteen nearest the profile every run, fifteen rotating."""
    slugs = tuple(f"s{index:03d}" for index in range(100))
    plan = CatalogPlan(resolved_at=NOW, slugs=slugs)

    opened, offset = pages_this_run(plan, 30)
    again, _ = pages_this_run(plan.model_copy(update={"offset": offset}), 30)

    assert len(opened) == 30
    assert opened[:15] == again[:15] == slugs[:15]
    assert set(opened[15:]).isdisjoint(again[15:])
    # The default breadth is what it always was.
    assert pages_this_run(plan)[0][:CATALOG_HEAD_PAGES] == slugs[:CATALOG_HEAD_PAGES]


def test_the_shipped_server_and_night_blocks_are_catalogue_only_and_fresh() -> None:
    """What the server and the night actually run, read from the file they read."""
    for profile in ("crawl", "night"):
        config = load_crawl(SITES_FILE, profile=profile)
        assert config.catalogue_only is True, profile
        assert config.max_age_days == 21, profile
        assert config.catalog_pages is not None and config.catalog_pages >= 30, profile
    raw = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    assert "rate" not in str(raw["crawl"]).lower()

"""The keyed aggregators — adzuna, jooble, careerjet — prepared up to the key.

No key exists for any of them, so two things are proven here and nothing more:

* without its credentials a source reports ``missing_credentials`` — inactive,
  not failing (docs/SOURCES.md rule 8);
* with credentials set (fake values, through ``settings.source_credentials``
  exactly as the jsearch tests do it) the request carries them where the
  vendor's documentation says it must, and a response shaped after that
  documentation normalises into postings.

The fixtures are **reconstructed from public documentation**, not captured, and
each says so in its ``_comment``. Re-record them on the first live run.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import SourceError
from app.db.enums import RemoteType
from app.schemas.crawl import SearchUse
from app.sources import registry
from app.sources._boards import KeyedSearch, keyed_searches, stable_id
from app.sources.adzuna import (
    DAILY_ALLOWANCE as ADZUNA_DAILY,
)
from app.sources.adzuna import (
    MONTHLY_ALLOWANCE,
    AdzunaSource,
)
from app.sources.adzuna import (
    REQUESTS_PER_RUN as ADZUNA_PER_RUN,
)
from app.sources.base import BaseSource, RawPosting, SearchQuery, SourceUnavailable
from app.sources.careerjet import API_URL as CAREERJET_URL
from app.sources.careerjet import CareerjetSource
from app.sources.http import SourceClient
from app.sources.jooble import DAILY_ALLOWANCE as JOOBLE_DAILY
from app.sources.jooble import REQUESTS_PER_RUN as JOOBLE_PER_RUN
from app.sources.jooble import JoobleSource
from app.sources.jsearch import load_countries

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

#: Fake values. Shaped like the real thing only where the shape matters: the
#: jooble key is a GUID because that is the shape ``http.redact`` hides.
ADZUNA_ID = "test-app-id"
ADZUNA_KEY = "test-app-key"
JOOBLE_KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"
CAREERJET_AFFID = "test-affid"

ADZUNA_DE = "https://api.adzuna.com/v1/api/jobs/de/search/1"
JOOBLE_URL = f"https://jooble.org/api/{JOOBLE_KEY}"

KEYED: tuple[type[BaseSource], ...] = (AdzunaSource, JoobleSource, CareerjetSource)


def payload(name: str) -> dict[str, Any]:
    """One saved response, re-read per call so no test can mutate another's."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


async def _instant(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep``."""
    return None


async def drain(source: BaseSource, *queries: SearchQuery) -> list[RawPosting]:
    """Everything one batch yields."""
    return [posting async for posting in source.search_batch(list(queries))]


def credentials() -> dict[str, SecretStr]:
    """Every keyed source's credential, set to a fake value."""
    return {
        "adzuna.app_id": SecretStr(ADZUNA_ID),
        "adzuna.app_key": SecretStr(ADZUNA_KEY),
        "jooble.api_key": SecretStr(JOOBLE_KEY),
        "careerjet.affid": SecretStr(CAREERJET_AFFID),
    }


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    """Every outbound request, intercepted before it leaves the process."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SourceClient]:
    """The shared client, cache off, waiting removed, every key set to a fake."""
    monkeypatch.setattr(settings, "http_cache_dir", None)
    monkeypatch.setattr(settings, "source_credentials", credentials())
    source_client = SourceClient(sleep=_instant)
    try:
        yield source_client
    finally:
        await source_client.aclose()


def bound[S: BaseSource](source: S, client: SourceClient) -> S:
    """A connector bound to the mocked client."""
    return source.bind(client.bind(source))


# ── without a key ─────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", KEYED, ids=lambda cls: cls.slug)
def test_without_credentials_the_source_is_inactive_not_failing(
    cls: type[BaseSource], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 8: a missing key means inactive, with the missing names listed."""
    monkeypatch.setattr(settings, "source_credentials", {})
    source = registry.get_source(cls.slug)

    reason = registry.disabled_reason(source)

    assert reason is not None
    assert reason.code is SourceUnavailable.MISSING_CREDENTIALS
    assert reason.missing_credentials == cls.required_credentials
    assert source not in registry.get_enabled_sources()


def test_half_of_adzunas_key_pair_is_still_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves are required; the missing one is named."""
    monkeypatch.setattr(settings, "source_credentials", {"adzuna.app_id": SecretStr("x")})

    assert AdzunaSource().missing_credentials() == ("adzuna.app_key",)


@pytest.mark.parametrize("cls", KEYED, ids=lambda cls: cls.slug)
def test_with_credentials_the_source_is_available(
    cls: type[BaseSource], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured means runnable, and the declared names are the ones checked."""
    monkeypatch.setattr(settings, "source_credentials", credentials())

    assert cls().unavailable() is None


@pytest.mark.parametrize("cls", KEYED, ids=lambda cls: cls.slug)
def test_each_keyed_source_declares_terms_attribution_and_a_ceiling(
    cls: type[BaseSource],
) -> None:
    """An API source summarises its terms; a run cannot outspend the day."""
    source = registry.get_source(cls.slug)

    assert source.terms_url
    assert source.attribution
    assert source.requires_auth
    assert source.rate_limit.requests_per_second <= 1.0
    assert timedelta(hours=6) <= source.min_interval <= timedelta(hours=24)


def test_adzunas_quota_fits_the_documented_month() -> None:
    """2,500 a month binds hardest; two runs a day stay inside the daily share."""
    runs_per_day = int(timedelta(days=1) / AdzunaSource.min_interval)

    assert AdzunaSource.daily_quota == ADZUNA_DAILY
    assert ADZUNA_DAILY * 31 <= MONTHLY_ALLOWANCE
    assert ADZUNA_DAILY <= 250
    assert ADZUNA_PER_RUN * runs_per_day <= ADZUNA_DAILY


def test_joobles_quota_is_declared_and_respected_per_run() -> None:
    """Metered by key: the project's own ceiling, until the issued number is known."""
    runs_per_day = int(timedelta(days=1) / JoobleSource.min_interval)

    assert JoobleSource.daily_quota == JOOBLE_DAILY
    assert JOOBLE_PER_RUN * runs_per_day <= JOOBLE_DAILY


# ── the plan ──────────────────────────────────────────────────────────


def test_keywords_are_joined_and_places_resolve_to_countries() -> None:
    """One request text per (words, place); the country from the shared table."""
    countries = load_countries()
    searches = keyed_searches(
        [
            SearchQuery(keywords=("Python", "Django"), area="Алматы"),
            SearchQuery(keywords=("Python", "Django"), area="Алматы"),
            SearchQuery(keywords=("Python",), remote=RemoteType.FULL),
            SearchQuery(keywords=()),
        ],
        countries,
    )

    assert searches == (
        KeyedSearch(keywords="Python Django", area="Алматы", country="kz"),
        KeyedSearch(keywords="Python remote", area=None, country=None),
    )


# ── adzuna ────────────────────────────────────────────────────────────


async def test_adzuna_sends_the_key_pair_and_the_query_in_the_documented_places(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Country in the path, ``app_id``/``app_key``/``what``/``where`` in the query."""
    route = http.get(ADZUNA_DE).mock(return_value=httpx.Response(200, json=payload("adzuna_de")))

    await drain(bound(AdzunaSource(), client), SearchQuery(keywords=("Python",), area="Berlin"))

    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    assert params["app_id"] == ADZUNA_ID
    assert params["app_key"] == ADZUNA_KEY
    assert params["what"] == "Python"
    assert params["where"] == "Berlin"


async def test_adzuna_normalises_results_and_ignores_predicted_salaries(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A predicted salary is Adzuna's guess, not the employer's offer."""
    http.get(ADZUNA_DE).mock(return_value=httpx.Response(200, json=payload("adzuna_de")))

    postings = await drain(
        bound(AdzunaSource(), client), SearchQuery(keywords=("Python",), area="Berlin")
    )

    assert [posting.external_id for posting in postings] == ["4711000001", "4711000002"]
    stated, predicted = postings
    assert stated.company == "Berlin Bytes GmbH"
    assert stated.url.startswith("https://www.adzuna.de/land/ad/4711000001")
    assert stated.raw["_derived"]["salary_min"] == 60000
    assert stated.raw["_derived"]["location"] == "Berlin, Deutschland"
    assert "salary_min" not in predicted.raw["_derived"]
    assert all(ADZUNA_KEY not in json.dumps(posting.raw) for posting in postings)


async def test_adzuna_spends_nothing_on_a_country_it_does_not_cover(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Kazakhstan is not on Adzuna's list, and a remote slot has no country at all."""
    route = http.get(url__startswith="https://api.adzuna.com/").mock(
        return_value=httpx.Response(200, json=payload("adzuna_de"))
    )
    source = bound(AdzunaSource(), client)
    queries = [
        SearchQuery(keywords=("Python",), area="Алматы"),
        SearchQuery(keywords=("Python",), remote=RemoteType.FULL),
    ]

    postings = await drain(source, *queries)
    preview = source.preview_search(queries, [])

    assert postings == []
    assert not route.called
    assert preview.use is SearchUse.QUERY
    assert preview.terms == []
    assert preview.note is not None
    assert "2" in preview.note


async def test_adzuna_never_sends_more_than_its_share_of_the_day(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A plan longer than the run's ceiling is cut at the ceiling."""
    route = http.get(ADZUNA_DE).mock(return_value=httpx.Response(200, json={"results": []}))
    queries = [
        SearchQuery(keywords=(f"skill{index}",), area="Berlin")
        for index in range(ADZUNA_PER_RUN + 5)
    ]

    await drain(bound(AdzunaSource(), client), *queries)

    assert route.call_count == ADZUNA_PER_RUN


async def test_adzuna_without_results_fails_loudly(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """An envelope with no ``results`` is a changed API, not an empty market."""
    http.get(ADZUNA_DE).mock(return_value=httpx.Response(200, json={"exception": "AUTH_FAIL"}))

    with pytest.raises(SourceError, match="results"):
        await drain(bound(AdzunaSource(), client), SearchQuery(keywords=("x",), area="Berlin"))


# ── jooble ────────────────────────────────────────────────────────────


async def test_jooble_posts_the_query_with_the_key_in_the_path(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """``POST /api/{key}`` with keywords, location and page in a JSON body."""
    route = http.post(JOOBLE_URL).mock(return_value=httpx.Response(200, json=payload("jooble")))

    postings = await drain(
        bound(JoobleSource(), client), SearchQuery(keywords=("Python", "Django"), area="Алматы")
    )

    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "keywords": "Python Django",
        "location": "Алматы",
        "page": "1",
    }
    assert [posting.external_id for posting in postings] == [
        "-1234567890123456789",
        "987654321",
    ]
    first = postings[0]
    assert first.company == "Kaspi Tech"
    assert first.raw["_derived"]["location"] == "Алматы"
    assert JoobleSource().publisher_of(first.raw) == "hh.kz"
    assert postings[1].company is None


async def test_a_jooble_error_never_prints_the_key(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """The key is a path segment, so the error message must have it redacted."""
    http.post(JOOBLE_URL).mock(return_value=httpx.Response(403, json={}))

    with pytest.raises(SourceError) as caught:
        await drain(bound(JoobleSource(), client), SearchQuery(keywords=("Python",)))

    assert "403" in str(caught.value)
    assert JOOBLE_KEY not in str(caught.value)


async def test_jooble_without_a_jobs_list_fails_loudly(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """Same rule as every other source here."""
    http.post(JOOBLE_URL).mock(return_value=httpx.Response(200, json={"totalCount": 0}))

    with pytest.raises(SourceError, match="jobs"):
        await drain(bound(JoobleSource(), client), SearchQuery(keywords=("Python",)))


# ── careerjet ─────────────────────────────────────────────────────────


async def test_careerjet_sends_the_affid_locale_and_user_agent(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """A Kazakh place picks the Kazakh locale; no user_ip is invented when none is set."""
    route = http.get(CAREERJET_URL).mock(
        return_value=httpx.Response(200, json=payload("careerjet"))
    )

    await drain(bound(CareerjetSource(), client), SearchQuery(keywords=("Python",), area="Алматы"))

    params = dict(route.calls[0].request.url.params)
    assert params["affid"] == CAREERJET_AFFID
    assert params["keywords"] == "Python"
    assert params["location"] == "Алматы"
    assert params["locale_code"] == "ru_KZ"
    assert params["user_agent"] == settings.user_agent
    assert "user_ip" not in params


async def test_careerjet_sends_the_configured_user_ip(
    client: SourceClient, http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end user's IP is configuration, sent only when set."""
    monkeypatch.setattr(
        settings,
        "source_credentials",
        {**credentials(), "careerjet.user_ip": SecretStr("203.0.113.7")},
    )
    route = http.get(CAREERJET_URL).mock(
        return_value=httpx.Response(200, json=payload("careerjet"))
    )

    await drain(bound(CareerjetSource(), client), SearchQuery(keywords=("Python",)))

    params = dict(route.calls[0].request.url.params)
    assert params["user_ip"] == "203.0.113.7"
    assert params["locale_code"] == "en_GB"


async def test_careerjet_normalises_results_with_a_stable_id(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """No vendor id: a digest of the URL, the same on every run."""
    http.get(CAREERJET_URL).mock(return_value=httpx.Response(200, json=payload("careerjet")))
    source = bound(CareerjetSource(), client)

    first = await drain(source, SearchQuery(keywords=("Python",), area="Алматы"))
    second = await drain(source, SearchQuery(keywords=("Python",), area="Алматы"))

    assert len(first) == 2
    assert [p.external_id for p in first] == [p.external_id for p in second]
    assert first[0].external_id == stable_id(first[0].url)
    derived = first[0].raw["_derived"]
    assert derived["salary_min"] == 600000
    assert derived["salary_currency"] == "KZT"
    assert derived["salary_period"] == "month"
    assert "salary_min" not in first[1].raw["_derived"]
    assert source.publisher_of(first[0].raw) == "hh.kz"


async def test_an_ambiguous_careerjet_location_yields_nothing(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """``LOCATIONS`` carries suggestions, not jobs."""
    http.get(CAREERJET_URL).mock(
        return_value=httpx.Response(200, json={"type": "LOCATIONS", "solveLocations": []})
    )

    postings = await drain(bound(CareerjetSource(), client), SearchQuery(keywords=("Python",)))

    assert postings == []


async def test_a_careerjet_error_fails_the_source(
    client: SourceClient, http: respx.MockRouter
) -> None:
    """``ERROR`` is the vendor saying no, with its reason."""
    http.get(CAREERJET_URL).mock(
        return_value=httpx.Response(200, json={"type": "ERROR", "error": "invalid affid"})
    )

    with pytest.raises(SourceError, match="invalid affid"):
        await drain(bound(CareerjetSource(), client), SearchQuery(keywords=("Python",)))

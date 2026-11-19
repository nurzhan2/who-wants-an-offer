"""hh.ru, read for remote work only (phase 22).

Four things are pinned here, each one a rule that looks optional and is not:

* **Remote is read off the page, after the page was bought.** The sitemap and
  the catalogue carry no work format, so nothing cheaper rules a posting out;
  the filter keeps ``vacancyView.workFormats == REMOTE`` and counts the rest as
  ``not_remote`` — the honest price of one remote vacancy.
* **hh.ru adds no pace.** The token bucket belongs to the *source*, not to the
  host: every hh host shares the 0.25 requests a second that was measured.
  Asserted end to end over two hosts, because a per-host bucket would double
  the rate the moment a second host joined a walk.
* **A country-wide host does not invent a city.** hh.ru's entry names a country;
  a remote posting that states no address stays without a city rather than
  being filed under one.
* **One vacancy, two hosts, one row.** hh's ids are shared across hh.kz and
  hh.ru, so the same posting read on both lands on the same
  ``(source, external_id)`` and the same fingerprint.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from structlog.testing import capture_logs
from test_hh_night import (
    ALMATY,
    FIXTURES,
    HH_ROBOTS,
    OVERHEAD,
    TEMPLATE,
    Clock,
    StateStore,
    _page,
    _sitemap,
    build,
    collect,
)

from app.pipeline.runner import to_vacancy
from app.sources.hh import SITES_FILE, HHSource, load_crawl
from app.sources.http import SourceClient

pytestmark = pytest.mark.unit

RU = "hh.ru"


def write_sites(path: Path) -> Path:
    """Almaty as it ships, and hh.ru as phase 22 adds it."""
    body = (
        "sites:\n"
        f"  - host: {ALMATY}\n    city: Алматы\n    country: KZ\n    default: true\n"
        "    aliases: [алматы]\n"
        f"  - host: {RU}\n    city: Россия\n    country: RU\n    default: true\n"
        "    remote_only: true\n    city_fallback: false\n    aliases: [россия]\n"
    )
    sites = path / "hh_sites.yaml"
    sites.write_text(body, encoding="utf-8")
    return sites


def serve(
    http: respx.MockRouter,
    host: str,
    ids: Sequence[str],
    *,
    formats: dict[str, str] | None = None,
    no_address: bool = False,
) -> None:
    """One host's robots, index, sitemaps and pages; ``formats`` per id."""
    http.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(200, text=HH_ROBOTS))
    http.get(f"https://{host}/sitemap/main.xml").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "hh_sitemap_index.xml")
            .read_text(encoding="utf-8")
            .replace(ALMATY, host),
        )
    )
    http.get(f"https://{host}/sitemap/vacancy1.xml").mock(
        return_value=httpx.Response(200, text=_sitemap(host, []))
    )
    http.get(f"https://{host}/sitemap/vacancy0.xml").mock(
        return_value=httpx.Response(200, text=_sitemap(host, ids))
    )
    for vacancy_id in ids:
        payload: dict[str, Any] = json.loads(
            (FIXTURES / f"{TEMPLATE}.json").read_text(encoding="utf-8")
        )
        view = payload["vacancyView"]
        view["vacancyId"] = int(vacancy_id)
        view["workFormats"] = [(formats or {}).get(vacancy_id, "ON_SITE")]
        if no_address:
            view["address"] = None
            view["area"] = None
        http.get(f"https://{host}/vacancy/{vacancy_id}").mock(
            return_value=httpx.Response(200, text=_page(payload))
        )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def http() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Any:
    from app.core.config import settings

    monkeypatch.setattr(settings, "http_cache_dir", None)
    source_client = SourceClient(clock=clock, sleep=clock.sleep)
    try:
        yield source_client
    finally:
        await source_client.aclose()


def only_ru(tmp_path: Path) -> Path:
    """hh.ru alone, so a walk's numbers are that host's."""
    sites = tmp_path / "hh_sites.yaml"
    sites.write_text(
        f"sites:\n  - host: {RU}\n    city: Россия\n    country: RU\n    default: true\n"
        "    remote_only: true\n    city_fallback: false\n",
        encoding="utf-8",
    )
    return sites


async def test_a_remote_only_host_keeps_remote_postings_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch, http: respx.MockRouter, client: SourceClient, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.sources.hh.SITES_FILE", only_ru(tmp_path))
    ids = [str(700_000_000 + n) for n in range(6)]
    serve(http, RU, ids, formats={ids[1]: "REMOTE", ids[4]: "REMOTE", ids[5]: "HYBRID"})
    source: HHSource = build(client, StateStore())

    with capture_logs() as logs:
        postings = await collect(source)

    assert sorted(posting.external_id for posting in postings) == [ids[1], ids[4]]
    finished = next(line for line in logs if line["event"] == "sources.hh.site_finished")
    assert (finished["fetched"], finished["stored"], finished["not_remote"]) == (6, 2, 4)


async def test_a_remote_posting_with_no_address_gets_no_city(
    monkeypatch: pytest.MonkeyPatch, http: respx.MockRouter, client: SourceClient, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.sources.hh.SITES_FILE", only_ru(tmp_path))
    ids = ["700000100"]
    serve(http, RU, ids, formats={ids[0]: "REMOTE"}, no_address=True)

    [posting] = await collect(build(client, StateStore()))

    assert posting.raw["_derived"]["city"] is None
    assert to_vacancy(posting).city is None


async def test_every_hh_host_shares_one_pace(
    monkeypatch: pytest.MonkeyPatch,
    http: respx.MockRouter,
    client: SourceClient,
    clock: Clock,
    tmp_path: Path,
) -> None:
    """Two hosts in one walk still go at 0.25 rps in total, not per host."""
    monkeypatch.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path))
    almaty = [str(900_000_000 + n) for n in range(5)]
    russia = [str(700_000_200 + n) for n in range(5)]
    serve(http, ALMATY, almaty)
    serve(http, RU, russia, formats={vacancy_id: "REMOTE" for vacancy_id in russia})

    postings = await collect(build(client, StateStore()))

    assert len(postings) == 10
    requests = 10 + 2 * OVERHEAD
    assert clock.now / requests >= 4.0, (
        f"{requests} requests over two hosts in {clock.now:.1f}s: the bucket is per host"
    )


async def test_one_vacancy_on_both_hosts_is_one_vacancy(
    monkeypatch: pytest.MonkeyPatch, http: respx.MockRouter, client: SourceClient, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.sources.hh.SITES_FILE", write_sites(tmp_path))
    shared = "700000300"
    serve(http, ALMATY, [shared], formats={shared: "REMOTE"})
    serve(http, RU, [shared], formats={shared: "REMOTE"})

    postings = await collect(build(client, StateStore()))

    assert {posting.key for posting in postings} == {("hh", shared)}
    assert len({to_vacancy(posting).fingerprint for posting in postings}) == 1


def test_the_shipped_file_reads_hh_ru_for_remote_work_with_a_night_share() -> None:
    sites = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    [ru] = [site for site in sites["sites"] if site["host"] == RU]
    assert ru["remote_only"] is True
    assert ru["city_fallback"] is False
    # Regional subdomains are not listed: nobody measured whether hh.ru's are
    # built like hh.kz's.
    assert not any(site["host"].endswith(".hh.ru") for site in sites["sites"])
    night = load_crawl(SITES_FILE, profile="night")
    assert any(city.host == RU and city.share > 0 for city in night.cities)

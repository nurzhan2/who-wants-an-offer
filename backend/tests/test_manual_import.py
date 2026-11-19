"""``POST /vacancies/import``: one vacancy by hand, inside the same limits as the crawl.

Synthetic pages and texts only. Pinned: a pasted text is parsed and stored
under ``linkedin_manual``; a link to LinkedIn, Indeed or Glassdoor is refused
without a request, while the same link with the text is accepted; any other
link is fetched once, robots.txt first, and read for its schema.org
JobPosting; the CSV export is read without fetching anything.
"""

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models import VacancySource
from app.services import manual_import

pytestmark = pytest.mark.db

URL = f"{settings.api_v1_prefix}/vacancies/import"
TEXT = (
    "Вакансия: Python Backend Developer\nКомпания: Synthetic Labs\nУдалённо\n"
    "Python, FastAPI, PostgreSQL. Откликаться: @synthetic_hr"
)
PAGE = (
    "<html><head><script type='application/ld+json'>"
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "LLM Engineer",
            "hiringOrganization": {"@type": "Organization", "name": "Example AI"},
            "jobLocationType": "TELECOMMUTE",
            "description": "<p>Build RAG pipelines in Python.</p>",
        }
    )
    + "</script></head><body>…</body></html>"
)


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse by the rules: no test here may reach a model."""
    monkeypatch.setattr(manual_import, "_router", lambda: None)


async def test_a_pasted_text_becomes_a_vacancy(async_client: AsyncClient, db_session: Any) -> None:
    response = await async_client.post(URL, json={"text": TEXT})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["parsed_by"] == "rules"
    assert "Python Backend Developer" in body["title"]
    slugs = (await db_session.scalars(select(VacancySource.source_slug))).all()
    assert slugs == ["linkedin_manual"]


async def test_the_same_text_twice_is_one_vacancy(async_client: AsyncClient) -> None:
    first = (await async_client.post(URL, json={"text": TEXT})).json()
    second = (await async_client.post(URL, json={"text": TEXT})).json()
    assert first["vacancy_id"] == second["vacancy_id"]
    assert second["created"] is False


@pytest.mark.parametrize(
    "link",
    [
        "https://www.linkedin.com/jobs/view/4100000001/",
        "https://kz.indeed.com/viewjob?jk=0a1b2c3d4e5f6789",
        "https://www.glassdoor.com/job-listing/x.htm?jl=1009999",
    ],
)
async def test_a_blocked_host_is_never_opened(async_client: AsyncClient, link: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        response = await async_client.post(URL, json={"url": link})
        assert router.calls.call_count == 0
    assert response.status_code == 422
    assert "Вставьте текст" in response.json()["detail"]


async def test_a_blocked_link_with_its_text_is_accepted_and_keeps_the_link(
    async_client: AsyncClient, db_session: Any
) -> None:
    link = "https://www.linkedin.com/jobs/view/4100000001/"
    with respx.mock(assert_all_called=False) as router:
        response = await async_client.post(URL, json={"url": link, "text": TEXT})
        assert router.calls.call_count == 0
    assert response.status_code == 201
    stored = (await db_session.scalars(select(VacancySource.url))).all()
    assert stored == [link]


async def test_another_link_is_fetched_once_and_read_for_its_job_posting(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "http_cache_dir", None)
    link = "https://jobs.example.test/llm-engineer"
    with respx.mock(assert_all_called=False) as router:
        router.get("https://jobs.example.test/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
        )
        page = router.get(link).mock(return_value=httpx.Response(200, text=PAGE))
        response = await async_client.post(URL, json={"url": link})
        assert page.call_count == 1

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["title"], body["company"], body["parsed_by"]) == (
        "LLM Engineer",
        "Example AI",
        "schema.org",
    )


async def test_nothing_to_import_is_a_422(async_client: AsyncClient) -> None:
    assert (await async_client.post(URL, json={})).status_code == 422
    assert (await async_client.post(URL, json={"url": "http://plain.example"})).status_code == 422


async def test_the_saved_jobs_csv_is_read_without_fetching(
    async_client: AsyncClient, db_session: Any
) -> None:
    csv_body = (
        "Job Title,Company Name,Location,Job URL\n"
        "Senior Python Engineer,Synthetic Labs,Remote,https://www.linkedin.com/jobs/view/1/\n"
        "No link,Nobody,Nowhere,\n"
    )
    with respx.mock(assert_all_called=False) as router:
        response = await async_client.post(
            f"{URL}/csv", files={"file": ("saved.csv", csv_body.encode(), "text/csv")}
        )
        assert router.calls.call_count == 0
    assert response.status_code == 201
    [item] = response.json()["items"]
    assert (item["title"], item["company"], item["parsed_by"]) == (
        "Senior Python Engineer",
        "Synthetic Labs",
        "csv",
    )

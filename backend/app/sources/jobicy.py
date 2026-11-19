"""Jobicy: a JSON feed of the newest remote jobs, fetched once and filtered here.

``GET https://jobicy.com/api/v2/remote-jobs`` accepts ``count``, ``tag``,
``geo`` and ``industry``. Only ``count`` is sent. ``tag`` takes one tag, and a
plan has a dozen keywords: asking once per keyword would multiply the requests
for an overlapping answer, and the union filter here (``_boards.FeedSource``)
matches phrases the vendor's tag search cannot. ``geo`` and ``industry`` take
vendor slugs this project has no table for, and a wrong slug is an empty feed
that looks like a quiet market.

``pubDate`` is documented as ``YYYY-MM-DD HH:MM:SS`` with no offset. It is read
as UTC — an assumption, stated here once, to be checked against a live
response. Salaries are annual (``annualSalaryMin``/``annualSalaryMax``) with a
``salaryCurrency``.

Nothing here was measured — the container had no network — and the fixture
says so in its own ``_comment``.
"""

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType
from app.sources._boards import (
    MAX_LOCATION,
    Amount,
    Derived,
    FeedEntry,
    FeedSource,
    StrTuple,
    Text,
    build_posting,
    collapse,
    haystack,
    parse_timestamp,
    positive,
    strip_tags,
)
from app.sources.base import AccessMode, RateLimit
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "https://jobicy.com/api/v2/remote-jobs"

#: Postings asked for in the single request. The documented ceiling, as
#: recalled offline, is at least this; a smaller answer is simply a smaller feed.
FEED_COUNT = 50


class JobicyJob(BaseModel):
    """One posting, in the vendor's camelCase, read through aliases."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    url: str
    job_title: str = Field(alias="jobTitle")
    job_slug: Text = Field(default="", alias="jobSlug")
    company_name: Text = Field(default="", alias="companyName")
    job_industry: StrTuple = Field(default=(), alias="jobIndustry")
    job_type: StrTuple = Field(default=(), alias="jobType")
    job_geo: Text = Field(default="", alias="jobGeo")
    job_level: Text = Field(default="", alias="jobLevel")
    job_excerpt: Text = Field(default="", alias="jobExcerpt")
    #: HTML.
    job_description: Text = Field(default="", alias="jobDescription")
    pub_date: Text = Field(default="", alias="pubDate")
    annual_salary_min: Amount = Field(default=None, alias="annualSalaryMin")
    annual_salary_max: Amount = Field(default=None, alias="annualSalaryMax")
    salary_currency: Text = Field(default="", alias="salaryCurrency")


class JobicyFeed(BaseModel):
    """The envelope. ``jobs`` is required: without it the API changed under us."""

    model_config = ConfigDict(extra="ignore")

    # Any: each entry is validated on its own by JobicyJob, so one bad row
    # costs one posting, and raw keeps the payload exactly as sent.
    jobs: list[dict[str, Any]]


@register_source
class JobicySource(FeedSource):
    """Jobicy's public remote-jobs API, used under the notice it publishes with it.

    Terms, as summarised from the API's documentation page and the
    ``friendlyNotice`` the response carries, recalled offline (re-read before
    the first live run): free, no key; link back to Jobicy and credit it as the
    source, which is :attr:`attribution`; do not poll aggressively — the feed
    is refreshed a few times a day and frequent polling may get the client
    blocked; do not resell the listings or feed them to other job boards.

    One request a run, no closer than six hours apart.
    """

    slug = "jobicy"
    name = "Jobicy"
    regions = ("remote",)
    access_mode = AccessMode.API
    terms_url = "https://jobicy.com/jobs-rss-feed"
    attribution = "Источник: Jobicy (jobicy.com)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    #: Refreshed a few times a day by the vendor's own account; four runs match it.
    min_interval = timedelta(hours=6)
    cache_ttl = timedelta(hours=6)

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every posting of one request."""
        payload = await self.http.get_json(API_URL, params={"count": FEED_COUNT})
        try:
            feed = JobicyFeed.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе нет списка jobs — форма API изменилась",
                source_slug=self.slug,
            ) from exc
        for item in feed.jobs:
            entry = self._entry(item)
            if entry is not None:
                yield entry

    def _entry(self, item: dict[str, Any]) -> FeedEntry | None:
        """One validated posting, or None with the reason logged."""
        try:
            job = JobicyJob.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "sources.jobicy.item_invalid",
                external_id=str(item.get("id", ""))[:200],
                errors=exc.error_count(),
            )
            return None
        minimum = positive(job.annual_salary_min)
        maximum = positive(job.annual_salary_max)
        derived = Derived(
            remote=RemoteType.FULL,
            location=collapse(job.job_geo, MAX_LOCATION),
            published_at=parse_timestamp(job.pub_date),
            salary_min=minimum,
            salary_max=maximum,
            salary_currency=job.salary_currency or None,
            salary_period="year" if minimum or maximum else None,
        )
        posting = build_posting(
            slug=self.slug,
            external_id=str(job.id),
            url=job.url,
            title=job.job_title,
            company=job.company_name,
            description=job.job_description or job.job_excerpt,
            raw=item,
            derived=derived,
        )
        if posting is None:
            return None
        text = haystack(
            job.job_title,
            " ".join(strip_tags(industry) for industry in job.job_industry),
            job.job_level,
            job.job_excerpt,
            strip_tags(job.job_description),
        )
        return FeedEntry(posting=posting, text=text)

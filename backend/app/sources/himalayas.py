"""Himalayas: a paginated JSON feed of remote jobs, with no search parameters.

``GET https://himalayas.app/jobs/api?limit=N&offset=M`` returns
``{"offset", "limit", "totalCount", "jobs": [...]}``. It filters nothing, so
the plan's keywords are matched here (see ``_boards.FeedSource``) and the feed
is walked page by page until the plan's limit, an empty page, ``totalCount`` or
:data:`MAX_PAGES` — whichever comes first.

The page size is :data:`PAGE_SIZE`, not the 100 the catalogue in
docs/SOURCES.md writes: the vendor's API page, as recalled when this was
written, caps ``limit`` at 20 per request, and asking for more than the cap is
either refused or silently clipped. 20 is safe under either reading; the first
live run should confirm which and whether a larger page is allowed.

Every posting is remote by construction of the board. ``locationRestrictions``
names the countries a candidate may work from and becomes
``_derived.location``; ``minSalary``/``maxSalary``/``currency`` are structured
and passed through. ``pubDate`` is documented as Unix seconds.

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

API_URL = "https://himalayas.app/jobs/api"

#: Postings per request; see the module docstring for why not 100.
PAGE_SIZE = 20
#: Backstop under every other stop condition: 25 pages is 500 postings, more
#: than one filtered run of a remote board has ever needed, and a wedged
#: ``totalCount`` must not become an endless walk.
MAX_PAGES = 25


class HimalayasJob(BaseModel):
    """One posting, in the vendor's camelCase, read through aliases."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    #: Stable per posting: the job's own himalayas.app URL.
    guid: str
    title: str
    application_link: Text = Field(default="", alias="applicationLink")
    company_name: Text = Field(default="", alias="companyName")
    excerpt: Text = ""
    #: HTML.
    description: Text = ""
    employment_type: Text = Field(default="", alias="employmentType")
    min_salary: Amount = Field(default=None, alias="minSalary")
    max_salary: Amount = Field(default=None, alias="maxSalary")
    currency: Text = ""
    location_restrictions: StrTuple = Field(default=(), alias="locationRestrictions")
    categories: StrTuple = ()
    seniority: StrTuple = ()
    #: Unix seconds.
    pub_date: int | None = Field(default=None, alias="pubDate")


class HimalayasPage(BaseModel):
    """The envelope. ``jobs`` is required: a page without it is a changed API."""

    model_config = ConfigDict(extra="ignore")

    total_count: int | None = Field(default=None, alias="totalCount")
    # Any: each entry is validated on its own by HimalayasJob, so one bad row
    # costs one posting rather than the page, and raw keeps the payload as sent.
    jobs: list[dict[str, Any]]


@register_source
class HimalayasSource(FeedSource):
    """Himalayas' public remote-jobs API, used under the terms on its API page.

    Terms, as summarised from https://himalayas.app/api when this was written
    (re-read before the first live run — the container had no network): free,
    no key; attribution to Himalayas with a link back to the job on
    himalayas.app is required, which is :attr:`attribution` and why ``url`` is
    the himalayas.app page; ``limit`` is capped per request (20); abusive
    request rates are throttled, with no published number.

    One page every two seconds, at most :data:`MAX_PAGES` pages, no closer than
    twelve hours apart: the heaviest walk among the keyless boards, so the
    longest interval.
    """

    slug = "himalayas"
    name = "Himalayas"
    regions = ("remote",)
    access_mode = AccessMode.API
    terms_url = "https://himalayas.app/api"
    attribution = "Источник: Himalayas (himalayas.app)"
    rate_limit = RateLimit(requests_per_second=0.5, burst=1)
    #: Up to 25 requests a run; twice a day keeps it a polite few dozen a day.
    min_interval = timedelta(hours=12)
    cache_ttl = timedelta(hours=6)

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every posting, page by page, until the feed or the backstop ends it."""
        offset = 0
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                API_URL, params={"limit": PAGE_SIZE, "offset": offset}
            )
            envelope = self._page(payload, page)
            if not envelope.jobs:
                break
            for item in envelope.jobs:
                entry = self._entry(item)
                if entry is not None:
                    yield entry
            offset += len(envelope.jobs)
            if envelope.total_count is not None and offset >= envelope.total_count:
                break

    def _page(self, payload: Any, page: int) -> HimalayasPage:
        """The envelope, or a failure naming what changed."""
        try:
            return HimalayasPage.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе нет списка jobs (страница {page}) — форма API изменилась",
                source_slug=self.slug,
            ) from exc

    def _entry(self, item: dict[str, Any]) -> FeedEntry | None:
        """One validated posting, or None with the reason logged."""
        try:
            job = HimalayasJob.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "sources.himalayas.item_invalid",
                guid=str(item.get("guid", ""))[:200],
                errors=exc.error_count(),
            )
            return None
        derived = Derived(
            remote=RemoteType.FULL,
            location=collapse(", ".join(job.location_restrictions), MAX_LOCATION),
            published_at=parse_timestamp(job.pub_date),
            salary_min=positive(job.min_salary),
            salary_max=positive(job.max_salary),
            salary_currency=job.currency or None,
        )
        # The himalayas.app page is the link the terms ask for; the employer's
        # own application link stays in raw.
        url = job.guid if job.guid.startswith("http") else job.application_link
        posting = build_posting(
            slug=self.slug,
            external_id=job.guid,
            url=url,
            title=job.title,
            company=job.company_name,
            description=job.description or job.excerpt,
            raw=item,
            derived=derived,
        )
        if posting is None:
            return None
        text = haystack(
            job.title,
            " ".join(job.categories),
            job.excerpt,
            strip_tags(job.description),
        )
        return FeedEntry(posting=posting, text=text)

"""Remote OK: one JSON array of the newest remote postings, with a legal notice first.

``GET https://remoteok.com/api`` takes no search parameters and answers with a
bare array. Its first element is not a job: it is the vendor's legal notice
(``{"last_updated": ..., "legal": "..."}``), and treating it as one would
either fail validation on every run or, worse, store the notice as a posting.
It is recognised by its ``legal`` key rather than by its position, so a feed
that ever drops or moves it is still read correctly.

The board is remote-only, so ``_derived.remote`` is ``full`` for every posting.
Salaries are structured (``salary_min``/``salary_max``) and ``0`` means "not
stated"; the currency is not in the payload and is left out rather than
assumed.

Nothing here was measured: the container that wrote this connector had no
network, so the fixture is reconstructed from the public API's documented
shape and the rate, interval and terms below must be re-checked on the first
live run.
"""

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

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

API_URL = "https://remoteok.com/api"

#: The key that marks the legal notice instead of a posting.
LEGAL_KEY = "legal"


class RemoteOkJob(BaseModel):
    """One posting. ``id`` and ``position`` are required; everything else defaults."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    position: str
    slug: Text = ""
    company: Text = ""
    #: The posting's own page on remoteok.com — the link their terms require.
    url: Text = ""
    apply_url: Text = ""
    #: HTML.
    description: Text = ""
    location: Text = ""
    tags: StrTuple = ()
    #: ISO 8601 with an offset, per the documented shape.
    date: Text = ""
    epoch: int | None = None
    salary_min: Amount = None
    salary_max: Amount = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        """Ids are documented as strings of digits; accept a bare number too."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


@register_source
class RemoteOkSource(FeedSource):
    """Remote OK's public JSON feed, used under the notice it ships inside the payload.

    Terms (the ``legal`` element of the response itself, which is where Remote
    OK publishes them): link back to the posting's URL on Remote OK — a
    followed link, not nofollow — and mention Remote OK as the source, or API
    access is suspended. That is :attr:`attribution`, and every posting's
    ``url`` is its remoteok.com page rather than the employer's apply link.

    No numeric rate limit is published; the feed is one cached snapshot, so
    asking more than every few hours buys nothing. One request a run, at most
    one a second, no closer than six hours apart.
    """

    slug = "remoteok"
    name = "Remote OK"
    regions = ("remote",)
    access_mode = AccessMode.API
    terms_url = "https://remoteok.com/api"
    attribution = "Источник: Remote OK (remoteok.com), ссылка на вакансию ведёт на remoteok.com"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    #: One snapshot feed: four runs a day is already more than it changes.
    min_interval = timedelta(hours=6)
    cache_ttl = timedelta(hours=6)

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every posting in the array, the legal notice skipped."""
        payload = await self.http.get_json(API_URL)
        if not isinstance(payload, list):
            raise SourceError(
                f"{self.slug}: ответ не является массивом — форма API изменилась",
                source_slug=self.slug,
            )
        for item in payload:
            if not isinstance(item, dict):
                continue
            if LEGAL_KEY in item:
                logger.debug("sources.remoteok.legal_notice_skipped")
                continue
            entry = self._entry(item)
            if entry is not None:
                yield entry

    def _entry(self, item: dict[str, Any]) -> FeedEntry | None:
        """One validated posting, or None with the reason logged."""
        try:
            job = RemoteOkJob.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "sources.remoteok.item_invalid",
                external_id=str(item.get("id", ""))[:200],
                errors=exc.error_count(),
            )
            return None
        derived = Derived(
            remote=RemoteType.FULL,
            location=collapse(job.location, MAX_LOCATION),
            published_at=parse_timestamp(job.date) or parse_timestamp(job.epoch),
            salary_min=positive(job.salary_min),
            salary_max=positive(job.salary_max),
        )
        posting = build_posting(
            slug=self.slug,
            external_id=job.id,
            url=job.url or job.apply_url,
            title=job.position,
            company=job.company,
            description=job.description,
            raw=item,
            derived=derived,
        )
        if posting is None:
            return None
        text = haystack(job.position, " ".join(job.tags), strip_tags(job.description))
        return FeedEntry(posting=posting, text=text)

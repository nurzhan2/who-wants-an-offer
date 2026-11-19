"""Jooble: a keyed search API with the key in the path and the query in a JSON body.

``POST https://jooble.org/api/{key}`` with ``{"keywords", "location", "page"}``.
The keywords go upstream (``_boards.KeyedSource``); the place goes in
``location`` as free text, which is how Jooble resolves a country — it covers
about seventy, Kazakhstan and Russia among them, so no search is skipped for
its country. A remote slot sends "remote" in the keywords and no location.

The key is a path segment. ``http.redact`` already recognises that shape (it
names jooble in its own docstring), so the key never reaches a log line or an
error message, and a POST is never written to the dev cache.

``snippet`` is a short excerpt, not the posting; ``salary`` is free text and is
not parsed here. ``id`` is Jooble's own numeric identifier.

**No key exists in this checkout.** Without ``jooble.api_key`` in
``SOURCE_CREDENTIALS`` the source reports ``missing_credentials`` and is never
run. The fixture is reconstructed from the public API documentation.
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.sources._boards import (
    MAX_LOCATION,
    Derived,
    KeyedSearch,
    KeyedSource,
    Text,
    build_posting,
    collapse,
    credential,
    parse_timestamp,
)
from app.sources.base import AccessMode, RateLimit, RawPosting
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "https://jooble.org/api/{key}"
API_KEY_KEY = "jooble.api_key"

#: Jooble publishes no number; a key is issued with an allowance stated in the
#: approval email. This is this project's own ceiling until that number is
#: known, chosen low on purpose — raise it to what the email says.
DAILY_ALLOWANCE = 50
RUN_EVERY = timedelta(hours=12)
REQUESTS_PER_RUN = DAILY_ALLOWANCE * int(RUN_EVERY.total_seconds()) // 86_400


class JoobleJob(BaseModel):
    """One result. ``id``, ``title`` and ``link`` are required."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    title: str
    link: str
    company: Text = ""
    location: Text = ""
    #: A short excerpt, sometimes with markup.
    snippet: Text = ""
    #: Free text; never parsed here.
    salary: Text = ""
    #: The board Jooble found the posting on.
    source: Text = ""
    type: Text = ""
    updated: Text = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        """Ids arrive as large integers; store them as text."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class JoobleResponse(BaseModel):
    """The envelope. ``jobs`` is required: without it the API changed under us."""

    model_config = ConfigDict(extra="ignore")

    # Any: each entry is validated on its own by JoobleJob, so one bad row
    # costs one posting, and raw keeps the payload exactly as sent.
    jobs: list[dict[str, Any]]


@register_source
class JoobleSource(KeyedSource):
    """Jooble's REST API for partners, under the terms it is issued with.

    Terms (https://jooble.org/api/about, summarised offline — re-read before
    the first live run): a free key issued on request to a partner; the
    allowance is stated when the key is issued, not published, so this class
    declares its own low ceiling (:data:`DAILY_ALLOWANCE`). Results must link
    to Jooble's ``link``, which is the posting's ``url``, and Jooble is
    credited as the source, which is :attr:`attribution`.

    Restrictions: the key is personal to the partner and is not to be shared;
    it is a secret and lives only in ``SOURCE_CREDENTIALS``.
    """

    slug = "jooble"
    name = "Jooble"
    regions = ("global", "KZ", "RU")
    requires_auth = True
    required_credentials = (API_KEY_KEY,)
    access_mode = AccessMode.API
    terms_url = "https://jooble.org/api/about"
    attribution = "Источник: Jooble (jooble.org)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    daily_quota = DAILY_ALLOWANCE
    min_interval = RUN_EVERY
    requests_per_run = REQUESTS_PER_RUN

    def body(self, search: KeyedSearch) -> dict[str, str]:
        """The JSON body for one search."""
        return {"keywords": search.keywords, "location": search.area or "", "page": "1"}

    async def fetch(self, search: KeyedSearch) -> list[RawPosting]:
        """The first page for one search."""
        url = API_URL.format(key=credential(self.slug, API_KEY_KEY))
        response = await self.http.request("POST", url, json_body=self.body(search))
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(
                f"{self.slug}: ответ не является JSON", source_slug=self.slug
            ) from exc
        try:
            page = JoobleResponse.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе нет списка jobs — форма API изменилась",
                source_slug=self.slug,
            ) from exc
        postings = [self._posting(item) for item in page.jobs]
        return [posting for posting in postings if posting is not None]

    def publisher_of(self, raw: dict[str, Any]) -> str | None:
        """The board Jooble found the posting on."""
        value = raw.get("source")
        if not isinstance(value, str):
            return None
        return value.strip()[:200] or None

    def _posting(self, item: dict[str, Any]) -> RawPosting | None:
        """One result as a posting, or None with the reason logged."""
        try:
            job = JoobleJob.model_validate(item)
        except ValidationError as exc:
            logger.warning("sources.jooble.item_invalid", errors=exc.error_count())
            return None
        derived = Derived(
            location=collapse(job.location, MAX_LOCATION),
            published_at=parse_timestamp(job.updated),
        )
        return build_posting(
            slug=self.slug,
            external_id=job.id,
            url=job.link,
            title=job.title,
            company=job.company,
            description=job.snippet,
            raw=item,
            derived=derived,
        )

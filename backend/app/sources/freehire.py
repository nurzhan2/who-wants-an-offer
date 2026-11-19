"""freehire: a public, keyless JSON API of tech jobs, at a base URL from configuration.

The catalogue entry in docs/SOURCES.md says only ``GET https://freehire.me/api``,
that the output is already structured (skills, grade, category), and that the
backend is MIT-licensed and self-hostable — which is why the base URL is
configuration rather than a constant: an instance can move, or be one's own.

**Configuration, not secrets.** ``config.py`` has no field for this source and
a connector may not add one (docs/ENGINEERING.md rule 5), so both values are
read from the per-source map the settings already decode,
``SOURCE_CREDENTIALS``:

* ``freehire.base_url`` — defaults to :data:`DEFAULT_BASE_URL`;
* ``freehire.jobs_path`` — defaults to :data:`DEFAULT_JOBS_PATH`.

Neither is a secret; they sit in that map because it is the only per-source
configuration a connector can read without editing a file outside
``sources/``. Neither is required, so the source is enabled by default.

**The endpoint path and the response shape are not known.** This was written
in a container with no network, from a one-line catalogue entry. The path is a
best guess (``/api/jobs``) and the parser is deliberately tolerant: it accepts a
bare list or an object holding the list under ``jobs``, ``data``, ``results``
or ``items``, and reads each field under the handful of names such an API
commonly uses (:class:`FreehireJob`). **The first live run must confirm the
path and the shape**, then narrow the aliases to the real ones and re-record
the fixture. Until then a changed or wrong path fails loudly — a 404 or an
envelope with no list — rather than reading as an empty market.

One request a run with no parameters, because no pagination or filter
parameter is documented; the plan's keywords are matched here
(``_boards.FeedSource``) against the title, skills, category and description.
"""

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    config_value,
    haystack,
    parse_timestamp,
    strip_tags,
)
from app.sources.base import AccessMode, RateLimit
from app.sources.registry import register_source

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://freehire.me"
#: A guess; see the module docstring.
DEFAULT_JOBS_PATH = "/api/jobs"
BASE_URL_KEY = "freehire.base_url"
JOBS_PATH_KEY = "freehire.jobs_path"

#: Envelope keys a list of jobs is looked for under, in this order.
LIST_KEYS: tuple[str, ...] = ("jobs", "data", "results", "items")


def _names(*names: str) -> AliasChoices:
    """The spellings one field is read under."""
    return AliasChoices(*names)


class FreehireJob(BaseModel):
    """One posting, read under every spelling a REST API of this kind commonly uses.

    Deliberately wide until a live response narrows it; see the module
    docstring. ``id``, ``title`` and ``url`` are required.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(validation_alias=_names("id", "uuid", "slug"))
    title: str = Field(validation_alias=_names("title", "name", "position"))
    url: str = Field(validation_alias=_names("url", "link", "apply_url", "applyUrl", "source_url"))
    company: Text = Field(
        default="", validation_alias=_names("company", "company_name", "companyName", "employer")
    )
    description: Text = Field(
        default="", validation_alias=_names("description", "description_html", "text")
    )
    location: Text = Field(default="", validation_alias=_names("location", "city"))
    remote: bool | None = Field(default=None, validation_alias=_names("remote", "is_remote"))
    skills: StrTuple = Field(default=(), validation_alias=_names("skills", "tags"))
    grade: Text = Field(default="", validation_alias=_names("grade", "seniority", "level"))
    category: Text = Field(default="", validation_alias=_names("category", "specialization"))
    published: Text = Field(
        default="",
        validation_alias=_names("published_at", "publishedAt", "created_at", "createdAt", "date"),
    )
    salary_from: Amount = Field(
        default=None, validation_alias=_names("salary_from", "salaryFrom", "salary_min")
    )
    salary_to: Amount = Field(
        default=None, validation_alias=_names("salary_to", "salaryTo", "salary_max")
    )
    currency: Text = Field(default="", validation_alias=_names("currency", "salary_currency"))

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        """Accept a numeric id."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("company", mode="before")
    @classmethod
    def _company_name(cls, value: Any) -> Any:
        """Accept a nested ``{"name": ...}`` company."""
        if isinstance(value, dict):
            return value.get("name") or value.get("title") or ""
        return value

    @field_validator("skills", mode="before")
    @classmethod
    def _skill_names(cls, value: Any) -> Any:
        """Accept skills as strings or as ``{"name": ...}`` objects."""
        if isinstance(value, list):
            return [item.get("name", "") if isinstance(item, dict) else item for item in value]
        return value

    @field_validator("published", mode="before")
    @classmethod
    def _published_as_text(cls, value: Any) -> Any:
        """Keep a Unix timestamp readable by ``parse_timestamp`` later."""
        return str(value) if isinstance(value, int | float) else value


def jobs_of(payload: Any) -> list[dict[str, Any]] | None:
    """The list of job objects in a response, wherever this API keeps it, or None."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return None


def endpoint() -> str:
    """The configured jobs URL, validated: base URL plus path."""
    base = (config_value(BASE_URL_KEY) or DEFAULT_BASE_URL).rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise SourceError(
            f"freehire: {BASE_URL_KEY} должен начинаться с https:// или http://",
            source_slug="freehire",
        )
    path = config_value(JOBS_PATH_KEY) or DEFAULT_JOBS_PATH
    return f"{base}/{path.lstrip('/')}"


@register_source
class FreehireSource(FeedSource):
    """freehire's public REST API: no key, JSON, tech jobs only.

    Terms: none could be read when this was written (the container had no
    network), and the catalogue states none beyond "public, no key". Until they
    are read, the defaults are this project's most conservative: one request a
    run, at most one a second, twelve hours apart, and :attr:`attribution`
    credits freehire next to every posting.

    Restrictions: unknown — check ``terms_url`` on the first live run and
    replace this paragraph with what it says.
    """

    slug = "freehire"
    name = "freehire"
    regions = ("global", "remote")
    access_mode = AccessMode.API
    terms_url = "https://freehire.me"
    attribution = "Источник: freehire (freehire.me)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    #: Unknown terms, so the longest interval of the keyless boards.
    min_interval = timedelta(hours=12)
    cache_ttl = timedelta(hours=6)

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every posting of one request."""
        url = endpoint()
        items = jobs_of(await self.http.get_json(url))
        if items is None:
            raise SourceError(
                f"{self.slug}: в ответе нет списка вакансий (ожидался массив или поле "
                f"{', '.join(LIST_KEYS)}) — проверь путь {JOBS_PATH_KEY}",
                source_slug=self.slug,
            )
        for item in items:
            entry = self._entry(item)
            if entry is not None:
                yield entry

    def _entry(self, item: dict[str, Any]) -> FeedEntry | None:
        """One validated posting, or None with the reason logged."""
        try:
            job = FreehireJob.model_validate(item)
        except ValidationError as exc:
            logger.warning("sources.freehire.item_invalid", errors=exc.error_count())
            return None
        remote = None if job.remote is None else (RemoteType.FULL if job.remote else RemoteType.NO)
        derived = Derived(
            remote=remote,
            location=collapse(job.location, MAX_LOCATION),
            published_at=parse_timestamp(job.published),
            salary_min=job.salary_from,
            salary_max=job.salary_to,
            salary_currency=job.currency or None,
        )
        posting = build_posting(
            slug=self.slug,
            external_id=job.id,
            url=job.url,
            title=job.title,
            company=job.company,
            description=job.description,
            raw=item,
            derived=derived,
        )
        if posting is None:
            return None
        text = haystack(
            job.title,
            " ".join(job.skills),
            job.category,
            job.grade,
            strip_tags(job.description),
        )
        return FeedEntry(posting=posting, text=text)

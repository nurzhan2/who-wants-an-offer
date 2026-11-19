"""Adzuna: a keyed search API, one country per request, asked with the plan's words.

``GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}`` with
``app_id``, ``app_key``, ``what`` (the keywords) and ``where`` (the place).
Unlike the remote boards this source is asked a question, so the keywords go
upstream and nothing is filtered here; see ``_boards.KeyedSource``.

**One country per request, and not every country.** The country is a path
segment and Adzuna covers a fixed list (:data:`COUNTRIES`); Kazakhstan and
Russia are not on it. A planned search whose place resolves — through the same
``jsearch_countries.yaml`` table JSearch uses — to a country outside the list,
or to none at all, is skipped without spending a request, and the preview says
how many were. A remote slot with no place has no country either, so it is
skipped too: Adzuna has no remote or worldwide endpoint.

``description`` is Adzuna's snippet, a few hundred characters of plain text,
not the posting; the full text lives behind ``redirect_url`` on adzuna's own
site and is not fetched. ``salary_min``/``salary_max`` are passed to
``_derived`` only when ``salary_is_predicted`` is not ``"1"``: a predicted
salary is Adzuna's estimate, not the employer's offer.

**No key exists in this checkout.** The connector is complete up to the key:
without ``adzuna.app_id`` and ``adzuna.app_key`` in ``SOURCE_CREDENTIALS`` it
reports ``missing_credentials`` and is never run. The fixture is reconstructed
from the public API documentation, not captured.
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.sources._boards import (
    MAX_LOCATION,
    Amount,
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

SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
APP_ID_KEY = "adzuna.app_id"
APP_KEY_KEY = "adzuna.app_key"

#: The countries the API serves, per its documentation, lower case.
COUNTRIES: frozenset[str] = frozenset(
    {
        "at",
        "au",
        "be",
        "br",
        "ca",
        "ch",
        "de",
        "es",
        "fr",
        "gb",
        "in",
        "it",
        "mx",
        "nl",
        "nz",
        "pl",
        "sg",
        "us",
        "za",
    }
)
#: The most a page may hold, per the documentation.
RESULTS_PER_PAGE = 50

#: The free tier, as documented: 25 a minute, 250 a day, 1,000 a week, 2,500 a
#: month. The month binds hardest — 2,500 / 31 is 80 a day — so that is the
#: daily figure declared, and the weekly cap (142 a day) is then never reached.
MONTHLY_ALLOWANCE = 2500
DAILY_ALLOWANCE = MONTHLY_ALLOWANCE // 31
RUN_EVERY = timedelta(hours=12)
#: A run's share of the day, so two runs never exceed the daily figure.
REQUESTS_PER_RUN = DAILY_ALLOWANCE * int(RUN_EVERY.total_seconds()) // 86_400


class AdzunaJob(BaseModel):
    """One result. ``id``, ``title`` and ``redirect_url`` are required."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    title: str
    redirect_url: str
    #: A plain-text snippet, not the full posting.
    description: Text = ""
    created: Text = ""
    company_name: Text = Field(default="", alias="company")
    location_name: Text = Field(default="", alias="location")
    salary_min: Amount = None
    salary_max: Amount = None
    salary_is_predicted: Text = ""
    contract_time: Text = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        """Ids are documented as strings; accept a number too."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("company_name", "location_name", mode="before")
    @classmethod
    def _display_name(cls, value: Any) -> Any:
        """``company`` and ``location`` are objects carrying a ``display_name``."""
        if isinstance(value, dict):
            return value.get("display_name") or ""
        return value

    @field_validator("salary_is_predicted", mode="before")
    @classmethod
    def _flag_as_text(cls, value: Any) -> Any:
        """Documented as ``"0"``/``"1"``; accept a number or a bool too."""
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value) if isinstance(value, int) else value


class AdzunaPage(BaseModel):
    """The envelope. ``results`` is required: without it the API changed under us."""

    model_config = ConfigDict(extra="ignore")

    # Any: each entry is validated on its own by AdzunaJob, so one bad row
    # costs one posting, and raw keeps the payload exactly as sent.
    results: list[dict[str, Any]]


@register_source
class AdzunaSource(KeyedSource):
    """Adzuna's job search API, under its developer terms.

    Terms (https://developer.adzuna.com/docs/terms_of_service, summarised
    offline — re-read before the first live run): a free key pair
    (``app_id`` + ``app_key``) issued on registration; the free tier allows
    25 requests a minute, 250 a day, 1,000 a week and 2,500 a month. Results
    must be shown with a visible "Jobs by Adzuna" credit linking to Adzuna,
    which is :attr:`attribution`, and the user is sent to ``redirect_url``.

    Restrictions, as recalled: the data may not be resold or republished as a
    competing service; this project shows it to its one owner.
    This class spends at most :data:`REQUESTS_PER_RUN` a run, twice a day.
    """

    slug = "adzuna"
    name = "Adzuna"
    regions = tuple(sorted(code.upper() for code in COUNTRIES))
    requires_auth = True
    required_credentials = (APP_ID_KEY, APP_KEY_KEY)
    access_mode = AccessMode.API
    terms_url = "https://developer.adzuna.com/docs/terms_of_service"
    attribution = "Jobs by Adzuna (adzuna.com)"
    #: 25 a minute is 0.42 a second; 0.3 keeps a margin.
    rate_limit = RateLimit(requests_per_second=0.3, burst=1)
    daily_quota = DAILY_ALLOWANCE
    min_interval = RUN_EVERY
    requests_per_run = REQUESTS_PER_RUN

    def supports(self, search: KeyedSearch) -> bool:
        """Only a search whose country is one Adzuna serves."""
        return search.country is not None and search.country in COUNTRIES

    def params(self, search: KeyedSearch) -> dict[str, str]:
        """The query string, credentials read at call time."""
        params = {
            "app_id": credential(self.slug, APP_ID_KEY),
            "app_key": credential(self.slug, APP_KEY_KEY),
            "what": search.keywords,
            "results_per_page": str(RESULTS_PER_PAGE),
            "content-type": "application/json",
        }
        if search.area:
            params["where"] = search.area
        return params

    async def fetch(self, search: KeyedSearch) -> list[RawPosting]:
        """The first page for one search."""
        country = search.country or ""
        payload = await self.http.get_json(
            SEARCH_URL.format(country=country, page=1), params=self.params(search)
        )
        try:
            page = AdzunaPage.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе нет списка results — форма API изменилась",
                source_slug=self.slug,
            ) from exc
        postings = [self._posting(item, country) for item in page.results]
        return [posting for posting in postings if posting is not None]

    def _posting(self, item: dict[str, Any], country: str) -> RawPosting | None:
        """One result as a posting, or None with the reason logged."""
        try:
            job = AdzunaJob.model_validate(item)
        except ValidationError as exc:
            logger.warning("sources.adzuna.item_invalid", errors=exc.error_count())
            return None
        stated = job.salary_is_predicted != "1"
        derived = Derived(
            location=collapse(job.location_name, MAX_LOCATION),
            published_at=parse_timestamp(job.created),
            salary_min=job.salary_min if stated else None,
            salary_max=job.salary_max if stated else None,
        )
        raw = dict(item)
        raw["_country"] = country
        return build_posting(
            slug=self.slug,
            external_id=job.id,
            url=job.redirect_url,
            title=job.title,
            company=job.company_name,
            description=job.description,
            raw=raw,
            derived=derived,
        )

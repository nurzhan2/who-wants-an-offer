"""Careerjet: a keyed (affiliate id) search API, one locale per request.

``GET http://public.api.careerjet.net/search`` with ``affid``, ``keywords``,
``location``, ``locale_code``, ``user_ip`` and ``user_agent``. The keywords go
upstream (``_boards.KeyedSource``); the country chooses the ``locale_code``
from :data:`LOCALES`, and a place in a country the table does not list is
asked under :data:`DEFAULT_LOCALE` with the place in ``location``.

**Plain http, on purpose and stated.** The documented host serves the API over
http. ``http.py`` does not refuse a non-https URL, so the documented URL is
used as is; what travels in clear is the affiliate id (an identifier that is
also embedded in every result link, not a credential that grants access) and
the search words. Whether ``https://public.api.careerjet.net`` answers was not
verifiable offline — if it does, switch :data:`API_URL` to it on the first live
run.

**``user_ip`` and ``user_agent`` describe the end user** to whom the results
are shown. This backend shows them to its one owner, so the user agent is this
project's own (``settings.user_agent``) and the IP is a configuration value,
``careerjet.user_ip`` in ``SOURCE_CREDENTIALS`` — optional, sent when set and
omitted otherwise. Whether the API accepts a request without it must be
checked on the first live run; if not, it becomes a required credential.

A response of ``type`` ``LOCATIONS`` means the place was ambiguous and carries
suggestions instead of jobs; it yields nothing and is logged. ``ERROR`` fails
the source. Careerjet gives no job id, so the external id is a digest of the
result URL — the first live run should confirm that URL is stable between
requests, or the upsert will insert every result again.

**No affiliate id exists in this checkout.** Without ``careerjet.affid`` the
source reports ``missing_credentials`` and is never run. The fixture is
reconstructed from the public API documentation.
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import settings
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
    config_value,
    credential,
    parse_timestamp,
    stable_id,
)
from app.sources.base import AccessMode, RateLimit, RawPosting
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "http://public.api.careerjet.net/search"
AFFID_KEY = "careerjet.affid"
USER_IP_KEY = "careerjet.user_ip"

#: Country to Careerjet locale. Only the countries ``jsearch_countries.yaml``
#: resolves plus the API's own default; ``ru_KZ`` is an assumption (careerjet
#: runs a Kazakh site, but the locale code could not be checked offline).
LOCALES: dict[str, str] = {
    "kz": "ru_KZ",
    "ru": "ru_RU",
    "de": "de_DE",
    "nl": "nl_NL",
    "pl": "pl_PL",
    "ae": "en_AE",
    "gb": "en_GB",
    "us": "en_US",
}
#: The API's documented default locale.
DEFAULT_LOCALE = "en_GB"
PAGE_SIZE = 50

#: Careerjet publishes no request allowance for affiliates; this is the
#: project's own ceiling per run, twice a day.
RUN_EVERY = timedelta(hours=12)
REQUESTS_PER_RUN = 8


class CareerjetJob(BaseModel):
    """One result. ``url`` and ``title`` are required; there is no id."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    url: str
    title: str
    company: Text = ""
    #: An excerpt with highlighted terms, not the posting.
    description: Text = ""
    locations: Text = ""
    #: RFC 822, per the documented shape.
    date: Text = ""
    salary: Text = ""
    salary_min: Amount = None
    salary_max: Amount = None
    salary_currency_code: Text = ""
    #: ``Y``, ``M``, ``W``, ``D``, ``H`` — per year, month, week, day, hour.
    salary_type: Text = ""
    site: Text = ""


class CareerjetResponse(BaseModel):
    """The envelope: ``type`` says what the rest of it is."""

    model_config = ConfigDict(extra="ignore")

    type: str
    # Any: each entry is validated on its own by CareerjetJob, so one bad row
    # costs one posting, and raw keeps the payload exactly as sent.
    jobs: list[dict[str, Any]] = []
    error: Text = ""


#: ``salary_type`` codes, as ``_derived.salary_period`` spells periods.
PERIODS: dict[str, str] = {"Y": "year", "M": "month", "W": "week", "D": "day", "H": "hour"}


@register_source
class CareerjetSource(KeyedSource):
    """Careerjet's public search API for affiliates, under its partner terms.

    Terms (https://www.careerjet.com/partners/api/, summarised offline —
    re-read before the first live run): free for registered affiliates, who
    pass their ``affid`` with every request together with the end user's IP
    and user agent; no allowance is published. Results must be shown with a
    "Jobs by Careerjet" credit, which is :attr:`attribution`, and each result
    links through its own ``url`` so the click is attributed.

    Restrictions: results are for display to end users, not for bulk
    re-publication. This class sends at most :data:`REQUESTS_PER_RUN` a run.
    """

    slug = "careerjet"
    name = "Careerjet"
    regions = ("global", "KZ", "RU")
    requires_auth = True
    required_credentials = (AFFID_KEY,)
    access_mode = AccessMode.API
    terms_url = "https://www.careerjet.com/partners/api/"
    attribution = "Jobs by Careerjet (careerjet.com)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    min_interval = RUN_EVERY
    requests_per_run = REQUESTS_PER_RUN

    def params(self, search: KeyedSearch) -> dict[str, str]:
        """The query string, credentials read at call time."""
        params = {
            "affid": credential(self.slug, AFFID_KEY),
            "keywords": search.keywords,
            "locale_code": LOCALES.get(search.country or "", DEFAULT_LOCALE),
            "user_agent": settings.user_agent,
            "pagesize": str(PAGE_SIZE),
            "page": "1",
            "sort": "date",
        }
        if search.area:
            params["location"] = search.area
        user_ip = config_value(USER_IP_KEY)
        if user_ip:
            params["user_ip"] = user_ip
        return params

    async def fetch(self, search: KeyedSearch) -> list[RawPosting]:
        """The first page for one search."""
        payload = await self.http.get_json(API_URL, params=self.params(search))
        try:
            response = CareerjetResponse.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе нет поля type — форма API изменилась",
                source_slug=self.slug,
            ) from exc
        if response.type == "ERROR":
            raise SourceError(
                f"{self.slug}: API ответил ошибкой: {response.error[:200]}",
                source_slug=self.slug,
            )
        if response.type != "JOBS":
            logger.info("sources.careerjet.no_jobs", type=response.type, search=search.label)
            return []
        postings = [self._posting(item) for item in response.jobs]
        return [posting for posting in postings if posting is not None]

    def publisher_of(self, raw: dict[str, Any]) -> str | None:
        """The site Careerjet found the posting on."""
        value = raw.get("site")
        if not isinstance(value, str):
            return None
        return value.strip()[:200] or None

    def _posting(self, item: dict[str, Any]) -> RawPosting | None:
        """One result as a posting, or None with the reason logged."""
        try:
            job = CareerjetJob.model_validate(item)
        except ValidationError as exc:
            logger.warning("sources.careerjet.item_invalid", errors=exc.error_count())
            return None
        derived = Derived(
            location=collapse(job.locations, MAX_LOCATION),
            published_at=parse_timestamp(job.date),
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_currency=job.salary_currency_code or None,
            salary_period=PERIODS.get(job.salary_type.upper()),
        )
        return build_posting(
            slug=self.slug,
            external_id=stable_id(job.url),
            url=job.url,
            title=job.title,
            company=job.company,
            description=job.description,
            raw=item,
            derived=derived,
        )

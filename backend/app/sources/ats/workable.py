"""Workable: the public widget endpoint, one request per account.

**Why this endpoint and not the one in docs/SOURCES.md.** The catalogue lists
``apply.workable.com/api/v1/jobs?token={account}``. That ``token`` is not the
account's public name: Workable's documented jobs API is the employer's own
account API and needs an access token the employer issues from its settings,
which a job seeker does not have and must not ask for. What every Workable
careers page *does* expose without a credential is the endpoint its embeddable
job widget calls:

``GET https://apply.workable.com/api/v1/widget/accounts/{account}``

It returns ``{"name": ..., "description": ..., "jobs": [...]}`` — every
published job of the account, no pagination. ``details=true`` is sent so the
entries carry their ``description``; that parameter is how the older
``www.workable.com/api/accounts/{account}`` widget endpoint behaved and is
**not verified** on this one, which is why ``description`` is optional below and
a missing one only costs the posting its body. Re-check on the first live run.

Workable states the city in a structured field (``city``, and per location in
``locations[]``) and remote as ``telecommuting``, so both reach ``_derived``.
"""

from typing import Annotated, Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from app.sources.ats._common import NONE_AS_EMPTY, AtsJob, AtsSource, BoardPage, CompanyBoard
from app.sources.registry import register_source

API_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}"
TERMS_URL = "https://www.workable.com/terms-of-service"


class _Location(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    city: str | None = None
    region: str | None = None
    country: str | None = None
    country_code: str | None = Field(default=None, alias="countryCode")


class WorkableJob(BaseModel):
    """One ``jobs`` entry. shortcode, title and url are required."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: The posting's id, also the last segment of its public URL.
    shortcode: str = Field(min_length=1)
    title: str
    url: str
    description: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    telecommuting: bool = False
    department: str | None = None
    function: str | None = None
    employment_type: str | None = None
    locations: Annotated[tuple[_Location, ...], NONE_AS_EMPTY] = ()


class _Account(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str | None = None
    # Any: each entry is validated on its own by WorkableJob.
    jobs: tuple[dict[str, Any], ...]


@register_source
class WorkableSource(AtsSource):
    """Workable careers-page widget endpoint: public, no key, read-only.

    This is the endpoint Workable's own embeddable job widget reads from an
    employer's public careers page; it is not part of the token-gated account
    API and needs no credential. It is less formally documented than the other
    five, so the governing terms are Workable's general terms of service.
    Limits: none published for it as known when this was written (network
    closed, not re-read live); we stay at about one request a second and one
    request per account per run.
    Attribution: none required; each posting links to apply.workable.com.
    Restrictions: no account API, no employer token, nothing beyond what the
    careers page itself shows. Terms: https://www.workable.com/terms-of-service
    """

    slug: ClassVar[str] = "workable"
    name: ClassVar[str] = "Workable"
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: no credit line is asked for; the URL is the employer's own page.
    attribution: ClassVar[str | None] = None

    def board_url(self, board: CompanyBoard) -> str:
        """The account's widget endpoint."""
        return API_URL.format(account=quote(board.slug, safe=""))

    def board_params(self, board: CompanyBoard) -> dict[str, str] | None:
        """``details=true``: descriptions inline (unverified; module docstring)."""
        return {"details": "true"}

    def unwrap(self, payload: Any) -> BoardPage:
        """The ``jobs`` array."""
        return BoardPage(entries=_Account.model_validate(payload).jobs)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One entry, with the first stated city as the structured one."""
        job = WorkableJob.model_validate(entry)
        city = job.city or next((place.city for place in job.locations if place.city), None)
        parts = [job.city, job.state, job.country]
        if not any(parts) and job.locations:
            first = job.locations[0]
            parts = [first.city, first.region, first.country]
        location = ", ".join(dict.fromkeys(part for part in parts if part)) or None
        return AtsJob(
            job_id=job.shortcode,
            title=job.title,
            url=job.url,
            description=job.description,
            location=location,
            city=city,
            remote=job.telecommuting,
            tags=tuple(
                value for value in (job.department, job.function, job.employment_type) if value
            ),
        )

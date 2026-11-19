"""Ashby: the public job posting API, one request per board.

``GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true``
returns ``{"apiVersion": "1", "jobs": [...]}`` with every posting on the board.
``includeCompensation=true`` adds the ``compensation`` block — the salary range
Ashby employers often publish and LinkedIn copies of the same posting often
drop — which stays in ``raw`` for normalisation to read.

Ashby is the one vendor here with both a structured remote flag (``isRemote``)
and a structured city (``address.postalAddress.addressLocality``), so its
postings reach the fingerprint with a city. ``isListed: false`` marks a posting
the employer keeps off its public board; those are skipped, not stored.
"""

from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.sources.ats._common import AtsJob, AtsSource, BoardPage, CompanyBoard
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{name}"
TERMS_URL = "https://developers.ashbyhq.com/docs/public-job-posting-api"


class _PostalAddress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    locality: str | None = Field(default=None, alias="addressLocality")
    region: str | None = Field(default=None, alias="addressRegion")
    country: str | None = Field(default=None, alias="addressCountry")


class _Address(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    postal_address: _PostalAddress | None = Field(default=None, alias="postalAddress")


class AshbyJob(BaseModel):
    """One ``jobs`` entry. id, title and job URL are required."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1)
    title: str
    job_url: str = Field(alias="jobUrl")
    description_html: str | None = Field(default=None, alias="descriptionHtml")
    location: str | None = None
    department: str | None = None
    team: str | None = None
    employment_type: str | None = Field(default=None, alias="employmentType")
    is_remote: bool | None = Field(default=None, alias="isRemote")
    #: "Remote", "Hybrid", "OnSite" in the docs' examples.
    workplace_type: str | None = Field(default=None, alias="workplaceType")
    is_listed: bool = Field(default=True, alias="isListed")
    address: _Address | None = None


class _Board(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    # Any: each entry is validated on its own by AshbyJob.
    jobs: tuple[dict[str, Any], ...]


@register_source
class AshbySource(AtsSource):
    """Ashby public job posting API: no key, read-only, the employer's own board.

    Ashby documents this endpoint for employers who render their job board on
    their own site; it needs no authentication and returns only postings the
    employer has published.
    Limits: none published for it in the docs as known when this was written
    (network closed, not re-read live); we stay at about one request a second
    and one request per board per run.
    Attribution: none required; each posting links to its jobs.ashbyhq.com
    page. Compensation is published by the employer and shown as such.
    Terms: https://developers.ashbyhq.com/docs/public-job-posting-api
    """

    slug: ClassVar[str] = "ashby"
    name: ClassVar[str] = "Ashby"
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: no credit line is asked for; the URL is the employer's own page.
    attribution: ClassVar[str | None] = None

    def board_url(self, board: CompanyBoard) -> str:
        """The board's posting-api endpoint."""
        return API_URL.format(name=quote(board.slug, safe=""))

    def board_params(self, board: CompanyBoard) -> dict[str, str] | None:
        """``includeCompensation=true``: the salary block, where published."""
        return {"includeCompensation": "true"}

    def unwrap(self, payload: Any) -> BoardPage:
        """The ``jobs`` array."""
        return BoardPage(entries=_Board.model_validate(payload).jobs)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One entry; None for a posting the employer has not listed."""
        job = AshbyJob.model_validate(entry)
        if not job.is_listed:
            logger.debug("sources.ashby.unlisted", board=board.slug, job_id=job.id)
            return None
        postal = job.address.postal_address if job.address else None
        return AtsJob(
            job_id=job.id,
            title=job.title,
            url=job.job_url,
            description=job.description_html,
            location=job.location,
            city=postal.locality if postal else None,
            remote=bool(job.is_remote) or (job.workplace_type or "").casefold() == "remote",
            tags=tuple(value for value in (job.department, job.team, job.employment_type) if value),
        )

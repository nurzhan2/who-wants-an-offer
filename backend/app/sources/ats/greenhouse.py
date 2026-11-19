"""Greenhouse: the public Job Board API, one request per board.

``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
returns every published job on the board in one response: ``content=true`` adds
the description, offices and departments, which is what makes one request per
board enough. There is no pagination and no search parameter.

Two shape facts worth knowing before touching the code:

* ``content`` is **entity-encoded HTML** (``&lt;p&gt;``), not HTML. It is stored
  exactly as returned — decoding is normalisation's job, done once — and
  unescaped only inside the keyword filter (``_common.plain_text``).
* ``location.name`` is the employer's free text ("Remote - EMEA", "NYC").
  Greenhouse has no structured city on this endpoint, so ``_derived`` carries
  no ``city`` and the fingerprint keys these postings without one.
"""

from typing import Annotated, Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from app.sources.ats._common import NONE_AS_EMPTY, AtsJob, AtsSource, BoardPage, CompanyBoard
from app.sources.registry import register_source

API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
TERMS_URL = "https://developers.greenhouse.io/job-board.html"


class _Named(BaseModel):
    """A department or an office: only the name is used."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = ""
    #: Offices carry a free-text location of their own.
    location: str | None = None


class _Location(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = ""


class GreenhouseJob(BaseModel):
    """One ``jobs`` entry. id, title and link are required; the rest default."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    title: str
    absolute_url: str
    #: Entity-encoded HTML; see the module docstring.
    content: str | None = None
    location: _Location | None = None
    departments: Annotated[tuple[_Named, ...], NONE_AS_EMPTY] = ()
    offices: Annotated[tuple[_Named, ...], NONE_AS_EMPTY] = ()
    updated_at: str | None = None


class _Board(BaseModel):
    """The envelope: ``{"jobs": [...], "meta": {"total": n}}``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Any: each entry is validated on its own by GreenhouseJob.
    jobs: tuple[dict[str, Any], ...]
    meta: dict[str, Any] | None = None


@register_source
class GreenhouseSource(AtsSource):
    """Greenhouse Job Board API: public, read-only, no key for GET requests.

    The Job Board API is the endpoint Greenhouse documents for employers to
    publish their own boards elsewhere; GET requests need no authentication
    and the data is each employer's own published postings.
    Limits: none published for these GET endpoints in the documentation as
    known when this was written (network closed, not re-read live); Greenhouse
    serves them from a cache, and we stay at about one request a second and one
    request per board per run.
    Attribution: none required by the docs; every posting links to the
    employer's own Greenhouse-hosted page, which is the credit that matters.
    Restrictions: read-only use of published postings; applying through the
    POST endpoint needs the employer's key and is not something this backend
    does. Terms: https://developers.greenhouse.io/job-board.html
    """

    slug: ClassVar[str] = "greenhouse"
    name: ClassVar[str] = "Greenhouse"
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: the docs ask for no credit line; the posting URL is the employer's.
    attribution: ClassVar[str | None] = None

    def board_url(self, board: CompanyBoard) -> str:
        """The board's jobs endpoint."""
        return API_URL.format(token=quote(board.slug, safe=""))

    def board_params(self, board: CompanyBoard) -> dict[str, str] | None:
        """``content=true``: descriptions, offices and departments inline."""
        return {"content": "true"}

    def unwrap(self, payload: Any) -> BoardPage:
        """The ``jobs`` array and the board's own total."""
        board = _Board.model_validate(payload)
        total = board.meta.get("total") if board.meta else None
        return BoardPage(entries=board.jobs, stated_total=total if isinstance(total, int) else None)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One entry, with offices' locations as the fallback location line."""
        job = GreenhouseJob.model_validate(entry)
        location = job.location.name if job.location else ""
        if not location.strip():
            location = "; ".join(office.location or office.name for office in job.offices)
        return AtsJob(
            job_id=str(job.id),
            title=job.title,
            url=job.absolute_url,
            description=job.content,
            location=location or None,
            tags=tuple(
                name
                for name in (*(d.name for d in job.departments), *(o.name for o in job.offices))
                if name
            ),
        )

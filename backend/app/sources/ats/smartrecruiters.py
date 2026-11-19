"""SmartRecruiters: the public Posting API, paginated, with a detail request.

``GET https://api.smartrecruiters.com/v1/companies/{company}/postings`` returns
``{"offset", "limit", "totalFound", "content": [...]}``, at most 100 entries a
page, paged with ``offset``. It is the only vendor here that paginates, so it
is the only one overriding ``fetch_board``.

The list entries carry the title, the department, a **structured location**
(``location.city``, ``location.remote``) and no description. The body lives at
``/postings/{id}`` as ``jobAd.sections`` — company description, job
description, qualifications, additional information — so this connector sets
``needs_detail_fetch``: the runner calls :meth:`fetch_detail` only for postings
that already passed the keyword filter, which keeps a large board from costing
one request per posting. The filter therefore matches the title, department
and function only; a posting whose title says nothing of the skill is missed.
That is the price of not reading every detail page, and it is stated rather
than hidden.

An unknown company is believed to answer 200 with an empty ``content`` rather
than 404 — unmeasured; the verification script prints the count, and a board
at zero is the one to look at.
"""

from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import RateLimitError, SourceError
from app.core.logging import get_logger
from app.sources.ats._common import (
    GONE_STATUSES,
    AtsJob,
    AtsSource,
    BoardPage,
    CompanyBoard,
    heading,
    join_html,
)
from app.sources.base import RawPosting
from app.sources.registry import register_source

logger = get_logger(__name__)

API_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}"
#: The public page of a posting: the candidate-facing URL, not the API's.
PUBLIC_URL = "https://jobs.smartrecruiters.com/{company}/{posting}"
TERMS_URL = "https://developers.smartrecruiters.com/docs/posting-api"

#: The page size the API documents as its maximum.
PAGE_SIZE = 100
#: Ceiling on pages per board: 2000 postings, far past any board in the YAML,
#: so a vendor that never stops answering cannot become an endless loop.
MAX_PAGES = 20

#: ``jobAd.sections`` keys, in the order the public page shows them.
SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


class _Label(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str | None = None


class _Location(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    city: str | None = None
    region: str | None = None
    country: str | None = None
    remote: bool = False
    full_location: str | None = Field(default=None, alias="fullLocation")


class _Company(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: str | None = None
    name: str | None = None


class SmartRecruitersPosting(BaseModel):
    """One ``content`` entry. id and name are required."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1)
    name: str
    company: _Company | None = None
    location: _Location | None = None
    department: _Label | None = None
    function: _Label | None = None
    type_of_employment: _Label | None = Field(default=None, alias="typeOfEmployment")
    released_date: str | None = Field(default=None, alias="releasedDate")


class _Page(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    # Any: each entry is validated on its own by SmartRecruitersPosting.
    content: tuple[dict[str, Any], ...]
    total_found: int | None = Field(default=None, alias="totalFound")


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str | None = None
    #: HTML.
    text: str | None = None


class _JobAd(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    sections: dict[str, _Section] = Field(default_factory=dict)


class _Detail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    job_ad: _JobAd | None = Field(default=None, alias="jobAd")


@register_source
class SmartRecruitersSource(AtsSource):
    """SmartRecruiters Posting API: public, no key, the employer's published ads.

    SmartRecruiters documents the Posting API for employers who show their
    openings on their own careers site; listing and reading published postings
    needs no authentication.
    Limits: none published for these public GETs in the docs as known when this
    was written (network closed, not re-read live); we stay at about one
    request a second, page through a board once per run and read a posting's
    detail only after it matched the keywords.
    Attribution: none required; each posting links to jobs.smartrecruiters.com.
    Terms: https://developers.smartrecruiters.com/docs/posting-api
    """

    slug: ClassVar[str] = "smartrecruiters"
    name: ClassVar[str] = "SmartRecruiters"
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: no credit line is asked for; the URL is the employer's own page.
    attribution: ClassVar[str | None] = None
    #: The list has no description; see the module docstring.
    needs_detail_fetch: ClassVar[bool] = True

    def board_url(self, board: CompanyBoard) -> str:
        """The company's postings endpoint."""
        return API_URL.format(company=quote(board.slug, safe=""))

    def unwrap(self, payload: Any) -> BoardPage:
        """One page's ``content`` and the stated total."""
        page = _Page.model_validate(payload)
        return BoardPage(entries=page.content, stated_total=page.total_found)

    async def fetch_board(
        self, board: CompanyBoard, *, max_requests: int | None = None
    ) -> BoardPage:
        """Page through the board with ``offset`` until it is exhausted.

        Stops on an empty page, on reaching ``totalFound``, after
        ``MAX_PAGES``, or after ``max_requests`` when the caller caps it.
        """
        pages = min(MAX_PAGES, max_requests) if max_requests else MAX_PAGES
        entries: list[dict[str, Any]] = []
        total: int | None = None
        for number in range(pages):
            payload = await self.http.get_json(
                self.board_url(board),
                params={"limit": str(PAGE_SIZE), "offset": str(number * PAGE_SIZE)},
            )
            page = self.envelope(payload, board)
            total = page.stated_total if page.stated_total is not None else total
            entries.extend(page.entries)
            if not page.entries or (total is not None and len(entries) >= total):
                break
        return BoardPage(entries=tuple(entries), stated_total=total)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One list entry; the description comes later, from the detail."""
        posting = SmartRecruitersPosting.model_validate(entry)
        place = posting.location or _Location()
        company = (posting.company.identifier if posting.company else None) or board.slug
        location = place.full_location or ", ".join(
            part for part in (place.city, place.region, place.country) if part
        )
        return AtsJob(
            job_id=posting.id,
            title=posting.name,
            url=PUBLIC_URL.format(
                company=quote(company, safe=""), posting=quote(posting.id, safe="")
            ),
            location=location or None,
            city=place.city,
            remote=place.remote,
            tags=tuple(
                label.label
                for label in (posting.department, posting.function, posting.type_of_employment)
                if label is not None and label.label
            ),
        )

    async def fetch_detail(self, posting: RawPosting) -> RawPosting:
        """The posting with ``jobAd.sections`` joined into its description.

        A detail that is gone or broken leaves the posting as the list described
        it — a stub with a title and a link, which is still a job the owner can
        open — rather than failing the run for one ad. Being told to slow down
        is the exception, as it is for the board walk.
        """
        derived = posting.raw.get("_derived")
        board = derived.get("board") if isinstance(derived, dict) else None
        posting_id = posting.raw.get("id")
        if not isinstance(board, str) or not isinstance(posting_id, str | int):
            return posting
        url = DETAIL_URL.format(
            company=quote(board, safe=""), posting=quote(str(posting_id), safe="")
        )
        try:
            payload = await self.http.get_json(url)
        except RateLimitError:
            raise
        except SourceError as exc:
            status = exc.extra.get("response_status")
            if status == 429:
                raise
            logger.warning(
                "sources.smartrecruiters.detail_unavailable",
                external_id=posting.external_id,
                status=status,
                gone=status in GONE_STATUSES,
            )
            return posting
        try:
            detail = _Detail.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "sources.smartrecruiters.detail_invalid",
                external_id=posting.external_id,
                errors=exc.error_count(),
            )
            return posting
        sections = detail.job_ad.sections if detail.job_ad else {}
        body = join_html(
            *(
                join_html(heading(sections[key].title), sections[key].text)
                for key in SECTION_ORDER
                if key in sections
            )
        )
        raw = dict(posting.raw)
        raw["_detail"] = payload if isinstance(payload, dict) else None
        return posting.with_detail(description=body or posting.description, raw=raw)

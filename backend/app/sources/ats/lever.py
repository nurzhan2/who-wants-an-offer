"""Lever: the public Postings API, one request per board.

``GET https://api.lever.co/v0/postings/{company}?mode=json`` returns a bare JSON
**array** of the company's published postings. Without ``limit`` and ``skip``
it returns all of them, so one request per board is the whole board.

Lever splits one posting's body across three fields: ``description`` (the
opening), ``lists`` (titled bullet lists — requirements, responsibilities,
benefits) and ``additional`` (the closing). Matching on ``description`` alone
would miss the requirements, which is where the skills are, so the stored
description is the three joined in the order Lever renders them. Each fragment
is kept byte for byte; only a list's title, which Lever sends as plain text, is
wrapped in an escaped ``<h3>``.

``categories.location`` is free text and there is no structured city, so
``_derived`` carries none. ``workplaceType`` (``remote``/``hybrid``/``on-site``)
is the structured remote flag.
"""

from typing import Annotated, Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.sources.ats._common import (
    NONE_AS_EMPTY,
    AtsJob,
    AtsSource,
    BoardPage,
    CompanyBoard,
    heading,
    join_html,
)
from app.sources.registry import register_source

API_URL = "https://api.lever.co/v0/postings/{company}"
TERMS_URL = "https://github.com/lever/postings-api"

# Any: each entry is validated on its own by LeverPosting.
_ENTRIES: TypeAdapter[tuple[dict[str, Any], ...]] = TypeAdapter(tuple[dict[str, Any], ...])


class _Categories(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    commitment: str | None = None
    department: str | None = None
    location: str | None = None
    team: str | None = None
    all_locations: Annotated[tuple[str, ...], NONE_AS_EMPTY] = Field(
        default=(), alias="allLocations"
    )


class _List(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str = ""
    #: HTML ``<li>`` items, without the enclosing ``<ul>``.
    content: str = ""


class LeverPosting(BaseModel):
    """One posting. id, title and hosted URL are required."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1)
    text: str
    hosted_url: str = Field(alias="hostedUrl")
    description: str | None = None
    lists: Annotated[tuple[_List, ...], NONE_AS_EMPTY] = ()
    additional: str | None = None
    categories: _Categories | None = None
    workplace_type: str | None = Field(default=None, alias="workplaceType")
    #: Unix milliseconds.
    created_at: int | None = Field(default=None, alias="createdAt")


@register_source
class LeverSource(AtsSource):
    """Lever Postings API: public, read-only, no key for listing postings.

    The Postings API is documented by Lever for employers who build their own
    careers pages; listing published postings needs no key, and the data is the
    employer's own.
    Limits: the docs as known when this was written (network closed, not
    re-read live) state no number for the GET listing; we stay at about one
    request a second and one request per board per run. The POST apply
    endpoint is separately rate limited and needs a key; this backend never
    calls it.
    Attribution: none required; each posting links to its jobs.lever.co page.
    Terms: https://github.com/lever/postings-api
    """

    slug: ClassVar[str] = "lever"
    name: ClassVar[str] = "Lever"
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: no credit line is asked for; the URL is the employer's own page.
    attribution: ClassVar[str | None] = None

    def board_url(self, board: CompanyBoard) -> str:
        """The company's postings endpoint."""
        return API_URL.format(company=quote(board.slug, safe=""))

    def board_params(self, board: CompanyBoard) -> dict[str, str] | None:
        """``mode=json``: without it the endpoint answers HTML."""
        return {"mode": "json"}

    def unwrap(self, payload: Any) -> BoardPage:
        """The array itself: Lever sends no envelope."""
        entries = _ENTRIES.validate_python(payload)
        return BoardPage(entries=entries)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One posting, with its three body fields joined."""
        posting = LeverPosting.model_validate(entry)
        categories = posting.categories or _Categories()
        location = categories.location or "; ".join(categories.all_locations) or None
        sections = [
            join_html(heading(item.text), f"<ul>{item.content}</ul>" if item.content else None)
            for item in posting.lists
        ]
        return AtsJob(
            job_id=posting.id,
            title=posting.text,
            url=posting.hosted_url,
            description=join_html(posting.description, *sections, posting.additional),
            location=location,
            remote=(posting.workplace_type or "").casefold() == "remote",
            tags=tuple(
                value
                for value in (categories.department, categories.team, categories.commitment)
                if value
            ),
        )

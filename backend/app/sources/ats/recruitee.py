"""Recruitee: the public Careers Site API, one request per company.

``GET https://{company}.recruitee.com/api/offers/`` returns
``{"offers": [...]}``: every published offer, no pagination. The company's
board token is a **subdomain** here rather than a path segment, so it is held
to a DNS label before any request is built — a token with a dot or an
underscore would otherwise point the request at a different host.

The body is split between ``description`` and ``requirements``; both are
joined, byte for byte, because the requirements are where the skills are.
``city`` is a structured field and ``remote`` a structured flag, so both reach
``_derived``.
"""

import re
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import SourceError
from app.sources.ats._common import (
    NONE_AS_EMPTY,
    AtsJob,
    AtsSource,
    BoardPage,
    CompanyBoard,
    join_html,
)
from app.sources.registry import register_source

API_URL = "https://{company}.recruitee.com/api/offers/"
TERMS_URL = "https://docs.recruitee.com/reference/intro-to-careers-site-api"

#: One DNS label, lower case: what a Recruitee subdomain can be.
_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class RecruiteeOffer(BaseModel):
    """One ``offers`` entry. id, title and careers URL are required."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    title: str
    careers_url: str
    description: str | None = None
    requirements: str | None = None
    location: str | None = None
    city: str | None = None
    country: str | None = None
    remote: bool | None = None
    department: str | None = None
    employment_type_code: str | None = None
    tags: Annotated[tuple[str, ...], NONE_AS_EMPTY] = Field(default=())
    published_at: str | None = None


class _Offers(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    # Any: each entry is validated on its own by RecruiteeOffer.
    offers: tuple[dict[str, Any], ...]


@register_source
class RecruiteeSource(AtsSource):
    """Recruitee Careers Site API: public, no key, the employer's published offers.

    Recruitee documents the Careers Site API for employers who build their own
    careers page; it needs no authentication and returns only offers the
    employer has published.
    Limits: none published for it in the docs as known when this was written
    (network closed, not re-read live); we stay at about one request a second
    and one request per company per run.
    Attribution: none required; each offer links to the employer's
    recruitee.com careers page. Terms:
    https://docs.recruitee.com/reference/intro-to-careers-site-api
    """

    slug: ClassVar[str] = "recruitee"
    name: ClassVar[str] = "Recruitee"
    regions: ClassVar[tuple[str, ...]] = ("EU", "remote", "KZ", "CIS")
    terms_url: ClassVar[str | None] = TERMS_URL
    #: None: no credit line is asked for; the URL is the employer's own page.
    attribution: ClassVar[str | None] = None

    def board_url(self, board: CompanyBoard) -> str:
        """The company's offers endpoint, on its own subdomain."""
        if not _SUBDOMAIN.match(board.slug):
            raise SourceError(
                f"{self.slug}: «{board.slug}» не годится как поддомен recruitee.com",
                source_slug=self.slug,
            )
        return API_URL.format(company=board.slug)

    def unwrap(self, payload: Any) -> BoardPage:
        """The ``offers`` array."""
        return BoardPage(entries=_Offers.model_validate(payload).offers)

    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One offer, with its requirements joined to its description."""
        offer = RecruiteeOffer.model_validate(entry)
        return AtsJob(
            job_id=str(offer.id),
            title=offer.title,
            url=offer.careers_url,
            description=join_html(offer.description, offer.requirements),
            location=offer.location,
            city=offer.city,
            remote=bool(offer.remote),
            tags=tuple(
                value
                for value in (offer.department, offer.employment_type_code, *offer.tags)
                if value
            ),
        )

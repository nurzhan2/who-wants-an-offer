"""``POST /vacancies/import``: one vacancy the owner found by hand.

docs/SOURCES.md, «`linkedin_manual` — ручной импорт». The third legal path to
LinkedIn, Indeed and Glassdoor, and a door for any posting no connector covers:
the owner pastes a link, a text, or both, and the vacancy joins the common
scoring under ``source_slug = linkedin_manual``.

**The limits are code, not intent.**

* One URL per call, no recursion: the page is fetched once and nothing it links
  to is followed.
* LinkedIn, Indeed and Glassdoor pages are **never** fetched — not even this
  one. ``BLOCKED_HOSTS`` refuses them at the transport, and this service asks
  for the text instead of trying: paste the posting, keep the link.
* No cookies, no authorisation headers: the fetch goes through the anonymous
  shared client, as a CRAWL source, so the host's robots.txt decides.
* The CSV of LinkedIn's own data export ("Saved jobs") is read as rows of
  links and titles; nothing is fetched for it.

A fetched page is read for a schema.org ``JobPosting`` first — the structured
block job boards publish for search engines — and only then as text through the
same post parser Telegram uses, with the same rule-based fallback.
"""

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ParsingError, SourceError
from app.core.logging import get_logger
from app.db.repositories.vacancy import VacancyRepository
from app.llm.router import LLMRouter, get_router
from app.normalize.sync import sync_requirements
from app.pipeline.runner import to_vacancy
from app.sources._telegram_posts import ParsedPost, fallback_parse, parse_post
from app.sources.base import AccessMode, BaseSource, RateLimit, RawPosting, SearchQuery
from app.sources.hh import strip_html
from app.sources.http import BLOCKED_HOSTS, get_client

logger = get_logger(__name__)

SLUG = "linkedin_manual"
MAX_TEXT = 20_000
MAX_CSV_ROWS = 500

_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)


class _ManualFetch(BaseSource):
    """Not a connector: the identity the one fetched page is requested under.

    Never registered, so no pipeline run ever calls it. CRAWL, so the shared
    client checks the host's robots.txt before the request.
    """

    slug: ClassVar[str] = SLUG
    name: ClassVar[str] = "Ручной импорт"
    access_mode: ClassVar[AccessMode] = AccessMode.CRAWL
    rate_limit: ClassVar[RateLimit] = RateLimit(requests_per_second=0.5)

    def search(self, query: SearchQuery) -> Any:  # pragma: no cover - never searched
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Imported:
    """What one import stored."""

    vacancy_id: UUID
    created: bool
    title: str
    company: str | None
    parsed_by: str


def blocked(url: str) -> bool:
    """Whether the transport refuses this host whatever anyone declares."""
    host = (httpx.URL(url).host or "").lower()
    return any(host == rule or host.endswith("." + rule) for rule in BLOCKED_HOSTS)


async def import_one(
    session: AsyncSession,
    *,
    url: str | None,
    text: str | None,
    router: LLMRouter | None = None,
) -> Imported:
    """Store one vacancy from a link, a pasted text, or both."""
    url = (url or "").strip() or None
    text = (text or "").strip() or None
    if url is None and text is None:
        raise ParsingError("нужна ссылка на вакансию или её текст")
    if url is not None and not re.match(r"^https://", url):
        raise ParsingError("ссылка должна начинаться с https://")
    if text is not None and len(text) > MAX_TEXT:
        raise ParsingError(f"текст длиннее {MAX_TEXT} знаков — вставьте только саму вакансию")

    parsed: ParsedPost | None = None
    description = text
    how = "rules"
    if text is None and url is not None:
        if blocked(url):
            raise ParsingError(
                "Страницы LinkedIn, Indeed и Glassdoor проект не открывает (их условия запрещают "
                "автосбор). Вставьте текст вакансии вместе со ссылкой."
            )
        page = await _fetch(url)
        parsed, description = _job_posting(page)
        if parsed is not None:
            how = "schema.org"
        else:
            description = strip_html(page)
    if parsed is None:
        parsed, how = await parse_post(
            description or "", router=router if router is not None else _router()
        )
    if not parsed.is_vacancy or not parsed.title:
        parsed = fallback_parse(description or url or "")

    digest = hashlib.sha256((url or text or "").encode("utf-8")).hexdigest()[:32]
    posting = RawPosting(
        source_slug=SLUG,
        external_id=f"{'url' if url else 'text'}:{digest}",
        url=url or f"https://manual.invalid/{digest}",
        title=(parsed.title or "Вакансия")[:300],
        company=parsed.company,
        description=description,
        raw={
            "_derived": {
                "city": parsed.city,
                "remote": parsed.remote,
                "skills": parsed.skills,
                "apply_contact": parsed.apply_contact,
                "parsed_by": how,
                "imported_by_hand": True,
            }
        },
    )
    result = await VacancyRepository(session).upsert_by_external_id(
        to_vacancy(posting),
        source_slug=SLUG,
        external_id=posting.external_id,
        url=posting.url,
        raw=posting.raw,
    )
    await sync_requirements(session, vacancy_ids=[result.vacancy_id])
    logger.info("vacancies.imported", created=result.created, parsed_by=how, fetched=text is None)
    return Imported(
        vacancy_id=result.vacancy_id,
        created=result.created,
        title=posting.title,
        company=posting.company,
        parsed_by=how,
    )


async def import_csv(session: AsyncSession, content: bytes) -> list[Imported]:
    """Rows of LinkedIn's "Saved jobs" export, as stubs. Nothing is fetched.

    The header names vary between exports, so the usual spellings of a title,
    a company, a link and a location are all accepted; a row without a title
    and an https link is skipped.
    """
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    except UnicodeDecodeError as exc:
        raise ParsingError("CSV должен быть в UTF-8") from exc
    stored: list[Imported] = []
    for index, row in enumerate(reader):
        if index >= MAX_CSV_ROWS:
            break
        fields = {
            key.strip().casefold(): (value or "").strip() for key, value in row.items() if key
        }
        title = _first(fields, "job title", "title", "position")
        url = _first(fields, "job url", "url", "link", "job link")
        if not title or not url or not url.startswith("https://"):
            continue
        company = _first(fields, "company name", "company", "employer")
        location = _first(fields, "location", "job location")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        posting = RawPosting(
            source_slug=SLUG,
            external_id=f"url:{digest}",
            url=url[:1000],
            title=title[:300],
            company=company[:200] if company else None,
            raw={"_derived": {"location": location, "imported_by_hand": True, "from_csv": True}},
        )
        result = await VacancyRepository(session).upsert_by_external_id(
            to_vacancy(posting),
            source_slug=SLUG,
            external_id=posting.external_id,
            url=posting.url,
            raw=posting.raw,
        )
        stored.append(
            Imported(result.vacancy_id, result.created, posting.title, posting.company, "csv")
        )
    if stored:
        await sync_requirements(session, vacancy_ids=[item.vacancy_id for item in stored])
    return stored


def _first(fields: dict[str, str], *names: str) -> str | None:
    for name in names:
        if fields.get(name):
            return fields[name]
    return None


def _router() -> LLMRouter | None:
    try:
        return get_router()
    except AppError:  # pragma: no cover - a router that cannot be built
        logger.warning("vacancies.import_without_model")
        return None


async def _fetch(url: str) -> str:
    """The one page, through the anonymous shared client, robots.txt checked."""
    http = get_client().bind(_ManualFetch())
    try:
        return await http.get_text(url)
    except SourceError as exc:
        raise ParsingError(f"страница не загрузилась: {exc.detail}") from exc


def _job_posting(page: str) -> tuple[ParsedPost | None, str | None]:
    """A schema.org JobPosting block, if the page publishes one."""
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                organisation = item.get("hiringOrganization")
                company = organisation.get("name") if isinstance(organisation, dict) else None
                place = item.get("jobLocation")
                place = place[0] if isinstance(place, list) and place else place
                address = place.get("address") if isinstance(place, dict) else None
                city = address.get("addressLocality") if isinstance(address, dict) else None
                description = strip_html(str(item.get("description") or "")) or None
                remote = "full" if item.get("jobLocationType") == "TELECOMMUTE" else None
                return (
                    ParsedPost(
                        is_vacancy=True,
                        title=str(item.get("title") or "")[:300] or None,
                        company=str(company)[:200] if company else None,
                        city=str(city)[:120] if city else None,
                        remote=remote,
                    ),
                    description,
                )
    return None, None

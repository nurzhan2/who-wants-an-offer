"""Shared pieces of the remote-board feeds and the keyed aggregators.

Not a connector. The leading underscore keeps registry discovery from importing
it as one (``registry.load_sources`` skips private modules), and it registers
nothing when a connector imports it.

Two shapes of source live on top of it, and the split is the same one
``arbeitnow`` and ``jsearch`` already draw.

:class:`FeedSource` is a board that cannot be asked a question: one feed, the
same for everybody, fetched once per run and filtered here by the union of the
plan's keywords. The filter is the one ``arbeitnow`` and ``remotive`` use —
:func:`app.sources.base.mentions` over the title, tags and tag-stripped
description — so "Python Developer" as a phrase matches a "Backend Developer
(Python)" posting on every board alike.

:class:`KeyedSource` is an aggregator that takes the keywords upstream: one
request per planned search, a per-run ceiling under the vendor's daily
allowance, and a country resolved from the free-text area with the same
``jsearch_countries.yaml`` table JSearch uses, so a place is mapped to a
country in exactly one file.

Both write what they worked out into ``RawPosting.raw["_derived"]`` through
:class:`Derived`, the seam ``jsearch`` and ``hh`` already use. ``remote`` there
is a :class:`~app.db.enums.RemoteType` value, as it is for those two, so a
pooled ``_derived`` block never carries the same key with two types.
"""

import hashlib
import html
import re
from abc import abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict

from app.core.config import settings
from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType
from app.schemas.crawl import SavedState, SearchPreview, SearchUse
from app.sources.base import (
    PREVIEW_TERMS,
    BaseSource,
    RawPosting,
    SearchQuery,
    distinct_terms,
    mentions,
)
from app.sources.jsearch import CountryAliases, country_for, load_countries

if TYPE_CHECKING:  # pragma: no cover - the runtime import would be a cycle
    from app.sources.http import SourceHTTP

logger = get_logger(__name__)

#: Mirrors of the ``RawPosting`` limits, which mirror the database columns.
#: Display strings are cut to fit; identifiers and URLs are never cut, because
#: a cut id is a different key and a cut URL leads nowhere.
MAX_EXTERNAL_ID = 200
MAX_URL = 1000
MAX_TITLE = 300
MAX_COMPANY = 200
#: A location line is free text; anything longer is a paragraph, not a place.
MAX_LOCATION = 200
#: The longest keyword text a keyed source sends upstream.
MAX_QUERY_CHARS = 200

#: Markup is stripped for keyword matching only — stored descriptions keep it —
#: so a keyword like "a" or "strong" cannot match an element name.
TAG_RE = re.compile(r"<[^>]+>")


def _none_as_empty_tuple(value: Any) -> Any:
    """Read a missing or null list as "no items"."""
    return () if value is None else value


def _none_as_empty_str(value: Any) -> Any:
    """Read a null string as an empty one."""
    return "" if value is None else value


def _amount(value: Any) -> float | None:
    """A salary figure, with ``""``, ``0``, ``null`` and prose all read as "not stated"."""
    return positive(value)


#: A list of strings a vendor sometimes sends as ``null``.
StrTuple = Annotated[tuple[str, ...], BeforeValidator(_none_as_empty_tuple)]
#: A string a vendor sometimes sends as ``null``.
Text = Annotated[str, BeforeValidator(_none_as_empty_str)]
#: A salary bound that may arrive as a number, a numeric string, ``""`` or ``0``.
Amount = Annotated[float | None, BeforeValidator(_amount)]


def strip_tags(markup: str | None) -> str:
    """Plain text out of an HTML fragment: tags removed, entities decoded, spaces collapsed."""
    if not markup:
        return ""
    return " ".join(html.unescape(TAG_RE.sub(" ", markup)).split())


def collapse(value: str | None, limit: int) -> str | None:
    """Whitespace-collapsed text cut to ``limit``, or None when nothing is left."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def positive(value: Any) -> float | None:
    """A salary bound as a number, or None for the ``0`` and ``""`` boards send for "none"."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def as_utc(value: datetime | None) -> datetime | None:
    """An aware UTC datetime. A naive one is read as UTC, which each caller states."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    """Every date shape these boards were documented to send, or None.

    Unix seconds, ISO 8601 (with ``Z`` or an offset, or naive), the
    ``YYYY-MM-DD HH:MM:SS`` some PHP boards print, and RFC 822 as RSS writes
    it. A naive value is read as UTC; the connectors that receive one say so in
    their own docstrings. Anything else is None rather than an exception: a
    posting with an unreadable date is still a posting.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return None


def stable_id(value: str) -> str:
    """A short digest for a source whose only identity is a long URL."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


def config_value(key: str) -> str | None:
    """A non-secret value read from ``SOURCE_CREDENTIALS``, or None when unset.

    ``config.py`` has no per-source field for these and connectors may not add
    one (docs/ENGINEERING.md rule 5), so the one per-source map the settings
    already decode carries them. They are not secrets; they sit there because
    it is the only place a connector can read configuration from without
    editing a file outside ``sources/``.
    """
    stored = settings.source_credentials.get(key)
    if stored is None:
        return None
    return stored.get_secret_value().strip() or None


def credential(slug: str, key: str) -> str:
    """A declared credential's value, read at call time so a test can set it."""
    value = config_value(key)
    if value is None:
        # Guarded by ``unavailable()`` upstream; reaching here is a wiring bug.
        raise SourceError(f"{slug}: не настроен ключ {key} в SOURCE_CREDENTIALS", source_slug=slug)
    return value


class Derived(BaseModel):
    """What a connector worked out about a posting, for ``raw["_derived"]``.

    A model rather than a dict per docs/ENGINEERING.md rule 3. Every field is
    optional and a None is left out of the dump, so a key's absence means "the
    payload did not say", never "false".
    """

    model_config = ConfigDict(frozen=True)

    remote: RemoteType | None = None
    location: str | None = None
    published_at: AwareDatetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    #: ``year``, ``month``, ``hour`` — only where the payload states it.
    salary_period: str | None = None

    def dump(self) -> dict[str, Any]:
        """The JSON block stored with the posting."""
        return self.model_dump(mode="json", exclude_none=True)


def build_posting(
    *,
    slug: str,
    external_id: str,
    url: str,
    title: str,
    company: str | None,
    description: str | None,
    raw: dict[str, Any],
    derived: Derived,
) -> RawPosting | None:
    """A ``RawPosting`` with the column limits applied, or None with the reason logged."""
    external_id = external_id.strip()
    url = url.strip()
    clean_title = collapse(title, MAX_TITLE)
    if not external_id or len(external_id) > MAX_EXTERNAL_ID:
        logger.warning("sources.board_item_skipped", slug=slug, reason="external_id")
        return None
    if not url.startswith(("https://", "http://")) or len(url) > MAX_URL:
        logger.warning(
            "sources.board_item_skipped", slug=slug, reason="url", external_id=external_id
        )
        return None
    if clean_title is None:
        logger.warning(
            "sources.board_item_skipped", slug=slug, reason="title", external_id=external_id
        )
        return None
    payload = dict(raw)
    payload["_derived"] = derived.dump()
    return RawPosting(
        source_slug=slug,
        external_id=external_id,
        url=url,
        title=clean_title,
        company=collapse(company, MAX_COMPANY),
        description=description or None,
        raw=payload,
    )


def text_matches(text: str, keywords: Sequence[str]) -> bool:
    """Whether any keyword occurs in an already casefolded text. No keywords: everything."""
    if not keywords:
        return True
    return any(mentions(text, word) for word in keywords)


def haystack(*parts: str | None) -> str:
    """The casefolded text a keyword is matched against."""
    return " ".join(part for part in parts if part).casefold()


class FeedEntry(BaseModel):
    """One posting and the text the local keyword filter reads."""

    model_config = ConfigDict(frozen=True)

    posting: RawPosting
    #: Casefolded, markup stripped. Built by the connector because only it knows
    #: which fields are prose and which are tags.
    text: str


class FeedSource(BaseSource):
    """A board with no usable search: fetched once per run, filtered here.

    Subclasses implement :meth:`feed`, which yields every posting the board
    serves in the order it serves them. Stopping is this class's business: the
    feed is consumed lazily and closed as soon as the plan's largest ``limit``
    is reached, so a paginated board is not walked past the point anybody
    needs.
    """

    @abstractmethod
    def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Yield every posting on the board, once per run.

        NOT ``async def``, for the reason ``BaseSource.search`` is not.
        """

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """One walk of the feed for the whole plan, filtered by the union of its keywords.

        The base implementation runs ``search`` per query, which re-downloads an
        unsearchable feed once per plan entry; arbeitnow measured 182 requests
        for what one walk returns. A posting matching several queries is
        yielded once.
        """
        keywords = tuple(distinct_terms(queries))
        limit = max((query.limit for query in queries), default=0)
        if not limit:
            return
        seen: set[str] = set()
        scanned = 0
        async with aclosing(self.feed()) as entries:
            async for entry in entries:
                scanned += 1
                key = entry.posting.external_id
                if key in seen or not text_matches(entry.text, keywords):
                    continue
                seen.add(key)
                yield entry.posting
                if len(seen) >= limit:
                    break
        logger.info(
            "sources.feed_filtered",
            slug=self.slug,
            scanned=scanned,
            yielded=len(seen),
            keywords=len(keywords),
        )

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query: the same single walk, with that query's keywords."""
        async for posting in self.search_batch([query]):
            yield posting


class KeyedSearch(BaseModel):
    """One request a keyed aggregator will send: words, place, country."""

    model_config = ConfigDict(frozen=True)

    keywords: str
    area: str | None = None
    #: ISO 3166-1 alpha-2, lower case, or None when the place names no country
    #: the shared table knows.
    country: str | None = None

    @property
    def label(self) -> str:
        """How a person reads this request in the preview."""
        parts = [self.keywords, self.area or "", (self.country or "").upper()]
        return " · ".join(part for part in parts if part)


def keyed_searches(
    queries: Sequence[SearchQuery], countries: Sequence[CountryAliases]
) -> tuple[KeyedSearch, ...]:
    """The plan as an aggregator sees it: one request per distinct (words, place).

    Keywords are joined into one text, because every one of these APIs takes a
    single keyword string. A remote slot with no place says "remote" in the
    words, since none of them has a remote filter.
    """
    searches: dict[KeyedSearch, None] = {}
    for query in queries:
        words = " ".join(query.keywords).strip()
        if not words:
            continue
        if query.remote is RemoteType.FULL and not query.area:
            words = f"{words} remote"
        country = (query.country or "").lower() or country_for(query.area, countries)
        search = KeyedSearch(keywords=words[:MAX_QUERY_CHARS], area=query.area, country=country)
        searches.setdefault(search, None)
    return tuple(searches)


class KeyedSource(BaseSource):
    """An aggregator that takes the keywords upstream, one request per planned search.

    Subclasses declare :attr:`requests_per_run` and implement :meth:`supports`
    and :meth:`fetch`. The base walks the plan in order, skips a search the
    vendor cannot answer (a country it does not cover) without spending a
    request on it, and stops at the run's ceiling.
    """

    #: The most requests one run may send. Under ``daily_quota`` divided by the
    #: number of runs ``min_interval`` allows a day, so a full run never takes
    #: the source past its allowance between the pipeline's start-of-run checks.
    requests_per_run: ClassVar[int] = 1

    def __init__(self, *, http: "SourceHTTP | None" = None) -> None:
        super().__init__(http=http)
        self._countries: tuple[CountryAliases, ...] | None = None

    @property
    def countries(self) -> tuple[CountryAliases, ...]:
        """The shared place-to-country table, read once per instance."""
        if self._countries is None:
            self._countries = load_countries()
        return self._countries

    def supports(self, search: KeyedSearch) -> bool:
        """Whether the vendor can answer this search at all. Everything, by default."""
        return True

    @abstractmethod
    async def fetch(self, search: KeyedSearch) -> list[RawPosting]:
        """One request for one search, its postings in the vendor's order."""

    def plan(self, queries: Sequence[SearchQuery]) -> list[KeyedSearch]:
        """The searches this source will actually send, first to be spent first."""
        return [s for s in keyed_searches(queries, self.countries) if self.supports(s)]

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """One request per supported search, up to :attr:`requests_per_run`."""
        limit = max((query.limit for query in queries), default=0)
        planned = keyed_searches(queries, self.countries)
        skipped = [search.label for search in planned if not self.supports(search)]
        if skipped:
            logger.info("sources.keyed_unsupported", slug=self.slug, skipped=len(skipped))
        seen: set[str] = set()
        sent = 0
        for search in self.plan(queries):
            if sent >= self.requests_per_run or len(seen) >= limit:
                break
            sent += 1
            for posting in await self.fetch(search):
                if posting.external_id in seen:
                    continue
                seen.add(posting.external_id)
                yield posting
                if len(seen) >= limit:
                    break
        logger.info("sources.keyed_finished", slug=self.slug, requests=sent, yielded=len(seen))

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query through the same path, so it obeys the same ceiling."""
        async for posting in self.search_batch([query]):
            yield posting

    def preview_search(
        self, queries: Sequence[SearchQuery], stored: Sequence[SavedState]
    ) -> SearchPreview:
        """The requests the next run will send, and the ones it cannot."""
        planned = self.plan(queries)
        unsupported = len(keyed_searches(queries, self.countries)) - len(planned)
        note = f"Не больше {self.requests_per_run} запросов за прогон."
        if unsupported:
            note += f" Пропущено поисков: {unsupported} — страна не поддерживается источником."
        return SearchPreview(
            use=SearchUse.QUERY,
            terms=[search.label for search in planned[:PREVIEW_TERMS]],
            more=max(0, len(planned) - PREVIEW_TERMS),
            note=note,
        )

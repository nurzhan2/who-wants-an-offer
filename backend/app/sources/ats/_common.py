"""What the six ATS connectors share: the board list, and one walk over it.

Every vendor here publishes the same kind of thing — one employer's open
positions, as JSON, at a URL keyed by that employer's board token — and none of
them can be asked a question. There is no search parameter on any of these
endpoints that would narrow a board by skill, so a run reads every configured
board once and matches the plan's keywords locally, exactly as ``arbeitnow``
does with its feed. What differs per vendor is the URL, the envelope and the
field names; that is what each connector module declares, and everything else
lives here.

**Not a connector.** The leading underscore keeps ``load_sources`` from
importing this module as one, and :class:`AtsSource` is never passed to
``@register_source``: it has no slug of its own.

**The board list is configuration, not code.** ``company_boards.yaml`` names
the employers per vendor. It was seeded from memory in an environment whose
network was closed, so every entry carries ``verified: false`` until
``scripts/verify_ats_boards.py`` has asked the board once. A board that answers
404 here is logged and skipped rather than failing the run — the same fact the
verification script reports, met during a real run instead.

**Failure is per board.** One employer moving to another vendor must not cost
the other thirty boards their postings, so a board that fails is logged and the
walk goes on. Two exceptions: a vendor saying "slow down" (429, or a
``Retry-After`` too long to wait) stops the walk, because continuing to the
next board on the same host is exactly what it asked us not to do; and a walk
in which *no* board answered and at least one failed for a reason other than
404 is raised as the source's failure, because "every board broke" is not a
quiet day on the job market.
"""

import html
import re
from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.exceptions import RateLimitError, SourceError
from app.core.logging import get_logger
from app.schemas.crawl import SavedState, SearchPreview
from app.sources.base import (
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
    SourceUnavailable,
    Unavailable,
    distinct_terms,
    mentions,
)

if TYPE_CHECKING:  # pragma: no cover - the runtime import would be a cycle
    from app.sources.http import SourceHTTP

logger = get_logger(__name__)

BOARDS_FILE = Path(__file__).with_name("company_boards.yaml")

#: The vendors ``company_boards.yaml`` may list. A key outside this set is a
#: misspelling, and the loader says so rather than quietly reading no boards.
ATS_KEYS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee")

#: Statuses that mean "this board does not exist here", as opposed to "this
#: board is broken right now". The first is skipped quietly at warning level.
GONE_STATUSES = frozenset({404, 410})

#: Mirrors of the ``RawPosting`` limits, which mirror the database columns.
MAX_EXTERNAL_ID = 200
MAX_URL = 1000
MAX_TITLE = 300
MAX_COMPANY = 200
#: Width of ``vacancy.city`` (``MAX_CITY`` in ``app.pipeline.runner``).
MAX_CITY = 120
#: The free-text location line carried in ``_derived``. Not a column; capped so
#: a vendor pasting a paragraph into the location field cannot bloat ``raw``.
MAX_LOCATION = 300

#: A board token. Conservative on purpose: it is interpolated into a URL path
#: (or, for Recruitee, a host name), and nothing a vendor issues needs more.
BOARD_SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

_TAG_RE = re.compile(r"<[^>]+>")
#: A location line that says the job is remote. Word-bounded so "Remoteville"
#: is not, and Russian stems included because CIS employers write «Удалённо».
_REMOTE_RE = re.compile(r"\b(remote|anywhere)\b|удал[её]н", re.IGNORECASE)


def none_as_empty(value: Any) -> Any:
    """Read a JSON ``null`` where a list belongs as an empty list.

    Vendors send ``"departments": null`` as often as ``[]``; treating the two
    differently would drop a posting for the vendor's spelling of "none".
    """
    return () if value is None else value


#: For ``Annotated[tuple[X, ...], NONE_AS_EMPTY]`` fields on the vendor models.
NONE_AS_EMPTY = BeforeValidator(none_as_empty)


def collapse(value: str | None, limit: int) -> str | None:
    """Whitespace-collapsed text that fits ``limit``, or None when nothing is left."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def plain_text(markup: str | None) -> str:
    """HTML reduced to words, for keyword matching only.

    Unescaped first, because Greenhouse returns its ``content`` entity-encoded
    (``&lt;p&gt;``) and stripping tags from that would strip nothing. The stored
    description is never passed through this: it stays as the vendor sent it.
    """
    if not markup:
        return ""
    return html.unescape(_TAG_RE.sub(" ", html.unescape(markup)))


def says_remote(location: str | None) -> bool:
    """Whether a free-text location line describes a remote job."""
    return bool(location) and _REMOTE_RE.search(location or "") is not None


def join_html(*parts: str | None) -> str | None:
    """Several HTML fragments as one description, or None when all are empty.

    Used where a vendor splits one posting's body across fields (Lever's lists,
    Recruitee's requirements, SmartRecruiters' sections). Each fragment is kept
    byte for byte; only the joins are ours.
    """
    kept = [part.strip() for part in parts if part and part.strip()]
    return "\n".join(kept) or None


def heading(text: str | None) -> str | None:
    """A section title as an ``<h3>``, escaped: vendors send it as plain text."""
    title = collapse(text, MAX_TITLE)
    return f"<h3>{html.escape(title)}</h3>" if title else None


# ── the board list ────────────────────────────────────────────────────


class Region(StrEnum):
    """Which market a board is listed for. A label for the owner, not a filter."""

    KZ = "kz"
    CIS = "cis"
    WORLD = "world"


class CompanyBoard(BaseModel):
    """One employer's board on one vendor, as ``company_boards.yaml`` lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The vendor's board token: ``boards.greenhouse.io/<slug>``,
    #: ``jobs.lever.co/<slug>``, ``<slug>.recruitee.com`` and so on.
    slug: str = Field(min_length=1, max_length=100, pattern=BOARD_SLUG_PATTERN)
    #: Display name, used as the posting's company. Some vendors carry their
    #: own; this one is ours and spelled the way the owner reads it.
    company: str = Field(min_length=1, max_length=MAX_COMPANY)
    region: Region
    #: Whether the employer is known to hire remotely at all. Informational:
    #: each posting's own location decides ``_derived.remote``.
    remote: bool = False
    note: str = ""
    #: False until one live request has confirmed the board exists and lists
    #: jobs. Set by hand after ``scripts/verify_ats_boards.py``.
    verified: bool = False

    @field_validator("note", mode="before")
    @classmethod
    def _no_note_is_empty(cls, value: Any) -> Any:
        """Accept ``note:`` with nothing after it."""
        return "" if value is None else value


class BoardsFile(BaseModel):
    """The whole ``company_boards.yaml``: one list per vendor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    greenhouse: tuple[CompanyBoard, ...] = ()
    lever: tuple[CompanyBoard, ...] = ()
    ashby: tuple[CompanyBoard, ...] = ()
    workable: tuple[CompanyBoard, ...] = ()
    smartrecruiters: tuple[CompanyBoard, ...] = ()
    recruitee: tuple[CompanyBoard, ...] = ()

    @field_validator(*ATS_KEYS, mode="before")
    @classmethod
    def _empty_section(cls, value: Any) -> Any:
        """A vendor key with no entries under it reads as ``null`` in YAML."""
        return none_as_empty(value)

    @model_validator(mode="after")
    def _unique_slugs(self) -> "BoardsFile":
        """Refuse one board listed twice for one vendor.

        Twice-listed is twice-fetched: a second request for the same postings,
        against a host whose rate we promised to respect.
        """
        for ats in ATS_KEYS:
            seen: set[str] = set()
            for board in self.for_ats(ats):
                key = board.slug.casefold()
                if key in seen:
                    raise ValueError(f"{ats}: board {board.slug!r} is listed twice")
                seen.add(key)
        return self

    def for_ats(self, ats: str) -> tuple[CompanyBoard, ...]:
        """The boards listed for one vendor."""
        boards: tuple[CompanyBoard, ...] = getattr(self, ats)
        return boards


def load_boards_file(path: Path | None = None) -> BoardsFile:
    """The whole board list, validated. Read on demand, never at import."""
    path = path or BOARDS_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SourceError(f"ats: не читается {path.name} со списком компаний: {exc}") from exc
    try:
        return BoardsFile.model_validate(raw)
    except ValidationError as exc:
        raise SourceError(
            f"ats: {path.name} не описывает список компаний: {exc.errors(include_url=False)}"
        ) from exc


def load_boards(ats: str, path: Path | None = None) -> tuple[CompanyBoard, ...]:
    """The boards listed for one vendor."""
    if ats not in ATS_KEYS:
        raise SourceError(f"ats: неизвестная ATS {ats!r}", source_slug=ats)
    return load_boards_file(path).for_ats(ats)


# ── what a connector hands back ───────────────────────────────────────


class AtsJob(BaseModel):
    """One posting, read out of whichever vendor's shape it arrived in.

    The common denominator the walk needs: an identity, a title, a link, the
    body, and what the vendor said about where the job is. Not stored — the
    ``RawPosting`` built from it carries the untouched entry instead.
    """

    model_config = ConfigDict(frozen=True)

    #: The vendor's own job id, unique within one board.
    job_id: str = Field(min_length=1)
    title: str
    url: str
    #: HTML, as the vendor sent it (joined, where the vendor splits it).
    description: str | None = None
    #: Free-text location line, for ``_derived.location``.
    location: str | None = None
    #: A city the vendor states in a structured field, never one cut out of
    #: ``location``. Feeds the fingerprint through ``stated_city``.
    city: str | None = None
    #: What the vendor's own structured flag says. OR-ed with the location
    #: line by the walk.
    remote: bool = False
    #: Departments, teams, tags: extra words a keyword may match.
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardPage:
    """What one board returned: its entries, and the total it claims to hold."""

    # Any: each entry's shape belongs to the vendor and is validated by the
    # connector's own model one step later, one entry at a time, so that one
    # malformed row costs one posting rather than the board.
    entries: tuple[dict[str, Any], ...]
    stated_total: int | None = None


class BoardProbe(BaseModel):
    """One board, asked once: does it exist, and how much does it list?"""

    model_config = ConfigDict(frozen=True)

    ats: str
    slug: str
    company: str
    status: Literal["ok", "missing", "error"]
    http_status: int | None = None
    #: Entries that validated into a posting.
    jobs: int = 0
    #: Entries the board returned, valid or not.
    entries: int = 0
    #: The vendor's own total, where it states one (SmartRecruiters).
    stated_total: int | None = None
    detail: str | None = None


# ── the connector base ────────────────────────────────────────────────


class AtsSource(BaseSource):
    """Base for the per-vendor connectors. Never registered itself.

    A subclass declares the vendor's terms (``terms_url``, the docstring the
    registry insists on, ``attribution``), how to build a board's URL, how to
    unwrap the envelope and how to read one entry. The walk, the filter, the
    limits and the per-board failure policy are here.
    """

    access_mode: ClassVar[AccessMode] = AccessMode.API
    #: About one request a second, with a little jitter. None of these vendors
    #: publishes a limit for its public board endpoint that this connector was
    #: able to read (the network was closed when it was written), so the rate
    #: is the project default chosen as politeness, not a measured ceiling.
    rate_limit: ClassVar[RateLimit] = RateLimit(
        requests_per_second=1.0, burst=1, jitter_seconds=0.5
    )
    #: Employers open positions over days, not minutes; twice a day finds them
    #: while they are fresh at a fraction of the requests.
    min_interval: ClassVar[timedelta] = timedelta(hours=12)
    cache_ttl: ClassVar[timedelta] = timedelta(hours=6)
    regions: ClassVar[tuple[str, ...]] = ("remote", "KZ", "CIS")

    def __init__(
        self,
        *,
        http: "SourceHTTP | None" = None,
        boards: Sequence[CompanyBoard] | None = None,
    ) -> None:
        super().__init__(http=http)
        #: Injected by tests and by nobody else; None means read the YAML.
        self._boards = tuple(boards) if boards is not None else None

    # ── per-vendor surface ────────────────────────────────────────────

    @abstractmethod
    def board_url(self, board: CompanyBoard) -> str:
        """The list endpoint for one board."""

    def board_params(self, board: CompanyBoard) -> dict[str, str] | None:
        """Query parameters for the list endpoint. None by default."""
        return None

    @abstractmethod
    def unwrap(self, payload: Any) -> BoardPage:
        """The entries out of the vendor's envelope. Raises ValidationError.

        ``Any`` because ``get_json`` hands back whatever the server sent; this
        is where it becomes typed.
        """

    @abstractmethod
    def read(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One entry as an :class:`AtsJob`; None to skip it on purpose.

        Raises ValidationError for an entry that is malformed, which the walk
        logs and drops. None is for an entry that is well-formed and not a job
        anybody can apply to (Ashby's unlisted postings).
        """

    async def fetch_board(
        self, board: CompanyBoard, *, max_requests: int | None = None
    ) -> BoardPage:
        """Read one board. One request, unless a vendor paginates.

        ``max_requests`` exists for the verification script, which promises one
        request per board; only a paginating vendor has anything to cap.
        """
        payload = await self.http.get_json(self.board_url(board), params=self.board_params(board))
        return self.envelope(payload, board)

    def envelope(self, payload: Any, board: CompanyBoard) -> BoardPage:
        """:meth:`unwrap`, with a vendor's change of shape named as such."""
        try:
            return self.unwrap(payload)
        except ValidationError as exc:
            logger.error(
                "sources.ats.envelope_changed",
                slug=self.slug,
                board=board.slug,
                errors=exc.errors(include_url=False),
            )
            raise SourceError(
                f"{self.slug}: ответ доски {board.slug} не похож на список вакансий",
                source_slug=self.slug,
            ) from exc

    # ── configuration ─────────────────────────────────────────────────

    def boards(self) -> tuple[CompanyBoard, ...]:
        """This vendor's boards: injected ones, or the YAML's."""
        if self._boards is not None:
            return self._boards
        return load_boards(self.slug)

    def unavailable(self) -> Unavailable | None:
        """Off, with the reason, when there is no board to walk.

        A connector with an empty or broken list is not failing — it has been
        given nothing to do — and the dashboard should say which.
        """
        reason = super().unavailable()
        if reason is not None:
            return reason
        try:
            boards = self.boards()
        except SourceError as exc:
            return Unavailable(code=SourceUnavailable.DISABLED_BY_CONFIG, detail=exc.detail)
        if not boards:
            return Unavailable(
                code=SourceUnavailable.DISABLED_BY_CONFIG,
                detail=(
                    f"Источник «{self.name}»: в {BOARDS_FILE.name} нет ни одной компании "
                    f"в разделе {self.slug}."
                ),
            )
        return None

    def preview_search(
        self, queries: Sequence[SearchQuery], stored: Sequence[SavedState]
    ) -> SearchPreview:
        """The local filter, with how many boards it runs over."""
        preview = super().preview_search(queries, stored)
        try:
            count = len(self.boards())
        except SourceError as exc:
            return preview.model_copy(update={"note": exc.detail})
        return preview.model_copy(
            update={
                "note": (
                    f"Обходит {count} досок {self.name} из {BOARDS_FILE.name} по разу за "
                    "прогон; фильтр по этим словам применяется здесь."
                )
            }
        )

    # ── the walk ──────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query: the same walk as a whole plan of one."""
        async for posting in self.search_batch([query]):
            yield posting

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Walk every configured board ONCE for the whole plan.

        The keywords of every query are unioned and matched locally, a posting
        matching several is yielded once, and the largest ``limit`` in the plan
        caps the walk as a whole — the boards are read in the YAML's order, so
        a board listed late is the one a small limit leaves out, and the log
        line says how many were left.
        """
        if not queries:
            return
        keywords = distinct_terms(queries)
        limit = max(query.limit for query in queries)
        boards = self.boards()
        seen: set[str] = set()
        yielded = 0
        answered = 0
        missing: list[str] = []
        failed: list[str] = []
        for index, board in enumerate(boards):
            if yielded >= limit:
                logger.info(
                    "sources.ats.limit_reached",
                    slug=self.slug,
                    limit=limit,
                    boards_left=len(boards) - index,
                )
                break
            page = await self._read_board(board, missing=missing, failed=failed)
            if page is None:
                continue
            answered += 1
            for entry in page.entries:
                job = self._job(entry, board)
                if job is None or not _matches(job, keywords):
                    continue
                posting = self._posting(job, entry, board)
                if posting is None or posting.external_id in seen:
                    continue
                seen.add(posting.external_id)
                yield posting
                yielded += 1
                if yielded >= limit:
                    break
        logger.info(
            "sources.ats.walk_finished",
            slug=self.slug,
            boards=len(boards),
            answered=answered,
            missing=missing,
            failed=failed,
            yielded=yielded,
            keywords=len(keywords),
        )
        if failed and answered == 0:
            raise SourceError(
                f"{self.slug}: не ответила ни одна доска из {len(boards)} "
                f"(сбой: {', '.join(failed[:5])})",
                source_slug=self.slug,
            )

    async def _read_board(
        self, board: CompanyBoard, *, missing: list[str], failed: list[str]
    ) -> BoardPage | None:
        """One board's page, or None when this board is skipped this run."""
        try:
            return await self.fetch_board(board)
        except RateLimitError:
            raise
        except SourceError as exc:
            status = exc.extra.get("response_status")
            if status == 429:
                # Retries already spent and the vendor still says slow down:
                # the next board is on the same host.
                raise
            if status in GONE_STATUSES:
                missing.append(board.slug)
                logger.warning(
                    "sources.ats.board_missing", slug=self.slug, board=board.slug, status=status
                )
            else:
                failed.append(board.slug)
                logger.error(
                    "sources.ats.board_failed",
                    slug=self.slug,
                    board=board.slug,
                    status=status,
                    detail=exc.detail,
                )
            return None

    async def probe_board(self, board: CompanyBoard) -> BoardProbe:
        """Ask one board once and report what came back. For the owner's script."""
        try:
            page = await self.fetch_board(board, max_requests=1)
        except SourceError as exc:
            status = exc.extra.get("response_status")
            return BoardProbe(
                ats=self.slug,
                slug=board.slug,
                company=board.company,
                status="missing" if status in GONE_STATUSES else "error",
                http_status=status if isinstance(status, int) else None,
                detail=exc.detail,
            )
        jobs = sum(1 for entry in page.entries if self._job(entry, board) is not None)
        return BoardProbe(
            ats=self.slug,
            slug=board.slug,
            company=board.company,
            status="ok",
            http_status=200,
            jobs=jobs,
            entries=len(page.entries),
            stated_total=page.stated_total,
        )

    def _job(self, entry: dict[str, Any], board: CompanyBoard) -> AtsJob | None:
        """One validated entry, or None when this single row is unusable."""
        try:
            return self.read(entry, board)
        except ValidationError as exc:
            logger.warning(
                "sources.ats.entry_invalid",
                slug=self.slug,
                board=board.slug,
                errors=exc.error_count(),
            )
            return None

    def _posting(
        self, job: AtsJob, entry: dict[str, Any], board: CompanyBoard
    ) -> RawPosting | None:
        """A ``RawPosting``, or None when no honest one can be built.

        The identifier is the board token plus the vendor's job id. The token is
        lower-cased so that a case edit in the YAML does not re-key every
        posting; the id is never truncated, because a cut id is a different key.
        """
        external_id = f"{board.slug.casefold()}:{job.job_id.strip()}"
        url = job.url.strip()
        title = collapse(job.title, MAX_TITLE)
        if len(external_id) > MAX_EXTERNAL_ID or not job.job_id.strip():
            logger.warning("sources.ats.skipped", slug=self.slug, reason="id", board=board.slug)
            return None
        if not url.startswith(("https://", "http://")) or len(url) > MAX_URL:
            logger.warning(
                "sources.ats.skipped", slug=self.slug, reason="url", external_id=external_id
            )
            return None
        if title is None:
            logger.warning(
                "sources.ats.skipped", slug=self.slug, reason="title", external_id=external_id
            )
            return None
        location = collapse(job.location, MAX_LOCATION)
        derived: dict[str, Any] = {
            "location": location,
            "remote": job.remote or says_remote(location),
            "board": board.slug,
            "region": board.region.value,
        }
        city = collapse(job.city, MAX_CITY)
        if city is not None:
            derived["city"] = city
        raw = dict(entry)
        raw["_derived"] = derived
        return RawPosting(
            source_slug=self.slug,
            external_id=external_id,
            url=url,
            title=title,
            company=collapse(board.company, MAX_COMPANY),
            description=job.description or None,
            raw=raw,
        )


def _matches(job: AtsJob, keywords: Sequence[str]) -> bool:
    """Whether any keyword occurs in the title, tags, location or body.

    Any rather than all, and a phrase by all of its words (``base.mentions``),
    for the reason arbeitnow gives: the planner crosses skill groups into one
    plan, and a board has no relevance ranking to fall back on. No keywords
    means no filter.
    """
    if not keywords:
        return True
    haystack = " ".join(
        [job.title, " ".join(job.tags), job.location or "", plain_text(job.description)]
    ).casefold()
    return any(mentions(haystack, word) for word in keywords)

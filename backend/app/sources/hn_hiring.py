"""Hacker News «Ask HN: Who is hiring?»: each top-level comment is one posting.

Two requests a run, both to the HN Search API that Algolia hosts:

1. ``search_by_date?tags=story,author_whoishiring`` lists the stories of the
   account that opens the monthly threads. It also opens «Who wants to be
   hired?» and «Freelancer? Seeking freelancer?», so the newest story whose
   title starts with :data:`STORY_PREFIX` is the one taken.
2. ``items/{story_id}`` returns the thread as a tree. Its direct children are
   the postings; replies under them are discussion and are not read.

**Structuring is a heuristic here, and deliberately nothing more.** By the
thread's own convention a comment opens with a line like
``Acme | Senior Backend Engineer | Remote (EU) | Full-time``. :func:`parse_header`
reads that line into a company (the first segment), a role (the first later
segment that names one) and a location (the first segment that says remote,
onsite or hybrid). It is pure and unit-tested; a comment that does not follow
the convention still becomes a posting, titled by its first line. Asking a
model to structure the text is a later step, on the stored HTML, which is kept
whole as the description.

**A re-run yields only comments it has not yielded before.** The thread is
read whole every time — the API has no "since" for a tree — so the connector
remembers, in ``source_state`` under :data:`STATE_KEY`, the story id and the
comment ids already written. The ids are recorded through
:meth:`record_progress`, from the pipeline's count of what it actually stored,
so a run that crashes before a write does not mark unwritten comments as seen.
A new month's story starts the record over.

Nothing here was measured — the container had no network — and the fixtures
say so in their own ``_comment``.
"""

import html
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType
from app.sources._boards import (
    MAX_COMPANY,
    MAX_LOCATION,
    Derived,
    FeedEntry,
    FeedSource,
    Text,
    build_posting,
    collapse,
    haystack,
    parse_timestamp,
    strip_tags,
)
from app.sources.base import AccessMode, RateLimit, RawPosting, SearchQuery
from app.sources.registry import register_source

if TYPE_CHECKING:  # pragma: no cover - the runtime import would be a cycle
    from app.sources.http import SourceHTTP

logger = get_logger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
POSTING_URL = "https://news.ycombinator.com/item?id={id}"
#: The tags that select the monthly threads' author. Comma means AND here.
STORY_TAGS = "story,author_whoishiring"
#: How many of that author's stories to look through: three a month, so this
#: covers several months even if one thread is missing.
STORY_HITS = 20
STORY_PREFIX = "ask hn: who is hiring"

#: Where the remembered thread lives in ``source_state``.
STATE_KEY = "thread"

#: A first line with no ``|`` is prose, not a header; this much of it is a title.
FALLBACK_TITLE_CHARS = 120

#: The first paragraph of an HN comment ends at the first ``<p>`` or newline.
_PARAGRAPH = re.compile(r"<p\b[^>]*>|\n", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

#: Words that make a segment the role rather than a place or a contract. Lower
#: case, matched as substrings of a casefolded segment.
ROLE_WORDS: tuple[str, ...] = (
    "engineer",
    "developer",
    "programmer",
    "scientist",
    "architect",
    "designer",
    "analyst",
    "manager",
    "lead",
    "head of",
    "director",
    "researcher",
    "devops",
    "sre",
    "cto",
    "intern",
    "backend",
    "back-end",
    "frontend",
    "front-end",
    "full stack",
    "full-stack",
    "fullstack",
)
#: Words that make a segment a location line.
LOCATION_WORDS: tuple[str, ...] = (
    "remote",
    "onsite",
    "on-site",
    "on site",
    "hybrid",
    "in-office",
    "in office",
)
_NO_REMOTE = re.compile(r"\b(?:no|not)\s+remote\b")
_ONSITE = re.compile(r"\b(?:onsite|on-site|on site|in-office|in office)\b")


class Header(BaseModel):
    """What the first line of a hiring comment says, heuristically."""

    model_config = ConfigDict(frozen=True)

    title: str
    company: str | None = None
    location: str | None = None
    remote: RemoteType | None = None


def first_line(comment_html: str) -> str:
    """The comment's first paragraph as plain text."""
    head = _PARAGRAPH.split(comment_html, maxsplit=1)[0]
    return " ".join(html.unescape(_TAG.sub(" ", head)).split())


def remoteness(line: str) -> RemoteType | None:
    """Remote, hybrid or onsite as the line states it; None when it states none."""
    lowered = line.casefold()
    if "remote" in lowered and not _NO_REMOTE.search(lowered):
        return RemoteType.FULL
    if "hybrid" in lowered:
        return RemoteType.HYBRID
    if _ONSITE.search(lowered) or _NO_REMOTE.search(lowered):
        return RemoteType.NO
    return None


def parse_header(comment_html: str) -> Header | None:
    """Read ``Company | Role | Location | ...`` off a comment, or None when it is empty.

    The company is the first segment; the role is the first later segment that
    contains one of :data:`ROLE_WORDS`, else the second segment; the location is
    the first remaining segment that contains one of :data:`LOCATION_WORDS`. A
    line with no ``|`` is not a header: it becomes the title, cut to
    :data:`FALLBACK_TITLE_CHARS`, with no company.
    """
    line = first_line(comment_html)
    if not line:
        return None
    segments = [segment.strip() for segment in line.split("|") if segment.strip()]
    remote = remoteness(line)
    if len(segments) < 2:
        return Header(title=line[:FALLBACK_TITLE_CHARS].strip(), remote=remote)
    company, rest = segments[0], segments[1:]
    role = next(
        (segment for segment in rest if any(word in segment.casefold() for word in ROLE_WORDS)),
        rest[0],
    )
    location = next(
        (
            segment
            for segment in rest
            if segment is not role and any(word in segment.casefold() for word in LOCATION_WORDS)
        ),
        None,
    )
    return Header(
        title=role,
        company=company[:MAX_COMPANY],
        location=collapse(location, MAX_LOCATION),
        remote=remote,
    )


class StoryHit(BaseModel):
    """One story in the search result."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    object_id: str = Field(alias="objectID")
    title: Text = ""
    created_at_i: int = 0


class StorySearch(BaseModel):
    """The search envelope."""

    model_config = ConfigDict(extra="ignore")

    hits: list[StoryHit]


class Comment(BaseModel):
    """One direct child of the story. A deleted one arrives with no text and no author."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    text: str | None = None
    author: str | None = None
    created_at: str | None = None
    type: str = "comment"


class Thread(BaseModel):
    """The story item and its children."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: Text = ""
    # Any: each child is validated on its own by Comment, so one odd child
    # costs one posting, and raw keeps the comment exactly as sent.
    children: list[dict[str, Any]] = Field(default_factory=list)


class ThreadState(BaseModel):
    """What this source remembers between runs."""

    story_id: int | None = None
    seen: list[int] = Field(default_factory=list)


def pick_story(hits: Sequence[StoryHit]) -> StoryHit | None:
    """The newest «Ask HN: Who is hiring?» among the author's stories."""
    threads = [hit for hit in hits if hit.title.casefold().startswith(STORY_PREFIX)]
    return max(threads, key=lambda hit: hit.created_at_i, default=None)


@register_source
class HNHiringSource(FeedSource):
    """The monthly «Who is hiring?» thread, read through the HN Search API (Algolia).

    Terms (https://hn.algolia.com/api, summarised offline — re-read before the
    first live run): a free public API, no key, limited to 10,000 requests an
    hour per IP; this source makes two a run. The comments are their authors'
    words published on Hacker News; :attr:`attribution` credits HN and every
    posting links to the comment on news.ycombinator.com.

    Restrictions: none beyond the rate for read access; the content is not
    ours to republish, and this project only shows it to its one owner.
    """

    slug = "hn_hiring"
    name = "HN: Who is hiring"
    regions = ("remote", "US", "EU")
    access_mode = AccessMode.API
    terms_url = "https://hn.algolia.com/api"
    attribution = "Источник: Hacker News, тред «Ask HN: Who is hiring?» (news.ycombinator.com)"
    rate_limit = RateLimit(requests_per_second=1.0, burst=1)
    #: A monthly thread whose comments arrive over its first days: twice a day
    #: picks them up within hours without re-reading an unchanged tree often.
    min_interval = timedelta(hours=12)
    #: Short, because the comment tree grows and a stale copy hides new posts.
    cache_ttl = timedelta(hours=1)

    def __init__(self, *, http: "SourceHTTP | None" = None) -> None:
        super().__init__(http=http)
        self._story_id: int | None = None
        self._seen_before: set[int] = set()
        self._yielded: list[int] = []

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """The base walk, recording which comments were handed to the pipeline."""
        self._story_id = None
        self._seen_before = set()
        self._yielded = []
        async for posting in super().search_batch(queries):
            self._yielded.append(int(posting.external_id))
            yield posting

    async def record_progress(self, durable: int) -> None:
        """Remember the comments the pipeline has stored, so the next run skips them."""
        if self._story_id is None:
            return
        written = set(self._yielded[:durable])
        state = ThreadState(story_id=self._story_id, seen=sorted(self._seen_before | written))
        await self.state_set(STATE_KEY, state.model_dump(mode="json"))

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every new top-level comment of the newest thread."""
        story = await self._latest_story()
        thread = self._thread(await self.http.get_json(ITEM_URL.format(id=story.object_id)))
        state = await self._state()
        self._story_id = thread.id
        self._seen_before = set(state.seen) if state.story_id == thread.id else set()
        skipped = 0
        for child in thread.children:
            entry = self._entry(child, thread.id)
            if entry is None:
                continue
            if int(entry.posting.external_id) in self._seen_before:
                skipped += 1
                continue
            yield entry
        logger.info(
            "sources.hn_hiring.thread_read",
            story_id=thread.id,
            comments=len(thread.children),
            already_seen=skipped,
        )

    async def _latest_story(self) -> StoryHit:
        """The thread to read, or a failure: a month always has one."""
        payload = await self.http.get_json(
            SEARCH_URL, params={"tags": STORY_TAGS, "hitsPerPage": STORY_HITS}
        )
        try:
            hits = StorySearch.model_validate(payload).hits
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: в ответе поиска нет списка hits — форма API изменилась",
                source_slug=self.slug,
            ) from exc
        story = pick_story(hits)
        if story is None:
            raise SourceError(
                f"{self.slug}: не найден тред «Ask HN: Who is hiring?» среди историй whoishiring",
                source_slug=self.slug,
            )
        return story

    def _thread(self, payload: Any) -> Thread:
        """The story tree, or a failure naming what changed."""
        try:
            return Thread.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                f"{self.slug}: ответ items не похож на тред — форма API изменилась",
                source_slug=self.slug,
            ) from exc

    async def _state(self) -> ThreadState:
        """The remembered thread, or a fresh record when none is stored or it does not parse."""
        stored = await self.state_get(STATE_KEY)
        if not stored:
            return ThreadState()
        try:
            return ThreadState.model_validate(stored)
        except ValidationError:
            logger.warning("sources.hn_hiring.state_unreadable")
            return ThreadState()

    def _entry(self, child: dict[str, Any], story_id: int) -> FeedEntry | None:
        """One comment as a posting, or None for a deleted, empty or odd one."""
        try:
            comment = Comment.model_validate(child)
        except ValidationError as exc:
            logger.warning("sources.hn_hiring.comment_invalid", errors=exc.error_count())
            return None
        if not comment.text or comment.type != "comment":
            return None
        header = parse_header(comment.text)
        if header is None:
            return None
        derived = Derived(
            remote=header.remote,
            location=header.location,
            published_at=parse_timestamp(comment.created_at),
        )
        # Replies are discussion, not postings, and can be most of the payload.
        raw = {key: value for key, value in child.items() if key != "children"}
        raw["story_id"] = story_id
        posting = build_posting(
            slug=self.slug,
            external_id=str(comment.id),
            url=POSTING_URL.format(id=comment.id),
            title=header.title,
            company=header.company,
            description=comment.text,
            raw=raw,
            derived=derived,
        )
        if posting is None:
            return None
        return FeedEntry(posting=posting, text=haystack(strip_tags(comment.text)))

"""We Work Remotely: the public RSS feeds, read with the standard library.

The board publishes one feed of everything (``/remote-jobs.rss``) and one per
category (``/categories/remote-<category>-jobs.rss``). A run reads the main
feed and the developer categories in :data:`FEED_URLS`, deduplicates by
``<guid>`` across them, and filters by the plan's keywords here
(``_boards.FeedSource``). The category feeds are read on top of the main one
because a single RSS document carries only the newest items, and the main feed
is shared with every non-developer category on the board.

**Parsed with ``xml.etree.ElementTree``, after refusing any DTD.** ``hh`` reads
its sitemaps with a regex precisely to avoid pointing an entity-expanding
parser at a third party's document. RSS cannot be read that way — items nest,
``<description>`` is escaped HTML or CDATA, and namespaced children appear
beside plain ones — so the risk is removed at the source instead: a document
carrying ``<!DOCTYPE`` or ``<!ENTITY`` is refused before it reaches the parser.
Without a DTD there is nothing to expand, and RSS 2.0 has no use for one.

``<title>`` is ``"Company: Role"``; the company is split off at the first
``": "``. ``<region>`` is the board's location line, ``<pubDate>`` is RFC 822,
and ``<description>`` is the posting's HTML, kept as the description.

A public feed on a website rather than an API under contract, so this is a
CRAWL source and robots.txt decides, checked by the shared client before the
first request. Nothing here was measured — the container had no network — and
the fixture carries an XML comment saying it was reconstructed.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from app.core.exceptions import SourceError
from app.core.logging import get_logger
from app.db.enums import RemoteType
from app.sources._boards import (
    MAX_LOCATION,
    Derived,
    FeedEntry,
    FeedSource,
    build_posting,
    collapse,
    haystack,
    parse_timestamp,
    strip_tags,
)
from app.sources.base import RateLimit
from app.sources.registry import register_source

logger = get_logger(__name__)

BASE_URL = "https://weworkremotely.com"
MAIN_FEED = f"{BASE_URL}/remote-jobs.rss"
#: The main feed, then the developer categories. Category slugs are the
#: board's own; a renamed one answers 404 and fails the run loudly rather than
#: quietly shrinking it.
FEED_URLS: tuple[str, ...] = (
    MAIN_FEED,
    f"{BASE_URL}/categories/remote-back-end-programming-jobs.rss",
    f"{BASE_URL}/categories/remote-full-stack-programming-jobs.rss",
    f"{BASE_URL}/categories/remote-devops-sysadmin-jobs.rss",
)

#: A DTD is the only way an XML document can declare entities, so refusing one
#: removes entity expansion entirely. Matched case-insensitively anywhere in
#: the document, because a declaration can follow a comment.
_DTD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

#: Where ``<title>`` separates the company from the role.
TITLE_SEPARATOR = ": "


def _local(tag: str) -> str:
    """An element's name without its namespace: ``{uri}content`` → ``content``."""
    return tag.rsplit("}", 1)[-1]


def parse_rss(document: str, *, slug: str = "weworkremotely") -> list[dict[str, str]]:
    """Every ``<item>`` as a mapping of child name to text.

    Namespaced children (``media:content``) are reduced to their local name and
    kept only when they carry text, so an attribute-only element cannot shadow
    a plain one. A document with a DTD, or one that does not parse, is refused
    with the reason rather than read as an empty feed.
    """
    if _DTD.search(document):
        raise SourceError(
            f"{slug}: RSS содержит DTD — такой документ не разбирается", source_slug=slug
        )
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise SourceError(f"{slug}: RSS не разбирается как XML: {exc}", source_slug=slug) from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError(f"{slug}: в RSS нет элемента channel", source_slug=slug)
    items: list[dict[str, str]] = []
    for item in channel.findall("item"):
        fields: dict[str, str] = {}
        for child in item:
            text = (child.text or "").strip()
            if text:
                fields.setdefault(_local(child.tag), text)
        items.append(fields)
    return items


def split_title(title: str) -> tuple[str | None, str]:
    """``"Acme: Senior Backend Engineer"`` → ``("Acme", "Senior Backend Engineer")``."""
    company, separator, role = title.partition(TITLE_SEPARATOR)
    if not separator or not company.strip() or not role.strip():
        return None, title.strip()
    return company.strip(), role.strip()


@register_source
class WeWorkRemotelySource(FeedSource):
    """We Work Remotely's public RSS feeds, crawled under robots.txt.

    A website feed rather than a contracted API, so ``access_mode`` stays
    CRAWL. The board asks for credit when its listings are shown elsewhere;
    :attr:`attribution` carries it and ``url`` is the posting's own page.
    """

    slug = "weworkremotely"
    name = "We Work Remotely"
    regions = ("remote",)
    attribution = "Источник: We Work Remotely (weworkremotely.com)"
    rate_limit = RateLimit(requests_per_second=0.5, burst=1)
    #: Four feeds a run; RSS readers poll hourly, four runs a day is far gentler.
    min_interval = timedelta(hours=6)
    cache_ttl = timedelta(hours=6)

    async def feed(self) -> AsyncGenerator[FeedEntry, None]:
        """Every item of every feed in :data:`FEED_URLS`, in feed order."""
        for url in FEED_URLS:
            document = await self.http.get_text(url)
            for item in parse_rss(document, slug=self.slug):
                entry = self._entry(item)
                if entry is not None:
                    yield entry

    def _entry(self, item: dict[str, str]) -> FeedEntry | None:
        """One item as a posting, or None when it has no title or link."""
        link = item.get("link") or item.get("guid") or ""
        title = item.get("title", "")
        if not link or not title:
            logger.warning("sources.weworkremotely.item_incomplete", has_link=bool(link))
            return None
        company, role = split_title(title)
        description = item.get("description")
        derived = Derived(
            remote=RemoteType.FULL,
            location=collapse(item.get("region"), MAX_LOCATION),
            published_at=parse_timestamp(item.get("pubDate")),
        )
        raw: dict[str, Any] = dict(item)
        posting = build_posting(
            slug=self.slug,
            external_id=item.get("guid") or link,
            url=link,
            title=role,
            company=company,
            description=description,
            raw=raw,
            derived=derived,
        )
        if posting is None:
            return None
        text = haystack(role, item.get("category"), item.get("skills"), strip_tags(description))
        return FeedEntry(posting=posting, text=text)

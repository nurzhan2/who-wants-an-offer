"""Public Telegram job channels, read through the owner's user session.

Telegram is where many KZ/RU/CIS IT vacancies appear first, often with a salary
range. This connector reads a list of public channels by ``@username``
(``telegram_channels.yaml``), keeps the posts that look like vacancies, and
turns each into a posting.

**The one source with a session.** Everything else in ``backend/`` is anonymous;
this needs a Telethon *user* session, because a bot cannot read a channel it does
not administer, while a user can read any public channel's history by username
without joining. The session reads and does nothing else — see
``_telegram_client.py`` for the exact calls — and docs/ARCHITECTURE.md records
the exception in one paragraph. The session file lives outside the repository
(``TELEGRAM_SESSION_PATH``) and is created by a person, once, with
``uv run python -m app.sources.telegram_login``.

**Applying is a person's job.** A Telegram vacancy's "apply" is the post's link
and the contact it names. Nothing here or anywhere in the project writes to a
recruiter: the card offers the text to copy, and the owner sends it.

Terms (https://core.telegram.org/api/terms, summarised; re-read before
widening anything): an API client must use its own ``api_id``, must not flood
the API and must honour ``FLOOD_WAIT`` exactly; must not be used for spam,
unsolicited messages or automated actions on behalf of users who did not ask for
them; data obtained must not be used to violate users' privacy. Reading public
channel history with the account owner's own session, slowly, is within that;
sending, joining or scraping private chats is not, and none of it is here.

How a run spends Telegram's patience:

* **Incremental.** ``last_message_id`` per channel in ``source_state``; a run
  asks only for posts after it, at most :data:`FIRST_RUN_LIMIT` on a channel's
  first read and :data:`PER_RUN_LIMIT` after that.
* **FloodWait.** Waited exactly as long as Telegram said, once, and written into
  the run report; a second one on the same channel skips it for this run. No
  retry policy is layered on top.
* **Two-stage filter.** A free heuristic first (``_telegram_posts``), the model
  only for what survives; the share the heuristic cut is logged every run.
* **Alive or not.** A channel whose newest post is older than
  :data:`STALE_AFTER` is reported dead, one whose username resolves to nothing is
  reported missing; ``scripts/prune_telegram_channels.py`` removes both from the
  yaml, naming each one.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.core.exceptions import ConfigurationError, SourceError
from app.core.logging import get_logger
from app.llm.router import LLMRouter, get_router
from app.sources._telegram_client import (
    ChannelMissingError,
    ChannelPost,
    ChannelReader,
    FloodWaitRequired,
    SessionExpiredError,
    TelethonReader,
)
from app.sources._telegram_posts import ParsedPost, looks_like_vacancy, parse_post
from app.sources.base import (
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
    SourceUnavailable,
    Unavailable,
    mentions,
)
from app.sources.registry import register_source

logger = get_logger(__name__)

CHANNELS_FILE = Path(__file__).with_name("telegram_channels.yaml")

#: Posts read from a channel the first time it is seen. Enough to judge whether
#: it is alive and to pick up the last weeks, not its whole archive.
FIRST_RUN_LIMIT = 50
#: Posts read per channel on every later run.
PER_RUN_LIMIT = 100
#: A channel with no post newer than this is dead (phase 22: «пост за последние
#: 30 дней»).
STALE_AFTER = timedelta(days=30)
#: Pause between channels. Not a Telegram figure — they publish none — but the
#: politeness every other source here has.
BETWEEN_CHANNELS_SECONDS = 2.0
#: A FloodWait longer than this ends the run instead of holding it for hours.
MAX_FLOOD_WAIT_SECONDS = 900

STATE_CHANNEL = "channel:"
STATE_RUN = "run:last"

MAX_TITLE = 300
MAX_COMPANY = 200

type Sleep = Callable[[float], Awaitable[None]]


class Channel(BaseModel):
    """One entry of ``telegram_channels.yaml``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    username: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
    region: Literal["kz", "ru", "cis", "world"]
    language: str = Field(default="ru", max_length=5)
    kind: Literal["jobs", "freelance", "relocation"] = "jobs"
    stack: tuple[str, ...] = ()
    verified: bool = False


def load_channels(path: Path | None = None) -> tuple[Channel, ...]:
    """The configured channels. A broken file is a configuration error, loudly."""
    target = path or CHANNELS_FILE
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return tuple(Channel.model_validate(entry) for entry in document.get("channels", []))
    except (OSError, yaml.YAMLError, ValidationError, AttributeError) as exc:
        raise ConfigurationError(f"{target.name} не читается: {exc}") from exc


def session_file(path: Path) -> Path:
    """Where Telethon actually keeps a session named ``path``.

    Telethon appends ``.session`` to a name that lacks it, so ``telegram`` and
    ``telegram.session`` are the same file.
    """
    return path if path.suffix == ".session" else path.with_name(path.name + ".session")


class RunReport(BaseModel):
    """What one run did, stored under ``run:last`` for the morning summary."""

    finished_at: datetime | None = None
    channels: int = 0
    posts: int = 0
    heuristic_rejected: int = 0
    keyword_rejected: int = 0
    not_vacancy: int = 0
    parsed_by_llm: int = 0
    parsed_by_rules: int = 0
    yielded: int = 0
    flood_waits: list[dict[str, Any]] = Field(default_factory=list)
    dead: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    session_expired: bool = False

    @property
    def heuristic_share(self) -> float | None:
        """Share of posts the free filter cut, the number the plan guessed at 80%."""
        return None if not self.posts else self.heuristic_rejected / self.posts


@register_source
class TelegramSource(BaseSource):
    """Public Telegram job channels, read-only, through the owner's user session.

    Terms: https://core.telegram.org/api/terms — own api_id, no flooding and
    FLOOD_WAIT honoured exactly, no spam or unsolicited messages, no automated
    actions for users who did not ask for them, no privacy violations.
    This connector reads public channel history only: no joins, no messages,
    no reactions. A run's FloodWaits are waited out once and reported.
    Attribution: each posting links to the original post on t.me.
    """

    slug: ClassVar[str] = "telegram"
    name: ClassVar[str] = "Telegram"
    regions: ClassVar[tuple[str, ...]] = ("KZ", "RU", "CIS", "world")
    requires_auth: ClassVar[bool] = True
    # MTProto, not HTTP: robots.txt has nothing to say about it, and the
    # published API terms govern — so API, with terms_url and the summary above.
    access_mode: ClassVar[AccessMode] = AccessMode.API
    terms_url: ClassVar[str | None] = "https://core.telegram.org/api/terms"
    attribution: ClassVar[str | None] = "Telegram: ссылка на исходный пост в канале"
    # Nominal: this connector makes no HTTP request through the shared client.
    rate_limit: ClassVar[RateLimit] = RateLimit(requests_per_second=0.5)
    min_interval: ClassVar[timedelta] = timedelta(hours=6)

    def __init__(
        self,
        *,
        reader: ChannelReader | None = None,
        router: LLMRouter | None = None,
        channels: Sequence[Channel] | None = None,
        sleep: Sleep | None = None,
        now: Callable[[], datetime] | None = None,
        use_model: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._reader = reader
        self._router = router
        self._channels = tuple(channels) if channels is not None else None
        self._sleep: Sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._use_model = use_model
        self._yielded = 0
        #: (postings yielded when the channel ended, state key, value). Saved
        #: only once the pipeline says that many are durable — a crash in the
        #: unwritten batch must not move a channel past posts nobody stored.
        self._pending: list[tuple[int, str, dict[str, Any]]] = []

    # ── availability ──────────────────────────────────────────────────

    def unavailable(self) -> Unavailable | None:
        """Not configured, or never logged in: inactive, not failing (rule 8)."""
        missing = tuple(
            name
            for name, value in (
                ("TELEGRAM_API_ID", settings.telegram_api_id),
                ("TELEGRAM_API_HASH", settings.telegram_api_hash),
                (
                    "TELEGRAM_SESSION_PATH",
                    settings.telegram_session_path or settings.telegram_session_string,
                ),
            )
            if not value
        )
        if missing and self._reader is None:
            return Unavailable(
                code=SourceUnavailable.MISSING_CREDENTIALS,
                detail=(
                    f"Telegram не настроен: не заданы {', '.join(missing)} в .env. "
                    "Шаги — README, раздел «Telegram»."
                ),
                missing_credentials=missing,
            )
        path = settings.telegram_session_path
        if (
            self._reader is None
            and settings.telegram_session_string is None
            and path is not None
            and not session_file(path).is_file()
        ):
            return Unavailable(
                code=SourceUnavailable.MISSING_CREDENTIALS,
                detail=(
                    "Сессии Telegram ещё нет: войдите один раз руками — "
                    "uv run python -m app.sources.telegram_login"
                ),
                missing_credentials=("TELEGRAM_SESSION_PATH",),
            )
        return None

    def _make_reader(self) -> ChannelReader:
        if self._reader is not None:
            return self._reader
        return TelethonReader.from_settings()

    def _model(self) -> LLMRouter | None:
        if not self._use_model:
            return None
        return self._router or get_router()

    # ── the run ───────────────────────────────────────────────────────

    def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        """One query is the whole plan for a feed of channels."""
        return self.search_batch([query])

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Walk every channel once and yield the vacancies its new posts carry.

        The plan's keywords narrow what reaches the model: a post must mention
        at least one of them (``base.mentions``), so a Java vacancy in a general
        IT channel costs no model call for a Python owner. No keywords means no
        narrowing.
        """
        keywords = tuple(dict.fromkeys(word for query in queries for word in query.keywords))
        channels = self._channels if self._channels is not None else load_channels()
        reader = self._make_reader()
        router = self._model()
        report = RunReport()
        self._yielded = 0
        self._pending = []
        try:
            for index, channel in enumerate(channels):
                if index:
                    await self._sleep(BETWEEN_CHANNELS_SECONDS)
                report.channels += 1
                async for posting in self._channel(channel, reader, router, keywords, report):
                    self._yielded += 1
                    report.yielded += 1
                    yield posting
                # Everything this channel yielded is now in the pipeline's
                # batch; its watermark waits in ``_pending`` until that batch
                # is durable (``record_progress``).
        except SessionExpiredError as exc:
            report.session_expired = True
            raise SourceError(str(exc), source_slug=self.slug) from exc
        finally:
            await reader.close()
            report.finished_at = self._now()
            await self.state_set(STATE_RUN, report.model_dump(mode="json"))
            logger.info(
                "sources.telegram.run_finished",
                channels=report.channels,
                posts=report.posts,
                heuristic_rejected=report.heuristic_rejected,
                heuristic_share=report.heuristic_share,
                keyword_rejected=report.keyword_rejected,
                not_vacancy=report.not_vacancy,
                parsed_by_llm=report.parsed_by_llm,
                parsed_by_rules=report.parsed_by_rules,
                yielded=report.yielded,
                flood_waits=len(report.flood_waits),
                dead=report.dead,
                missing=report.missing,
            )

    async def record_progress(self, durable: int) -> None:
        """Advance the channels whose every posting is now written."""
        still: list[tuple[int, str, dict[str, Any]]] = []
        for needed, key, value in self._pending:
            if needed <= durable:
                await self.state_set(key, value)
            else:
                still.append((needed, key, value))
        self._pending = still

    async def _read(
        self,
        channel: Channel,
        reader: ChannelReader,
        *,
        after_id: int,
        limit: int,
        report: RunReport,
    ) -> Sequence[ChannelPost] | None:
        """One channel's new posts, waiting out one FloodWait; None to skip it."""
        for attempt in (1, 2):
            try:
                return await reader.recent(channel.username, after_id=after_id, limit=limit)
            except FloodWaitRequired as wait:
                report.flood_waits.append(
                    {"channel": channel.username, "seconds": wait.seconds, "attempt": attempt}
                )
                logger.warning(
                    "sources.telegram.flood_wait", channel=channel.username, seconds=wait.seconds
                )
                if attempt == 2 or wait.seconds > MAX_FLOOD_WAIT_SECONDS:
                    return None
                # Exactly as long as Telegram said, and then the same request
                # once. Not a retry policy: the wait is the protocol.
                await self._sleep(float(wait.seconds))
        return None  # pragma: no cover - the loop returns on both attempts

    async def _channel(
        self,
        channel: Channel,
        reader: ChannelReader,
        router: LLMRouter | None,
        keywords: Sequence[str],
        report: RunReport,
    ) -> AsyncIterator[RawPosting]:
        key = f"{STATE_CHANNEL}{channel.username}"
        stored = await self.state_get(key) or {}
        after_id = int(stored.get("last_message_id") or 0)
        limit = PER_RUN_LIMIT if after_id else FIRST_RUN_LIMIT
        try:
            posts = await self._read(channel, reader, after_id=after_id, limit=limit, report=report)
        except ChannelMissingError:
            report.missing.append(channel.username)
            logger.warning("sources.telegram.channel_missing", channel=channel.username)
            self._pending.append(
                (
                    self._yielded,
                    key,
                    {**stored, "status": "missing", "checked_at": _iso(self._now())},
                )
            )
            return
        if posts is None:
            return

        newest = max((post.date for post in posts), default=None)
        last_post_at = _parse(stored.get("last_post_at"))
        if newest is not None and (last_post_at is None or newest > last_post_at):
            last_post_at = newest
        alive = last_post_at is not None and self._now() - last_post_at <= STALE_AFTER
        if not alive:
            report.dead.append(channel.username)
            logger.warning(
                "sources.telegram.channel_dead",
                channel=channel.username,
                last_post_at=_iso(last_post_at) if last_post_at else None,
            )

        for post in sorted(posts, key=lambda item: item.id):
            report.posts += 1
            if not looks_like_vacancy(post.text):
                report.heuristic_rejected += 1
                continue
            if keywords and not any(mentions(post.text.casefold(), word) for word in keywords):
                report.keyword_rejected += 1
                continue
            parsed, how = await parse_post(post.text, router=router)
            if how == "llm":
                report.parsed_by_llm += 1
            else:
                report.parsed_by_rules += 1
            if not parsed.is_vacancy or not parsed.title:
                report.not_vacancy += 1
                continue
            yield self._posting(channel, post, parsed, how)

        value = {
            "last_message_id": max([after_id, *(post.id for post in posts)]),
            "last_post_at": _iso(last_post_at) if last_post_at else None,
            "status": "live" if alive else "dead",
            "checked_at": _iso(self._now()),
        }
        # ``_yielded`` already counts this channel's last posting: the caller
        # incremented it before asking this generator for the next one.
        self._pending.append((self._yielded, key, value))

    def _posting(
        self, channel: Channel, post: ChannelPost, parsed: ParsedPost, how: str
    ) -> RawPosting:
        url = f"https://t.me/{channel.username}/{post.id}"
        salary = (
            {
                "min": str(parsed.salary_min) if parsed.salary_min is not None else None,
                "max": str(parsed.salary_max) if parsed.salary_max is not None else None,
                "currency": parsed.currency,
            }
            if parsed.salary_min is not None or parsed.salary_max is not None
            else None
        )
        return RawPosting(
            source_slug=self.slug,
            external_id=f"{channel.username.lower()}/{post.id}",
            url=url,
            title=(parsed.title or "")[:MAX_TITLE],
            company=parsed.company[:MAX_COMPANY] if parsed.company else None,
            description=post.text,
            raw={
                "_derived": {
                    "city": parsed.city,
                    "remote": parsed.remote,
                    "salary": salary,
                    "skills": parsed.skills,
                    "seniority": parsed.seniority,
                    "several_positions": parsed.several_positions,
                    # How to apply, as the post says it. A person uses it; this
                    # project never writes to it.
                    "apply_contact": parsed.apply_contact,
                    "post_url": url,
                    "published_at": _iso(post.date),
                    "channel": channel.username,
                    "region": channel.region,
                    "language": channel.language,
                    "kind": channel.kind,
                    "parsed_by": how,
                }
            },
        )

    def publisher_of(self, raw: dict[str, Any]) -> str | None:
        """The channel the post came from, for «telegram → @channel» on the card."""
        derived = raw.get("_derived")
        channel = derived.get("channel") if isinstance(derived, dict) else None
        return f"@{channel}" if isinstance(channel, str) else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

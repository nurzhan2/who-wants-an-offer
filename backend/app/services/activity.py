"""Long actions the owner started from the dashboard, and how each one ended.

The owner's complaint of 22 Nov 2026: reload the page while a letter is being
written or a resume parsed, and the page forgets — nothing says whether it is
still going, finished, or broke. Two things were wrong, and this module fixes
both:

**The work died with the request.** The dashboard password middleware is a
``BaseHTTPMiddleware``, which cancels the handler when the browser disconnects,
and the handler's database session is closed with the request. A reload during
a letter could cancel the letter. :func:`run` therefore starts the work as its
own task, with its own session (the caller's factory opens one), and the
request only waits for it behind :func:`asyncio.shield`: a reload cancels the
wait, not the work.

**Nobody remembered.** Every action is recorded here — kind, what it is about,
when it started, and how it ended (done with a line for the owner, or failed
with the reason) — and ``GET /api/v1/activity`` returns the recent ones, so a
page reloaded mid-action asks the server instead of its own memory.

In-memory, like the crawl's job registry (``app.services.pipeline``): one API
process, and a restart that forgets a finished letter loses nothing — the
letter itself is in the database. A finished entry is kept for
:data:`KEEP_FINISHED` so the page can say «готово» after a reload.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

Kind = Literal["letter", "cv", "cover_letter", "resume"]
State = Literal["running", "done", "failed"]

#: How long a finished action stays visible after it ended.
KEEP_FINISHED = timedelta(minutes=30)
#: Entries kept at most; the oldest finished ones go first.
MAX_ENTRIES = 100


@dataclass(slots=True)
class Activity:
    """One action: what, about what, and where it got to."""

    kind: Kind
    key: str
    label: str
    id: str = field(default_factory=lambda: uuid4().hex)
    state: State = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail,
        }


_REGISTRY: "OrderedDict[str, Activity]" = OrderedDict()
#: The tasks themselves, so a running one is not garbage-collected mid-way.
_TASKS: set["asyncio.Task[object]"] = set()


def _prune(now: datetime) -> None:
    for key in [
        key
        for key, item in _REGISTRY.items()
        if item.finished_at is not None and now - item.finished_at > KEEP_FINISHED
    ]:
        del _REGISTRY[key]
    while len(_REGISTRY) > MAX_ENTRIES:
        oldest = next(
            (key for key, item in _REGISTRY.items() if item.state != "running"),
            next(iter(_REGISTRY)),
        )
        del _REGISTRY[oldest]


def start(kind: Kind, key: str, label: str) -> Activity:
    item = Activity(kind=kind, key=key, label=label)
    _REGISTRY[item.id] = item
    _prune(item.started_at)
    return item


def finish(item: Activity, detail: str | None = None) -> None:
    item.state = "done"
    item.finished_at = datetime.now(UTC)
    item.detail = detail


def fail(item: Activity, error: BaseException) -> None:
    item.state = "failed"
    item.finished_at = datetime.now(UTC)
    # The reason the owner can act on: an AppError's own message, otherwise the
    # class — never a traceback, which can carry a path or a URL.
    item.detail = (
        error.detail
        if isinstance(error, AppError) and error.detail
        else f"ошибка на сервере ({type(error).__name__}); подробности в журнале"
    )
    logger.warning("activity.failed", kind=item.kind, key=item.key, error=type(error).__name__)


def recent() -> list[dict[str, object]]:
    """Running actions and those finished in the last :data:`KEEP_FINISHED`, newest first."""
    _prune(datetime.now(UTC))
    return [item.as_dict() for item in reversed(_REGISTRY.values())]


async def run[T](
    kind: Kind,
    key: str,
    label: str,
    work: Callable[[], Awaitable[T]],
    *,
    describe: Callable[[T], str | None] = lambda _: None,
) -> T:
    """Run ``work`` as its own task, recorded; wait for it unless the client leaves.

    ``work`` must open its own database session: the request's is closed when
    the request is cancelled, and the work must outlive that.
    """
    item = start(kind, key, label)

    async def tracked() -> T:
        try:
            result = await work()
        except BaseException as exc:
            fail(item, exc)
            raise
        finish(item, describe(result))
        return result

    task: asyncio.Task[T] = asyncio.create_task(tracked(), name=f"activity-{kind}-{key}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return await asyncio.shield(task)

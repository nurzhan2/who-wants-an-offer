"""The worker that runs around the clock on the server (phase 22b).

    python -m app.worker

Two loops in one process:

* **fast sources** — every enabled source except hh, every
  ``WORKER_TICK_SECONDS`` (5 minutes). Each source's own ``min_interval`` still
  decides whether it is asked on a given tick, so a tick costs nothing for a
  source that is cooling down;
* **hh** — its own loop, because one hh run is up to an hour and a half at the
  measured 0.25 requests a second, and a Telegram vacancy must not wait for it.

After every crawl the vacancies it brought in go straight to
:func:`app.services.react.react`: scored against every active resume, given a
letter and a CV when they clear the floor, and announced in the owner's
Telegram. Once a day, at ``WORKER_SUMMARY_AT_UTC``, the morning summary goes too.

Nothing here applies on hh. The agent that does stays on the owner's laptop and
reads the queue this server builds, with the owner's confirmation.
"""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.db.models import Vacancy
from app.db.repositories.source_state import SourceStateRepository
from app.db.session import session_factory
from app.pipeline.runner import run_pipeline
from app.services import bot, daily, notify, react
from app.services.outreach import Sender
from app.sources.registry import get_enabled_sources
from app.workshop import seed as workshop_seed

logger = get_logger(__name__)

HH_SLUG = "hh"
#: How long the hh loop waits between attempts; hh's own min_interval decides
#: whether an attempt crawls.
HH_IDLE_SECONDS = 600
#: When the morning summary goes out, UTC. 03:30 UTC is 08:30 in Almaty.
SUMMARY_AT_UTC = time(3, 30)
#: On start, what the worker looks back over: vacancies a previous run found
#: but could not act on (no resume yet, no vectors yet, a crash). Reacting is
#: idempotent per vacancy, so nothing is answered twice.
CATCH_UP = timedelta(days=3)


async def new_since(since: datetime) -> list[UUID]:
    """Vacancies first seen after ``since``: what a crawl just brought in."""
    async with session_factory() as session:
        rows = await session.scalars(select(Vacancy.id).where(Vacancy.first_seen_at >= since))
        return list(rows.all())


async def crawl_and_react(slugs: Sequence[str], *, sender: Sender | None) -> int:
    """One crawl of these sources, then the reaction to what it found."""
    if not slugs:
        return 0
    since = datetime.now(UTC) - timedelta(seconds=5)
    try:
        await run_pipeline(source_slugs=list(slugs))
    except AppError as error:
        logger.warning("worker.crawl_failed", slugs=list(slugs), detail=error.detail)
        return 0
    fresh = await new_since(since)
    if fresh:
        async with session_factory() as session:
            reactions = await react.react(session, fresh, sender=sender)
        logger.info("worker.reacted", new=len(fresh), acted=len(reactions))
    return len(fresh)


async def catch_up(*, sender: Sender | None) -> int:
    """React to the recent vacancies a previous run left unanswered."""
    recent = await new_since(datetime.now(UTC) - CATCH_UP)
    if recent:
        async with session_factory() as session:
            reactions = await react.react(session, recent, sender=sender)
        logger.info("worker.caught_up", recent=len(recent), acted=len(reactions))
    return len(recent)


def fast_slugs() -> list[str]:
    """Every enabled source but hh."""
    return [source.slug for source in get_enabled_sources() if source.slug != HH_SLUG]


async def fast_loop(sender: Sender | None) -> None:
    while True:
        await crawl_and_react(fast_slugs(), sender=sender)
        await maybe_summary()
        await asyncio.sleep(settings.worker_tick_seconds)


async def hh_loop(sender: Sender | None) -> None:
    while True:
        if any(source.slug == HH_SLUG for source in get_enabled_sources()):
            await crawl_and_react([HH_SLUG], sender=sender)
        await asyncio.sleep(HH_IDLE_SECONDS)


async def bot_loop(sender: Sender | None) -> None:
    """Long-poll the owner's bot: /filters and the buttons on each card."""
    while True:
        if not notify.configured():
            await asyncio.sleep(60)
            continue
        try:
            async with session_factory() as session:
                await bot.poll_once(session, sender=sender)
        except Exception as exc:  # the bot must outlive a bad update or a blip
            logger.warning("worker.bot_failed", error=type(exc).__name__)
            await asyncio.sleep(5)


async def maybe_summary(now: datetime | None = None) -> bool:
    """Send the morning summary once a day, after ``SUMMARY_AT_UTC``."""
    moment = now or datetime.now(UTC)
    if moment.time() < SUMMARY_AT_UTC:
        return False
    key = f"summary:{moment.date().isoformat()}"
    async with session_factory() as session:
        states = SourceStateRepository(session)
        if await states.get(react.STATE_SLUG, key) is not None:
            return False
        summary = await daily.build_summary(session, since=moment - timedelta(days=1))
        await states.set(react.STATE_SLUG, key, {"at": moment.isoformat()})
        await session.commit()
    await daily.notify(summary.render())
    return True


def outreach_sender() -> Sender | None:
    """The Telegram session that writes to recruiters, when outreach is on."""
    if not settings.telegram_outreach_enabled:
        return None
    from app.sources._telegram_client import TelethonReader

    try:
        return TelethonReader.from_settings()
    except AppError as error:
        logger.warning("worker.outreach_unavailable", detail=error.detail)
        return None


async def main() -> None:
    configure_logging()
    sender = outreach_sender()
    logger.info(
        "worker.start",
        tick_seconds=settings.worker_tick_seconds,
        outreach=sender is not None,
        budget_usd=settings.llm_max_cost_per_day_usd,
    )
    try:
        async with session_factory() as session:
            await workshop_seed.apply_seed(session)
    except Exception as exc:  # a seed that fails must not keep the worker down
        logger.warning("worker.workshop_seed_failed", error=type(exc).__name__)
    await catch_up(sender=sender)
    tasks = [
        asyncio.create_task(fast_loop(sender), name="worker-fast"),
        asyncio.create_task(hh_loop(sender), name="worker-hh"),
        asyncio.create_task(bot_loop(sender), name="worker-bot"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":  # pragma: no cover - the process entry point
    asyncio.run(main())

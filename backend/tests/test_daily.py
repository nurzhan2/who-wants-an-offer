"""The morning summary: what it counts, what it says, and that it only notifies.

Synthetic rows only. Pinned: new vacancies per source since the run started;
the top by each vacancy's best profile, with the winner's slug and the link;
how many wait for confirmation; what broke — a failed or partial source, a
FloodWait, an expired Telegram session; and that the message goes to the
owner's own chat through the bot API and nowhere else.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.enums import PipelineRunStatus
from app.db.models import PipelineRun, SourceState
from app.services import autopilot, daily
from app.services.autopilot import ChainStepFailedError
from factories import make_match, make_profile, make_vacancy

pytestmark = pytest.mark.db


async def _vacancy(vacancies: Any, seed: str, slug: str = "hh", external: str = "1") -> Any:
    return (
        await vacancies.upsert_by_external_id(
            make_vacancy(seed, title=f"Python {seed}"),
            source_slug=slug,
            external_id=external,
            url=f"https://example.test/{slug}/{external}",
        )
    ).vacancy_id


async def test_the_summary_counts_ranks_and_lists_what_broke(
    db_session: AsyncSession, profiles: Any, vacancies: Any, matches: Any
) -> None:
    since = datetime.now(UTC) - timedelta(minutes=5)
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    llm = await profiles.create(make_profile(slug="llm", priority=1))
    first = await _vacancy(vacancies, "a", "hh", "137000001")
    second = await _vacancy(vacancies, "b", "telegram", "chan/1")
    await matches.bulk_upsert(
        [
            make_match(backend.id, first, Decimal("88")),
            make_match(llm.id, first, Decimal("70")),
            make_match(llm.id, second, Decimal("91")),
        ]
    )
    db_session.add(
        PipelineRun(
            source_slug="hh",
            status=PipelineRunStatus.PARTIAL,
            errors=[{"stage": "crawl", "detail": "проверка на робота"}],
        )
    )
    db_session.add(
        SourceState(
            source_slug="telegram",
            key="run:last",
            value={
                "flood_waits": [{"channel": "synthetic_jobs", "seconds": 37}],
                "session_expired": True,
                "dead": ["quiet_channel"],
                "missing": [],
            },
        )
    )
    await db_session.flush()

    summary = await daily.build_summary(db_session, since=since)

    assert summary.new_by_source == {"hh": 1, "telegram": 1}
    assert [(row["profile"], row["score"]) for row in summary.top] == [
        ("llm", 91.0),
        ("backend", 88.0),
    ]
    text = summary.render()
    assert "Новых вакансий: 2" in text
    assert "91 · llm · Python b" in text
    assert "https://example.test/telegram/chan/1" in text
    assert "hh: частично" in text and "проверка на робота" in text
    assert "FloodWait 37 с на @synthetic_jobs" in text
    assert "сессия протухла" in text
    assert "@quiet_channel молчит" in text
    assert "пачку подтверждаете вы" in text
    assert len(text) <= daily.TELEGRAM_LIMIT


async def test_a_failed_chain_is_reported_and_the_summary_still_comes(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    async def broken(_steps: Any) -> list[str]:
        raise ChainStepFailedError("обход закончился неуспешно.")

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(autopilot, "run_chain", broken)
    monkeypatch.setattr(daily, "session_factory", factory)

    summary = await daily.run_daily()

    assert summary.broken[0] == "цепочка: обход закончился неуспешно."


@pytest.mark.unit
async def test_the_message_goes_to_the_owners_chat_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:synthetic"))
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bot123:synthetic/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert await daily.notify("привет") is True
    body = route.calls[0].request.content.decode()
    assert '"chat_id":"42"' in body.replace(" ", "")


@pytest.mark.unit
async def test_without_a_bot_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    with respx.mock(assert_all_called=False) as router:
        assert await daily.notify("привет") is False
        assert router.calls.call_count == 0


@pytest.mark.unit
async def test_a_refusing_bot_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:synthetic"))
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    with respx.mock() as router:
        router.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=httpx.Response(401)
        )
        assert await daily.notify("привет") is False

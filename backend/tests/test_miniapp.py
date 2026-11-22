"""The Telegram Mini App: who may call it, and what it shows.

Signatures are made here the way Telegram makes them, with a synthetic bot
token; nothing reaches Telegram.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import telegram_auth
from app.core.config import settings
from app.services import miniapp, react
from factories import make_match, make_profile, make_vacancy

TOKEN = "123456:synthetic-token"
OWNER = "1001"


def signed(user_id: str, *, token: str = TOKEN, issued: float | None = None) -> str:
    fields = {
        "auth_date": str(int(issued if issued is not None else time.time())),
        "query_id": "AAE-synthetic",
        "user": json.dumps({"id": int(user_id), "first_name": "Synthetic"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def owner_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "telegram_chat_id", OWNER)


@pytest.mark.unit
def test_only_telegram_s_signature_for_this_bot_is_accepted(owner_bot: None) -> None:
    assert telegram_auth.verify(signed(OWNER)) == {"id": 1001, "first_name": "Synthetic"}
    assert telegram_auth.verify(signed(OWNER, token="999:other-bot")) is None
    assert telegram_auth.verify(signed(OWNER, issued=time.time() - 3 * 86400)) is None
    assert telegram_auth.verify(signed(OWNER).replace("Synthetic", "Mallory")) is None
    assert telegram_auth.verify("") is None
    assert telegram_auth.is_owner({"id": 1001}) and not telegram_auth.is_owner({"id": 7})


@pytest.mark.db
async def test_the_api_refuses_anyone_but_the_owner(
    async_client: AsyncClient, owner_bot: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dashboard_password", SecretStr("dashboard-secret"))
    url = f"{settings.api_v1_prefix}/miniapp/home"

    assert (await async_client.get(url)).status_code == 401
    stranger = await async_client.get(url, headers={"X-Telegram-Init-Data": signed("7")})
    assert stranger.status_code == 403
    owner = await async_client.get(url, headers={"X-Telegram-Init-Data": signed(OWNER)})
    assert owner.status_code == 200, "no dashboard password inside Telegram"
    assert set(owner.json()) == {"stats", "days"}


async def _posting(vacancies: Any, seed: str, slug: str) -> UUID:
    result = await vacancies.upsert_by_external_id(
        make_vacancy(seed, title=f"Python {seed}", published_at=datetime.now(UTC)),
        source_slug=slug,
        external_id=f"{slug}-{seed}",
        url=f"https://example.test/{slug}/{seed}",
        raw={},
    )
    return UUID(str(result.vacancy_id))


@pytest.mark.db
async def test_days_tracks_and_one_vacancy(
    db_session: AsyncSession,
    async_client: AsyncClient,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    owner_bot: None,
) -> None:
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    llm = await profiles.create(make_profile(slug="llm", priority=1))
    hh = await _posting(vacancies, "a", "hh")
    remote = await _posting(vacancies, "b", "remotive")
    low = await _posting(vacancies, "c", "remotive")
    await matches.bulk_upsert(
        [
            make_match(backend.id, hh, Decimal("90")),
            make_match(llm.id, hh, Decimal("80")),
            make_match(llm.id, remote, Decimal("88")),
            make_match(backend.id, low, Decimal("40")),
        ]
    )
    headers = {"X-Telegram-Init-Data": signed(OWNER)}

    home = (
        await async_client.get(
            f"{settings.api_v1_prefix}/miniapp/home?with_hh=true", headers=headers
        )
    ).json()
    today = datetime.now(miniapp.LOCAL).date().isoformat()
    assert home["days"] == [{"date": today, "count": 2, "tracks": {"backend": 1, "llm": 1}}]
    assert home["stats"]["matched_today"] == 2

    listed = (
        await async_client.get(
            f"{settings.api_v1_prefix}/miniapp/days/{today}/backend?with_hh=true", headers=headers
        )
    ).json()
    assert [item["vacancy_id"] for item in listed] == [str(hh)], "llm lost hh to backend"

    one = (
        await async_client.get(f"{settings.api_v1_prefix}/miniapp/vacancies/{hh}", headers=headers)
    ).json()
    assert one["slug"] == "backend" and one["is_hh"] and one["url"].endswith("/hh/a")
    assert one["letter"] is None and one["cv_filename"] is None


@pytest.mark.db
async def test_the_letter_button_writes_once_within_budget(
    db_session: AsyncSession,
    profiles: Any,
    vacancies: Any,
    matches: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.base import uuid7
    from app.db.models import Application

    written: list[UUID] = []

    async def letter(session: AsyncSession, vacancy_id: UUID, profile: Any, **_: Any) -> Any:
        written.append(vacancy_id)
        session.add(Application(id=uuid7(), vacancy_id=vacancy_id, cover_letter="Синтетическое."))
        await session.flush()

    monkeypatch.setattr(react.letters, "write_letter", letter)
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    v = await _posting(vacancies, "w", "remotive")
    await matches.bulk_upsert([make_match(backend.id, v, Decimal("90"))])

    assert await miniapp.write_letter(db_session, v) == "Синтетическое."
    assert await miniapp.write_letter(db_session, v) == "Синтетическое."
    assert written == [v]


@pytest.mark.db
async def test_hh_is_hidden_unless_asked_and_the_floor_is_the_caller_s(
    db_session: AsyncSession, profiles: Any, vacancies: Any, matches: Any
) -> None:
    """hh goes through the agent's queue; the Mini App browses everything else."""
    backend = await profiles.create(make_profile(slug="backend", priority=0))
    hh = await _posting(vacancies, "h", "hh")
    board = await _posting(vacancies, "r", "remotive")
    await matches.bulk_upsert(
        [make_match(backend.id, hh, Decimal("90")), make_match(backend.id, board, Decimal("65"))]
    )

    default = {item.vacancy_id for item in await miniapp.won_items(db_session)}
    assert default == {board}, "hh hidden, and 65 clears the browsing floor of 60"
    strict = await miniapp.won_items(db_session, min_score=70, with_hh=True)
    assert {item.vacancy_id for item in strict} == {hh}


@pytest.mark.unit
def test_a_server_reads_telegram_and_the_alert_mailbox_often(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source's built-in interval is a laptop's; the server's override wins."""
    from datetime import timedelta

    from app.sources.telegram import TelegramSource

    source = TelegramSource.__new__(TelegramSource)
    started = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "source_min_minutes", {"telegram": 15})
    assert source.cooldown_until(started) == started + timedelta(minutes=15)
    monkeypatch.setattr(settings, "source_min_minutes", {})
    assert source.cooldown_until(started) == started + timedelta(hours=6)

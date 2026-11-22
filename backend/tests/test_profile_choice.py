"""The resume a vacancy's documents are written from: the winner, in the posting's language."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import profile_choice
from factories import make_match, make_profile, make_vacancy


def resume(slug: str, track: str, priority: int, headline: str, summary: str) -> Any:
    profile = make_profile(slug=slug, track=track, priority=priority, summary=summary)
    return profile.model_copy(update={"headline": headline})


@pytest.mark.unit
def test_a_text_s_language_is_its_cyrillic_share() -> None:
    assert profile_choice.text_language("Разработал LLM-агента на FastAPI") == "ru"
    assert profile_choice.text_language("Built an LLM agent with FastAPI") == "en"
    assert profile_choice.text_language("123 · —") is None


async def _vacancy(vacancies: Any, seed: str, title: str, description: str) -> UUID:
    result = await vacancies.upsert_by_external_id(
        make_vacancy(
            seed, title=title, description_raw=description, published_at=datetime.now(UTC)
        ),
        source_slug="remotive",
        external_id=seed,
        url=f"https://example.test/{seed}",
        raw={},
    )
    return UUID(str(result.vacancy_id))


@pytest.mark.db
async def test_an_english_posting_gets_the_english_resume_of_the_winning_track(
    db_session: AsyncSession, profiles: Any, vacancies: Any, matches: Any
) -> None:
    """22 Nov: English headings over Russian bullets, from backend-ru on an English posting."""
    ru = await profiles.create(
        resume(
            "backend-ru",
            "backend",
            0,
            "Бэкенд-разработчик Python",
            "Разрабатываю сервисы на FastAPI.",
        )
    )
    en = await profiles.create(
        resume("backend-en", "backend", 1, "Python Backend Developer", "I build FastAPI services.")
    )
    llm_en = await profiles.create(
        resume("llm-en", "llm", 2, "LLM Engineer", "I build LLM agents.")
    )
    english = await _vacancy(
        vacancies, "en1", "Backend Engineer", "We build APIs with Python and FastAPI."
    )
    russian = await _vacancy(
        vacancies, "ru1", "Python-разработчик", "Разрабатываем сервисы на Python и FastAPI."
    )
    await matches.bulk_upsert(
        [
            make_match(ru.id, english, Decimal("90")),
            make_match(en.id, english, Decimal("85")),
            make_match(llm_en.id, english, Decimal("80")),
            make_match(ru.id, russian, Decimal("90")),
            make_match(en.id, russian, Decimal("85")),
        ]
    )

    assert await profile_choice.for_vacancy(db_session, english) == en.id
    assert await profile_choice.for_vacancy(db_session, russian) == ru.id, (
        "the winner already matches"
    )

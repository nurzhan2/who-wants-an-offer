"""The Telegram Mini App's API. Every route requires the owner's signed initData.

See :mod:`app.services.miniapp` for what is shown and
:mod:`app.core.telegram_auth` for why this sits outside the dashboard password.
"""

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telegram_auth import require_owner
from app.db.session import get_session
from app.services import miniapp

router = APIRouter(prefix="/miniapp", tags=["miniapp"], dependencies=[Depends(require_owner)])

Session = Annotated[AsyncSession, Depends(get_session)]
MinScore = Annotated[int, Query(ge=0, le=100)]
WithHh = Annotated[bool, Query()]


@router.get("/home", summary="Statistics and the days with vacancies")
async def home(
    session: Session, min_score: MinScore = miniapp.BROWSE_MIN_SCORE, with_hh: WithHh = False
) -> dict[str, Any]:
    items = await miniapp.won_items(session, min_score=min_score, with_hh=with_hh)
    return {"stats": await miniapp.stats(session, items), "days": miniapp.by_day(items)}


@router.get("/days/{day}/{slug}", summary="One track's vacancies of one day, best first")
async def day_track(
    day: date,
    slug: str,
    session: Session,
    min_score: MinScore = miniapp.BROWSE_MIN_SCORE,
    with_hh: WithHh = False,
) -> list[dict[str, Any]]:
    items = await miniapp.won_items(session, min_score=min_score, with_hh=with_hh)
    return [item.brief() for item in miniapp.of_day_and_track(items, day, slug)]


@router.get("/vacancies/{vacancy_id}", summary="Where to apply, the letter and the CV")
async def vacancy(vacancy_id: UUID, session: Session) -> dict[str, Any]:
    found = await miniapp.detail(session, vacancy_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вакансия больше не активна")
    return found


@router.post("/vacancies/{vacancy_id}/letter", summary="Write the letter now")
async def letter(vacancy_id: UUID, session: Session) -> dict[str, Any]:
    text = await miniapp.write_letter(session, vacancy_id)
    if text is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Письмо не написано: дневной бюджет модели исчерпан"
        )
    return {"letter": text}


@router.post("/vacancies/{vacancy_id}/cv", summary="Send the CV file to the owner's chat")
async def cv(vacancy_id: UUID, session: Session) -> dict[str, Any]:
    if not await miniapp.send_cv(session, vacancy_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Нет файла резюме: замените файл на дашборде")
    return {"sent": True}

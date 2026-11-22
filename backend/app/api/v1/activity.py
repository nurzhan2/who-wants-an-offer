"""What the owner's long actions are doing: running, done, or failed and why.

The page asks this after a reload instead of trusting its own memory; see
:mod:`app.services.activity`.
"""

from fastapi import APIRouter

from app.services import activity

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("", summary="Recent long actions, newest first")
async def recent() -> list[dict[str, object]]:
    return activity.recent()

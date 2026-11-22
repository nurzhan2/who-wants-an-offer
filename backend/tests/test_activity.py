"""Long actions survive a reload, and the page can ask how each one ended."""

import asyncio

import pytest

from app.core.exceptions import AppError
from app.services import activity

pytestmark = pytest.mark.unit


async def test_a_finished_action_is_remembered_with_its_line() -> None:
    async def work() -> int:
        return 3

    assert (
        await activity.run("letter", "v-done", "Backend", work, describe=lambda n: f"{n} ok") == 3
    )
    entry = next(item for item in activity.recent() if item["key"] == "v-done")
    assert entry["state"] == "done" and entry["detail"] == "3 ok" and entry["finished_at"]


async def test_a_failure_is_remembered_with_a_reason_the_owner_can_read() -> None:
    async def work() -> None:
        raise AppError("дневной бюджет модели исчерпан")

    with pytest.raises(AppError):
        await activity.run("cv", "v-failed", "Backend", work)
    entry = next(item for item in activity.recent() if item["key"] == "v-failed")
    assert entry["state"] == "failed" and entry["detail"] == "дневной бюджет модели исчерпан"


async def test_a_reload_cancels_the_wait_not_the_work() -> None:
    """22 Nov: a reload during a letter could cancel the letter itself."""
    release = asyncio.Event()

    async def work() -> str:
        await release.wait()
        return "letter"

    waiter = asyncio.create_task(activity.run("letter", "v-reload", "Backend", work))
    await asyncio.sleep(0)
    waiter.cancel()  # the browser went away
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert next(i for i in activity.recent() if i["key"] == "v-reload")["state"] == "running"

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert next(i for i in activity.recent() if i["key"] == "v-reload")["state"] == "done"

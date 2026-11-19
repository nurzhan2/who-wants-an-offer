"""Storing the job titles the owner searches for, and carrying them to a new resume.

The titles are typed by hand; a new file is not a new job search, and losing
them on upload would silently put the search back on skill keywords.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateProfile
from app.db.repositories.profile import ProfileRepository
from factories import make_profile


async def stored_profile(
    profiles: ProfileRepository,
    session: AsyncSession,
    *,
    titles: list[str] | None = None,
    active: bool = True,
) -> CandidateProfile:
    """A profile row with titles set on it; the extraction schema carries none."""
    instance = await profiles.create(make_profile())
    instance.target_titles = list(titles or [])
    instance.is_active = active
    await session.flush()
    return instance


async def test_patch_stores_the_cleaned_list(
    async_client: AsyncClient, profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """What the owner typed, cleaned, is what the row holds."""
    profile = await stored_profile(profiles, db_session)

    response = await async_client.patch(
        f"/api/v1/profile/{profile.id}",
        json={"target_titles": [" Python Developer", "python developer", "Junior Python"]},
    )

    assert response.status_code == 200
    assert response.json()["target_titles"] == ["Python Developer", "Junior Python"]
    stored = await db_session.scalar(
        select(CandidateProfile.target_titles).where(CandidateProfile.id == profile.id)
    )
    assert stored == ["Python Developer", "Junior Python"]


async def test_a_replacement_inherits_the_replaced_profiles_titles(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """A new version of the same resume is not a new job search."""
    old_id = (await stored_profile(profiles, db_session, titles=["Python Developer"])).id
    new_id = (await stored_profile(profiles, db_session, active=False)).id
    await profiles.activate(new_id)

    await profiles.take_over(new_id, old_id)

    db_session.expire_all()
    assert (await profiles.get(new_id)).target_titles == ["Python Developer"]  # type: ignore[union-attr]
    assert (await profiles.get(old_id)).is_active is False  # type: ignore[union-attr]


async def test_a_replacement_with_its_own_titles_keeps_them(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """What the owner typed on the new profile wins over the old list."""
    old_id = (await stored_profile(profiles, db_session, titles=["Python Developer"])).id
    new_id = (await stored_profile(profiles, db_session, titles=["Data Engineer"])).id

    await profiles.take_over(new_id, old_id)

    db_session.expire_all()
    assert (await profiles.get(new_id)).target_titles == ["Data Engineer"]  # type: ignore[union-attr]


async def test_a_new_track_does_not_inherit_another_tracks_titles(
    profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """Since phase 22 an upload without ``replace`` is a new track: the LLM
    resume must not start searching with the backend resume's titles."""
    await stored_profile(profiles, db_session, titles=["Python Developer"])
    new = await stored_profile(profiles, db_session)

    assert new.target_titles == []

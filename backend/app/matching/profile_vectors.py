"""Each active profile's two vectors, computed once when they are missing.

Moved here from ``scripts/run_matching.py`` on 2026-09-16, unchanged in
behaviour, because the dashboard now starts a rescoring from a button and the
API process must not import a script. The script calls this module; so does
``app.services.operations``.

Not the raw resume: :func:`encode_profile` embeds the extracted competencies,
because a vector built from the document describes its formatting, which every
resume shares. The headline is embedded alone, as the profile's side of title
similarity.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandidateProfile, ProfileSkill
from app.db.repositories.profile import ProfileRepository
from app.matching.embeddings import EmbeddingError, encode_profile, encode_texts


async def ensure_profile_embedding(session: AsyncSession, *, allowed: bool) -> str | None:
    """Embed every active profile that has no vector. Returns a line for the report.

    Every active profile, not one: since migration 0018 several are scored at
    once, and a profile without its vectors would score on rules alone and lose
    every vacancy to one that has them. These are the only vectors a new
    profile costs — the vacancies' are shared.

    Commits after each vector it writes, so a later failure in the caller does
    not throw away a model call that already succeeded. ``None`` when there is
    no active profile or nothing had to be done.
    """
    profiles = await ProfileRepository(session).list_active()
    lines: list[str] = []
    for profile in profiles:
        notes = [await _ensure_headline_embedding(session, profile, allowed=allowed)]
        notes.append(await _ensure_resume_embedding(session, profile, allowed=allowed))
        written = [note for note in notes if note]
        if written:
            prefix = f"{profile.slug}: " if len(profiles) > 1 else ""
            lines.append(prefix + "; ".join(written))
    return "; ".join(lines) if lines else None


async def _ensure_headline_embedding(
    session: AsyncSession, profile: CandidateProfile, *, allowed: bool
) -> str | None:
    """Embed the headline alone, the profile's side of title similarity."""
    if profile.headline_embedding is not None:
        return None
    headline = (profile.headline or "").strip()
    if not headline:
        return "у профиля нет заголовка, название вакансии сравнивать не с чем"
    if not allowed:
        return "у заголовка профиля нет эмбеддинга, название не сравнивается (--no-embed-profile)"
    try:
        vector = (await encode_texts([headline]))[0]
    except EmbeddingError as error:
        return f"эмбеддинг заголовка не посчитан: {error}"
    await ProfileRepository(session).set_headline_embedding(profile.id, vector)
    await session.commit()
    return "эмбеддинг заголовка посчитан впервые"


async def _ensure_resume_embedding(
    session: AsyncSession, profile: CandidateProfile, *, allowed: bool
) -> str | None:
    """Embed the extracted competencies, the profile's side of description similarity."""
    if profile.embedding is not None:
        return None
    if not allowed:
        return "у профиля нет эмбеддинга, семантика не считается (--no-embed-profile)"
    names = (
        await session.execute(
            select(ProfileSkill.canonical_name).where(ProfileSkill.profile_id == profile.id)
        )
    ).all()
    try:
        vector = await encode_profile(
            headline=profile.headline,
            skills=[row[0] for row in names],
            titles=[],
            domains=[],
        )
    except EmbeddingError as error:
        return f"эмбеддинг профиля не посчитан: {error}"
    await ProfileRepository(session).set_embedding(profile.id, vector)
    await session.commit()
    return "эмбеддинг профиля посчитан впервые"

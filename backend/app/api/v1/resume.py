"""Resume upload endpoints: one file, or several at once with a slug each."""

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile, status
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ParsingError
from app.db.session import get_session
from app.schemas.profile import (
    HHResumeId,
    ProfileSlug,
    ProfileTrack,
    ResumeBatchItem,
    ResumeBatchResponse,
    ResumeUploadResponse,
)
from app.services import resume as resume_service

router = APIRouter(prefix="/resume", tags=["resume"])

#: How many files one batch may carry. The owner has six tracks; a ceiling keeps
#: one request from staging an unbounded number of uploads on disk.
MAX_BATCH_FILES = 12

_BATCH_META = TypeAdapter(list[ResumeBatchItem])


async def _read_bounded(file: UploadFile) -> bytes:
    """The file's bytes, refused past the configured size before it is all read."""
    # Bounded read: an UploadFile is a stream, and reading it whole before
    # checking the size would let a large upload decide our memory use.
    limit = settings.resume_max_file_size_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise ParsingError(f"file is larger than the {settings.resume_max_file_size_mb} MB limit")
    return content


def _answer(accepted: resume_service.UploadAccepted, filename: str) -> ResumeUploadResponse:
    return ResumeUploadResponse(
        profile_id=accepted.profile_id,
        parse_status=accepted.parse_status,
        slug=accepted.slug,
        replaces=accepted.replaces,
        filename=filename,
    )


@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResumeUploadResponse,
    summary="Upload a resume and start parsing it",
)
async def upload_resume(
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(description="PDF, DOCX, TXT or Markdown, up to 10 MB.")],
    replace: Annotated[
        ProfileSlug | None,
        Query(description="Slug of the profile this file replaces once it has parsed."),
    ] = None,
    slug: Annotated[ProfileSlug | None, Query(description="Name for a new profile.")] = None,
    track: Annotated[ProfileTrack | None, Query()] = None,
    priority: Annotated[int | None, Query(ge=0, le=10_000)] = None,
    hh_resume_id: Annotated[HHResumeId | None, Query()] = None,
) -> ResumeUploadResponse:
    """Accept a resume and hand back an id to poll.

    202, not 201: the profile exists but is not finished. Parsing needs an LLM
    round trip and an embedding, which is far longer than a request should hold
    a connection open for.

    **It no longer retires the other profiles.** Several resumes are active at
    once and every vacancy is scored against each. ``?replace=<slug>`` names the
    one this file supersedes; that profile steps down only after the new one
    has parsed, and hands over its slug and settings.
    """
    content = await _read_bounded(file)
    filename = file.filename or "resume"
    accepted, path = await resume_service.accept_upload(
        session,
        content=content,
        filename=filename,
        target=resume_service.UploadTarget(
            slug=slug, track=track, priority=priority, hh_resume_id=hh_resume_id, replace=replace
        ),
    )
    await session.commit()

    background.add_task(
        resume_service.parse_in_background, accepted.profile_id, path, replaces=accepted.replaces
    )
    return _answer(accepted, filename)


@router.post(
    "/batch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResumeBatchResponse,
    summary="Upload several resumes at once, each with its own slug and track",
)
async def upload_batch(
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    files: Annotated[list[UploadFile], File(description="Up to 12 resumes.")],
    meta: Annotated[
        str | None,
        Form(
            description=(
                "JSON list, one object per file in the same order: "
                '[{"slug": "backend", "track": "backend", "hh_resume_id": "...", '
                '"replace": null}]. Shorter than the file list means the rest get none.'
            )
        ),
    ] = None,
) -> ResumeBatchResponse:
    """Stage every file, then parse them one after another in the background.

    All or nothing at the request: every file is size-checked and every slug is
    validated before the first profile row is written, so a typo in the third
    slug does not leave two of six resumes uploaded. Parsing itself is
    sequential — one LLM call at a time, as for a single upload.
    """
    if not files:
        raise ParsingError("no files in the batch")
    if len(files) > MAX_BATCH_FILES:
        raise ParsingError(f"at most {MAX_BATCH_FILES} files in one batch")
    try:
        items = _BATCH_META.validate_python(json.loads(meta)) if meta else []
    except (ValueError, ValidationError) as error:
        raise ParsingError(f"meta is not a valid list of profile descriptions: {error}") from error
    if len(items) > len(files):
        raise ParsingError("meta describes more profiles than there are files")
    items += [ResumeBatchItem() for _ in range(len(files) - len(items))]
    named = [item.slug for item in items if item.slug] + [
        item.replace for item in items if item.replace
    ]
    if len(named) != len(set(named)):
        raise ParsingError("two files in one batch name the same profile")

    contents = [await _read_bounded(file) for file in files]
    targets = [
        resume_service.UploadTarget(
            slug=item.slug,
            track=item.track,
            priority=item.priority,
            hh_resume_id=item.hh_resume_id,
            replace=item.replace,
        )
        for item in items
    ]
    for target in targets:
        await resume_service.resolve_target(session, target)

    staged: list[tuple[resume_service.UploadAccepted, Path, str]] = []
    answers: list[ResumeUploadResponse] = []
    for file, content, target in zip(files, contents, targets, strict=True):
        filename = file.filename or "resume"
        accepted, path = await resume_service.accept_upload(
            session, content=content, filename=filename, target=target
        )
        staged.append((accepted, path, filename))
        answers.append(_answer(accepted, filename))
    await session.commit()

    background.add_task(
        resume_service.parse_batch_in_background,
        [(accepted.profile_id, path, accepted.replaces) for accepted, path, _ in staged],
    )
    return ResumeBatchResponse(items=answers)

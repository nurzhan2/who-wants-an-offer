"""Orchestration: an uploaded file becomes a stored, embedded profile.

The whole pipeline in one place, so the order and the failure handling are
readable together:

    extract → LLM (the PDF itself, or text) → enrich → persist → embed

Two things this module is careful about.

**PDFs are handed to the model as documents.** The text pdfplumber produces is
stored for full-text search and never sent to the model, because resumes are
usually two-column and line-oriented extraction reads straight across them.

**Nothing here logs resume content.** Every log line carries the profile id, the
format, sizes and durations. The same goes for ``parse_error``, which reaches
both the API and the logs.
"""

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.enums import ParseStatus
from app.db.repositories.profile import ProfileRepository
from app.llm import usage as usage_ledger
from app.llm.base import Document, LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router
from app.matching import embeddings
from app.resume import ats_audit, enricher
from app.resume.extractor import ExtractedDocument
from app.schemas.llm import ProfileExtraction
from app.schemas.profile import CandidateProfileCreate, ExperienceCreate, SkillCreate

logger = get_logger(__name__)

#: Which task this is. Everything else — provider, model, effort, tool policy —
#: follows from it through configuration, so moving resume extraction between
#: providers never touches this module.
TASK = LLMTask.RESUME_EXTRACTION


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What building a profile produced, for logs and for the report."""

    profile_id: UUID
    status: ParseStatus
    skill_count: int
    total_years: float
    usage: LLMUsage | None
    cost_usd: float | None
    duration_seconds: float
    warnings: tuple[str, ...]


def _documents_for(document: ExtractedDocument) -> tuple[Document, ...]:
    """The file itself, when the model can read it better than we can.

    PDF only. DOCX and plain text have no column problem worth solving, and
    sending them as text keeps the request small. How the file reaches the
    model is the provider's business: the API inlines it as base64, the CLI
    writes it into a directory of its own and reads it.
    """
    if document.source_format == "pdf":
        return (
            Document(content=document.file_bytes, media_type="application/pdf", path="resume.pdf"),
        )
    return ()


def _text_for(document: ExtractedDocument) -> str:
    """The prompt variable carrying the resume, for non-PDF formats.

    For a PDF the text is deliberately absent: pdfplumber reads two-column
    layouts straight across, so its output would be worse than no text at all.
    The file goes to the model instead.
    """
    if document.source_format == "pdf":
        return "(the resume is attached as a file — read it rather than looking for it here)"
    return document.raw_text


async def extract_profile(
    document: ExtractedDocument,
    *,
    router: LLMRouter,
    today: date,
) -> tuple[ProfileExtraction, LLMUsage]:
    """Ask whichever provider serves this task to read the resume."""
    result = await router.complete_json(
        "extract_profile",
        ProfileExtraction,
        task=TASK,
        variables={"today": today.isoformat(), "resume_text": _text_for(document)},
        documents=_documents_for(document),
    )
    return result.value, usage_ledger.record(result.usage)


def to_profile_create(
    extraction: ProfileExtraction,
    enriched: enricher.EnrichedProfile,
    document: ExtractedDocument,
) -> CandidateProfileCreate:
    """Assemble what gets written, taking experience from the computation.

    ``total_years`` comes from ``enriched``, never from
    ``extraction.stated_total_years``. That field records the resume's own
    claim so the two can be compared; using it would reintroduce the very bug
    the dates module exists to prevent.
    """
    return CandidateProfileCreate(
        name=extraction.full_name,
        headline=extraction.headline,
        seniority=enriched.seniority,
        total_years=enriched.total_years,
        summary=extraction.summary,
        locations=[extraction.city] if extraction.city else [],
        relocation=bool(extraction.relocation),
        remote_pref=extraction.remote_pref,
        salary_min=(
            None
            if extraction.salary_expectation is None
            else round(extraction.salary_expectation, 2)
        ),
        salary_currency=extraction.salary_currency,
        languages=[language.model_dump() for language in extraction.languages],
        education=[degree.model_dump() for degree in extraction.education],
        projects=to_projects(extraction),
        raw_text=document.raw_text,
        skills=[
            SkillCreate(
                canonical_name=skill.canonical_name,
                raw_names=list(skill.raw_names),
                years=skill.years,
                level=skill.level,
                evidence=skill.evidence,
                last_used_year=skill.last_used_year,
            )
            for skill in enriched.skills
        ],
        experience=to_experience(extraction),
    )


def to_experience(extraction: ProfileExtraction) -> list[ExperienceCreate]:
    """The extraction's work periods, as rows, in the order the resume gave them.

    ``position`` is the index in that order and it is the handle a generated CV
    refers to a job by, so it has to be assigned here rather than derived later:
    a stored arrangement names ``ref 2``, and ``ref 2`` has to keep meaning the
    same job for as long as that version is readable.

    A period with neither an employer nor a title is dropped. There is nothing
    to print on the line, and a blank entry in a CV reads to an employer as
    something hidden rather than as something missing.

    Dates are passed through exactly as the extraction normalised them —
    "YYYY-MM", or None where the resume gave none — because this is the last
    place they could be quietly changed and the whole point of storing them is
    that a generated document cannot.

    Achievements are the same kind of quotation and are treated the same way:
    trimmed, bullet markers removed, and otherwise kept as the resume wrote
    them. See :func:`clean_achievements` for the little that is done to them.
    """
    rows: list[ExperienceCreate] = []
    for period in extraction.work_periods:
        company = period.company.strip()
        title = period.title.strip()
        if not company and not title:
            continue
        rows.append(
            ExperienceCreate(
                position=len(rows),
                company=company[:300],
                title=title[:300],
                start=period.start,
                end=period.end,
                is_current=period.is_current,
                stack=[name.strip() for name in period.stack if name.strip()],
                domains=[name.strip() for name in period.domains if name.strip()],
                achievements=clean_achievements(period.achievements),
            )
        )
    return rows


#: Most projects kept. A resume listing more is a portfolio page; a CV shows
#: two or three, and the extraction's first ones are the resume's own lead.
MAX_PROJECTS = 8


def to_projects(extraction: ProfileExtraction) -> list[dict[str, object]]:
    """The extraction's projects as stored records, bullets cleaned like a job's.

    A project with no name is dropped — there is nothing to print on its line —
    and so is one whose name is an employer from ``work_periods``: work done in
    a job belongs to that job's bullets, and printing it twice would make one
    piece of work look like two.
    """
    employers = {period.company.strip().casefold() for period in extraction.work_periods}
    kept: list[dict[str, object]] = []
    for project in extraction.projects:
        name = " ".join(project.name.split())[:200]
        if not name or name.casefold() in employers:
            continue
        link = (project.link or "").strip() or None
        kept.append(
            {
                "name": name,
                "link": link[:300] if link else None,
                "stack": [item.strip() for item in project.stack if item.strip()],
                "year": project.year,
                "achievements": clean_achievements(project.achievements),
            }
        )
    return kept[:MAX_PROJECTS]


#: Most bullets kept per job. A resume with more than this under one employer is
#: a duty list rather than a set of results, and nothing downstream shows more
#: than five; the cap only stops a pathological extraction from filling a row.
MAX_ACHIEVEMENTS_PER_JOB = 12

#: Longest bullet kept, in characters. A real bullet is one or two lines; past
#: this the extraction has glued a paragraph together, and a CV line that long
#: is not one an employer reads.
MAX_ACHIEVEMENT_CHARS = 400

#: A bullet glyph or list number at the start of an extracted line. The model is
#: asked for the text without one, and sometimes keeps it anyway; stored with
#: the glyph, a bullet would print twice once the renderer adds its own. A
#: number counts as a marker only when a space follows it, so "3.5x faster"
#: keeps its figure.
_LEADING_MARKER = re.compile(
    r"^\s*(?:[-*\u2022\u2013\u2014\u00b7\u25aa\u25cf\u25cb\u25e6\u25ba\u2713\u2714]\s*|\d{1,2}[.)]\s+)"
)


def clean_achievements(raw: list[str]) -> list[str]:
    """The job's bullets as stored: verbatim, minus the bullet glyph and blanks.

    Nothing is reworded, merged or translated — a generated document prints
    these strings as they are, and the guarantee that a number in a CV is the
    owner's own rests on nobody between the resume and the renderer having
    touched them. Internal whitespace is collapsed, because a line break inside
    a bullet is the PDF's layout and not the author's text. Duplicates go: the
    same line read twice off a two-column page is one achievement.
    """
    kept: list[str] = []
    for item in raw:
        text = " ".join(_LEADING_MARKER.sub("", item).split())
        if not text or len(text) > MAX_ACHIEVEMENT_CHARS or text in kept:
            continue
        kept.append(text)
    return kept[:MAX_ACHIEVEMENTS_PER_JOB]


async def _record_ats_report(
    profiles: ProfileRepository,
    profile_id: UUID,
    document: ExtractedDocument,
    extraction: ProfileExtraction,
) -> None:
    """Redo the readability audit now that there is something to compare against.

    Upload could only judge the file itself. This run adds the comparison the
    report exists for: what the model read off the page, against what survives
    into the text layer an employer's parser sees.

    Failure here is logged and dropped. The audit is a diagnostic; losing it
    must not fail a parse that otherwise succeeded, and the profile keeps the
    structural report written at upload rather than nothing.
    """
    try:
        report = await asyncio.to_thread(
            ats_audit.audit,
            document.file_bytes,
            source_format=document.source_format,
            raw_text=document.raw_text,
            extraction=extraction,
        )
    except Exception as exc:
        logger.exception("resume.ats_coverage_failed", error=type(exc).__name__)
        return
    await profiles.set_ats_report(profile_id, report)


async def build_profile(
    document: ExtractedDocument,
    *,
    session: AsyncSession,
    profile_id: UUID,
    router: LLMRouter | None = None,
    today: date | None = None,
    replaces: UUID | None = None,
) -> BuildResult:
    """Fill in a profile row that already exists in ``pending``.

    ``replaces`` is the profile this upload was sent to replace
    (``?replace=<slug>``). It is retired only here, after the new one parsed:
    a failed upload must not leave the track with no resume at all. Without it
    the new profile simply joins the active ones.

    The row is created by the upload endpoint so the client has an id to poll
    immediately. Everything here happens in the caller's transaction: a partial
    profile is worse than none, so either the whole thing lands or the row is
    marked failed with a reason.
    """
    started = time.perf_counter()
    today = today or datetime.now(UTC).date()
    profiles = ProfileRepository(session)
    usage: LLMUsage | None = None
    cost: float | None = None

    try:
        extraction, usage = await extract_profile(
            document, router=router or get_router(), today=today
        )
        cost = usage.cost_usd
        enriched = enricher.enrich(extraction, today=today)
        payload = to_profile_create(extraction, enriched, document)

        await profiles.update_from_extraction(profile_id, payload)
        await profiles.replace_skills(profile_id, payload.skills)
        await profiles.replace_experience(profile_id, payload.experience)
        await _record_ats_report(profiles, profile_id, document, extraction)

        vector = await embeddings.encode_profile(
            headline=payload.headline,
            skills=[skill.canonical_name for skill in enriched.skills],
            titles=list(enriched.titles),
            domains=list(enriched.domains),
        )
        await profiles.set_embedding(profile_id, vector)
        # The row was created inactive so a half-parsed profile could never be
        # scored against, and this is the only place that undoes that. Since
        # migration 0018 nothing else is retired by an upload: several resumes
        # are live at once, one per track. Only the profile this upload was
        # named to replace steps down, and hands over its name and settings.
        await profiles.activate(profile_id)
        if replaces is not None:
            await profiles.take_over(profile_id, replaces)
        await profiles.set_parse_status(profile_id, ParseStatus.READY)

    except AppError as exc:
        # Every expected failure in this pipeline is an AppError, and the
        # handler catches the base class rather than a list of subclasses on
        # purpose. Listing ParsingError and LLMError individually silently
        # excluded EmbeddingError — a sibling, not a subclass — so a missing
        # [embeddings] extra left the profile pending for ever with no reason
        # recorded. A new failure mode must not be able to reintroduce that.
        # Anything that is not an AppError is a bug and is re-raised for the
        # caller to log with its traceback.
        await profiles.set_parse_status(profile_id, ParseStatus.FAILED, error=str(exc))
        duration = time.perf_counter() - started
        logger.warning(
            "resume.parse_failed",
            profile_id=str(profile_id),
            reason=type(exc).__name__,
            source_format=document.source_format,
            duration_seconds=round(duration, 2),
            cost_usd=cost,
        )
        return BuildResult(
            profile_id=profile_id,
            status=ParseStatus.FAILED,
            skill_count=0,
            total_years=0.0,
            usage=usage,
            cost_usd=cost,
            duration_seconds=duration,
            warnings=(str(exc),),
        )

    duration = time.perf_counter() - started
    logger.info(
        "resume.parsed",
        profile_id=str(profile_id),
        source_format=document.source_format,
        page_count=document.page_count,
        size_bytes=document.size_bytes,
        skills=len(enriched.skills),
        jobs=len(payload.experience),
        total_years=float(enriched.total_years),
        stated_years_delta=(
            None if enriched.stated_years_delta is None else float(enriched.stated_years_delta)
        ),
        provider=usage.provider if usage else None,
        model=usage.model if usage else None,
        accounting=usage.accounting if usage else None,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        cost_usd=cost,
        duration_seconds=round(duration, 2),
    )
    return BuildResult(
        profile_id=profile_id,
        status=ParseStatus.READY,
        skill_count=len(enriched.skills),
        total_years=float(enriched.total_years),
        usage=usage,
        cost_usd=cost,
        duration_seconds=duration,
        warnings=enriched.warnings,
    )

"""Arranging one CV: ask, check the answer, ask once more, then fall back.

The shape docs/ENGINEERING.md prescribes for every LLM feature — validate with a Pydantic
model, one retry carrying the error, then a rule-based fallback — with the same
two kinds of wrong answer the letter generator distinguishes:

* **the wrong shape**, which the provider handles: it validates
  :class:`CVDraft` and feeds the validator's own message back;
* **the wrong content**, which is this module's business, and which here means
  an arrangement that names a skill the profile has not got, renames one skill
  into another, refers to a job that does not exist, attributes a technology
  to an employer the resume never connected it to, picks a bullet the job does
  not have, or writes a summary with a figure nobody recorded.

The second check is code reading the answer and then code reading the finished
document, never an instruction in the prompt. The prompt does say all of it —
that raises the first-pass hit rate and costs nothing — but what makes it true is
:mod:`app.documents.guard` running twice and the arrangement being thrown away
when it fails.

**The fallback is a real CV, not a stub.** ``compose_fallback`` arranges the same
document from the context alone: skills ordered by the vacancy's own requirement
list first, jobs in the order the resume gave them, each job's achievements in
the resume's own order up to the per-job ceiling, each job's whole recorded
stack, the profile's own summary. It is not tailored beyond that ordering, and
that is the entire difference between it and a model's answer — the facts are
identical, because in both cases the facts come from the database. A person can
see which they got from :attr:`GeneratedCV.source`.

**The fallback is checked too**, and by the same functions. "Safe by
construction" is an argument, not a guarantee: the fallback is assembled from
the profile, so it cannot invent a skill, but it can still be too short to be a
CV — a profile with two skills and no jobs produces a name and a heading. So it
goes through :func:`app.documents.guard.check_document` like the model's answer,
and when even it fails, :func:`generate` raises :class:`CVUnwritableError`
rather than returning something the caller will store and hand over.

**What this module does not do is render.** It produces an arrangement and the
text that arrangement makes; turning that into a file and auditing the file
belong to :mod:`app.documents.render` and :mod:`app.documents.review`, because
the checks here have to run before anything is written anywhere.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import AppError, LLMError
from app.core.logging import get_logger
from app.db.enums import RuleScope
from app.documents import guard
from app.documents import prompt as prompt_builder
from app.documents.context import (
    MAX_BULLETS_OLDER_JOB,
    MAX_BULLETS_PER_JOB,
    MAX_EXPERIENCE_ENTRIES,
    RECENT_JOBS,
    CVContext,
    SkillChoice,
)
from app.documents.guard import CVProblem
from app.documents.render import CVArrangement, JobChoice, fits_one_page, to_text
from app.letters.context import fold, text_language
from app.letters.prompt import FENCE_CLOSE, FENCE_OPEN
from app.llm import usage as usage_ledger
from app.llm.base import LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router
from app.workshop import rules as workshop_rules
from app.workshop.rules import RuleSpec, RuleViolation

logger = get_logger(__name__)

#: Which task this is. Provider, model, effort and tool policy all follow from
#: it through configuration; like COVER_LETTER it is denied every tool, because
#: its prompt carries a vacancy description fetched from a job board.
TASK = LLMTask.CV_TAILORING

PROMPT_NAME = "tailored_cv"

#: One arrangement, then one correction. The same figure the letter generator
#: settled on, and for the same reason: a third attempt has not been observed to
#: fix what a second did not, and the fallback is always available.
MAX_ATTEMPTS = 2

#: Where the arrangement came from.
CVSource = Literal["model", "fallback"]


class CVUnwritableError(AppError):
    """No arrangement could be produced that passes every hard rule.

    Raised only when the rule-based arrangement itself fails the checks, which
    means the profile has too little in it to make a CV from — no jobs and
    almost no skills. The alternative is storing a two-line document under the
    owner's name and reporting it as generated, and there is no way for them to
    find out that happened until an employer opens it.

    Declared here rather than in ``app/core/exceptions.py`` because that file
    belongs to another change; it still subclasses :class:`AppError`, so the
    problem+json handler renders it like every other domain failure.
    """

    #: 422: the request was understood and the data in it cannot yield a CV.
    status_code = 422
    title = "No usable CV could be generated"
    problem_type = "cv-unwritable"

    def __init__(
        self,
        detail: str,
        *,
        problems: tuple[CVProblem, ...] = (),
        broke_rules: tuple[RuleViolation, ...] = (),
    ) -> None:
        super().__init__(
            detail,
            problems=[problem.value for problem in problems],
            broke_rules=[violation.rule_id for violation in broke_rules],
        )
        self.broke_rules = broke_rules
        self.problems = problems


class DraftSkill(BaseModel):
    """One skill in the model's answer: which skill, and what to call it."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    #: Defaults to the canonical name, so a model that omits it has chosen not
    #: to rename rather than chosen to print an empty string.
    shown_as: str = ""


class DraftJob(BaseModel):
    """One job in the model's answer: which job, its technologies, its bullets.

    ``bullets`` are indices into the job's achievement list as the prompt shows
    it, most relevant first. Integers and nothing else: the text of a bullet is
    the owner's, and a field that could carry text is a field a model would
    eventually use to improve one.
    """

    model_config = ConfigDict(extra="forbid")

    ref: int
    stack: list[str] = Field(default_factory=list)
    bullets: list[int] = Field(default_factory=list)


class CVDraft(BaseModel):
    """What the model must return. Shape only — the content checks are code.

    Worth reading as a list of what a model is *able* to say, because that is
    the design. There is no company field, no title field, no date field, no
    level field and no bullet text — a job's bullets are chosen by index — not
    because a model would fill them in badly, but so that the question of
    whether it filled them in badly never arises.
    """

    model_config = ConfigDict(extra="forbid")

    headline: str = ""
    summary: str = ""
    skills: list[DraftSkill] = Field(default_factory=list)
    experience: list[DraftJob] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GeneratedCV:
    """One arrangement, the text it makes, and how it was arrived at.

    A dataclass rather than a Pydantic model for the same reason
    :class:`app.letters.generator.GeneratedLetter` is one: it carries
    ``LLMUsage`` objects and never crosses a serialisation boundary. The
    arrangement inside it does — and that is a Pydantic model.
    """

    arrangement: CVArrangement
    text: str
    source: CVSource
    attempts: int
    #: Everything the checks caught on the way, in order. Empty on a clean first
    #: answer. Kept even when a later attempt succeeded: "the model wanted to put
    #: Kubernetes in it" is worth seeing in a log.
    rejected_for: tuple[CVProblem, ...] = ()
    usages: tuple[LLMUsage, ...] = ()

    @property
    def cost_usd(self) -> float | None:
        """What the attempts cost, or None when no model priced them."""
        priced = [usage.cost_usd for usage in self.usages if usage.cost_usd is not None]
        return sum(priced) if priced else None


def to_arrangement(draft: CVDraft, context: CVContext) -> CVArrangement:
    """Turn a validated draft into an arrangement, normalising what it omitted.

    Two normalisations, both of which turn an incomplete answer into a defined
    one rather than into a rejection:

    * a skill with no ``shown_as`` is shown under its canonical name;
    * the job list is truncated at :data:`app.documents.context.
      MAX_EXPERIENCE_ENTRIES`, which the prompt states, so a model that returned
      every job gets the ceiling applied rather than the whole answer thrown
      away over a rule about length.

    Duplicate skills are collapsed, keeping the first: a repeated entry is a
    model listing something twice, and a CV that names PostgreSQL three times
    reads worse than one that names it once. Duplicate refs are collapsed for a
    stronger reason — the same job printed twice is a document that misrepresents
    a career, and it is the sort of thing nobody notices in review.

    Bullet indices are passed through untouched, duplicates and all. An index
    that is out of range or repeated is a mistake about the candidate's record,
    not about formatting, and :func:`app.documents.guard.check_experience`
    rejects it so the retry is told; repairing it here would hide the fact that
    the model misread which bullets exist.
    """
    skills: list[SkillChoice] = []
    seen_skills: set[str] = set()
    for item in draft.skills:
        key = fold(item.canonical_name)
        if key in seen_skills:
            continue
        seen_skills.add(key)
        shown = item.shown_as.strip() or item.canonical_name.strip()
        skills.append(SkillChoice(canonical_name=item.canonical_name.strip(), shown_as=shown))

    jobs: list[JobChoice] = []
    seen_refs: set[int] = set()
    for job in draft.experience:
        if job.ref in seen_refs:
            continue
        seen_refs.add(job.ref)
        jobs.append(
            JobChoice(
                ref=job.ref,
                stack=tuple(name.strip() for name in job.stack if name.strip()),
                bullets=tuple(job.bullets),
            )
        )

    return CVArrangement(
        headline=draft.headline.strip(),
        summary=draft.summary.strip(),
        skills=tuple(skills),
        experience=tuple(jobs[:MAX_EXPERIENCE_ENTRIES]),
    )


def inspect(arrangement: CVArrangement, context: CVContext) -> list[CVProblem]:
    """Every hard rule, checked against the arrangement and the document it makes.

    Two passes, and both are needed. The arrangement checks catch the faults that
    are about *provenance* — a skill that is not the candidate's, a job that does
    not exist, a technology moved between employers — and they can name what is
    wrong precisely enough for a retry to fix it. The document check catches what
    is about the *finished text*: it renders the arrangement and searches the
    result for any requirement the candidate does not hold, which is the check
    the brief's own example turns on.

    The order matters for the feedback message, not for the verdict: a person and
    a retrying model both read the specific fault before the general one.
    """
    problems: list[CVProblem] = []

    if arrangement.headline and arrangement.headline not in context.allowed_headlines:
        problems.append(CVProblem.HEADLINE_NOT_SUPPORTED)
    problems.extend(guard.check_skills(arrangement.skills, context))
    problems.extend(guard.check_experience(arrangement.experience, context))
    problems.extend(guard.check_summary(arrangement.summary, context))

    text = to_text(arrangement, context)
    if FENCE_OPEN in text or FENCE_CLOSE in text:
        problems.append(CVProblem.LEAKED_FENCE)
    problems.extend(guard.check_document(text, context))

    # Deduplicated, order kept: the document check and the summary check can
    # both report an unsupported claim, and telling a model the same thing twice
    # in one feedback message reads as two separate faults.
    return list(dict.fromkeys(problems))


async def generate(
    context: CVContext,
    *,
    router: LLMRouter | None = None,
    rules: Sequence[RuleSpec] = (),
) -> GeneratedCV:
    """One arrangement for one vacancy, guaranteed to pass every hard rule.

    The guarantee is the return type's and it is checked rather than argued: a
    ``GeneratedCV`` coming out of here has been through :func:`inspect` clean,
    whichever branch produced it.

    ``rules`` are the workshop's, scoped to CV, loaded by the caller because it
    is the one holding a session. They are checked against the rendered text the
    same way the letter generator checks its own — code reading the finished
    document, never an instruction in the prompt — and a hard one that survives
    the retries is a refusal, not a warning printed beside a document that went
    out anyway.

    A bad model answer is not an error — it becomes the rule-based arrangement,
    and the caller can see from ``source`` which it got. A rule-based arrangement
    that is itself unusable *is* an error and raises :class:`CVUnwritableError`:
    there is nothing left to fall back to, and both ways of not saying so —
    storing the stub, or returning it and letting the service call that success —
    end with an employer reading it.
    """
    router = router or get_router()
    usages: list[LLMUsage] = []
    seen: list[CVProblem] = []
    seen_rules: list[RuleViolation] = []
    feedback = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await router.complete_json(
                PROMPT_NAME,
                CVDraft,
                task=TASK,
                variables=prompt_builder.variables(context, feedback=feedback),
            )
        except LLMError as exc:
            # The provider is gone, or it failed to produce the shape twice.
            # Both mean there is no answer to check; the fallback is the answer.
            # Narrow on purpose: a bug in this module must still raise.
            logger.warning(
                "documents.generate.no_model_answer",
                vacancy_id=str(context.vacancy.vacancy_id),
                attempt=attempt,
                reason=type(exc).__name__,
            )
            break

        usages.append(usage_ledger.record(result.usage))
        arrangement = fit_to_page(to_arrangement(result.value, context), context)
        problems = inspect(arrangement, context)
        broken = workshop_rules.hard(
            workshop_rules.check(to_text(arrangement, context), scope=RuleScope.CV, rules=rules)
        )
        if not problems and not broken:
            return GeneratedCV(
                arrangement=arrangement,
                text=to_text(arrangement, context),
                source="model",
                attempts=attempt,
                rejected_for=tuple(seen),
                usages=tuple(usages),
            )

        seen.extend(problems)
        seen_rules.extend(broken)
        feedback = _feedback(problems, broken)
        logger.warning(
            "documents.generate.rejected",
            vacancy_id=str(context.vacancy.vacancy_id),
            attempt=attempt,
            problems=[problem.value for problem in problems],
            broke_rules=[violation.rule_id for violation in broken],
        )

    fallback = compose_fallback(context)
    logger.warning(
        "documents.generate.fell_back",
        vacancy_id=str(context.vacancy.vacancy_id),
        problems=[problem.value for problem in seen],
    )

    remaining = inspect(fallback, context)
    remaining_rules = workshop_rules.hard(
        workshop_rules.check(to_text(fallback, context), scope=RuleScope.CV, rules=rules)
    )
    if remaining_rules and not remaining:
        # The structural checks pass and one of the owner's own rules does not.
        # That is a different sentence and a different fix — edit the rule, or
        # add what it asks for to the profile — so it is refused in its own
        # terms rather than reported as a profile too thin to write from.
        logger.warning(
            "documents.generate.fallback_breaks_rules",
            vacancy_id=str(context.vacancy.vacancy_id),
            broke_rules=[violation.rule_id for violation in remaining_rules],
        )
        raise CVUnwritableError(
            "the arrangement breaks a hard rule from the workshop and no retry "
            "fixed it: " + "; ".join(violation.message for violation in remaining_rules),
            problems=(),
            broke_rules=tuple(remaining_rules),
        )
    if remaining:
        logger.error(
            "documents.generate.fallback_unusable",
            vacancy_id=str(context.vacancy.vacancy_id),
            problems=[problem.value for problem in remaining],
            skills=len(context.profile.skills),
            jobs=len(context.experience),
        )
        raise CVUnwritableError(
            "the rule-based arrangement fails the checks too, so there is nothing "
            "left to fall back to: this profile has too little recorded in it to "
            "make a CV from",
            problems=tuple(remaining),
        )

    return GeneratedCV(
        arrangement=fallback,
        text=to_text(fallback, context),
        source="fallback",
        attempts=len(usages),
        rejected_for=tuple(seen),
        usages=tuple(usages),
    )


def _feedback(problems: Sequence[CVProblem], broken: Sequence[RuleViolation]) -> str:
    """What the model is told to fix, structural faults first.

    Two vocabularies in one message on purpose: the structural problems are this
    module's and the rule violations are the owner's own words, and a model given
    both fixes both in one more attempt rather than trading one for the other.
    """
    parts = [guard.feedback_for(list(problems))] if problems else []
    parts.extend(violation.message for violation in broken)
    return "\n".join(part for part in parts if part)


#: Most skills the rule-based CV lists. The research's range is 8 to 25 (C3);
#: past that the section is the resume's whole vocabulary rather than a list an
#: employer reads, and the ones cut are the last of the unrequested.
MAX_FALLBACK_SKILLS = 25


def compose_fallback(context: CVContext) -> CVArrangement:
    """An arrangement built from the context alone, with no model involved.

    Correct, complete and untailored beyond one ordering rule: skills the
    vacancy asked for come first, in the vacancy's own order and under the
    vacancy's own spelling, and everything else follows in the order the profile
    holds it. Jobs stay in the resume's order — reverse chronological, as they
    were written — and each shows its whole recorded stack and its achievements
    in the order the resume listed them, up to :data:`app.documents.context.
    MAX_BULLETS_PER_JOB` for the two most recent jobs and :data:`app.documents.
    context.MAX_BULLETS_OLDER_JOB` for older ones. Choosing *which* bullets
    matter for this vacancy is a judgement, and this branch makes none; the
    owner's own order is the one a person chose.

    The vacancy's spelling is used for a matched skill here, and that is not a
    liberty: ``matched`` means the fold keys agree, so the two strings are two
    names for one thing, and the employer's own word is the one their parser is
    searching for. It is the single piece of tailoring that needs no model,
    which is why it is the piece this branch keeps.

    The summary is the profile's own, and only when it passes the same check the
    model's would and is written in the CV's language: it was written by a
    person about themselves, but it is prose, and prose is where an unsupported
    claim lives — and a Russian paragraph under ``SUMMARY`` is the mixed-language
    document the owner rejected. Either way it is dropped rather than the
    document failed; nothing is lost, since the same words are already in the
    resume the owner uploaded.
    """
    by_key = {fold(skill.canonical_name): skill for skill in context.profile.skills}
    by_key.pop("", None)

    skills: list[SkillChoice] = []
    used: set[str] = set()
    for requirement in context.vacancy.key_skills:
        key = fold(requirement)
        held = by_key.get(key)
        if held is None or key in used:
            continue
        used.add(key)
        skills.append(SkillChoice(canonical_name=held.canonical_name, shown_as=requirement.strip()))
    for key, skill in by_key.items():
        if key not in used:
            skills.append(SkillChoice(canonical_name=skill.canonical_name, shown_as=skill.spelling))

    summary = (context.profile.summary or "").strip()
    if (
        guard.check_summary(summary, context)
        or text_language(summary, default=context.language) != context.language
    ):
        summary = ""

    arrangement = CVArrangement(
        headline=context.allowed_headlines[0] if context.allowed_headlines else "",
        summary=summary,
        skills=tuple(skills[:MAX_FALLBACK_SKILLS]),
        experience=tuple(
            JobChoice(
                ref=entry.ref,
                stack=entry.stack,
                bullets=tuple(
                    range(
                        min(
                            len(entry.achievements),
                            MAX_BULLETS_PER_JOB
                            if position < RECENT_JOBS
                            else MAX_BULLETS_OLDER_JOB,
                        )
                    )
                ),
            )
            for position, entry in enumerate(context.experience)
        ),
    )
    return fit_to_page(arrangement, context)


#: The fewest bullets trimming leaves a job with: two for a recent role, which
#: is the research's floor for one (S11, S27), one for an older role.
MIN_BULLETS_RECENT_JOB = 2
MIN_BULLETS_OLDER_JOB = 1


def fit_to_page(arrangement: CVArrangement, context: CVContext) -> CVArrangement:
    """The arrangement with bullets dropped, oldest job first, until it fits a page.

    A person fitting a CV to one page compresses the oldest role first and the
    least relevant line of it first, and this does the same: it removes the last
    bullet of the job furthest down the page, one at a time, re-measuring with
    :func:`app.documents.render.estimated_lines` after each, and never takes a
    recent job below :data:`MIN_BULLETS_RECENT_JOB` or an older one below
    :data:`MIN_BULLETS_OLDER_JOB`. "Last" is the arrangement's own order: for a
    model's answer that is its least relevant choice, for the rule-based one the
    resume's last bullet.

    Only bullets are ever removed, so nothing can be added to a document by
    fitting it, and a profile too large to fit even at the floors is returned at
    the floors rather than refused — a CV that runs a few lines long is a
    formatting fault, not a reason to hand over nothing.
    """
    jobs = list(arrangement.experience)
    current = arrangement
    for index in range(len(jobs) - 1, -1, -1):
        floor = MIN_BULLETS_RECENT_JOB if index < RECENT_JOBS else MIN_BULLETS_OLDER_JOB
        while not fits_one_page(current, context) and len(jobs[index].bullets) > floor:
            jobs[index] = jobs[index].model_copy(update={"bullets": jobs[index].bullets[:-1]})
            current = arrangement.model_copy(update={"experience": tuple(jobs)})
        if fits_one_page(current, context):
            break
    return current

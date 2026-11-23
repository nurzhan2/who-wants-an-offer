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

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.exceptions import AppError, LLMError
from app.core.logging import get_logger
from app.db.enums import RuleScope
from app.documents import guard
from app.documents import prompt as prompt_builder
from app.documents.context import (
    MAX_BULLETS_OLDER_JOB,
    MAX_BULLETS_PER_JOB,
    MAX_BULLETS_PER_PROJECT,
    MAX_EXPERIENCE_ENTRIES,
    MAX_PROJECTS_SHOWN,
    MAX_SUMMARY_CHARS,
    RECENT_JOBS,
    CVContext,
    ExperienceEntry,
    SkillChoice,
)
from app.documents.guard import CVProblem
from app.documents.pdf import fits_one_page
from app.documents.render import (
    CVArrangement,
    JobChoice,
    ProjectChoice,
    to_text,
)
from app.letters.context import (
    AchievementFact,
    LetterContext,
    fold,
    text_language,
    written_for,
)
from app.letters.context import build_context as letter_context
from app.letters.prompt import FENCE_CLOSE, FENCE_OPEN
from app.llm import usage as usage_ledger
from app.llm.base import LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router
from app.review import recruiter
from app.review.recruiter import Verdict
from app.workshop import rules as workshop_rules
from app.workshop.rules import RuleSpec, RuleViolation, SectionItemCountParams
from app.workshop.sections import SECTION_ALIASES, fold_heading

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

#: Model calls for arrangements in all — guard corrections and review rewrites
#: together — so two guard failures and two review rounds can both happen in
#: one generation. The reviewer's own calls are bounded by
#: ``recruiter.MAX_ROUNDS``.
MAX_DRAFT_CALLS = 4

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


class DraftProject(BaseModel):
    """One project in the model's answer: which project, which of its bullets."""

    model_config = ConfigDict(extra="forbid")

    ref: int
    bullets: list[int] = Field(default_factory=list)


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
    projects: list[DraftProject] = Field(default_factory=list)


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
    #: The strict recruiter's last reading of this arrangement's text, or None
    #: when it was not reviewed (switched off, no answer, or the fallback).
    verdict: Verdict | None = None
    #: How many reviews the arrangement went through.
    reviews: int = 0

    @property
    def review_passes(self) -> bool | None:
        """Whether the reviewer passed it; None when it was not reviewed."""
        return self.verdict.passes if self.verdict is not None else None

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
        # Passed through as given, repeats and all, for the guard to judge:
        # a project named twice is a mistake the retry should hear about.
        projects=tuple(
            ProjectChoice(ref=project.ref, bullets=tuple(project.bullets))
            for project in draft.projects
        ),
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
    problems.extend(guard.check_projects(arrangement.projects, context))
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

    best: GeneratedCV | None = None
    verdict: Verdict | None = None
    reviews = 0
    guard_failures = 0
    reviewer_context = review_context(context)

    for attempt in range(1, MAX_DRAFT_CALLS + 1):
        try:
            result = await router.complete_json(
                PROMPT_NAME,
                CVDraft,
                task=TASK,
                variables=prompt_builder.variables(context, feedback=feedback),
            )
        except LLMError as exc:
            # The provider is gone, or it failed to produce the shape twice.
            # Both mean there is no answer to check; the best reviewed
            # arrangement so far, or the fallback, is the answer. Narrow on
            # purpose: a bug in this module must still raise.
            logger.warning(
                "documents.generate.no_model_answer",
                vacancy_id=str(context.vacancy.vacancy_id),
                attempt=attempt,
                reason=type(exc).__name__,
            )
            break

        usages.append(usage_ledger.record(result.usage))
        arrangement = fit_to_page(
            polish(to_arrangement(result.value, context), context, rules), context
        )
        problems = inspect(arrangement, context)
        broken = workshop_rules.hard(
            workshop_rules.check(to_text(arrangement, context), scope=RuleScope.CV, rules=rules)
        )
        if problems or broken:
            seen.extend(problems)
            seen_rules.extend(broken)
            guard_failures += 1
            logger.warning(
                "documents.generate.rejected",
                vacancy_id=str(context.vacancy.vacancy_id),
                attempt=attempt,
                problems=[problem.value for problem in problems],
                broke_rules=[violation.rule_id for violation in broken],
            )
            if guard_failures >= MAX_ATTEMPTS:
                break
            feedback = _feedback(problems, broken) + (
                review_feedback(verdict) if verdict is not None else ""
            )
            continue

        draft = GeneratedCV(
            arrangement=arrangement,
            text=to_text(arrangement, context),
            source="model",
            attempts=attempt,
            rejected_for=tuple(seen),
            usages=tuple(usages),
        )
        if not settings.review_enabled:
            return _reviewed(draft, context, reviews, usages)

        verdict, review_usage = await recruiter.review(
            "cv", draft.text, reviewer_context, router=router
        )
        if review_usage is not None:
            usages.append(review_usage)
        if verdict is None:
            return _reviewed(draft, context, reviews, usages)
        reviews += 1
        draft = replace(draft, verdict=verdict)
        if best is None or best.verdict is None or verdict.lowest >= best.verdict.lowest:
            best = draft
        if verdict.passes or reviews >= recruiter.MAX_ROUNDS:
            return _reviewed(best, context, reviews, usages)
        feedback = review_feedback(verdict)

    if best is not None:
        # The calls ran out after a reviewed arrangement that passed every
        # guard: it is returned rather than the rule-based one, whatever the
        # reviewer still wanted. The review polishes; the guards are the gate.
        return _reviewed(best, context, reviews, usages)

    fallback = compose_fallback(context, rules)
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


def review_context(context: CVContext) -> LetterContext:
    """What the reviewer is shown about the vacancy and the candidate.

    The reviewer reads a letter-shaped context (vacancy facts, profile facts),
    built here from the CV's own. Its achievements are the CV's — every stored
    bullet of every job this CV may show, with its employer — so it can say
    "lead the first job with the RAG bullet" about a bullet that exists, and
    nothing it is shown is something the CV could not print.
    """
    achievements = tuple(
        AchievementFact(company=entry.company, title=entry.title, text=text)
        for entry in context.experience
        for text in entry.achievements
    ) + tuple(
        # A project's bullets are labelled as a project's, so the reviewer can
        # say "show the eval-harness project" without mistaking it for a job.
        AchievementFact(company=f"project {project.name}", title="project", text=text)
        for project in context.projects
        for text in project.achievements
    )
    profile = context.profile.model_copy(update={"achievements": achievements})
    return letter_context(context.vacancy, profile, language=context.language)


def review_feedback(verdict: Verdict) -> str:
    """The reviewer's instructions, restated in the terms a CV can meet them in."""
    return (
        "\n## A strict recruiter reviewed the CV your previous answer produced\n\n"
        f"{verdict.feedback()}\n\n"
        "Meet these instructions the only ways this CV can change: which bullets "
        "each job shows and in what order (by index — the text is printed as "
        "recorded), the order and spelling of skills, the headline, and the "
        "summary. Return the JSON object and nothing else.\n"
    )


def _reviewed(
    generated: GeneratedCV, context: CVContext, reviews: int, usages: list[LLMUsage]
) -> GeneratedCV:
    """The CV being returned, with the review count and every call's usage."""
    verdict = generated.verdict
    logger.info(
        "documents.generate.reviewed",
        vacancy_id=str(context.vacancy.vacancy_id),
        reviews=reviews,
        passes=verdict.passes if verdict is not None else None,
        relevance=verdict.relevance if verdict is not None else None,
        evidence=verdict.evidence if verdict is not None else None,
        clarity=verdict.clarity if verdict is not None else None,
        form=verdict.form if verdict is not None else None,
    )
    return replace(generated, reviews=reviews, usages=tuple(usages))


def _feedback(problems: Sequence[CVProblem], broken: Sequence[RuleViolation]) -> str:
    """What the model is told to fix, structural faults first.

    Two vocabularies in one message on purpose: the structural problems are this
    module's and the rule violations are the owner's own words, and a model given
    both fixes both in one more attempt rather than trading one for the other.
    """
    parts = [guard.feedback_for(list(problems))] if problems else []
    parts.extend(violation.message for violation in broken)
    return "\n".join(part for part in parts if part)


#: Most skills a CV lists, whichever branch arranged it. The research's range
#: is 8 to 25 (C3), and the owner's own resume shows about fifteen on six
#: labelled lines; a recruiter's ten-second read finds "do they know Postgres"
#: in sixteen names and does not in forty. The ones cut are those the vacancy
#: did not ask for and nothing shown on the page evidences.
MAX_SKILLS_SHOWN = 16

#: Most technologies the rule-based CV prints under one job.
MAX_STACK_SHOWN = 8

#: Most sentences a summary keeps: "two or three sentences" (L3).
MAX_SUMMARY_SENTENCES = 3


def asked_for(name: str, context: CVContext) -> bool:
    """Whether the vacancy asks for this skill: in its list, or named in its text.

    The description counts as well as the structured list because the list is
    often short or missing (a Telegram post has none) while the text names the
    stack in as many words; a skill the employer wrote down is one they asked
    about either way.
    """
    if not name.strip():
        return False
    key = fold(name)
    if key and key in {fold(requirement) for requirement in context.vacancy.key_skills}:
        return True
    return guard.mentions(context.vacancy.description or "", name)


def shown_evidence(arrangement: CVArrangement, context: CVContext) -> str:
    """Everything the page itself says the candidate did, outside the skills.

    The bullets each shown job prints and the stack line under it, and each
    shown project's name, stack and bullets. A skill named in here is one a
    reader can see used, which is what earns it a place in the skills section
    when the vacancy did not ask for it.
    """
    parts: list[str] = []
    by_ref = context.by_ref
    for job in arrangement.experience:
        entry = by_ref.get(job.ref)
        if entry is None:
            continue
        parts.extend(
            entry.achievements[index] for index in job.bullets if index < len(entry.achievements)
        )
        parts.extend(job.stack)
    projects = context.projects_by_ref
    for choice in arrangement.projects:
        project = projects.get(choice.ref)
        if project is None:
            continue
        parts.extend((project.name, *project.stack))
        parts.extend(
            project.achievements[index]
            for index in choice.bullets
            if index < len(project.achievements)
        )
    return "\n".join(parts)


def skills_floor(rules: Sequence[RuleSpec]) -> int:
    """The fewest skills a CV may show: :data:`guard.MIN_SKILLS`, or the owner's own.

    "At least 21 skills" is a hard rule an owner can write in the workshop,
    and pruning below it would turn every CV into a refusal; their number wins.
    """
    floor = guard.MIN_SKILLS
    for rule in rules:
        params = rule.params
        if (
            rule.is_active
            and isinstance(params, SectionItemCountParams)
            and params.minimum is not None
            and fold_heading(params.section) in SECTION_ALIASES[0]
        ):
            floor = max(floor, params.minimum)
    return floor


def prune_skills(
    arrangement: CVArrangement, context: CVContext, *, floor: int = guard.MIN_SKILLS
) -> tuple[SkillChoice, ...]:
    """The skills worth the space: asked for, or evidenced on the page.

    First every skill the vacancy asks for (:func:`asked_for`), then every skill
    the page shows being used (:func:`shown_evidence`), each group in the
    arrangement's own order, up to :data:`MAX_SKILLS_SHOWN`. The rest — the
    resume's vocabulary that neither the employer nor the page mentions — goes,
    unless that would leave fewer than ``floor``, in which case the first of
    them come back. Only ever removes: nothing reaches the list that the
    arrangement did not already hold.
    """
    evidence = shown_evidence(arrangement, context)
    spellings: dict[str, set[str]] = {}
    for skill in context.profile.skills:
        spellings.setdefault(fold(skill.canonical_name), set()).update(
            {skill.canonical_name, skill.spelling}
        )
    asked: list[SkillChoice] = []
    seen: list[SkillChoice] = []
    rest: list[SkillChoice] = []
    for choice in arrangement.skills:
        names = {choice.shown_as, choice.canonical_name} | spellings.get(
            fold(choice.canonical_name), set()
        )
        if any(asked_for(name, context) for name in names):
            asked.append(choice)
        elif any(guard.mentions(evidence, name) for name in names):
            seen.append(choice)
        else:
            rest.append(choice)
    kept = [*asked, *seen][: max(MAX_SKILLS_SHOWN, floor)]
    if len(kept) < floor:
        kept.extend(rest[: floor - len(kept)])
    return tuple(kept)


#: Where one sentence ends and the next begins: a full stop, a question or an
#: exclamation mark, a space, then a capital letter of either alphabet.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u0410-\u042f\u0401])")


def trimmed_summary(summary: str) -> str:
    """The first two or three sentences of a summary that fit the length rule.

    Whole sentences only, never a cut one: the rule-based CV quotes the
    owner's own summary, and a quotation that stops mid-clause is worse than a
    shorter one. Empty when even the first sentence is too long.
    """
    kept = ""
    for count, sentence in enumerate(_SENTENCE_END.split(summary.strip()), start=1):
        candidate = f"{kept} {sentence}".strip()
        if count > MAX_SUMMARY_SENTENCES or len(candidate) > MAX_SUMMARY_CHARS:
            break
        kept = candidate
    return kept


def polish(
    arrangement: CVArrangement, context: CVContext, rules: Sequence[RuleSpec] = ()
) -> CVArrangement:
    """What either branch's arrangement goes through before it is fitted and checked.

    Two edits, both of which only remove or reformat: the skills are pruned to
    what the vacancy asks for and the page evidences (:func:`prune_skills`),
    and the summary's figures are written the posting's way
    (:func:`written_for`). The checks then run on the result, so neither edit
    can carry anything past them.
    """
    return arrangement.model_copy(
        update={
            "skills": prune_skills(arrangement, context, floor=skills_floor(rules)),
            "summary": written_for(arrangement.summary, context.language),
        }
    )


def compose_fallback(context: CVContext, rules: Sequence[RuleSpec] = ()) -> CVArrangement:
    """An arrangement built from the context alone, with no model involved.

    Correct, complete and untailored beyond ordering and selection rules that
    need no judgement. Skills the vacancy asked for come first, in the
    vacancy's own order and under the vacancy's own spelling, then the rest in
    the order the profile holds them, and :func:`polish` keeps only those asked
    for or evidenced on the page. Jobs stay in the resume's order — reverse
    chronological, as they were written — each showing up to
    :data:`app.documents.context.MAX_BULLETS_PER_JOB` bullets for the two most
    recent jobs and :data:`app.documents.context.MAX_BULLETS_OLDER_JOB` for
    older ones, and a stack line of the vacancy's technologies and the ones its
    shown bullets name.

    The bullets within a job are put in an order that needs no judgement: those
    naming more of the vacancy's requirements first (the research's rule for a
    role, docs/WORKSHOP-RESEARCH.md 4.3), then those carrying a figure (C2),
    the resume's own order otherwise — so the owner's first, label-led bullet
    leads unless the vacancy plainly asks about another, and a bullet that
    names nothing the vacancy asked for never moves ahead of one the owner put
    first. :func:`relevance` counts, it does not judge. Projects are chosen and
    ordered the same way, and page fitting then drops from the end, which is
    now the least relevant line.

    The vacancy's spelling is used for a matched skill here, and that is not a
    liberty: ``matched`` means the fold keys agree, so the two strings are two
    names for one thing, and the employer's own word is the one their parser is
    searching for. It is the single piece of tailoring that needs no model,
    which is why it is the piece this branch keeps.

    The summary is the profile's own — its first two or three whole sentences
    within the length rule — and only when it passes the same check the
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

    summary = trimmed_summary(context.profile.summary or "")
    if (
        guard.check_summary(written_for(summary, context.language), context)
        or text_language(summary, default=context.language) != context.language
    ):
        summary = ""

    projects = sorted(
        context.projects,
        key=lambda project: (
            -relevance(" ".join((project.name, *project.stack, *project.achievements)), context)
        ),
    )
    jobs: list[JobChoice] = []
    for position, entry in enumerate(context.experience):
        limit = MAX_BULLETS_PER_JOB if position < RECENT_JOBS else MAX_BULLETS_OLDER_JOB
        bullets = _ranked(entry.achievements, context, limit)
        jobs.append(
            JobChoice(ref=entry.ref, stack=_stack(entry, bullets, context), bullets=bullets)
        )
    arrangement = CVArrangement(
        headline=context.allowed_headlines[0] if context.allowed_headlines else "",
        summary=summary,
        skills=tuple(skills),
        experience=tuple(jobs),
        projects=tuple(
            ProjectChoice(
                ref=project.ref,
                # Chosen by rank, printed in the resume's order: a project's
                # first bullet says what it is, and the rest only make sense
                # after it.
                bullets=tuple(
                    sorted(_ranked(project.achievements, context, MAX_BULLETS_PER_PROJECT))
                ),
            )
            for project in projects[:MAX_PROJECTS_SHOWN]
        ),
    )
    return fit_to_page(polish(arrangement, context, rules), context)


def _stack(entry: ExperienceEntry, bullets: tuple[int, ...], context: CVContext) -> tuple[str, ...]:
    """A job's stack line: the vacancy's technologies first, then those its bullets name.

    Only ever a subset of what the resume attributed to this job, in the
    order: asked for by the vacancy, then named in the bullets shown, then the
    rest of the recorded stack — cut at :data:`MAX_STACK_SHOWN`.
    """
    shown = "\n".join(entry.achievements[index] for index in bullets)
    asked = [name for name in entry.stack if asked_for(name, context)]
    named = [name for name in entry.stack if name not in asked and guard.mentions(shown, name)]
    rest = [name for name in entry.stack if name not in asked and name not in named]
    return tuple([*asked, *named, *rest][:MAX_STACK_SHOWN])


def relevance(text: str, context: CVContext) -> int:
    """How many of the vacancy's requirements this text names, as whole words.

    Both spellings of a matched requirement count — the vacancy's and the
    resume's — because a bullet is the owner's sentence and names the skill
    the owner's way.
    """
    names = {name for name in context.vacancy.key_skills if name.strip()}
    names.update(skill.possessed_as for skill in context.overlap.matched)
    found = {fold(name) for name in names if guard.mentions(text, name)}
    return len(found)


def _ranked(achievements: tuple[str, ...], context: CVContext, limit: int) -> tuple[int, ...]:
    """Indices of the strongest bullets for this vacancy, strongest first.

    By how many requirements a bullet names, then by whether it carries a
    figure, then by the resume's own order.
    """
    order = sorted(
        range(len(achievements)),
        key=lambda index: (
            -relevance(achievements[index], context),
            not any(char.isdigit() for char in achievements[index]),
            index,
        ),
    )
    return tuple(order[:limit])


#: The fewest bullets trimming leaves a job with: two for a recent role, which
#: is the research's floor for one (S11, S27), one for an older role.
MIN_BULLETS_RECENT_JOB = 2
MIN_BULLETS_OLDER_JOB = 1


def fit_to_page(arrangement: CVArrangement, context: CVContext) -> CVArrangement:
    """The arrangement with lines dropped, least valuable first, until it fits a page.

    A person fitting a CV to one page cuts in a predictable order, and this
    follows it, re-measuring with :func:`app.documents.pdf.fits_one_page`
    after every cut:

    1. projects' extra bullets, last project first, down to one each;
    2. jobs' last bullets, oldest job first, never taking a recent job below
       :data:`MIN_BULLETS_RECENT_JOB` or an older one below
       :data:`MIN_BULLETS_OLDER_JOB`;
    3. whole projects, last first.

    "Last" is the arrangement's own order: for a model's answer that is its
    least relevant choice, for the rule-based one the resume's last line.

    Only lines are ever removed, so nothing can be added to a document by
    fitting it, and a profile too large to fit even at the floors is returned at
    the floors rather than refused — a CV that runs a few lines long is a
    formatting fault, not a reason to hand over nothing.
    """
    current = arrangement

    def fits() -> bool:
        return fits_one_page(current, context)

    projects = list(current.projects)
    for index in range(len(projects) - 1, -1, -1):
        while not fits() and len(projects[index].bullets) > 1:
            projects[index] = projects[index].model_copy(
                update={"bullets": projects[index].bullets[:-1]}
            )
            current = current.model_copy(update={"projects": tuple(projects)})

    jobs = list(current.experience)
    for index in range(len(jobs) - 1, -1, -1):
        floor = MIN_BULLETS_RECENT_JOB if index < RECENT_JOBS else MIN_BULLETS_OLDER_JOB
        while not fits() and len(jobs[index].bullets) > floor:
            jobs[index] = jobs[index].model_copy(update={"bullets": jobs[index].bullets[:-1]})
            current = current.model_copy(update={"experience": tuple(jobs)})

    while not fits() and current.projects:
        current = current.model_copy(update={"projects": current.projects[:-1]})
    return current

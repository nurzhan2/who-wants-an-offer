"""The strict recruiter wired into both generators: rewrites, budgets, never a block.

``app.review.recruiter`` is tested on its own in ``test_review.py``. These are
the loops around it in :func:`app.letters.generator.generate` and
:func:`app.documents.generator.generate`, run against a router scripted per
prompt — drafts for the generator's prompt, verdicts for the reviewer's — so
each test can say exactly which calls a generation made and what each was told.

What is asserted is the contract the owner was given: a failing verdict buys
exactly one rewrite carrying the reviewer's instructions; a passing one costs a
single review call; a reviewer that is missing or keeps failing a document
never stops it from being returned; and a rewrite still goes through every
guard, so an instruction can never smuggle a fact in.
"""

from collections import deque
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import settings
from app.core.exceptions import LLMError
from app.documents import generator as cv_generator
from app.documents.contacts import ContactBlock
from app.documents.context import EducationEntry, ExperienceEntry
from app.documents.context import build_context as cv_context_of
from app.documents.generator import CVDraft, DraftJob, DraftSkill
from app.letters import generator as letter_generator
from app.letters.context import ProfileFacts, SkillFact, VacancyFacts, build_context
from app.letters.generator import CoverLetterDraft
from app.llm.base import LLMResult, LLMTask, LLMUsage
from app.llm.router import LLMRouter
from app.review import recruiter
from app.review.recruiter import Verdict

pytestmark = pytest.mark.unit

USAGE = LLMUsage(provider="fake", model="fake", task=LLMTask.COVER_LETTER, cost_usd=0.001)

PASSING = Verdict(relevance=9, evidence=9, clarity=8, form=9, verdict="Interview.")
FAILING = Verdict(
    relevance=6,
    evidence=7,
    clarity=8,
    form=9,
    must_fix=["Lead with the payments result instead of the onboarding guide."],
)
WORSE = Verdict(relevance=5, evidence=5, clarity=8, form=8, must_fix=["Be specific."])


class PerPrompt(LLMRouter):
    """Answers each prompt from its own queue; the last answer repeats."""

    def __init__(self, **queues: list[Any]) -> None:
        super().__init__(providers={})
        self.queues = {name: deque(answers) for name, answers in queues.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete_json(  # type: ignore[override] # a scripted stand-in
        self,
        prompt_name: str,
        response_model: Any,
        *,
        task: LLMTask,
        variables: Any = None,
        **_: Any,
    ) -> LLMResult[Any]:
        self.calls.append((prompt_name, dict(variables or {})))
        queue = self.queues[prompt_name]
        answer = queue.popleft() if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(value=answer, usage=USAGE, attempts=1)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


# ── letters ───────────────────────────────────────────────────────────

LETTER = recruiter.PROMPT_NAME


def letter_context() -> Any:
    vacancy = VacancyFacts(
        vacancy_id=uuid4(),
        title="Backend Engineer",
        company="Northwind",
        description="We build payment APIs in Python. Fully remote.",
        key_skills=("Python", "PostgreSQL"),
    )
    profile = ProfileFacts(
        profile_id=uuid4(),
        name="Alex Doe",
        headline="Backend Engineer",
        skills=(
            SkillFact(canonical_name="python", spelling="Python"),
            SkillFact(canonical_name="postgresql", spelling="PostgreSQL"),
        ),
    )
    return build_context(vacancy, profile)


def letter_draft(opening: str) -> CoverLetterDraft:
    body = (
        "I'm applying for the Backend Engineer role at Northwind. I build Python services "
        "on PostgreSQL and have shipped payment integrations to production. Happy to do a "
        "technical call this week and walk you through the work."
    )
    return CoverLetterDraft(
        letter=f"Hello,\n\n{opening} {body}\n\nBest regards,\nAlex Doe",
        language="en",
        addressed_skills=["Python", "PostgreSQL"],
    )


FIRST, SECOND, THIRD = (letter_draft(word) for word in ("First.", "Second.", "Third."))


async def test_a_failing_letter_review_buys_exactly_one_rewrite_with_its_instructions() -> None:
    router = PerPrompt(cover_letter=[FIRST, SECOND], **{LETTER: [FAILING, PASSING]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert router.names() == ["cover_letter", LETTER, "cover_letter", LETTER]
    rewrite = router.calls[2][1]["feedback"]
    assert "Lead with the payments result" in rewrite
    assert "strict recruiter" in rewrite
    assert result.text == SECOND.letter
    assert result.verdict == PASSING and result.review_passes is True
    assert result.reviews == 2
    assert len(result.usages) == 4, "drafts and reviews are both on the ledger"


async def test_a_passing_letter_review_costs_one_call() -> None:
    router = PerPrompt(cover_letter=[FIRST], **{LETTER: [PASSING]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert router.names() == ["cover_letter", LETTER]
    assert result.text == FIRST.letter and result.reviews == 1


async def test_a_missing_reviewer_leaves_the_letter_as_written() -> None:
    router = PerPrompt(cover_letter=[FIRST], **{LETTER: [LLMError("no reviewer")]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert result.source == "model" and result.text == FIRST.letter
    assert result.verdict is None and result.review_passes is None


async def test_when_the_rounds_run_out_the_best_reviewed_letter_is_returned() -> None:
    """Two failing reviews: the higher-scoring draft goes out, and nothing raises."""
    router = PerPrompt(cover_letter=[FIRST, SECOND, THIRD], **{LETTER: [FAILING, WORSE]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert router.names().count(LETTER) == recruiter.MAX_ROUNDS
    assert result.text == FIRST.letter, "FAILING (lowest 6) beats WORSE (lowest 5)"
    assert result.verdict == FAILING and result.review_passes is False


async def test_a_rewrite_that_breaks_a_guard_does_not_replace_the_reviewed_letter() -> None:
    """The reviewer cannot smuggle anything past the guards: a rewrite carrying a
    link is rejected, and the earlier clean draft is what is returned."""
    linked = CoverLetterDraft(
        letter=FIRST.letter + "\nSee https://example.com/me",
        language="en",
        addressed_skills=["Python"],
    )
    router = PerPrompt(cover_letter=[FIRST, linked, linked], **{LETTER: [FAILING]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert result.text == FIRST.letter
    assert "example.com" not in result.text
    second_feedback = router.calls[-1][1]["feedback"]
    assert "Lead with the payments result" in second_feedback, "the instructions still stand"


async def test_the_review_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "review_enabled", False)
    router = PerPrompt(cover_letter=[FIRST], **{LETTER: [PASSING]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert router.names() == ["cover_letter"]
    assert result.verdict is None and result.reviews == 0


async def test_the_fallback_letter_is_never_reviewed() -> None:
    router = PerPrompt(cover_letter=[LLMError("no model")], **{LETTER: [PASSING]})

    result = await letter_generator.generate(letter_context(), router=router)

    assert result.source == "fallback"
    assert LETTER not in router.names()


# ── CVs ───────────────────────────────────────────────────────────────

CV = cv_generator.PROMPT_NAME

BULLETS = (
    "Wrote the onboarding guide used by 6 new engineers.",
    "Built a Python payments API on PostgreSQL handling 1,200 requests per second.",
    "Cut p95 latency of the checkout API from 840 ms to 210 ms.",
)


def cv_context() -> Any:
    facts = letter_context()
    profile = facts.profile.model_copy(
        update={
            "skills": tuple(
                SkillFact(canonical_name=name.lower(), spelling=name)
                for name in ("Python", "PostgreSQL", "FastAPI", "Redis", "Docker", "Git")
            )
        }
    )
    return cv_context_of(
        facts.vacancy,
        profile,
        contacts=ContactBlock(
            name="Alex Doe", email="alex@example.com", phone="+7 700 000 00 00", city="Almaty"
        ),
        experience=(
            ExperienceEntry(
                ref=0,
                company="Acme",
                title="Backend Engineer",
                start="2025-01",
                is_current=True,
                stack=("Python", "PostgreSQL"),
                achievements=BULLETS,
            ),
        ),
        education=(EducationEntry(institution="Example University", degree="BSc"),),
    )


def cv_draft(*bullets: int) -> CVDraft:
    return CVDraft(
        headline="Backend Engineer",
        skills=[
            DraftSkill(canonical_name=name.lower(), shown_as=name)
            for name in ("Python", "PostgreSQL", "FastAPI", "Redis", "Docker", "Git")
        ],
        experience=[DraftJob(ref=0, stack=["Python", "PostgreSQL"], bullets=list(bullets))],
    )


async def test_a_failing_cv_review_buys_one_rearrangement_with_its_instructions() -> None:
    router = PerPrompt(**{CV: [cv_draft(0, 1), cv_draft(1, 2)], LETTER: [FAILING, PASSING]})

    result = await cv_generator.generate(cv_context(), router=router)

    assert router.names() == [CV, LETTER, CV, LETTER]
    feedback = router.calls[2][1]["feedback"]
    assert "Lead with the payments result" in feedback
    assert "by index" in feedback, "told how a CV can meet it: by choosing bullets"
    assert result.arrangement.experience[0].bullets == (1, 2)
    assert result.text.index(BULLETS[1]) < result.text.index(BULLETS[2])
    assert result.verdict == PASSING and result.reviews == 2


async def test_the_cv_reviewer_is_shown_the_stored_bullets_it_may_ask_for() -> None:
    """It can only say "lead with bullet X" about a bullet it can see."""
    router = PerPrompt(**{CV: [cv_draft(0, 1)], LETTER: [PASSING]})

    await cv_generator.generate(cv_context(), router=router)

    review_variables = router.calls[1][1]
    for bullet in BULLETS:
        assert bullet in review_variables["candidate_facts"]
    assert BULLETS[0] in review_variables["document"]


async def test_cv_rounds_running_out_return_the_best_arrangement_never_an_error() -> None:
    router = PerPrompt(**{CV: [cv_draft(0, 1), cv_draft(0, 2)], LETTER: [FAILING, WORSE]})

    result = await cv_generator.generate(cv_context(), router=router)

    assert result.source == "model"
    assert result.arrangement.experience[0].bullets == (0, 1)
    assert result.review_passes is False


async def test_a_missing_cv_reviewer_leaves_the_arrangement_as_chosen() -> None:
    router = PerPrompt(**{CV: [cv_draft(2, 1)], LETTER: [LLMError("no reviewer")]})

    result = await cv_generator.generate(cv_context(), router=router)

    assert result.arrangement.experience[0].bullets == (2, 1)
    assert result.verdict is None
    assert router.names() == [CV, LETTER]


async def test_a_cv_rearrangement_still_goes_through_the_guards() -> None:
    """A reviewer asking for a bullet the job does not have gets nowhere: the
    rewrite naming index 9 is rejected and the reviewed arrangement stands."""
    router = PerPrompt(**{CV: [cv_draft(0, 1), cv_draft(9), cv_draft(9)], LETTER: [FAILING]})

    result = await cv_generator.generate(cv_context(), router=router)

    assert result.arrangement.experience[0].bullets == (0, 1)
    assert result.source == "model"

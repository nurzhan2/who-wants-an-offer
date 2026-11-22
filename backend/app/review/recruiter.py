"""A second reader: the strictest recruiter, asked before a document goes out.

The owner's standard (22 Nov 2026) is a CV and a letter that no recruiter turns
down *for the document's own faults*. The generators already guarantee what
code can decide — nothing invented, no cliché on the list, the right length.
What code cannot decide is what a recruiter decides in ten seconds: is this
written for this vacancy, is every claim backed, can it be taken in at a
glance. That needs a reader, so there is one: a model call with the rubric the
workshop research produced (``docs/WORKSHOP-RESEARCH.md``), returning four
scores and at most five concrete instructions.

Three properties make it safe to wire into generation:

**It cannot make a document less true.** The reviewer is told the candidate's
facts are fixed and shown what they are; it may ask for a different choice,
order or wording, never for a new fact. And whatever it asks for, the rewrite
goes back through the same guards as the first draft — an instruction that
could only be met by inventing fails there, not here.

**It never blocks.** A judge is a model and can be wrong or unavailable. A
missing or failed review is ``None`` and the document stands on its guards; a
review that still wants changes after the last round is recorded beside a
document that is returned anyway. The hard gates are the guards; this is the
polish.

**It is bounded.** At most :data:`MAX_ROUNDS` reviews per document, each one
call of the same task the document was written with, so it follows the same
routing, budget and ledger.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.letters import prompt as letter_prompt
from app.letters.context import LetterContext
from app.llm import usage as usage_ledger
from app.llm.base import LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router

logger = get_logger(__name__)

PROMPT_NAME = "recruiter_review"
DocumentKind = Literal["cv", "cover_letter"]

#: The score every criterion must reach. The prompt defines 8 as "I would
#: interview on the strength of this point".
PASS_SCORE = 8
#: Reviews per document. Two: one to find what a first draft gets wrong, one to
#: confirm the rewrite; a third round mostly trades one preference for another.
MAX_ROUNDS = 2

DOCUMENT_OPEN = "<<<DOCUMENT_UNDER_REVIEW_BEGIN>>>"
DOCUMENT_CLOSE = "<<<DOCUMENT_UNDER_REVIEW_END>>>"

_TASKS: dict[DocumentKind, LLMTask] = {
    "cv": LLMTask.CV_TAILORING,
    "cover_letter": LLMTask.COVER_LETTER,
}
_KIND_NAMES: dict[DocumentKind, str] = {"cv": "CV (resume)", "cover_letter": "cover letter"}


class Verdict(BaseModel):
    """What the reviewer concluded. Flat on purpose: the API compiles the schema
    into a grammar and refuses complex ones."""

    model_config = ConfigDict(extra="ignore")

    relevance: int = Field(ge=0, le=10)
    evidence: int = Field(ge=0, le=10)
    clarity: int = Field(ge=0, le=10)
    form: int = Field(ge=0, le=10)
    must_fix: list[str] = Field(default_factory=list, max_length=8)
    verdict: str = ""

    @property
    def lowest(self) -> int:
        return min(self.relevance, self.evidence, self.clarity, self.form)

    @property
    def passes(self) -> bool:
        """Every criterion at the bar. ``must_fix`` on a passing review is advice."""
        return self.lowest >= PASS_SCORE

    def feedback(self) -> str:
        """The instructions as the next attempt's feedback block."""
        fixes = [fix.strip() for fix in self.must_fix[:5] if fix.strip()]
        if not fixes:
            fixes = [self.verdict.strip() or "Make it more specific to this vacancy."]
        lines = "\n".join(f"- {fix}" for fix in fixes)
        return (
            "A strict recruiter reviewed your previous answer and would not pass it yet "
            f"(relevance {self.relevance}/10, evidence {self.evidence}/10, clarity "
            f"{self.clarity}/10, form {self.form}/10). Change exactly this, and nothing "
            "that already works. Every earlier rule still applies: use only the "
            f"candidate's facts.\n{lines}"
        )


def _letter_length_note(context: LetterContext) -> str:
    return (
        "600 to 1 000 characters for a message to a recruiter, never more than 1 500"
        if context.vacancy.letter_max_length >= 1500
        else f"at most {context.vacancy.letter_max_length} characters"
    )


async def review(
    kind: DocumentKind,
    text: str,
    context: LetterContext,
    *,
    router: LLMRouter | None = None,
) -> tuple[Verdict | None, LLMUsage | None]:
    """One strict reading of ``text``. ``(None, None)`` when no reviewer answered."""
    router = router or get_router()
    try:
        result = await router.complete_json(
            PROMPT_NAME,
            Verdict,
            task=_TASKS[kind],
            variables={
                "document_kind": _KIND_NAMES[kind],
                "language": context.language,
                "letter_length_note": _letter_length_note(context),
                "vacancy_facts": letter_prompt.vacancy_block(context),
                "candidate_facts": letter_prompt.candidate_block(context),
                "fence_open": letter_prompt.FENCE_OPEN,
                "fence_close": letter_prompt.FENCE_CLOSE,
                "description": letter_prompt.fenced_description(context),
                "document_open": DOCUMENT_OPEN,
                "document_close": DOCUMENT_CLOSE,
                "document": text.replace(DOCUMENT_OPEN, "").replace(DOCUMENT_CLOSE, ""),
            },
        )
    except LLMError as exc:
        logger.warning(
            "review.unavailable",
            kind=kind,
            vacancy_id=str(context.vacancy.vacancy_id),
            reason=type(exc).__name__,
        )
        return None, None
    # Typed as Verdict, but a stand-in or a misrouted provider can hand back
    # anything; something else has not reviewed the document.
    answer: object = result.value
    if not isinstance(answer, Verdict):
        return None, None
    verdict = answer
    logger.info(
        "review.done",
        kind=kind,
        vacancy_id=str(context.vacancy.vacancy_id),
        relevance=verdict.relevance,
        evidence=verdict.evidence,
        clarity=verdict.clarity,
        form=verdict.form,
        passes=verdict.passes,
        fixes=len(verdict.must_fix),
    )
    return verdict, usage_ledger.record(result.usage)

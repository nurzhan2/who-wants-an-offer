"""What a Telegram post is: a vacancy or not, and what it says if it is.

Two stages, priced very differently, which is the whole design
(docs/SOURCES.md, «`telegram` — подробно»):

1. :func:`looks_like_vacancy` — a few regular expressions over the text. Free.
   It throws away what is obviously not a job: a digest, a meme, a course
   advert, a person posting their own resume. The plan says about 80% of a job
   channel's posts go here; the run logs the real share.
2. :func:`parse_post` — the model, through the router's ``telegram_parse`` task,
   only for what survived. The answer is validated by :class:`ParsedPost`; any
   failure — no provider, an invalid answer after the router's own retry — falls
   back to :func:`fallback_parse`, which keeps the post as a vacancy with its
   first line as the title rather than losing it.

The heuristic errs towards letting a post through: a vacancy it wrongly drops is
gone, while one it wrongly keeps costs one cheap model call that then says
``is_vacancy: false``.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.llm.base import LLMTask
from app.llm.router import LLMRouter

logger = get_logger(__name__)

#: How much of a post reaches the model. Job posts are a few hundred characters;
#: a longer one is a digest or an article, and the first job in it is near the top.
POST_LIMIT = 4000

#: Words a job post uses. Russian, Kazakh-Russian and English, because the
#: channels are all three. Matched case-insensitively on word starts.
_JOB_WORDS = re.compile(
    r"(?<![\w])("
    r"ваканси|ищем|ищу\s+(?:в\s+команду|разработчик|специалист)|требуется|требуются|"
    r"открыта\s+позиция|в\s+команду|нанимаем|приглашаем|"
    r"зп|з/п|зарплат|оклад|вилка|"
    r"hiring|we\s*are\s*hiring|we're\s*hiring|looking\s+for|join\s+(?:our|us)|"
    r"vacancy|position|salary|job\b|#job|#vacancy|#вакансия"
    r")",
    re.IGNORECASE,
)

#: How to apply: a handle, a link, an e-mail. A job post nearly always has one.
_CONTACT = re.compile(
    r"(@[A-Za-z][\w]{3,}|https?://\S+|t\.me/\S+|[\w.+-]+@[\w-]+\.[\w.]+)", re.IGNORECASE
)

#: Money, in the forms these channels write it.
_MONEY = re.compile(
    r"(\d[\d\s.,]*\s*(?:k|к|тыс|000)?\s*(?:\$|€|₽|₸|usd|eur|rub|руб|kzt|тенге))"
    r"|((?:\$|€|₽|₸)\s*\d)",
    re.IGNORECASE,
)

#: A person looking for work, not an employer looking for a person.
_SEEKER = re.compile(
    r"(#резюме|#resume|#cv\b|#ищуработу|ищу\s+работу|looking\s+for\s+a\s+job)", re.I
)

#: Posts that are about jobs in general rather than one job.
_NOT_A_JOB = re.compile(r"(#дайджест|#digest|#курс|#вебинар|#webinar|#реклама|#ad\b)", re.I)

#: The shortest text worth calling a vacancy.
MIN_CHARS = 60


def looks_like_vacancy(text: str) -> bool:
    """The free first stage: could this post be one employer offering a job?

    A job word is required, and so is a way to apply or a salary — a job word
    alone is every digest's headline. A job seeker's post and an obvious digest
    or advert are refused outright.
    """
    if len(text.strip()) < MIN_CHARS:
        return False
    if _SEEKER.search(text) or _NOT_A_JOB.search(text):
        return False
    if not _JOB_WORDS.search(text):
        return False
    return bool(_CONTACT.search(text) or _MONEY.search(text))


def contact_in(text: str) -> str | None:
    """The first handle, link or e-mail in the post, as written."""
    found = _CONTACT.search(text)
    return found.group(1).rstrip(".,;)") if found else None


class ParsedPost(BaseModel):
    """The model's reading of one post. Validated; never trusted unchecked."""

    model_config = ConfigDict(extra="ignore")

    is_vacancy: bool
    several_positions: bool = False
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    remote: Literal["full", "hybrid", "no"] | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_max: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    apply_contact: str | None = Field(default=None, max_length=300)
    skills: list[str] = Field(default_factory=list, max_length=20)
    seniority: Literal["intern", "junior", "middle", "senior", "lead"] | None = None

    @field_validator("title", "company", "city", "apply_contact", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            return cleaned or None
        return value

    @field_validator("skills", mode="before")
    @classmethod
    def _skills(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip().casefold() for item in value if str(item).strip()][:20]
        return value


def _first_line(text: str) -> str:
    """The first line with words in it, without emoji and hashtags."""
    for line in text.splitlines():
        cleaned = re.sub(r"[#][\w]+", "", line)
        cleaned = re.sub(r"[^\w\s().,/+\-–—&]", "", cleaned).strip(" -–—:|")
        if len(cleaned) >= 3:
            return " ".join(cleaned.split())[:200]
    return "Вакансия из Telegram"


def _amount(raw: str) -> Decimal | None:
    digits = re.sub(r"[^\d]", "", raw)
    try:
        return Decimal(digits) if digits else None
    except InvalidOperation:
        return None


def fallback_parse(text: str) -> ParsedPost:
    """A rule-based reading for when the model is not there or not usable.

    Keeps the post as a vacancy with its first line as the title and the first
    contact it carries. Fields a regular expression cannot read honestly — the
    company, the city — stay empty rather than guessed.
    """
    remote: Literal["full", "hybrid", "no"] | None = None
    lowered = text.casefold()
    if re.search(r"удал[её]нн|remote|удаленка|удалёнка", lowered):
        remote = "hybrid" if re.search(r"гибрид|hybrid", lowered) else "full"
    return ParsedPost(
        is_vacancy=True,
        title=_first_line(text),
        apply_contact=contact_in(text),
        remote=remote,
    )


async def parse_post(text: str, *, router: LLMRouter | None) -> tuple[ParsedPost, str]:
    """The structured reading of a post, and which stage produced it: llm or rules."""
    if router is None:
        return fallback_parse(text), "rules"
    try:
        result = await router.complete_json(
            "parse_vacancy_post",
            ParsedPost,
            task=LLMTask.TELEGRAM_PARSE,
            variables={"post": text[:POST_LIMIT]},
        )
    except (AppError, ValidationError) as exc:
        # The router retries an invalid answer once with the error and gives up
        # with an AppError; a provider that is not configured is one too. Either
        # way the post is kept by the rules rather than dropped.
        logger.warning("sources.telegram.parse_fallback", reason=type(exc).__name__)
        return fallback_parse(text), "rules"
    return result.value, "llm"

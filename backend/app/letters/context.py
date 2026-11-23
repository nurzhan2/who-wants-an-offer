"""What the letter is allowed to know, and the set intersection at its centre.

The whole design rests on one property of the data: hh ships ``keySkills`` as a
structured list rather than as prose, so "what this vacancy asks for" and "what
this candidate has" are two sets, and the letter is built around their exact
intersection. No model is asked to guess the overlap; it is computed here, and
the model is given the answer.

That is also the honesty mechanism. The model receives the candidate's skills
and nothing else about the candidate, so the material for a claim about them
simply is not in the prompt — and the claims it does make are declared in a
structured field that :mod:`app.letters.generator` checks against the same set.
Whatever remains uncovered is named as uncovered. Inventing experience is lying
to an employer, not marketing.

Two shapes of the stored data are worth knowing before reading the code:

* **Nothing populates ``vacancy_skill`` yet.** Phase 3 writes hh's requirement
  list into ``vacancy_source.raw["_derived"]["key_skills"]``, and normalisation
  into ``VacancySkill`` rows is phase 4's. So the requirement list is read from
  the rows when they exist and from the payload when they do not, and a vacancy
  from a source with no structured skills (arbeitnow, remotive) simply has an
  empty required set — the letter then leans on the description, and says less.
* **``letterMaxLength`` is nobody's column, and nothing reaches it today.** It
  is hh's per-vacancy ceiling, it lives on the page the anonymous crawler reads,
  and the crawler does not keep it: ``hh.py`` stores
  ``raw={"_derived": HHDerived.model_dump(mode="json")}``, which is the derived
  block and not the payload it was derived from. So
  :func:`letter_max_length` returns hh's measured 10 000 for every vacancy in
  the database right now, and :func:`role_names` returns nothing — hh keeps
  ``professional_role_ids``, which are ids, and this deliberately refuses ids.
  Both functions are kept, and tested, because hardcoding 10 000 as *the rule*
  would be wrong the first time a vacancy sets a smaller one; but "it starts
  working the day a connector keeps the field" is a claim about a change nobody
  has made, and it is worth being plain that today it is the default branch that
  runs.
"""

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.letters.guard import DEFAULT_MAX_LENGTH
from app.resume.skills import fold as fold_skill

#: Longest description handed to the model. A posting is a page of prose; past
#: this it is a company brochure, and every character is billed. Cutting at a
#: fixed size is safe here because the requirement list travels separately in
#: ``key_skills`` — the part that gets truncated is never the part being matched.
MAX_DESCRIPTION_CHARS = 12_000

#: How many skills outside the requirement list are worth offering. Beyond a
#: handful they read as a dump of the resume rather than as an answer.
MAX_OTHER_SKILLS = 8


class Facts(BaseModel):
    """Base for the letter's inputs: immutable, and closed to stray keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SkillFact(Facts):
    """One skill the candidate has, as the profile records it."""

    #: The canonicalised name, which is what the intersection is computed on.
    canonical_name: str
    #: How the resume itself spelled it. Preferred in the letter: a candidate
    #: who wrote "PostgreSQL" should not be made to say "postgresql".
    spelling: str
    years: float | None = None
    level: str | None = None


class MatchedSkill(Facts):
    """One requirement the candidate demonstrably meets."""

    #: The requirement, spelled the way the vacancy spelled it.
    required_as: str
    #: The same skill, spelled the way the resume spelled it.
    possessed_as: str
    years: float | None = None
    level: str | None = None


#: Where a letter is read; see :attr:`VacancyFacts.channel`.
type LetterChannel = Literal["letter", "chat"]


class VacancyFacts(Facts):
    """Everything about one vacancy the letter may use."""

    vacancy_id: UUID
    title: str
    company: str | None = None
    city: str | None = None
    #: Untrusted: somebody else's text from somebody else's site. It reaches the
    #: model fenced and labelled as data — see :mod:`app.letters.prompt`.
    description: str | None = None
    #: The vacancy's own requirement list, in the order it stated them.
    key_skills: tuple[str, ...] = ()
    #: The subset of ``key_skills`` nobody actually stated: names read out of
    #: the description by :mod:`app.normalize.description`. A subset rather than
    #: a separate list because they are requirements either way — the letter
    #: answers them, the coverage counts them — and what changes is only what a
    #: report is allowed to call them.
    inferred_skills: tuple[str, ...] = ()
    #: hh mixes language requirements into the same list; the connector keeps
    #: them apart so the skill intersection stays a skill intersection. Verbatim,
    #: because the level and its label are both inside the string.
    language_requirements: tuple[str, ...] = ()
    #: Rendered where the payload carries a rendering ("От 1 года до 3 лет"),
    #: otherwise hh's own code. Never invented.
    work_experience: str | None = None
    #: Named roles only. hh stores ``professionalRoleIds`` as integers with no
    #: name anywhere in the payload, and a bare id tells a letter nothing.
    professional_roles: tuple[str, ...] = ()
    #: This vacancy's ceiling, already resolved by :func:`letter_max_length`.
    letter_max_length: int = DEFAULT_MAX_LENGTH
    #: Where the letter is read: ``"letter"`` in a job board's form or an
    #: e-mail, ``"chat"`` as the first message to a recruiter in Telegram —
    #: a vacancy found only in a Telegram channel. A chat message is shorter
    #: and ends on a question; see :data:`app.letters.guard.CHAT_MAX_LENGTH`.
    channel: LetterChannel = "letter"


class AchievementFact(Facts):
    """One bullet from one job, verbatim from the resume.

    Carried so a letter's proof can be a real result with the owner's own
    number in it rather than a list of technologies. The rule-based letter
    quotes it and never paraphrases it; a model may paraphrase it, and then
    :func:`unsupported_numbers` is what checks that no figure was added on the
    way.
    """

    company: str
    title: str
    text: str


class ProfileFacts(Facts):
    """Everything about the candidate the letter may use.

    This is the whole evidence base. A claim the letter makes that is not
    supported here is an invention, and the generator rejects it.
    """

    profile_id: UUID
    name: str | None = None
    headline: str | None = None
    summary: str | None = None
    seniority: str | None = None
    total_years: float | None = None
    locations: tuple[str, ...] = ()
    #: "en C1", "ru native" — as stored, because the level vocabulary is not ours.
    languages: tuple[str, ...] = ()
    skills: tuple[SkillFact, ...] = ()
    #: Every job's bullets, newest job first, each in the resume's own order.
    achievements: tuple[AchievementFact, ...] = ()
    #: The work format the resume states: "full" (remote), "hybrid", "no", or
    #: None when it says nothing. Printed in a CV's header; never inferred.
    remote: str | None = None

    @property
    def recorded_text(self) -> str:
        """Every string the profile stores about this person, joined.

        What a number in a generated text is checked against. Not the resume's
        raw text: that also holds the phone number, and "a figure appears
        somewhere in the upload" is a weaker promise than "a figure is one of
        the facts this profile records".
        """
        parts: list[str] = [
            self.headline or "",
            self.summary or "",
            *self.languages,
            *(f"{skill.spelling} {skill.canonical_name}" for skill in self.skills),
            *(f"{item.title} {item.company} {item.text}" for item in self.achievements),
        ]
        if self.total_years is not None:
            parts.append(f"{self.total_years:g}")
        parts.extend(f"{skill.years:g}" for skill in self.skills if skill.years is not None)
        return "\n".join(part for part in parts if part)


#: A figure as prose writes one: digits, optionally grouped or with a decimal
#: part ("12,000", "6,1", "2.4"). Not preceded by a letter, digit or separator,
#: so the 3 in "S3", the 2 in "EC2" and the 95 in "p95" are part of a name
#: rather than a claim; a unit glued on after it ("2.4s", "40%") is allowed.
_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[,\u00a0 ]\d{3}(?!\d))+|\d+)(?:[.,]\d+)?(?![.,]?\d)")

#: Thousands written in groups of three: "12,000", "12 000".
_GROUPED = re.compile(r"^\d{1,3}(?:[,\u00a0 ]\d{3})+$")


def numbers_in(text: str) -> frozenset[str]:
    """Every figure in ``text``, each in the spellings it may be matched under.

    "12,000", "12 000" and "12000" are one number, and so are "6,1" and "6.1":
    Russian writes a decimal comma and English a decimal point, and a CV in one
    language quoting a resume in the other is the ordinary case on this market.
    Anything looser ("12k" for "12,000") is deliberately not a match: rounding is
    the first step of inventing, and a false alarm here costs only a regenerated
    summary.
    """
    found: set[str] = set()
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        if _GROUPED.match(token):
            found.add(re.sub(r"[,\u00a0 ]", "", token))
        found.add(re.sub(r"[\u00a0 ]", "", token).replace(",", "."))
    return frozenset(found)


def unsupported_numbers(text: str, evidence: str) -> tuple[str, ...]:
    """Figures in ``text`` that appear nowhere in ``evidence``, in text order.

    A figure is unsupported only when none of the spellings :func:`numbers_in`
    gives it is among the spellings of the evidence's figures.
    """
    known = numbers_in(evidence)
    missing: list[str] = []
    for match in _NUMBER.finditer(text):
        token = match.group(0).strip()
        if not numbers_in(token) & known and token not in missing:
            missing.append(token)
    return tuple(missing)


class SkillOverlap(Facts):
    """The set intersection the letter is built around."""

    matched: tuple[MatchedSkill, ...] = ()
    #: Asked for and not held. Named in the letter rather than hidden: a gap the
    #: employer will find in the first five minutes is not a secret worth keeping.
    missing: tuple[str, ...] = ()
    #: Held and not asked for. Offered briefly, if at all.
    other: tuple[str, ...] = ()

    @property
    def coverage(self) -> float:
        """Share of the requirement list the candidate covers, 0.0 to 1.0."""
        total = len(self.matched) + len(self.missing)
        return len(self.matched) / total if total else 0.0


class LetterContext(Facts):
    """One vacancy, one profile, and the computed overlap between them."""

    vacancy: VacancyFacts
    profile: ProfileFacts
    overlap: SkillOverlap
    #: ISO 639-1. The letter is written in the language the posting is written
    #: in; on this market that is Russian unless the posting says otherwise.
    language: str = "ru"

    @property
    def possessed(self) -> frozenset[str]:
        """Fold keys of every skill the profile holds.

        The generator checks the model's declared claims against this: a skill
        the letter says the candidate has, that is not in here, is an invention.
        """
        return frozenset(fold(skill.canonical_name) for skill in self.profile.skills)


def fold(name: str) -> str:
    """The key two spellings of one skill must share to intersect.

    Re-exported from :func:`app.resume.skills.fold` rather than reimplemented:
    the ATS audit reports the same intersection this letter is written around,
    and two copies of the rule drifting apart would let a letter claim a skill
    the audit calls unnamed. Kept as a name in this module because that is what
    the rest of the package imports.
    """
    return fold_skill(name)


def overlap_of(required: tuple[str, ...], profile: ProfileFacts) -> SkillOverlap:
    """Intersect the vacancy's requirement list with the candidate's skills.

    Order follows the vacancy, not the profile: a letter answers requirements in
    the order the employer listed them.
    """
    by_key: dict[str, SkillFact] = {}
    for skill in profile.skills:
        by_key.setdefault(fold(skill.canonical_name), skill)

    matched: list[MatchedSkill] = []
    missing: list[str] = []
    claimed: set[str] = set()

    for requirement in required:
        key = fold(requirement)
        if not key:
            continue
        held = by_key.get(key)
        if held is None:
            if requirement not in missing:
                missing.append(requirement)
            continue
        if key in claimed:
            continue
        claimed.add(key)
        matched.append(
            MatchedSkill(
                required_as=requirement,
                possessed_as=held.spelling,
                years=held.years,
                level=held.level,
            )
        )

    other = [
        skill.spelling for key, skill in by_key.items() if key not in claimed and skill.spelling
    ]
    return SkillOverlap(
        matched=tuple(matched),
        missing=tuple(missing),
        other=tuple(other[:MAX_OTHER_SKILLS]),
    )


#: Below this share of Cyrillic letters a posting is answered in English. A
#: Russian posting quotes English stack names all the time, so the bar is low.
CYRILLIC_SHARE_FOR_RUSSIAN = 0.3


def posting_language(vacancy: VacancyFacts) -> str:
    """``ru`` or ``en``: the language the posting itself is written in.

    A letter and a CV answer a posting in its own language. Counted over the
    title and the description; an empty posting stays Russian, this market's
    default.
    """
    return text_language(f"{vacancy.title} {vacancy.description or ''}")


def text_language(text: str, *, default: str = "ru") -> str:
    """``ru`` or ``en`` by the share of Cyrillic letters; ``default`` for none.

    The measure a posting is judged by, applied to any other text \u2014 a resume
    bullet, a summary \u2014 so "is this sentence in the document's language" is
    answered the same way "which language is the document in" was.
    """
    cyrillic = sum(1 for char in text if "\u0400" <= char <= "\u04ff")
    latin = sum(1 for char in text if "a" <= char.lower() <= "z")
    if cyrillic + latin == 0:
        return default
    return "ru" if cyrillic / (cyrillic + latin) >= CYRILLIC_SHARE_FOR_RUSSIAN else "en"


def build_context(
    vacancy: VacancyFacts, profile: ProfileFacts, *, language: str | None = None
) -> LetterContext:
    """Assemble the context a letter is generated from, in the posting's language."""
    return LetterContext(
        vacancy=vacancy,
        profile=profile,
        overlap=overlap_of(vacancy.key_skills, profile),
        language=language or posting_language(vacancy),
    )


# The raw payloads below are ``dict[str, Any]`` because that is what a JSONB
# column is: a source's own shape, different per source, validated by nobody.
# Reading it defensively is the point of these two functions.
def letter_max_length(raws: list[dict[str, Any]]) -> int:
    """The smallest ceiling any stored payload names, or hh's measured default.

    The smallest rather than the first: the same vacancy can be held under
    several sources, and a letter has to fit wherever it is eventually sent.

    **No connector stores this today, so in production this is the default
    branch.** hh's ``letterMaxLength`` is on the page the crawler reads and not
    in ``HHDerived``, which is the only thing ``hh.py`` writes to ``raw``; the
    API sources have no such field at all. Both spellings are looked for —
    hh's camelCase, and the snake_case a ``model_dump`` would emit — so that
    whichever way a connector eventually keeps it is read, but until one does,
    every call returns :data:`app.letters.guard.DEFAULT_MAX_LENGTH`.
    """
    found = [
        value
        for raw in raws
        for value in _search(raw, ("letterMaxLength", "letter_max_length"), depth=6)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]
    return min(found) if found else DEFAULT_MAX_LENGTH


def role_names(raws: list[dict[str, Any]]) -> tuple[str, ...]:
    """Named professional roles from whatever shape a source stored them in.

    Strings and ``{"name": ...}`` / ``{"text": ...}`` records are read; integers
    are skipped, because hh's ``professionalRoleIds`` are ids whose names live in
    a dictionary this repository does not hold. Putting a bare ``96`` in a prompt
    is worse than saying nothing: it is a number the model will try to explain.

    **This returns nothing for every vacancy in the database today**, and not by
    accident: ids are exactly what hh stores and exactly what this refuses. Both
    spellings of the named key are searched so that a source which does keep
    names is read without a change here, but no source keeps them yet.
    """
    names: list[str] = []
    for raw in raws:
        for value in _search(raw, ("professionalRoles", "professional_roles"), depth=6):
            for item in value if isinstance(value, list) else [value]:
                name = _role_name(item)
                if name and name not in names:
                    names.append(name)
    return tuple(names)


def _role_name(item: object) -> str | None:
    """One role's name, if this item carries one."""
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        for key in ("name", "text", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _search(node: object, keys: tuple[str, ...], *, depth: int) -> list[Any]:
    """Every value stored under any of ``keys`` in a payload, depth-limited.

    A payload is a source's own shape and it nests differently per source, so
    the key is looked for rather than addressed. Several spellings, because a
    field kept verbatim from hh and the same field kept through a Pydantic
    ``model_dump`` are the same field under two names. The depth limit is what
    keeps a pathological document from turning a lookup into a walk of the whole
    tree.
    """
    if depth <= 0:
        return []
    found: list[Any] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name in keys:
                found.append(value)
            else:
                found.extend(_search(value, keys, depth=depth - 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_search(item, keys, depth=depth - 1))
    return found


def clip_description(description: str | None) -> str | None:
    """The description, bounded. See :data:`MAX_DESCRIPTION_CHARS`."""
    if description is None:
        return None
    text = description.strip()
    if not text:
        return None
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    return text[:MAX_DESCRIPTION_CHARS].rstrip() + "\n[...]"

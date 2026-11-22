"""What a tailored CV may know, and the two different sets at its centre.

The letter package established the shape: a vacancy's requirement list and a
candidate's skills are two sets, their intersection is computed in code, and the
model is handed the answer rather than asked to guess it. A CV works the same
way and reuses the same code — :class:`app.letters.context.ProfileFacts`,
:class:`~app.letters.context.VacancyFacts`, :func:`~app.letters.context.fold`
and :func:`~app.letters.context.overlap_of` are imported here rather than
written again.

That import is deliberate and it is not just thrift. If the CV computed its own
overlap, the CV and the covering letter sent to the same employer on the same
day could disagree about which requirements the candidate covers. Nothing would
catch it — both would be internally consistent — and the person reading them
would be the one to notice. One definition of "covered", shared.

**The two sets are not the same set, and the difference is the whole design.**

``overlap``
    what the candidate *covers*: ``profile_skill`` rows against the vacancy's
    requirement list. This is a matching judgement, it is the same one the
    scorer and the letter make, and it decides what the CV leads with.

:attr:`CVContext.traceable`
    what the profile *records at all*: those same skills, plus every technology
    the resume attributed to a specific job. This is a provenance fact, not a
    judgement, and it decides what the document is allowed to mention.

A name in the second set but not the first is not an invention — the resume says
the candidate used it at a named employer — it simply is not what the vacancy
asked for. Collapsing the two would either forbid the CV from listing a job's
real stack or let it claim a requirement it does not meet, and those are the two
opposite ways of getting this feature wrong.

**Experience is referenced, never retyped.** Each job carries a ``ref``, and
everything downstream — the prompt, the model's answer, the renderer — moves the
``ref`` around. Company, title and dates are read from the database at render
time. "The generator does not change dates, company names or job titles" is
therefore a property of where those strings come from, not a rule somebody has
to remember to check.
"""

from pydantic import BaseModel, ConfigDict

from app.documents.contacts import ContactBlock
from app.letters.context import (
    Facts,
    ProfileFacts,
    SkillOverlap,
    VacancyFacts,
    fold,
    overlap_of,
    posting_language,
)

#: Longest summary a generated CV may carry, in characters. A CV summary is
#: two or three sentences that say what the person does and one result; past
#: this it is a cover letter in the wrong document, and it pushes the first job
#: below the fold on the page an employer actually skims. The research behind
#: the workshop (docs/WORKSHOP-RESEARCH.md, rule L3) puts it at 50 words, which
#: is about this many characters in either language.
MAX_SUMMARY_CHARS = 450

#: Most achievement bullets one job shows. The research's figure for a recent
#: role is three to five (S11, S27); more reads as a duty list, and on a
#: one-page CV the sixth bullet of one job is the second job pushed off it.
MAX_BULLETS_PER_JOB = 5

#: What the rule-based CV shows for a job past the :data:`RECENT_JOBS` most
#: recent: older roles earn fewer lines, the same compression a person applies
#: by hand (S28).
MAX_BULLETS_OLDER_JOB = 3

#: How many of the newest jobs count as recent for :data:`MAX_BULLETS_PER_JOB`.
RECENT_JOBS = 2

#: Most jobs a CV shows. Beyond this it is a work history rather than a CV, and
#: the ones past it are old enough that no employer reads them. Selecting which
#: to drop is the model's job; this is the ceiling it selects under.
MAX_EXPERIENCE_ENTRIES = 8


class ExperienceEntry(Facts):
    """One job, exactly as the resume recorded it.

    Everything here is quoted from ``profile_experience`` and nothing in it may
    be edited by a generated document. What a generated document may do is
    decide whether this entry appears, where in the order it appears, and which
    of :attr:`stack` it shows.
    """

    #: Stable handle for this job within one generation. The row's ``position``,
    #: which is unique per profile, so it survives being written into a stored
    #: payload and read back when the document is re-rendered.
    ref: int
    company: str
    title: str
    #: "YYYY-MM", or None where the resume gave no date at all.
    start: str | None = None
    end: str | None = None
    is_current: bool = False
    #: The technologies the resume attributed to *this* job. A tailored CV shows
    #: a subset chosen for the vacancy; anything outside it is an invention
    #: about this employer, which is a worse lie than an invention in general.
    stack: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    #: The resume's own bullets for this job, verbatim. A generated CV chooses
    #: some of them by index and prints those exactly as they are here; it has
    #: no way to write one, which is what keeps every result and every number
    #: in the document the owner's own.
    achievements: tuple[str, ...] = ()


class EducationEntry(Facts):
    """One degree or programme, as the resume recorded it.

    Carried for the same reason as :class:`ExperienceEntry` and edited just as
    little. It earns its place in the context because this project's own auditor
    marks a resume down for having no education heading, so a CV generated
    without the data would be criticised by the code in the next package along.
    """

    institution: str
    degree: str | None = None
    field: str | None = None
    end_year: int | None = None


class CVContext(BaseModel):
    """One vacancy, one profile, and everything a CV for the pair may use.

    Not a :class:`~app.letters.context.Facts` subclass only because it carries
    the contact block, which is the owner's personal data and is deliberately
    kept in a model of its own — see :mod:`app.documents.contacts`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vacancy: VacancyFacts
    profile: ProfileFacts
    contacts: ContactBlock
    experience: tuple[ExperienceEntry, ...] = ()
    education: tuple[EducationEntry, ...] = ()
    overlap: SkillOverlap = SkillOverlap()
    #: ISO 639-1. A CV is written in the language of the posting it answers; on
    #: this market that is Russian unless the posting says otherwise.
    language: str = "ru"

    @property
    def refs(self) -> frozenset[int]:
        """Every job handle a generated arrangement may name."""
        return frozenset(entry.ref for entry in self.experience)

    @property
    def by_ref(self) -> dict[int, ExperienceEntry]:
        """The jobs by handle, for resolving an arrangement back to facts."""
        return {entry.ref: entry for entry in self.experience}

    @property
    def traceable(self) -> frozenset[str]:
        """Fold keys of every technology the profile records anywhere.

        The union of the skill rows and every job's recorded stack. This is the
        set a generated document's every technical claim has to land inside; see
        :mod:`app.documents.guard`, which is where it is enforced.

        The union rather than the skill rows alone because the two disagree in
        practice and the skill rows are the smaller of them: extraction records
        a technology under a job it does not always also promote to a skill. A
        CV that could not name a job's own stack would be forbidden from stating
        something the resume states in as many words.
        """
        keys = {fold(skill.canonical_name) for skill in self.profile.skills}
        keys.update(fold(name) for entry in self.experience for name in entry.stack)
        return frozenset(key for key in keys if key)

    @property
    def achievement_text(self) -> str:
        """Every stored bullet of every job, one per line.

        A requirement named in here is something the resume says the candidate
        did, in the owner's own words — so :mod:`app.documents.guard` treats a
        mention of it as traceable, exactly as it treats a job's recorded stack.
        Without that, a bullet printed verbatim could fail the document for
        naming a technology extraction never promoted to a skill row, and the
        rule-based CV — which prints every bullet — would be refused for quoting
        the resume.
        """
        return "\n".join(text for entry in self.experience for text in entry.achievements)

    @property
    def recorded_text(self) -> str:
        """Everything the profile records, for checking where a number came from.

        The profile's own text (headline, summary, skills, years) plus every job
        — title, employer, dates and bullets — and every degree. A figure in a
        model-written summary has to be one of the figures in here; see
        :func:`app.documents.guard.check_summary`.
        """
        jobs = [
            " ".join(
                part
                for part in (
                    entry.title,
                    entry.company,
                    entry.start,
                    entry.end,
                    *entry.achievements,
                )
                if part
            )
            for entry in self.experience
        ]
        degrees = [
            " ".join(
                str(part)
                for part in (item.institution, item.degree, item.field, item.end_year)
                if part
            )
            for item in self.education
        ]
        return "\n".join((self.profile.recorded_text, *jobs, *degrees))

    @property
    def allowed_headlines(self) -> tuple[str, ...]:
        """Every string the CV's headline is permitted to be.

        A closed list rather than free text, and the reason is the same one that
        makes experience a reference: a headline is the first line an employer
        reads and "Senior Kubernetes Engineer" is a claim, not a formatting
        choice. So the model chooses from what the profile already says about
        this person — their own headline, and the titles they actually held —
        and choosing is all it does.
        """
        titles: list[str] = []
        for value in (self.profile.headline, *(entry.title for entry in self.experience)):
            text = (value or "").strip()
            if text and text not in titles:
                titles.append(text)
        return tuple(titles)


def build_context(
    vacancy: VacancyFacts,
    profile: ProfileFacts,
    *,
    contacts: ContactBlock,
    experience: tuple[ExperienceEntry, ...] = (),
    education: tuple[EducationEntry, ...] = (),
    language: str | None = None,
) -> CVContext:
    """Assemble the context a tailored CV is generated from.

    The overlap is computed here, once, by the letter package's function. The
    experience list is capped at :data:`MAX_EXPERIENCE_ENTRIES` *before* the
    model sees it, in the resume's own order: a job the CV is never going to
    show is a job not worth paying to put in a prompt, and cutting the oldest is
    the same choice a person makes with the same list.
    """
    return CVContext(
        vacancy=vacancy,
        profile=profile,
        contacts=contacts,
        experience=experience[:MAX_EXPERIENCE_ENTRIES],
        education=education,
        overlap=overlap_of(vacancy.key_skills, profile),
        language=language or posting_language(vacancy),
    )


def experience_from_rows(rows: list[dict[str, object]]) -> tuple[ExperienceEntry, ...]:
    """Turn stored experience rows into context entries, skipping the unusable.

    A row with no company and no title cannot be rendered as a job, and rendering
    it as a blank one would be worse than leaving it out: an employer reads a
    gap in a CV as something hidden.
    """
    entries: list[ExperienceEntry] = []
    for row in rows:
        position = row.get("position")
        company = str(row.get("company") or "").strip()
        title = str(row.get("title") or "").strip()
        # ``position`` is the handle everything downstream refers to, so a row
        # without one is not renderable at all — unlike a missing company, which
        # only costs a line. Both are dropped here rather than downstream, so
        # every entry the rest of the feature sees is one that can appear in a
        # document.
        if not isinstance(position, int) or isinstance(position, bool):
            continue
        if not company and not title:
            continue
        entries.append(
            ExperienceEntry(
                ref=position,
                company=company,
                title=title,
                start=_month(row.get("start")),
                end=_month(row.get("end")),
                is_current=bool(row.get("is_current")),
                stack=_strings(row.get("stack")),
                domains=_strings(row.get("domains")),
                achievements=_strings(row.get("achievements")),
            )
        )
    return tuple(entries)


def _month(value: object) -> str | None:
    """A "YYYY-MM" string, or None for anything else."""
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None


def _strings(value: object) -> tuple[str, ...]:
    """A JSONB list read defensively.

    The column's declared ``list[str]`` is a promise PostgreSQL does not keep:
    it was last written from an LLM extraction, and checking what actually
    arrived costs one line.
    """
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def education_from_rows(rows: object) -> tuple[EducationEntry, ...]:
    """Turn the profile's stored education list into context entries.

    ``rows`` is a JSONB column, so it is ``object`` until it has been looked at.
    An entry with no institution is dropped for the same reason a job with no
    employer is: there is nothing to print on the line.
    """
    if not isinstance(rows, list):
        return ()
    entries: list[EducationEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        institution = str(row.get("institution") or "").strip()
        if not institution:
            continue
        end_year = row.get("end_year")
        entries.append(
            EducationEntry(
                institution=institution,
                degree=_text(row.get("degree")),
                field=_text(row.get("field")),
                end_year=end_year
                if isinstance(end_year, int) and not isinstance(end_year, bool)
                else None,
            )
        )
    return tuple(entries)


def _text(value: object) -> str | None:
    """A non-empty string, or None."""
    text = value.strip() if isinstance(value, str) else ""
    return text or None


#: Month names as each language's CV writes a period. English abbreviates
#: (``Aug 2025``), Russian writes the nominative in full and in lower case
#: (``август 2025``) — rules D1 and D2 of the workshop research. Both forms are
#: ones ``app.resume.ats_audit.DATE_TOKEN`` recognises, which is the other half
#: of the contract: an employment period the auditor cannot find reads to an
#: applicant tracking system as no experience at all.
MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ru": (
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    ),
}

#: The word for a job that has not ended, per language.
PRESENT: dict[str, str] = {"en": "Present", "ru": "настоящее время"}

#: Between the two ends of a period: an en dash with spaces, the typographic
#: range sign in both languages, and inside cp1251.
PERIOD_DASH = " \u2013 "


def render_period(entry: ExperienceEntry, language: str = "ru") -> str:
    """One job's dates, in the CV's language and in a form the auditor finds.

    ``Aug 2025 - Present`` in English, ``август 2025 - настоящее время`` in
    Russian (the dash printed is an en dash). A language this module has no
    month names for is written in English, the lingua franca of the remote
    postings that are the only other case. A month that is not ``YYYY-MM`` (a
    resume that gave only a year, read through an older extraction) is printed
    as stored rather than guessed at.
    """
    lang = language if language in MONTHS else "en"
    start = _month_name(entry.start, lang)
    if entry.is_current:
        return f"{start}{PERIOD_DASH}{PRESENT[lang]}" if start else PRESENT[lang]
    end = _month_name(entry.end, lang)
    if start and end:
        return f"{start}{PERIOD_DASH}{end}"
    return start or end or ""


def _month_name(month: str | None, language: str) -> str:
    """ "2022-04" as "Apr 2022" / "апрель 2022"; anything else unchanged or empty."""
    if not month:
        return ""
    parts = month.split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return month
    index = int(parts[1])
    if not 1 <= index <= len(MONTHS[language]):
        return month
    return f"{MONTHS[language][index - 1]} {parts[0]}"


class SkillChoice(BaseModel):
    """One skill, and the word this CV uses for it.

    ``shown_as`` exists because of a measured property of employers' parsers:
    they look for exact strings, so "PostgreSQL" and "постгрес" are two
    different technologies to a machine that has been told to find the first.
    Spelling a skill the vacancy's way when it is the same skill is the whole of
    what "tailoring" means here — and because it is the same skill, it is
    checkable: :mod:`app.documents.guard` requires the two names to fold to one
    key, so renaming Python to Kubernetes fails the check rather than the taste.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The profile's own canonical name for this skill.
    canonical_name: str
    #: What the document prints.
    shown_as: str

    @property
    def is_renamed(self) -> bool:
        """Whether this CV spells the skill differently from the profile."""
        return self.canonical_name.strip().lower() != self.shown_as.strip().lower()

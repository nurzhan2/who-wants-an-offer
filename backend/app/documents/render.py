"""Turning an arrangement plus the profile's rows into a file and its text.

**The format is DOCX, and the choice is the auditor's rather than a preference.**
``app/resume/ats_audit.py`` exists because phase 2 measured what an employer's
parser gets from a two-column PDF resume: a sidebar heading welded onto a
sentence from the other column, from which "the model extracts confident
nonsense". Everything the audit reports as critical — ``NO_TEXT_LAYER``,
``BROKEN_GLYPHS``, ``UNMAPPED_FONT``, ``COLUMN_INTERLEAVING`` — is a failure of
the step that turns characters into drawn glyphs at fixed coordinates. A DOCX
has no such step. Its text *is* text, in document order, and the four findings
that cost the most points are unreachable by construction rather than avoided by
care.

The audit does raise ``FORMAT_NOT_PDF`` on a DOCX, and the honest thing is to
quote it: it says most employers expect a PDF, and it costs zero points, because
it is a note about what was checked rather than a defect. So the trade is a real
one and it is stated where the owner can see it — the document is generated in
the format that parses best, and the report handed over with it says in as many
words that exporting to PDF changes the layout and should be re-audited. The
other half of the choice is docs/ENGINEERING.md's rule about dependencies: ``python-docx``
is already a project dependency, and no PDF writer is. ``reportlab`` is in the
dev group, where it generates test fixtures; promoting a dev tool into the
runtime to produce a format the auditor scores lower is the wrong trade twice.

**One column, no tables, no text boxes.** Not a style choice either.
``check_tables`` reports the share of words living inside a table and warns past
a configured ratio, precisely because many parsers read a table cell by cell and
turn a skills grid into a shuffled bag of words. So the skills section is
paragraphs, the experience section is paragraphs, and nothing in this module
constructs a table at all.

**Rendering is a pure function.** Same arrangement and same profile rows in,
same bytes and same text out. That is what lets ``generated_document`` store the
arrangement rather than the file: a stored version can be rebuilt on demand, and
a file that has drifted from the profile it claims to describe cannot exist,
because there is no file kept to drift.

The plain-text rendering is not a convenience copy. It is the document as the
audit reads it, and it is stored alongside so the audit's verdict stays
explainable after the fact.

**The document is in the posting's language, all of it that this module
writes.** The owner's first generated CV had «О себе:», «Навыки:» and «Стек:»
over English text, because the headings were Russian constants. Every label
here — the headings, the stack line, the word for a job that has not ended, the
month names, the names of spoken languages — now comes from a per-language
table keyed by :attr:`app.documents.context.CVContext.language`, and a test
asserts that an English CV carries no Cyrillic label. What this module cannot
translate is what it did not write: a bullet, a job title or a company is the
owner's text and is printed as stored, in whatever language the resume used.

**Layout, and why each number is what it is.** One A4 page is the target for
the owner's 1–2 years (docs/WORKSHOP-RESEARCH.md, L1/L2): 1.8 cm side margins
and 1.5 cm top and bottom, an 11 pt body, an 18 pt name, headings in bold
capitals at 11 pt with a rule under them. A job is a bold "Title — Company" line
with its period pushed to the right margin by a right-aligned tab stop — a tab,
not a table, so the text layer reads "title, company, dates" in that order on
one line, which is how an applicant tracking system attributes dates to a job.
Bullets are paragraphs that begin with "• " under a hanging indent, rather than
Word's numbering: a numbering definition is one more structure for a parser to
interpret, and the glyph being real text is what keeps :func:`to_text` and the
file saying the same characters.
"""

import io
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docx import Document as DocxDocument
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, ConfigDict, field_validator

from app.documents import skill_groups
from app.documents.context import (
    CVContext,
    EducationEntry,
    ExperienceEntry,
    SkillChoice,
    render_period,
)
from app.letters.context import text_language

#: What the file is. Kept as a constant because it is written into every stored
#: row and read back to pick a media type.
FILE_FORMAT = "docx"

#: The media type a browser needs to save the file with the right icon.
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class Headings:
    """The section headings of one language's CV."""

    summary: str
    skills: str
    experience: str
    projects: str
    education: str
    languages: str

    @property
    def all(self) -> frozenset[str]:
        """Every heading, for the renderer to recognise its own lines."""
        return frozenset(
            (
                self.summary,
                self.skills,
                self.experience,
                self.projects,
                self.education,
                self.languages,
            )
        )


#: Section headings per language, and a contract with two readers.
#:
#: ``app.resume.ats_audit.SECTION_PATTERNS`` looks for "experience", "skills"
#: and "education" (or «опыт работы», «навыки», «образование») case-blind; the
#: audit reports ``MISSING_SECTIONS`` when it cannot find them, so renaming one
#: here to something prettier makes the document score worse for no reason a
#: reader would see. ``app.workshop.sections`` reads a heading only when the
#: line carries a marker — ``#``, bold, a trailing colon, or all capitals — and
#: these are all capitals, which is also how the research's reference CVs set
#: them; it maps both languages' spellings onto one section, so the owner's rule
#: «в навыках не меньше 21 пункта» finds ``SKILLS`` and ``КЛЮЧЕВЫЕ НАВЫКИ``.
#: ``projects`` is in the table for when projects are stored; nothing renders it
#: today, because nothing records a project yet.
HEADINGS: dict[str, Headings] = {
    "en": Headings(
        summary="SUMMARY",
        skills="SKILLS",
        experience="EXPERIENCE",
        projects="PROJECTS",
        education="EDUCATION",
        languages="LANGUAGES",
    ),
    "ru": Headings(
        summary="О СЕБЕ",
        skills="КЛЮЧЕВЫЕ НАВЫКИ",
        experience="ОПЫТ РАБОТЫ",
        projects="ПРОЕКТЫ",
        education="ОБРАЗОВАНИЕ",
        languages="ЯЗЫКИ",
    ),
}

#: The label in front of a job's technologies. Kept as a label with a colon,
#: the way every CV on this market writes it; ``app.workshop.sections`` knows
#: this one line is not a section, see ``INLINE_LABELS`` there.
STACK_LABEL: dict[str, str] = {"en": "Stack", "ru": "Стек"}

#: Spoken languages by ISO 639-1 code, as each CV language names them. The
#: profile stores codes ("en C1", "ru native"), which no reader should see.
LANGUAGE_NAMES: dict[str, dict[str, str]] = {
    "en": {
        "ru": "Russian",
        "en": "English",
        "kk": "Kazakh",
        "de": "German",
        "fr": "French",
        "es": "Spanish",
        "tr": "Turkish",
        "uz": "Uzbek",
        "zh": "Chinese",
        "uk": "Ukrainian",
    },
    "ru": {
        "ru": "Русский",
        "en": "Английский",
        "kk": "Казахский",
        "de": "Немецкий",
        "fr": "Французский",
        "es": "Испанский",
        "tr": "Турецкий",
        "uz": "Узбекский",
        "zh": "Китайский",
        "uk": "Украинский",
    },
}

#: How each CV language says "native".
NATIVE: dict[str, str] = {"en": "native", "ru": "родной"}


@dataclass(frozen=True, slots=True)
class Place:
    """A city as each CV language writes it, with its country and UTC offset."""

    en: str
    ru: str
    country_en: str
    country_ru: str
    utc: str


#: The cities this market's candidates live in. The reference CVs open the
#: contact line with "Almaty, Kazakhstan (UTC+5)": a remote recruiter's first
#: two questions are where and which hours, and the city alone answers neither
#: for someone who has never heard of it. None of this is a claim about the
#: candidate — it is the geography of the city the profile records — and a
#: city not listed here is printed exactly as stored, with nothing added.
#: Kazakhstan has been on a single UTC+5 since March 2024.
PLACES: tuple[Place, ...] = (
    Place("Almaty", "Алматы", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Astana", "Астана", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Shymkent", "Шымкент", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Karaganda", "Караганда", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Aktobe", "Актобе", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Atyrau", "Атырау", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Pavlodar", "Павлодар", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Oskemen", "Усть-Каменогорск", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Bishkek", "Бишкек", "Kyrgyzstan", "Кыргызстан", "UTC+6"),
    Place("Tashkent", "Ташкент", "Uzbekistan", "Узбекистан", "UTC+5"),
    Place("Moscow", "Москва", "Russia", "Россия", "UTC+3"),
    Place("Saint Petersburg", "Санкт-Петербург", "Russia", "Россия", "UTC+3"),
    Place("Minsk", "Минск", "Belarus", "Беларусь", "UTC+3"),
    Place("Tbilisi", "Тбилиси", "Georgia", "Грузия", "UTC+4"),
)

#: Other spellings of the same cities, as resumes write them.
PLACE_ALIASES: dict[str, str] = {
    "алма-ата": "Almaty",
    "alma-ata": "Almaty",
    "нур-султан": "Astana",
    "nur-sultan": "Astana",
    "усть-каменогорск": "Oskemen",
    "ust-kamenogorsk": "Oskemen",
    "st. petersburg": "Saint Petersburg",
    "санкт петербург": "Saint Petersburg",
    "karagandy": "Karaganda",
}


def place_of(city: str) -> Place | None:
    """The place a stored city names, in either alphabet, or None."""
    key = " ".join(city.split()).casefold()
    key = PLACE_ALIASES.get(key, key).casefold()
    return next(
        (place for place in PLACES if key in (place.en.casefold(), place.ru.casefold())), None
    )


#: How the profile's work-format preference is written in the header. Only the
#: two that say something an employer filters on; "no" (on-site only) is the
#: default reading of a CV with a city on it and is not printed.
WORK_FORMAT: dict[str, dict[str, str]] = {
    "en": {"full": "Open to remote", "hybrid": "Hybrid"},
    "ru": {"full": "удалённо", "hybrid": "гибрид"},
}


#: What a bullet line starts with, in the file and in the text alike.
BULLET = "• "

#: Point sizes. A name, a headline, a heading, a body and a note; the research's
#: references use the same handful, and more than that reads as a template.
SIZE_NAME = 18
SIZE_HEADLINE = 11.5
SIZE_HEADING = 11
SIZE_BODY = 11
SIZE_NOTE = 10

#: A4, the paper every employer on this market and in Europe prints on. The
#: python-docx template is US Letter, which turns a one-page CV into a page and
#: two lines when an A4 reader opens it.
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
MARGIN_SIDE_CM = 1.8
MARGIN_TOP_CM = 1.5

#: Where a job's period is pushed to: the right edge of the text block.
RIGHT_EDGE_CM = PAGE_WIDTH_CM - 2 * MARGIN_SIDE_CM

#: The hanging indent a bullet's continuation lines align under.
BULLET_INDENT_CM = 0.27

#: The font. Present on every machine an employer opens a .docx on, and it has
#: Cyrillic, which a Russian CV needs and a fallback font substitutes badly.
FONT = "Calibri"

#: The quieter colour of the headline, the contact line and the stack line.
GREY = RGBColor(0x44, 0x44, 0x44)


def headings_for(language: str) -> Headings:
    """This language's headings, or English for one the table does not hold."""
    return HEADINGS.get(language, HEADINGS["en"])


class JobChoice(BaseModel):
    """One job as the CV shows it: which job, which technologies, which bullets.

    ``bullets`` are indices into :attr:`app.documents.context.ExperienceEntry.
    achievements`, in the order the CV prints them. An index is all a generated
    arrangement can say about a bullet: the text is read from the profile when
    the document is built, so a reworded result or an inflated number has no
    field to arrive in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: int
    stack: tuple[str, ...] = ()
    bullets: tuple[int, ...] = ()


class ProjectChoice(BaseModel):
    """One project as the CV shows it: which project and which of its bullets.

    Indices into :attr:`app.documents.context.ProjectEntry.achievements`, for
    the same reason a job's bullets are: the text is the owner's and is read
    from the profile when the document is built.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: int
    bullets: tuple[int, ...] = ()


class CVArrangement(BaseModel):
    """The decisions a generated CV consists of, and nothing else.

    This is the whole of what a model is allowed to decide, and reading the
    field list is the shortest way to see what the feature does and does not do.
    There is no field here for a company, a job title, a date, a skill level, a
    number of years or the text of a bullet — those are read from the database
    when the document is built — so the model has no way to express a change to
    one.

    Stored verbatim in ``generated_document.payload``, which is why it is a
    Pydantic model with ``extra="forbid"``: it crosses into JSONB and comes back
    out, and a key that arrived from a future version has to fail loudly rather
    than be rendered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One of :attr:`app.documents.context.CVContext.allowed_headlines`, verbatim.
    headline: str = ""
    #: The one free-text field in the document. May be empty.
    summary: str = ""
    #: Every skill the CV shows, in the order it shows them, each with the word
    #: this document uses for it.
    skills: tuple[SkillChoice, ...] = ()
    #: The jobs in the order the CV presents them.
    experience: tuple[JobChoice, ...] = ()
    #: The projects the CV shows, in order. Empty in every version stored
    #: before migration 0021, which is exactly what those documents showed.
    projects: tuple[ProjectChoice, ...] = ()

    @field_validator("experience", mode="before")
    @classmethod
    def _accept_stored_pairs(cls, value: Any) -> Any:
        """Read a payload stored before bullets existed.

        Versions written before migration 0020 hold each job as a ``[ref,
        [stack]]`` pair. They are still downloadable — :func:`app.documents.
        service.rebuild` re-renders them — so the old shape is read as a job
        with no bullets, which is exactly what those documents showed.
        """
        if not isinstance(value, list | tuple):
            return value
        converted: list[Any] = []
        for item in value:
            if isinstance(item, list | tuple) and len(item) == 2 and isinstance(item[0], int):
                converted.append({"ref": item[0], "stack": tuple(item[1] or ())})
            else:
                converted.append(item)
        return converted


class LineKind(StrEnum):
    """What a line of the layout is, which is what decides how it is drawn."""

    NAME = "name"
    HEADLINE = "headline"
    CONTACT = "contact"
    HEADING = "heading"
    TEXT = "text"
    #: "Title — Company" with the period at the right margin.
    ENTRY = "entry"
    BULLET = "bullet"
    #: The quiet line under a job's bullets: its technologies.
    NOTE = "note"
    #: "Backend: FastAPI, Django" — a labelled line of the skills section.
    GROUP = "group"


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the layout: its kind, its text, and what sits at its right."""

    kind: LineKind
    text: str
    #: Right-aligned on the same line, after a tab: a job's period, a degree's
    #: year. Part of the line's text in both renderings.
    aside: str = ""

    @property
    def rendered(self) -> str:
        """The line as plain text — and, character for character, as the file."""
        return f"{self.text}\t{self.aside}" if self.aside else self.text


def _resolved(
    arrangement: CVArrangement, context: CVContext
) -> list[tuple[ExperienceEntry, JobChoice]]:
    """Each arranged job paired with the row it refers to, unknown refs dropped.

    Dropping rather than raising because rendering is also how a stored payload
    is rebuilt, and a payload can outlive the experience row it names — a resume
    re-parsed with one job removed leaves exactly that. An old version of a CV
    then renders without the job it no longer has evidence for, which is the
    conservative direction: the alternative is rendering a job from a string in
    the payload, and a job that exists only inside a generated document is the
    invention this whole feature is built to prevent.
    """
    by_ref = context.by_ref
    resolved: list[tuple[ExperienceEntry, JobChoice]] = []
    for job in arrangement.experience:
        entry = by_ref.get(job.ref)
        if entry is not None:
            resolved.append((entry, job))
    return resolved


def _chosen(achievements: tuple[str, ...], indices: tuple[int, ...]) -> list[str]:
    """The stored bullets at these indices, in this order, unknown ones skipped."""
    shown: list[str] = []
    for index in indices:
        if 0 <= index < len(achievements) and achievements[index] not in shown:
            shown.append(achievements[index])
    return shown


def _bullets(entry: ExperienceEntry, job: JobChoice) -> list[str]:
    """The chosen bullets' stored text, in the chosen order.

    An index the row does not have is skipped for the same reason an unknown
    ref is: a stored arrangement can outlive a re-parse that shortened the list,
    and an old version must render with less rather than with a wrong bullet.
    The guard rejects such an index on a fresh arrangement, so this only ever
    runs on old ones.
    """
    shown: list[str] = []
    for index in job.bullets:
        if 0 <= index < len(entry.achievements):
            text = entry.achievements[index]
            if text not in shown:
                shown.append(text)
    return shown


def _contact_lines(context: CVContext) -> list[str]:
    """Where and which format on one line; how to reply on the next.

    The order is the reference CVs': location (with country and UTC offset for a
    known city) and the work format the profile records, then email, phone,
    Telegram — where recruiters on this market start — and the code links. Two
    lines because one carrying all of it wraps at an arbitrary middle dot on A4;
    split here, each half reads as a unit. Lines and not a table: see the module
    docstring. Links are printed as their addresses, which name themselves
    ("github.com/…"), so neither line carries a "Label:" a parser could mistake
    for a section.
    """
    contacts = context.contacts
    language = context.language
    where: list[str] = []
    city = (contacts.city or "").strip()
    if city:
        place = place_of(city)
        if place is None:
            where.append(city)
        elif language == "ru":
            where.append(f"{place.ru}, {place.country_ru} ({place.utc})")
        else:
            where.append(f"{place.en}, {place.country_en} ({place.utc})")
    work_format = WORK_FORMAT.get(language, WORK_FORMAT["en"]).get(context.profile.remote or "")
    if work_format:
        where.append(work_format)
    reach = [value.strip() for value in (contacts.email, contacts.phone) if value and value.strip()]
    links = sorted(contacts.links, key=lambda link: link[0] != "Telegram")
    reach.extend(_link_text(label, address) for label, address in links)
    return [line for line in (" · ".join(where), " · ".join(p for p in reach if p)) if line]


def _link_text(label: str, address: str) -> str:
    """One link as the header prints it: a Telegram handle, or the bare address."""
    address = address.strip()
    if label == "Telegram":
        handle = address.rsplit("/", 1)[-1].lstrip("@")
        return f"Telegram @{handle}" if handle else ""
    if "." in address or "/" in address:
        return address
    return f"{label} {address}"


def _languages_line(languages: tuple[str, ...], language: str) -> str:
    """ "en C1", "ru native" as "English (C1) · Russian (native)" or the Russian.

    The profile stores a code and a level because that is what extraction can
    normalise; a reader should see names. An entry this table cannot name is
    printed as stored rather than dropped — losing a language off a CV is worse
    than printing its code.
    """
    names = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES["en"])
    native = NATIVE.get(language, NATIVE["en"])
    rendered: list[str] = []
    for item in languages:
        code, _, level = item.strip().partition(" ")
        name = names.get(code.lower())
        if name is None:
            rendered.append(item.strip())
            continue
        level = level.strip()
        if level.lower() == "native":
            level = native
        if language == "ru":
            rendered.append(f"{name} — {level}" if level else name)
        else:
            rendered.append(f"{name} ({level})" if level else name)
    return " · ".join(item for item in rendered if item)


def layout(arrangement: CVArrangement, context: CVContext) -> list[Line]:
    """The document as typed lines, in the order it is laid out.

    The single source of both renderings. The .docx draws these lines and the
    plain text joins them, so the file and the text the audit reads cannot
    describe different documents — which they could if each were assembled
    separately, and nothing would notice until an audit explained a finding
    that was not in the file.
    """
    language = context.language
    headings = headings_for(language)
    lines: list[Line] = []

    if name := (context.contacts.name or context.profile.name or "").strip():
        lines.append(Line(LineKind.NAME, name))
    if headline := arrangement.headline.strip():
        lines.append(Line(LineKind.HEADLINE, headline))
    lines.extend(Line(LineKind.CONTACT, text) for text in _contact_lines(context))

    if summary := arrangement.summary.strip():
        lines.extend((Line(LineKind.HEADING, headings.summary), Line(LineKind.TEXT, summary)))

    if arrangement.skills:
        lines.append(Line(LineKind.HEADING, headings.skills))
        groups = skill_groups.grouped(
            [(skill.canonical_name, skill.shown_as) for skill in arrangement.skills], language
        )
        if groups is None:
            shown = ", ".join(skill.shown_as for skill in arrangement.skills)
            lines.append(Line(LineKind.TEXT, shown))
        else:
            lines.extend(
                Line(LineKind.GROUP, f"{label}: {', '.join(names)}") for label, names in groups
            )

    resolved = _resolved(arrangement, context)
    if resolved:
        lines.append(Line(LineKind.HEADING, headings.experience))
        for entry, job in resolved:
            period = render_period(entry, language)
            lines.append(Line(LineKind.ENTRY, _job_title_line(entry), period))
            lines.extend(Line(LineKind.BULLET, f"{BULLET}{text}") for text in _bullets(entry, job))
            if job.stack:
                label = STACK_LABEL.get(language, STACK_LABEL["en"])
                lines.append(Line(LineKind.NOTE, f"{label}: {', '.join(job.stack)}"))

    shown_projects = [
        (project, choice)
        for choice in arrangement.projects
        if (project := context.projects_by_ref.get(choice.ref)) is not None
    ]
    if shown_projects:
        lines.append(Line(LineKind.HEADING, headings.projects))
        for project, choice in shown_projects:
            title = f"{project.name} — {project.link}" if project.link else project.name
            year = str(project.year) if project.year else ""
            lines.append(Line(LineKind.ENTRY, title, year))
            lines.extend(
                Line(LineKind.BULLET, f"{BULLET}{text}")
                for text in _chosen(project.achievements, choice.bullets)
            )
            if project.stack:
                label = STACK_LABEL.get(language, STACK_LABEL["en"])
                lines.append(Line(LineKind.NOTE, f"{label}: {', '.join(project.stack)}"))

    if context.education:
        lines.append(Line(LineKind.HEADING, headings.education))
        lines.extend(_education_line(item) for item in context.education)

    spoken = _languages_line(context.profile.languages, language)
    if spoken:
        lines.extend((Line(LineKind.HEADING, headings.languages), Line(LineKind.TEXT, spoken)))

    return lines


#: Characters of body text one line of the A4 text block holds at the body
#: size, measured on the samples LibreOffice lays out (Carlito, metrically
#: Calibri). A bullet loses its hanging indent; the header and the notes are
#: set smaller and hold more.
CHARS_PER_LINE = 95
CHARS_PER_BULLET_LINE = 92
CHARS_PER_SMALL_LINE = 108

#: Cyrillic letters are wider than Latin ones in the same face: a line of
#: Russian holds about this share of the characters a line of English does.
CYRILLIC_WIDTH_SHARE = 0.82

#: Body lines one page holds, in the units of :func:`estimated_lines`.
#: Measured, not derived, by rendering untrimmed CVs through LibreOffice: a
#: full A4 text block is about 62 units in either language (a sample estimated
#: at 60.8 filled 745 of 757 points; a Russian one at 64.8 spilled onto a
#: second page). A heading kept with its next line can push the last section
#: over with a line or two still free, so the budget keeps that in reserve.
PAGE_LINES = 59.5

#: What each kind of line costs beyond its wrapped text: a larger size, or the
#: space set before and after it.
_LINE_COST: dict[LineKind, float] = {
    LineKind.NAME: 1.9,
    LineKind.HEADLINE: 1.15,
    LineKind.CONTACT: 0.0,
    LineKind.HEADING: 1.6,
    LineKind.ENTRY: 0.6,
    LineKind.BULLET: 0.15,
    LineKind.NOTE: 0.0,
    LineKind.GROUP: 0.08,
    LineKind.TEXT: 0.08,
}


def estimated_lines(arrangement: CVArrangement, context: CVContext) -> float:
    """How much of a page this arrangement fills, in body lines.

    An estimate, and deliberately a slightly pessimistic one: rendering is
    Word's or LibreOffice's, not this module's, and the only question asked of
    the number is "does it fit on one page" (:data:`PAGE_LINES`). It wraps each
    line at a measured number of characters and adds what the line's size and
    spacing cost. A one-page CV is the research's rule for under three years of
    experience (docs/WORKSHOP-RESEARCH.md, L1/L2), and a CV whose last section
    spills onto page two looks unfinished.
    """
    total = 0.0
    for line in layout(arrangement, context):
        text = line.rendered
        if line.kind is LineKind.BULLET:
            width = CHARS_PER_BULLET_LINE
        elif line.kind in (LineKind.CONTACT, LineKind.NOTE):
            width = CHARS_PER_SMALL_LINE
        else:
            width = CHARS_PER_LINE
        if text_language(text, default="en") == "ru":
            width = int(width * CYRILLIC_WIDTH_SHARE)
        wrapped = max(1, -(-len(text) // width))
        total += wrapped + _LINE_COST.get(line.kind, 0.0)
    return total


def fits_one_page(arrangement: CVArrangement, context: CVContext) -> bool:
    """Whether :func:`estimated_lines` stays within :data:`PAGE_LINES`."""
    return estimated_lines(arrangement, context) <= PAGE_LINES


def to_lines(arrangement: CVArrangement, context: CVContext) -> list[str]:
    """The document as plain lines, a blank one before every heading."""
    out: list[str] = []
    for line in layout(arrangement, context):
        if line.kind is LineKind.HEADING and out:
            out.append("")
        out.append(line.rendered)
    return out


def _job_title_line(entry: ExperienceEntry) -> str:
    """ "Backend Developer — Kaspi" from the columns, never from an answer."""
    parts = [part for part in (entry.title, entry.company) if part]
    return " — ".join(parts)


def _education_line(item: EducationEntry) -> Line:
    """One degree on one line, its year at the right, omitting what was not stated.

    Degree and field first, then the institution — "BSc, Computer Science —
    KazNU" — the way the reference CVs write it: the degree is what a filter
    asks about, the institution what a reader checks second.
    """
    head = ", ".join(part for part in (item.degree, item.field) if part)
    text = f"{head} — {item.institution}" if head else item.institution
    return Line(LineKind.ENTRY, text, str(item.end_year) if item.end_year else "")


def to_text(arrangement: CVArrangement, context: CVContext) -> str:
    """The document as plain text — what the audit reads and what is stored."""
    return "\n".join(to_lines(arrangement, context)).strip() + "\n"


def _page_setup(document: Any) -> None:
    """A4, the margins in the module docstring, and the body font as the default.

    ``Any`` because python-docx's ``Document`` is a factory function whose
    return type is not exported under a name mypy can see.
    """
    for section in document.sections:
        section.page_width = Cm(PAGE_WIDTH_CM)
        section.page_height = Cm(PAGE_HEIGHT_CM)
        section.left_margin = section.right_margin = Cm(MARGIN_SIDE_CM)
        section.top_margin = section.bottom_margin = Cm(MARGIN_TOP_CM)
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(SIZE_BODY)
    # The east-Asian and complex-script slots are what Word falls back to for
    # characters the Latin slot does not cover; naming the font in all of them
    # keeps a Cyrillic word from being drawn in a different face.
    fonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(slot), FONT)
    spacing = normal.paragraph_format
    spacing.space_before = Pt(0)
    spacing.space_after = Pt(0)
    spacing.line_spacing = 1.08


def _rule_under(paragraph: Paragraph) -> None:
    """A thin line under a heading: a paragraph border, not a drawn shape.

    A border is a property of the paragraph and carries no text, so it changes
    nothing a parser reads; a drawn line would be a shape, which is the kind of
    object the auditor has to reason about.
    """
    properties = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "808080")
    borders.append(bottom)
    properties.append(borders)


def _draw(document: Any, line: Line) -> None:
    """One layout line as one paragraph, styled by its kind."""
    paragraph = document.add_paragraph()
    spacing = paragraph.paragraph_format
    if line.kind is LineKind.NAME:
        run = paragraph.add_run(line.text)
        run.bold = True
        run.font.size = Pt(SIZE_NAME)
        spacing.space_after = Pt(2)
    elif line.kind is LineKind.HEADLINE:
        run = paragraph.add_run(line.text)
        run.font.size = Pt(SIZE_HEADLINE)
        run.font.color.rgb = GREY
        spacing.space_after = Pt(1)
    elif line.kind is LineKind.CONTACT:
        run = paragraph.add_run(line.text)
        run.font.size = Pt(SIZE_NOTE)
        run.font.color.rgb = GREY
    elif line.kind is LineKind.HEADING:
        run = paragraph.add_run(line.text)
        run.bold = True
        run.font.size = Pt(SIZE_HEADING)
        spacing.space_before = Pt(12)
        spacing.space_after = Pt(4)
        spacing.keep_with_next = True
        _rule_under(paragraph)
    elif line.kind is LineKind.ENTRY:
        spacing.space_before = Pt(7)
        spacing.space_after = Pt(2)
        spacing.keep_with_next = True
        spacing.tab_stops.add_tab_stop(Cm(RIGHT_EDGE_CM), WD_TAB_ALIGNMENT.RIGHT)
        paragraph.add_run(line.text).bold = True
    elif line.kind is LineKind.BULLET:
        spacing.left_indent = Cm(BULLET_INDENT_CM)
        spacing.first_line_indent = Cm(-BULLET_INDENT_CM)
        spacing.space_after = Pt(2)
        paragraph.add_run(line.text)
    elif line.kind is LineKind.GROUP:
        label, _, values = line.text.partition(": ")
        paragraph.add_run(f"{label}: ").bold = True
        paragraph.add_run(values)
        spacing.space_after = Pt(1)
    elif line.kind is LineKind.NOTE:
        run = paragraph.add_run(line.text)
        run.italic = True
        run.font.size = Pt(SIZE_NOTE)
        run.font.color.rgb = GREY
        spacing.space_before = Pt(1)
    else:
        spacing.tab_stops.add_tab_stop(Cm(RIGHT_EDGE_CM), WD_TAB_ALIGNMENT.RIGHT)
        paragraph.add_run(line.text)
        spacing.space_after = Pt(1)
    if line.aside:
        paragraph.add_run("\t")
        paragraph.add_run(line.aside)


def to_docx(arrangement: CVArrangement, context: CVContext) -> bytes:
    """The document as a .docx file: one column, paragraphs only, no tables.

    The heading paragraphs are styled by size and weight rather than by Word's
    built-in Heading styles. Both survive extraction identically — the text is
    the text — and a plain paragraph cannot bring an outline level or a
    numbering definition with it, which is one fewer thing for a parser to
    interpret. Spacing between sections is paragraph spacing rather than empty
    paragraphs, so the file's text is exactly :func:`to_text` without its blank
    lines.
    """
    document = DocxDocument()
    _page_setup(document)
    for line in layout(arrangement, context):
        _draw(document, line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def filename_for(context: CVContext) -> str:
    """What the downloaded file is called.

    The candidate's name and the company, because the person downloading this
    will have twenty of them in one folder and "cv.docx" twenty times is not a
    filename. Reduced to characters that survive every filesystem: a job title
    with a slash in it is a real posting and an unopenable file.
    """
    parts = [context.contacts.name or context.profile.name or "cv", context.vacancy.company or ""]
    stem = "-".join(_slug(part) for part in parts if _slug(part)) or "cv"
    return f"{stem}.{FILE_FORMAT}"


def _slug(value: str) -> str:
    """A filename-safe fragment, keeping letters of either alphabet."""
    kept = [char if char.isalnum() else "-" for char in value.strip()]
    return "-".join(part for part in "".join(kept).split("-") if part)[:60]


def letter_to_docx(text: str, context: CVContext) -> bytes:
    """A cover letter as a .docx, so it can be attached rather than only pasted.

    The letter's text is not touched. It was generated, checked and stored by
    :mod:`app.letters`, a person may have edited it since, and this function's
    only job is to put it in a file — so it splits on blank lines into
    paragraphs and writes nothing of its own. In particular it adds no contact
    block: hh filters a letter carrying an address as spam, ``app.letters.guard``
    enforces that by reading the text, and a header helpfully added here would
    break the rule from outside the module that guards it. The page is the CV's
    — A4, the same margins and font — so the two files read as one application.
    """
    document = DocxDocument()
    _page_setup(document)
    for block in [part.strip() for part in text.split("\n\n")]:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(8)
        run = paragraph.add_run(block)
        run.font.size = Pt(SIZE_BODY + 0.5)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def letter_filename_for(context: CVContext) -> str:
    """What the downloaded letter is called: the CV's name with a suffix."""
    stem = filename_for(context).rsplit(".", 1)[0]
    return f"{stem}-letter.{FILE_FORMAT}"

"""Turning an arrangement plus the profile's rows into a file and its text.

**Two files, one layout.** A .docx, because it is what hh and an applicant
tracking system parse best — ``app/resume/ats_audit.py`` exists because phase 2
measured what an employer's parser gets from a two-column PDF resume: a sidebar
heading welded onto a sentence from the other column. A DOCX has no step that
turns characters into glyphs at coordinates, so the audit's costliest findings
(``NO_TEXT_LAYER``, ``BROKEN_GLYPHS``, ``COLUMN_INTERLEAVING``) are unreachable
by construction. And a PDF (:mod:`app.documents.pdf`), because that is what a
recruiter opens and what Telegram previews: drawn from the same typed lines,
one column, its text layer in reading order, and audited by the same PDF checks
before it is offered. Neither file is drawn from anything but :func:`layout`.

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

**Layout, and where it comes from.** The owner's own one-page resume is the
quality bar: a serif face, the name and title centred over the contact lines,
labelled skill lines, each role as a bold title with its period at the right
margin over the employer and the resume's italic line about it, and bullets
that open with a short **bold label** ("**Evaluation loop.** Built…") when the
resume writes them that way. The label is found in the stored bullet and only
drawn in bold; the text is the stored text. A period is pushed right with a
right-aligned tab stop — a tab, not a table, so the text layer reads "title,
dates" in that order on one line, which is how an applicant tracking system
attributes dates to a job. Bullets are paragraphs that begin with "• " under a
hanging indent rather than Word's numbering: a numbering definition is one more
structure for a parser to interpret, and the glyph being real text is what
keeps :func:`to_text` and the files saying the same characters. Every size and
space is in :data:`STYLES`, read by both renderers.
"""

import io
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, ConfigDict, field_validator

from app.core.places import place_of
from app.documents import skill_groups
from app.documents.context import (
    PERIOD_DASH,
    CVContext,
    EducationEntry,
    ExperienceEntry,
    ProjectEntry,
    SkillChoice,
    render_period,
)

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


#: How the profile's work-format preference is written in the header. Only the
#: two that say something an employer filters on; "no" (on-site only) is the
#: default reading of a CV with a city on it and is not printed.
WORK_FORMAT: dict[str, dict[str, str]] = {
    "en": {"full": "Open to remote", "hybrid": "Hybrid"},
    "ru": {"full": "удалённо", "hybrid": "гибрид"},
}


#: What a bullet line starts with, in the file and in the text alike.
BULLET = "• "

#: Between a role's title and the line's other half, a company and its
#: descriptor, a project and its stack, a degree and its institution.
SEPARATOR = " — "

#: A4, the paper every employer on this market and in Europe prints on. The
#: python-docx template is US Letter, which turns a one-page CV into a page and
#: two lines when an A4 reader opens it.
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
MARGIN_SIDE_CM = 1.7
MARGIN_TOP_CM = 1.3

#: Where a job's period is pushed to: the right edge of the text block.
RIGHT_EDGE_CM = PAGE_WIDTH_CM - 2 * MARGIN_SIDE_CM

#: The typeface, by the name Word knows it and by the files the PDF embeds.
#:
#: A serif, because the quality bar is the owner's own LaTeX one-pager and a
#: serif is most of what makes it read as one. Times New Roman specifically,
#: and the reason is measurement rather than taste: Liberation Serif, which is
#: vendored in ``fonts/`` under the SIL Open Font License and embedded in the
#: PDF, is *metrically identical* to it — every glyph has the same advance
#: width. So the .docx opened in Word, the .docx opened in LibreOffice (which
#: maps Times New Roman onto Liberation Serif) and the PDF break every line at
#: the same word, and "fits one page" measured once (:mod:`app.documents.pdf`)
#: is true of all three. Cambria and Georgia look a little more modern, and
#: neither has a free metric twin with Cyrillic, so with either of them the
#: page measured would not be the page a recruiter opens. Both faces carry
#: Cyrillic, which a Russian CV needs.
FONT = "Times New Roman"

#: Point sizes. A name, a headline, a heading, a body and a note; the research's
#: references use the same handful, and more than that reads as a template.
SIZE_NAME = 21
SIZE_HEADLINE = 12.5
SIZE_HEADING = 11.5
SIZE_BODY = 11
SIZE_NOTE = 10

#: Line height as a multiple of the size, set exactly in both files so a line
#: is the same height in Word as in the PDF: Word's "single" spacing is a
#: property of the font's own metrics and differs between applications.
LEADING = 1.2

#: Text colours, as hex. Near-black for the body; a quieter grey for the
#: contact line, the stack line and the rules under headings.
INK = "111111"
GREY = "4A4A4A"
RULE = "8C8C8C"

#: The hanging indent a bullet's continuation lines align under: the width of
#: "• " at the body size in this face (0.35 em for the bullet, 0.25 for the
#: space), so the second line of a bullet starts exactly under its first word.
BULLET_INDENT_PT = round(SIZE_BODY * 0.6, 2)


class Align(StrEnum):
    """How a line sits between the margins."""

    LEFT = "left"
    CENTER = "center"


@dataclass(frozen=True, slots=True)
class LineStyle:
    """How one kind of line is drawn, by both renderers.

    One table for the .docx and the PDF, so the two files cannot drift apart
    in size or spacing; every number is in points.
    """

    size: float = SIZE_BODY
    bold: bool = False
    italic: bool = False
    color: str = INK
    align: Align = Align.LEFT
    space_before: float = 0.0
    space_after: float = 0.0
    #: A bullet: continuation lines start under the first word, not the glyph.
    hanging: bool = False
    #: Never the last line on a page: a heading or a job's title line.
    keep_with_next: bool = False
    #: A thin rule under the line: the section headings.
    rule_below: bool = False

    @property
    def leading(self) -> float:
        """The height of one line of this kind, in points."""
        return round(self.size * LEADING, 2)


class LineKind(StrEnum):
    """What a line of the layout is, which is what decides how it is drawn."""

    NAME = "name"
    HEADLINE = "headline"
    CONTACT = "contact"
    HEADING = "heading"
    TEXT = "text"
    #: A title with something pushed to the right margin: a role and its
    #: period, a project and its link, a degree and its years.
    ENTRY = "entry"
    #: The line under a role: the employer, and the resume's own italic line
    #: about what the employer is.
    ORG = "org"
    BULLET = "bullet"
    #: The quiet line under a job's bullets: its technologies.
    NOTE = "note"
    #: "Backend: FastAPI, Django" — a labelled line of the skills section.
    GROUP = "group"


#: Every kind's style. Tight but readable: the space a section heading gets
#: above it is what separates the sections, and everything inside a section is
#: set close, the way the owner's LaTeX original is.
STYLES: dict[LineKind, LineStyle] = {
    LineKind.NAME: LineStyle(size=SIZE_NAME, bold=True, align=Align.CENTER, space_after=1),
    LineKind.HEADLINE: LineStyle(size=SIZE_HEADLINE, align=Align.CENTER, space_after=2),
    LineKind.CONTACT: LineStyle(size=SIZE_NOTE, color=GREY, align=Align.CENTER),
    LineKind.HEADING: LineStyle(
        size=SIZE_HEADING,
        bold=True,
        space_before=8,
        space_after=3,
        keep_with_next=True,
        rule_below=True,
    ),
    LineKind.TEXT: LineStyle(space_after=1),
    LineKind.GROUP: LineStyle(space_after=0.5),
    LineKind.ENTRY: LineStyle(space_before=4, keep_with_next=True),
    LineKind.ORG: LineStyle(space_after=1.5, keep_with_next=True),
    LineKind.BULLET: LineStyle(hanging=True, space_after=1.5),
    LineKind.NOTE: LineStyle(size=SIZE_NOTE, italic=True, color=GREY, space_after=0.5),
}


@dataclass(frozen=True, slots=True)
class Span:
    """A run of one line's text with its own weight or slant."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the layout: its kind, its text, and what sits at its right.

    ``spans`` say which parts of ``text`` are bold or italic — a bullet's
    lead-in label, a project's stack. They are presentation only and they are
    checked to *be* the text: a line whose spans spell anything else is a
    programming error, raised at once, because the file would then say
    something the plain text (and the audit) did not.
    """

    kind: LineKind
    text: str
    #: Right-aligned on the same line, after a tab: a job's period, a degree's
    #: year. Part of the line's text in both renderings.
    aside: str = ""
    spans: tuple[Span, ...] = ()

    def __post_init__(self) -> None:
        if self.spans and "".join(span.text for span in self.spans) != self.text:
            raise ValueError(f"spans do not spell the line: {self.text!r}")

    @property
    def runs(self) -> tuple[Span, ...]:
        """The spans, or the whole text as one plain span."""
        return self.spans or (Span(self.text),)

    @property
    def style(self) -> LineStyle:
        """How this line is drawn."""
        return STYLES[self.kind]

    @property
    def rendered(self) -> str:
        """The line as plain text — and, character for character, as the file."""
        return f"{self.text}\t{self.aside}" if self.aside else self.text


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
    """The stored bullets at these indices, in this order, unknown ones skipped.

    An index the row does not have is skipped for the same reason an unknown
    ref is: a stored arrangement can outlive a re-parse that shortened the
    list, and an old version must render with less rather than with a wrong
    bullet. The guard rejects such an index on a fresh arrangement, so this
    only ever runs on old ones.
    """
    shown: list[str] = []
    for index in indices:
        if 0 <= index < len(achievements) and achievements[index] not in shown:
            shown.append(achievements[index])
    return shown


#: A bullet that opens with a short label: "LLM sales agent. Designed and
#: built…", «Оценка качества: собрал…». The label is the resume's own words and
#: stays exactly where it is; only its weight changes.
_LEAD_IN = re.compile(r"^(?P<label>[^\W\d_][^.:;!?\d]{1,46}?[.:])\s+(?=\S)")

#: The longest label, in words. Past this a "label" is a first sentence.
MAX_LEAD_IN_WORDS = 5

#: What has to follow a label for it to be one: a sentence, not a fragment.
MIN_TEXT_AFTER_LEAD_IN = 20

#: The fewest labelled bullets that make a list label-first.
MIN_LABELLED_BULLETS = 2


def lead_in(text: str) -> str:
    """The short label a bullet opens with ("Evaluation loop."), or "".

    A label is a few words without a figure, ending in a full stop or a colon,
    with a real sentence after it. "Cut p95 latency by 40%. Then…" is not one
    (it carries a number), nor is "Built the API." when the bullet ends there.
    """
    found = _LEAD_IN.match(text)
    if found is None:
        return ""
    label = found.group("label")
    if len(label.split()) > MAX_LEAD_IN_WORDS:
        return ""
    if len(text) - found.end() < MIN_TEXT_AFTER_LEAD_IN:
        return ""
    return label


def labels_bullets(achievements: tuple[str, ...]) -> bool:
    """Whether a list of bullets is written in the label-first style.

    Decided over the whole stored list rather than bullet by bullet: a resume
    that leads every bullet with a bold label does it on purpose, and a single
    bullet that happens to open with a short sentence is not that style. Half
    or more is the line, and never fewer than two: a CV that bolds one bullet
    in five, or the one bullet of a project, looks like a typo.
    """
    labelled = sum(1 for text in achievements if lead_in(text))
    return labelled >= max(MIN_LABELLED_BULLETS, -(-len(achievements) // 2))


def _bullet(text: str, *, labelled: bool) -> Line:
    """One bullet line, its lead-in label in bold when the list uses them."""
    label = lead_in(text) if labelled else ""
    if not label:
        return Line(LineKind.BULLET, f"{BULLET}{text}")
    return Line(
        LineKind.BULLET,
        f"{BULLET}{text}",
        spans=(Span(BULLET), Span(label, bold=True), Span(text[len(label) :])),
    )


#: Characters one centred contact line holds at the note size before it wraps.
CONTACT_LINE_CHARS = 104


def _contact_lines(context: CVContext) -> list[str]:
    """Where and which format on one line; how to reply on the next.

    The order is the reference CVs': location (with country and UTC offset for a
    known city) and the work format the profile records, then email, phone,
    Telegram — where recruiters on this market start — and the code links. Two
    lines because one carrying all of it wraps at an arbitrary middle dot on A4;
    split here, each half reads as a unit — three when the second would itself
    wrap, the links then on a line of their own. Lines and not a table: see the module
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
    reach.extend(
        _link_text(label, address) for label, address in contacts.links if label == "Telegram"
    )
    links = [_link_text(label, address) for label, address in contacts.links if label != "Telegram"]
    reach_line = " · ".join(part for part in reach if part)
    links_line = " · ".join(part for part in links if part)
    together = " · ".join(part for part in (reach_line, links_line) if part)
    if len(together) <= CONTACT_LINE_CHARS:
        return [line for line in (" · ".join(where), together) if line]
    # Too long for one centred line: the code links get a line of their own
    # rather than one of them wrapping alone onto a third.
    return [line for line in (" · ".join(where), reach_line, links_line) if line]


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


def _labelled(label: str, values: str) -> Line:
    """ "Backend: FastAPI, Django" with the label in bold."""
    head = f"{label}:"
    return Line(
        LineKind.GROUP, f"{head} {values}", spans=(Span(head, bold=True), Span(f" {values}"))
    )


def _job_lines(entry: ExperienceEntry, job: JobChoice, language: str) -> list[Line]:
    """A role, its employer, its bullets and its stack.

    The owner's layout: the title in bold with the period at the right margin,
    then the employer with the resume's own italic line about it, then the
    bullets. A job stored with no title prints the employer on the first line
    and has no second one.
    """
    period = render_period(entry, language)
    lines: list[Line] = []
    if entry.title:
        lines.append(
            Line(LineKind.ENTRY, entry.title, period, spans=(Span(entry.title, bold=True),))
        )
        if entry.company:
            lines.append(_org_line(entry.company, entry.company_note))
    else:
        lines.append(
            Line(LineKind.ENTRY, entry.company, period, spans=(Span(entry.company, bold=True),))
        )
    labelled = labels_bullets(entry.achievements)
    lines.extend(
        _bullet(text, labelled=labelled) for text in _chosen(entry.achievements, job.bullets)
    )
    if job.stack:
        label = STACK_LABEL.get(language, STACK_LABEL["en"])
        lines.append(Line(LineKind.NOTE, f"{label}: {', '.join(job.stack)}"))
    return lines


def _org_line(company: str, note: str | None) -> Line:
    """ "Acme — B2B marketplace for industrial parts", the note in italics."""
    if not note:
        return Line(LineKind.ORG, company)
    return Line(
        LineKind.ORG,
        f"{company}{SEPARATOR}{note}",
        spans=(Span(company), Span(SEPARATOR), Span(note, italic=True)),
    )


def _project_lines(project: ProjectEntry, choice: ProjectChoice) -> list[Line]:
    """ "Name — Python, pgvector" with its link at the right, then its bullets.

    The owner's form: the name in bold and the stack in italics on the same
    line, which says what it was built with without a "Stack:" line of its own.
    The link goes to the right margin where a job's dates go; a project with no
    link shows its year there instead.
    """
    spans = [Span(project.name, bold=True)]
    if project.stack:
        stack = ", ".join(project.stack)
        spans.extend((Span(SEPARATOR), Span(stack, italic=True)))
    text = "".join(span.text for span in spans)
    aside = project.link or (str(project.year) if project.year else "")
    lines = [Line(LineKind.ENTRY, text, aside, spans=tuple(spans))]
    labelled = labels_bullets(project.achievements)
    lines.extend(
        _bullet(text, labelled=labelled) for text in _chosen(project.achievements, choice.bullets)
    )
    return lines


def _education_lines(item: EducationEntry) -> list[Line]:
    """One degree: degree and field in bold, the institution, the years; then its lines.

    Degree and field first, then the institution — "BSc, Computer Science —
    KazNU" — the way the reference CVs write it: the degree is what a filter
    asks about, the institution what a reader checks second. The detail lines
    (honours, courses, a contest result) are the resume's own and print as
    bullets under it, exactly as stored.
    """
    head = ", ".join(part for part in (item.degree, item.field) if part)
    if head:
        text = f"{head}{SEPARATOR}{item.institution}"
        spans: tuple[Span, ...] = (Span(head, bold=True), Span(f"{SEPARATOR}{item.institution}"))
    else:
        text, spans = item.institution, (Span(item.institution, bold=True),)
    if item.start_year and item.end_year and item.start_year != item.end_year:
        years = f"{item.start_year}{PERIOD_DASH}{item.end_year}"
    else:
        years = str(item.end_year or item.start_year or "")
    lines = [Line(LineKind.ENTRY, text, years, spans=spans)]
    labelled = labels_bullets(item.details)
    lines.extend(_bullet(detail, labelled=labelled) for detail in item.details)
    return lines


def layout(arrangement: CVArrangement, context: CVContext) -> list[Line]:
    """The document as typed lines, in the order it is laid out.

    The single source of every rendering. The .docx and the PDF draw these
    lines and the plain text joins them, so the files and the text the audit
    reads cannot describe different documents — which they could if each were
    assembled separately, and nothing would notice until an audit explained a
    finding that was not in the file.
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
            lines.extend(_labelled(label, ", ".join(names)) for label, names in groups)

    resolved = _resolved(arrangement, context)
    if resolved:
        lines.append(Line(LineKind.HEADING, headings.experience))
        for entry, job in resolved:
            lines.extend(_job_lines(entry, job, language))

    shown_projects = [
        (project, choice)
        for choice in arrangement.projects
        if (project := context.projects_by_ref.get(choice.ref)) is not None
    ]
    if shown_projects:
        lines.append(Line(LineKind.HEADING, headings.projects))
        for project, choice in shown_projects:
            lines.extend(_project_lines(project, choice))

    # Spoken languages close the education section on a labelled line, as the
    # owner's original does; a CV with no degree to list gets them under a
    # heading of their own.
    spoken = _languages_line(context.profile.languages, language)
    if context.education:
        lines.append(Line(LineKind.HEADING, headings.education))
        for item in context.education:
            lines.extend(_education_lines(item))
        if spoken:
            lines.append(_labelled(headings.languages.capitalize(), spoken))
    elif spoken:
        lines.extend((Line(LineKind.HEADING, headings.languages), Line(LineKind.TEXT, spoken)))

    return lines


def to_lines(arrangement: CVArrangement, context: CVContext) -> list[str]:
    """The document as plain lines, a blank one before every heading."""
    out: list[str] = []
    for line in layout(arrangement, context):
        if line.kind is LineKind.HEADING and out:
            out.append("")
        out.append(line.rendered)
    return out


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
    normal.font.color.rgb = RGBColor.from_string(INK)
    # The east-Asian and complex-script slots are what Word falls back to for
    # characters the Latin slot does not cover; naming the font in all of them
    # keeps a Cyrillic word from being drawn in a different face.
    fonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(slot), FONT)
    spacing = normal.paragraph_format
    spacing.space_before = Pt(0)
    spacing.space_after = Pt(0)
    spacing.line_spacing = Pt(STYLES[LineKind.TEXT].leading)


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
    bottom.set(qn("w:color"), RULE)
    borders.append(bottom)
    properties.append(borders)


def _draw(document: Any, line: Line) -> None:
    """One layout line as one paragraph, styled from :data:`STYLES`."""
    style = line.style
    paragraph = document.add_paragraph()
    spacing = paragraph.paragraph_format
    spacing.space_before = Pt(style.space_before)
    spacing.space_after = Pt(style.space_after)
    # Exact, not "single": the PDF sets the same leading, so a line is the
    # same height in both files (see :data:`LEADING`).
    spacing.line_spacing = Pt(style.leading)
    spacing.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    if style.align is Align.CENTER:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if style.keep_with_next:
        spacing.keep_with_next = True
    if style.hanging:
        spacing.left_indent = Pt(BULLET_INDENT_PT)
        spacing.first_line_indent = Pt(-BULLET_INDENT_PT)
    if style.rule_below:
        _rule_under(paragraph)
    for span in line.runs:
        _run(paragraph, span.text, style, bold=span.bold, italic=span.italic)
    if line.aside:
        spacing.tab_stops.add_tab_stop(Cm(RIGHT_EDGE_CM), WD_TAB_ALIGNMENT.RIGHT)
        _run(paragraph, "\t", style)
        _run(paragraph, line.aside, style)


def _run(
    paragraph: Paragraph, text: str, style: LineStyle, *, bold: bool = False, italic: bool = False
) -> None:
    """One run, carrying the line's size and colour and the span's emphasis."""
    run = paragraph.add_run(text)
    run.font.size = Pt(style.size)
    if style.bold or bold:
        run.bold = True
    if style.italic or italic:
        run.italic = True
    if style.color != INK:
        run.font.color.rgb = RGBColor.from_string(style.color)


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


def filename_for(context: CVContext, file_format: str = FILE_FORMAT) -> str:
    """What the downloaded file is called.

    The candidate's name and the company, because the person downloading this
    will have twenty of them in one folder and "cv.docx" twenty times is not a
    filename. Reduced to characters that survive every filesystem: a job title
    with a slash in it is a real posting and an unopenable file.
    """
    parts = [context.contacts.name or context.profile.name or "cv", context.vacancy.company or ""]
    stem = "-".join(_slug(part) for part in parts if _slug(part)) or "cv"
    return f"{stem}.{file_format}"


def _slug(value: str) -> str:
    """A filename-safe fragment, keeping letters of either alphabet."""
    kept = [char if char.isalnum() else "-" for char in value.strip()]
    return "-".join(part for part in "".join(kept).split("-") if part)[:60]


#: Between the paragraphs of a letter, in points.
LETTER_PARAGRAPH_SPACE = 8


def letter_paragraphs(text: str) -> list[str]:
    """A letter's paragraphs as stored: split on blank lines, nothing added."""
    return [part.strip() for part in text.split("\n\n") if part.strip()]


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
    style = LineStyle(size=SIZE_BODY + 0.5, space_after=LETTER_PARAGRAPH_SPACE)
    for block in letter_paragraphs(text):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(style.space_after)
        paragraph.paragraph_format.line_spacing = Pt(style.leading)
        # A single newline inside a paragraph (a proof's bullets) becomes a
        # line break: python-docx writes "\n" in a run as <w:br/>.
        _run(paragraph, block, style)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def letter_filename_for(context: CVContext, file_format: str = FILE_FORMAT) -> str:
    """What the downloaded letter is called: the CV's name with a suffix."""
    stem = filename_for(context).rsplit(".", 1)[0]
    return f"{stem}-letter.{file_format}"

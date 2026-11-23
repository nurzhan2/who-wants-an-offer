"""The same CV, and the same letter, as a PDF: what a recruiter actually opens.

A .docx is what a parser reads best and what hh accepts; a PDF is what a person
on the other side of a Telegram chat taps on, what a phone previews without an
office suite, and what most of the research's employers ask for. So both are
made, from the one typed layout in :mod:`app.documents.render` — never from a
second assembly of the same facts — and the PDF is the one sent.

**ATS-safe by the same rules as the .docx.** One column; every line a single
paragraph drawn in reading order; a period pushed to the right margin on its
own line rather than set in a table cell; real text in an embedded TrueType
face with a Unicode map, so the audit's ``UNMAPPED_FONT`` and ``BROKEN_GLYPHS``
have nothing to find. A test runs the PDF through ``app.resume.ats_audit`` —
the checks written for other people's PDFs — and through ``pdfplumber``,
asserting the extracted words are the plain text's words in the same order.

**The face is vendored.** ``fonts/`` holds Liberation Serif (SIL Open Font
License, ``fonts/OFL.txt``), which has Cyrillic and is metrically identical to
the Times New Roman the .docx names: the two files break every line at the same
word. The slim server image has no fonts at all, and a PDF that silently fell
back to a face without Cyrillic would print a Russian CV as boxes.

**Rendering is a pure function here too.** ``invariant`` turns off the
timestamp and random document id reportlab would otherwise write, so the same
arrangement and the same profile make the same bytes.

**This module also answers "does it fit on one page".** It is the engine that
draws the page, so it measures it: :func:`fits_one_page` lays every line out at
the real text width with the real glyph widths and adds the spacing
:data:`app.documents.render.STYLES` gives it — the same numbers the .docx is
written with, at exact line heights, so the measure holds for both files.
"""

import io
from functools import cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, PageTemplate, Paragraph

from app.documents.context import CVContext
from app.documents.render import (
    BULLET_INDENT_PT,
    LETTER_PARAGRAPH_SPACE,
    MARGIN_SIDE_CM,
    MARGIN_TOP_CM,
    PAGE_HEIGHT_CM,
    PAGE_WIDTH_CM,
    RULE,
    SIZE_BODY,
    Align,
    CVArrangement,
    Line,
    LineStyle,
    layout,
    letter_paragraphs,
    unbreakable,
)

#: What the file is, and the media type it is served under.
FILE_FORMAT = "pdf"
MEDIA_TYPE = "application/pdf"

#: Where the vendored face lives, and what each of its four files is.
FONT_DIR = Path(__file__).parent / "fonts"
FAMILY = "CVSerif"
FACES: dict[tuple[bool, bool], tuple[str, str]] = {
    (False, False): (FAMILY, "LiberationSerif-Regular.ttf"),
    (True, False): (f"{FAMILY}-Bold", "LiberationSerif-Bold.ttf"),
    (False, True): (f"{FAMILY}-Italic", "LiberationSerif-Italic.ttf"),
    (True, True): (f"{FAMILY}-BoldItalic", "LiberationSerif-BoldItalic.ttf"),
}

#: Points in a centimetre, and the text block in points.
CM = 72 / 2.54
PAGE_WIDTH = PAGE_WIDTH_CM * CM
PAGE_HEIGHT = PAGE_HEIGHT_CM * CM
TEXT_WIDTH = (PAGE_WIDTH_CM - 2 * MARGIN_SIDE_CM) * CM
TEXT_HEIGHT = (PAGE_HEIGHT_CM - 2 * MARGIN_TOP_CM) * CM

#: Room kept free at the foot of the page when measuring. Word places an
#: exact-height line a fraction of a point differently from this engine, and
#: a heading's border adds its own width; a CV measured to the last point
#: would spill one line in one of the two applications.
PAGE_RESERVE = 6.0

#: Between a title and what sits at the right margin, at the least.
ASIDE_GAP = 8.0

#: The least share of the line a title keeps beside its right-margin text.
MIN_TITLE_SHARE = 1 / 3

#: The rule under a heading: how far below the text, and how thick.
RULE_GAP = 1.5
RULE_WIDTH = 0.5


@cache
def register_fonts() -> str:
    """Register the vendored face with reportlab once; return the family name.

    A family, so ``<b>`` and ``<i>`` inside a paragraph pick the right file.
    """
    for (_bold, _italic), (name, filename) in FACES.items():
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
    pdfmetrics.registerFontFamily(
        FAMILY,
        normal=FACES[(False, False)][0],
        bold=FACES[(True, False)][0],
        italic=FACES[(False, True)][0],
        boldItalic=FACES[(True, True)][0],
    )
    return FAMILY


def _face(*, bold: bool, italic: bool) -> str:
    """The registered name of one of the four faces."""
    register_fonts()
    return FACES[(bold, italic)][0]


def _paragraph_style(style: LineStyle, *, plain: bool = False) -> ParagraphStyle:
    """A reportlab style from the shared table; ``plain`` drops the line's weight.

    Spacing is left to the flowable wrapping the paragraph, so a paragraph
    style here never carries space of its own.
    """
    return ParagraphStyle(
        name="cv",
        fontName=_face(bold=style.bold and not plain, italic=style.italic and not plain),
        fontSize=style.size,
        leading=style.leading,
        textColor=HexColor(f"#{style.color}"),
        alignment=TA_CENTER if style.align is Align.CENTER else TA_LEFT,
        leftIndent=BULLET_INDENT_PT if style.hanging else 0,
        firstLineIndent=-BULLET_INDENT_PT if style.hanging else 0,
        spaceBefore=0,
        spaceAfter=0,
        # Never hyphenate and never split inside a word: a bullet is the
        # owner's sentence and a parser reads a broken word as two.
        splitLongWords=0,
    )


def _markup(line: Line) -> str:
    """The line's spans as reportlab paragraph markup, every character escaped."""
    parts: list[str] = []
    for span in line.runs:
        text = escape(unbreakable(span.text))
        if span.italic:
            text = f"<i>{text}</i>"
        if span.bold:
            text = f"<b>{text}</b>"
        parts.append(text)
    return "".join(parts)


class _LineFlowable(Flowable):  # type: ignore[misc]
    """One layout line: a paragraph, an optional right-margin text, an optional rule.

    The right-margin text (a period, a year, a link) is a one-line paragraph
    of its own drawn level with the first line of the left one, so the two
    share a baseline and the text layer reads "title … dates" on one line, as
    the .docx's tab stop does. Space before and after come from the shared
    table and are read by the frame, which this module tells not to overlap
    them — Word adds both, so this does too.
    """

    def __init__(self, line: Line) -> None:
        super().__init__()
        self.line = line
        style = line.style
        self.spaceBefore = style.space_before
        self.spaceAfter = style.space_after
        self.keepWithNext = style.keep_with_next
        self._left = Paragraph(_markup(line), _paragraph_style(style))
        self._right = (
            Paragraph(escape(line.aside), _paragraph_style(style, plain=True))
            if line.aside
            else None
        )
        self._right_width = (
            pdfmetrics.stringWidth(line.aside, _face(bold=False, italic=False), style.size)
            if line.aside
            else 0.0
        )
        self._width = 0.0
        self._left_height = 0.0

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self._width = available_width
        room = available_width - (self._right_width + ASIDE_GAP if self._right else 0.0)
        # A link long enough to leave the title no room still leaves it a
        # third of the line: the two then overlap rather than the build fail.
        room = max(room, available_width * MIN_TITLE_SHARE)
        _, self._left_height = self._left.wrap(room, available_height)
        height = self._left_height
        if self._right is not None:
            self._right.wrap(self._right_width + 1, available_height)
            height = max(height, self.line.style.leading)
        if self.line.style.rule_below:
            height += RULE_GAP + RULE_WIDTH
        self.width, self.height = available_width, height
        return available_width, height

    def draw(self) -> None:
        canvas = self.canv
        top = self.height
        self._left.drawOn(canvas, 0, top - self._left_height)
        if self._right is not None:
            leading = self.line.style.leading
            self._right.drawOn(canvas, self._width - self._right_width, top - leading)
        if self.line.style.rule_below:
            canvas.setStrokeColor(HexColor(f"#{RULE}"))
            canvas.setLineWidth(RULE_WIDTH)
            canvas.line(0, RULE_WIDTH / 2, self._width, RULE_WIDTH / 2)


def _flowables(lines: list[Line]) -> list[Flowable]:
    """One flowable per layout line, in order."""
    register_fonts()
    return [_LineFlowable(line) for line in lines]


def content_height(lines: list[Line]) -> float:
    """How tall these lines are when set at the page's width, in points.

    Spacing as the frame applies it with overlap turned off: each line's space
    before (except the first's) and after (except the last's) added in full.
    """
    total = 0.0
    flowables = _flowables(lines)
    for index, flowable in enumerate(flowables):
        height = float(flowable.wrap(TEXT_WIDTH, TEXT_HEIGHT)[1])
        total += height
        if index:
            total += flowable.getSpaceBefore()
        if index < len(flowables) - 1:
            total += flowable.getSpaceAfter()
    return total


def fits_one_page(arrangement: CVArrangement, context: CVContext) -> bool:
    """Whether this CV, set as it will be drawn, stays on one A4 page.

    A one-page CV is the research's rule for under three years of experience
    (docs/WORKSHOP-RESEARCH.md, L1/L2), and a CV whose last section spills
    onto page two looks unfinished. Measured, not estimated: the lines are laid
    out at the real width in the real face, with :data:`PAGE_RESERVE` kept free.
    """
    return content_height(layout(arrangement, context)) <= TEXT_HEIGHT - PAGE_RESERVE


def _build(flowables: list[Flowable], *, title: str, author: str) -> bytes:
    """Lay flowables out on A4 pages and return the file."""
    buffer = io.BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
        leftMargin=MARGIN_SIDE_CM * CM,
        rightMargin=MARGIN_SIDE_CM * CM,
        topMargin=MARGIN_TOP_CM * CM,
        bottomMargin=MARGIN_TOP_CM * CM,
        title=title,
        author=author,
        creator="who-wants-an-offer",
        invariant=1,
    )
    frame = Frame(
        MARGIN_SIDE_CM * CM,
        MARGIN_TOP_CM * CM,
        TEXT_WIDTH,
        TEXT_HEIGHT,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        overlapAttachedSpace=0,
    )
    document.addPageTemplates([PageTemplate(id="page", frames=[frame])])
    document.build(flowables)
    return buffer.getvalue()


def _author(context: CVContext) -> str:
    return (context.contacts.name or context.profile.name or "").strip()


def to_pdf(arrangement: CVArrangement, context: CVContext) -> bytes:
    """The CV as a PDF: the lines of :func:`app.documents.render.layout`, drawn."""
    author = _author(context)
    title = f"{author} — CV" if author else "CV"
    return _build(_flowables(layout(arrangement, context)), title=title, author=author)


class _LetterParagraph(Flowable):  # type: ignore[misc]
    """One paragraph of a letter, its single newlines kept as line breaks."""

    def __init__(self, text: str, style: ParagraphStyle, space_after: float) -> None:
        super().__init__()
        markup = "<br/>".join(escape(part) for part in text.split("\n"))
        self._paragraph = Paragraph(markup, style)
        self.spaceAfter = space_after

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        width, height = self._paragraph.wrap(available_width, available_height)
        self.width, self.height = width, height
        return width, height

    def draw(self) -> None:
        self._paragraph.drawOn(self.canv, 0, 0)

    def split(self, available_width: float, available_height: float) -> list[Any]:
        result: list[Any] = self._paragraph.split(available_width, available_height)
        return result


def letter_to_pdf(text: str, context: CVContext) -> bytes:
    """A cover letter as a PDF, its text untouched — see :func:`render.letter_to_docx`.

    The same page, face and size as the .docx letter, so the CV and the letter
    sent together read as one application; nothing is added to the text.
    """
    register_fonts()
    style = _paragraph_style(LineStyle(size=SIZE_BODY + 0.5))
    flowables: list[Flowable] = [
        _LetterParagraph(block, style, LETTER_PARAGRAPH_SPACE) for block in letter_paragraphs(text)
    ]
    author = _author(context)
    return _build(flowables, title=f"{author} — letter" if author else "Letter", author=author)

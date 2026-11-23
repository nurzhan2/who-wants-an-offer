"""The recruiter-ready layer of the tailored CV: its look, its PDF, its selection.

The owner's own one-page resume is the bar these tests hold the generator to,
on a fictional candidate shaped like it: one job with seven label-led bullets
full of figures, a company descriptor, two projects, a degree with honours.
Nothing here is the owner's data.

Three groups, matching what changed:

* **typography** — bold lead-in labels drawn from the stored text without
  changing it, the italic company line, education's own lines, centred header;
* **the PDF** — the same words in the same order as the plain text and the
  .docx, one page, embedded fonts that carry Cyrillic, clean under the audit
  written for other people's PDFs, the same bytes twice;
* **selection** — four or five bullets for the main job, skills pruned to the
  vacancy and the evidence on the page, the summary's figures in the posting's
  notation and nothing else changed.
"""

import io
import re
from typing import Any

import pdfplumber
import pytest

from app.db.base import uuid7
from app.documents import contacts, generator, guard, pdf, render, skill_groups
from app.documents.context import (
    EducationEntry,
    ExperienceEntry,
    ProjectEntry,
    build_context,
    education_from_rows,
    experience_from_rows,
)
from app.documents.generator import (
    asked_for,
    compose_fallback,
    inspect,
    prune_skills,
    skills_floor,
    trimmed_summary,
)
from app.documents.render import CVArrangement, JobChoice, LineKind, Span
from app.letters.context import ProfileFacts, SkillFact, VacancyFacts, written_for
from app.resume import ats_audit
from app.workshop.rules import RuleSpec, SectionItemCountParams
from app.workshop.sections import find_section

pytestmark = pytest.mark.unit

BULLETS = (
    "LLM sales agent. Designed and built a chat agent that qualifies inbound leads and books "
    "demos; it now handles 62% of conversations end to end in 3 languages.",
    "Grounding and guards. Added retrieval over 14,000 product cards on pgvector and a "
    "citation check, cutting unsupported answers from 9,1% to 1,4%.",
    "Evaluation loop. Built an eval set of 480 real dialogues scored by rubric and an LLM "
    "judge; every prompt change runs it in CI and is blocked below a 95,3% pass rate.",
    "Latency and cost. Moved routing to a small model and cached tool results: p95 fell from "
    "7,8 s to 2,9 s and cost per dialogue by 41%.",
    "Backend. Built the FastAPI service, PostgreSQL schema and Celery workers behind the "
    "agent, serving 35,000 messages a day.",
    "Observability. Traced every model call to OpenTelemetry with per-dialogue cost, so a "
    "regression is caught within an hour.",
    "Team. Mentored two junior engineers and ran weekly reviews of failed dialogues.",
)

BULLETS_RU = (
    "LLM-агент продаж. Сделал чат-агента, который квалифицирует заявки и записывает на демо; "
    "ведёт 62% диалогов от начала до конца.",
    "Цикл оценки. Собрал набор из 480 реальных диалогов; каждое изменение промпта "
    "прогоняется в CI и блокируется ниже 95,3%.",
    "Бэкенд. Сделал сервис на FastAPI и схему PostgreSQL под агентом: 35 000 сообщений в день.",
)


def skill(canonical: str, spelling: str) -> SkillFact:
    return SkillFact(canonical_name=canonical, spelling=spelling)


SKILLS = (
    skill("python", "Python"),
    skill("typescript", "TypeScript"),
    skill("fastapi", "FastAPI"),
    skill("celery", "Celery"),
    skill("postgresql", "PostgreSQL"),
    skill("pgvector", "pgvector"),
    skill("redis", "Redis"),
    skill("docker", "Docker"),
    skill("llm", "LLM"),
    skill("rag", "RAG"),
    skill("langgraph", "LangGraph"),
    skill("openai api", "OpenAI API"),
    skill("embeddings", "Embeddings"),
    skill("spacy", "spaCy"),
    skill("pandas", "pandas"),
    skill("react", "React"),
    skill("linux", "Linux"),
    skill("git", "Git"),
)


def sample_context(*, language: str = "en", track: str = "llm", **overrides: Any) -> Any:
    """Alex Sample, a fictional candidate shaped like the owner's resume."""
    english = language == "en"
    job = ExperienceEntry(
        ref=0,
        company="Northwind Labs",
        title="LLM Engineer" if english else "LLM-инженер",
        company_note="B2B SaaS for wholesale distributors"
        if english
        else "B2B SaaS для дистрибьюторов",
        start="2024-03",
        is_current=True,
        stack=(
            "Python",
            "FastAPI",
            "PostgreSQL",
            "pgvector",
            "Celery",
            "Redis",
            "OpenAI API",
            "LangGraph",
            "Docker",
            "OpenTelemetry",
        ),
        achievements=BULLETS if english else BULLETS_RU,
    )
    profile = ProfileFacts(
        profile_id=uuid7(),
        name="Alex Sample",
        headline="LLM Engineer" if english else "LLM-инженер",
        summary=(
            "LLM engineer who builds production chat agents on Python and PostgreSQL. The "
            "sales agent handles 62% of conversations end to end. An eval loop of 480 real "
            "dialogues gates every prompt change at a 95,3% pass rate. Before that, years of "
            "backend work."
            if english
            else "LLM-инженер: делаю чат-агентов на Python и PostgreSQL. Агент продаж ведёт 62% "
            "диалогов от начала до конца."
        ),
        locations=("Almaty",),
        languages=("en C1", "ru native"),
        skills=SKILLS,
        remote="full",
    )
    if track == "llm":
        vacancy = VacancyFacts(
            vacancy_id=uuid7(),
            title="LLM Engineer",
            company="Quillstack",
            description=(
                "Build LLM agents and RAG with LangGraph and the OpenAI API on PostgreSQL. Remote."
                if english
                else "LLM-агенты и RAG на LangGraph и PostgreSQL. Удалённо."
            ),
            key_skills=(
                "Python",
                "LLM",
                "RAG",
                "LangGraph",
                "OpenAI API",
                "pgvector",
                "Kubernetes",
            ),
        )
    else:
        vacancy = VacancyFacts(
            vacancy_id=uuid7(),
            title="Python Backend Engineer",
            company="Ledgerline",
            description="REST APIs on Python, FastAPI, PostgreSQL, Redis and Celery. Remote.",
            key_skills=("Python", "FastAPI", "PostgreSQL", "Redis", "Celery", "Docker"),
        )
    values: dict[str, Any] = {
        "vacancy": vacancy,
        "profile": profile,
        "contacts": contacts.ContactBlock(
            name="Alex Sample",
            email="alex.sample@example.com",
            phone="+7 700 000 00 00",
            city="Almaty",
            links=(("Telegram", "t.me/alex_sample"), ("GitHub", "github.com/alex-sample")),
        ),
        "experience": (job,),
        "education": (
            EducationEntry(
                institution="Northern Plains University",
                degree="BSc",
                field="Computer Science",
                start_year=2018,
                end_year=2022,
                details=(
                    "Graduated with honours, GPA 3.8/4.0.",
                    "Courses: algorithms, machine learning, NLP, databases.",
                ),
            ),
        ),
        "projects": (
            ProjectEntry(
                ref=0,
                name="EvalKit",
                link="github.com/alex-sample/evalkit",
                stack=("Python", "pytest", "DuckDB"),
                achievements=(
                    "Open-source harness that runs prompt variants against a YAML test set.",
                    "Used by 3 teams; 140 GitHub stars.",
                ),
            ),
        ),
    }
    values.update(overrides)
    context = build_context(
        values["vacancy"],
        values["profile"],
        contacts=values["contacts"],
        experience=values["experience"],
        education=values["education"],
        projects=values["projects"],
    )
    return context


# ── typography ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (BULLETS[0], "LLM sales agent."),
        (BULLETS[2], "Evaluation loop."),
        (BULLETS_RU[1], "Цикл оценки."),
        ("Оценка качества: собрал набор из 480 диалогов и прогоняю его в CI.", "Оценка качества:"),
        ("Cut p95 by 40%. Then moved the cache in front of the model.", ""),
        ("Built the API.", ""),
        ("Designed and built the onboarding flow that every new client goes through now.", ""),
        ("e.g. something that is not a label at all, written in lower case.", ""),
    ],
)
def test_a_lead_in_label_is_a_few_words_before_a_full_stop(text: str, label: str) -> None:
    """Short, no figure, a real sentence after it — otherwise not a label."""
    assert render.lead_in(text) == label


def test_only_a_label_first_list_gets_bold_labels() -> None:
    """One bullet that happens to open with a short sentence is not the style."""
    assert render.labels_bullets(BULLETS)
    assert not render.labels_bullets(("Backend. Built the FastAPI service behind the agent.",))
    assert not render.labels_bullets((BULLETS[0], "Wrote things.", "Did more things.", "More."))


def test_the_label_is_bold_and_the_text_is_the_stored_text() -> None:
    """Presentation only: the line spells the stored bullet character for character."""
    context = sample_context()
    lines = render.layout(compose_fallback(context), context)

    bullets = [line for line in lines if line.kind is LineKind.BULLET and "agent" in line.text]
    first = bullets[0]
    assert first.text == render.BULLET + BULLETS[0]
    assert first.spans == (
        Span(render.BULLET),
        Span("LLM sales agent.", bold=True),
        Span(BULLETS[0][len("LLM sales agent.") :]),
    )
    with pytest.raises(ValueError, match="spans do not spell the line"):
        render.Line(LineKind.BULLET, "• x", spans=(Span("• y"),))


def test_a_role_is_a_title_with_its_dates_over_the_employer_and_its_italic_line() -> None:
    """The owner's layout: role and period, then company and the resume's own descriptor."""
    import docx

    context = sample_context()
    arrangement = compose_fallback(context)

    text = render.to_text(arrangement, context)
    assert (
        "\nLLM Engineer\tMar 2024 – Present\nNorthwind Labs — B2B SaaS for wholesale distributors\n"
        in text
    )

    document = docx.Document(io.BytesIO(render.to_docx(arrangement, context)))
    org = next(p for p in document.paragraphs if p.text.startswith("Northwind Labs"))
    assert [run.italic for run in org.runs] == [None, None, True]
    bullet = next(p for p in document.paragraphs if "LLM sales agent." in p.text)
    assert [run.text for run in bullet.runs if run.bold] == ["LLM sales agent."]
    name = document.paragraphs[0]
    assert name.text == "Alex Sample" and name.alignment == 1  # centred
    assert document.tables == []


def test_education_keeps_its_years_details_and_the_spoken_languages() -> None:
    context = sample_context()

    text = render.to_text(compose_fallback(context), context)

    assert "\nBSc, Computer Science — Northern Plains University\t2018 – 2022\n" in text
    assert "\n• Graduated with honours, GPA 3.8/4.0.\n" in text
    assert text.rstrip().endswith("Languages: English (C1) · Russian (native)")


def test_a_project_is_its_name_and_italic_stack_with_the_link_at_the_margin() -> None:
    context = sample_context()

    lines = render.layout(compose_fallback(context), context)

    entry = next(line for line in lines if line.text.startswith("EvalKit"))
    assert entry.rendered == "EvalKit — Python, pytest, DuckDB\tgithub.com/alex-sample/evalkit"
    assert entry.spans[0] == Span("EvalKit", bold=True)
    assert entry.spans[-1] == Span("Python, pytest, DuckDB", italic=True)


def test_long_contacts_give_the_links_a_line_of_their_own() -> None:
    """One link wrapping alone onto a third line is what the first sample showed."""
    context = sample_context()
    long_links = context.contacts.model_copy(
        update={"links": (*context.contacts.links, ("LinkedIn", "linkedin.com/in/alex-sample"))}
    )
    context = context.model_copy(update={"contacts": long_links})

    lines = [line.text for line in render.layout(compose_fallback(context), context)]

    assert lines[2] == "Almaty, Kazakhstan (UTC+5) · Open to remote"
    assert lines[3] == "alex.sample@example.com · +7 700 000 00 00 · Telegram @alex_sample"
    assert lines[4] == "github.com/alex-sample · linkedin.com/in/alex-sample"


def test_a_labelled_bullet_with_a_colon_is_not_read_as_a_section() -> None:
    """The workshop's reader would otherwise split EXPERIENCE at «Оценка качества:»."""
    text = (
        "ОПЫТ РАБОТЫ\nLLM-инженер\nNorthwind\n• Оценка качества: собрал набор.\n"
        "• Бэкенд: сделал сервис.\nОБРАЗОВАНИЕ\n• Курсы: алгоритмы, NLP.\n• ICPC 2021: 7 место.\n"
    )

    experience = find_section(text, "опыт работы")
    education = find_section(text, "образование")

    assert experience is not None and len(experience.items) == 4
    assert education is not None
    assert education.items == ("Курсы: алгоритмы, NLP.", "ICPC 2021: 7 место.")


# ── the PDF ───────────────────────────────────────────────────────────


def _words(text: str) -> list[str]:
    return text.replace("\t", " ").split()


@pytest.mark.parametrize("language", ["en", "ru"])
def test_the_pdf_says_the_text_s_words_in_the_text_s_order_on_one_page(language: str) -> None:
    """The text↔file contract, for the file that is sent: nothing added, lost or moved."""
    context = sample_context(language=language)
    arrangement = compose_fallback(context)

    drawn = pdf.to_pdf(arrangement, context)

    with pdfplumber.open(io.BytesIO(drawn)) as document:
        assert len(document.pages) == 1
        extracted = "\n".join(page.extract_text() or "" for page in document.pages)
        fonts = {char["fontname"].split("+")[-1] for char in document.pages[0].chars}
    # The vendored face, embedded as a subset under its own name; nothing the
    # reader's machine would have to substitute.
    assert _words(extracted) == _words(render.to_text(arrangement, context))
    assert fonts and all(font.startswith("LiberationSerif") for font in fonts), fonts
    assert drawn == pdf.to_pdf(arrangement, context), "the same arrangement, the same bytes"


def test_the_pdf_passes_the_audit_written_for_other_people_s_pdfs() -> None:
    """No text-layer, column, glyph or font finding: what a parser reads is the text."""
    context = sample_context(language="ru")
    drawn = pdf.to_pdf(compose_fallback(context), context)

    report = ats_audit.audit(drawn, source_format="pdf")

    assert report.critical == []
    flagged = {finding.code.value for finding in report.findings}
    assert not flagged & {"no_text_layer", "column_interleaving", "broken_glyphs", "unmapped_font"}


def test_the_measured_page_is_the_drawn_page() -> None:
    """fits_one_page says what the PDF does: an arrangement it rejects spills over."""
    long_bullets = tuple(f"{text} {text}" for text in BULLETS)
    job = sample_context().experience[0].model_copy(update={"achievements": long_bullets})
    context = sample_context(
        experience=tuple(job.model_copy(update={"ref": ref}) for ref in range(3))
    )
    everything = CVArrangement(
        headline="LLM Engineer",
        skills=compose_fallback(context).skills,
        experience=tuple(JobChoice(ref=ref, bullets=(0, 1, 2, 3, 4)) for ref in range(3)),
    )

    assert not pdf.fits_one_page(everything, context)
    with pdfplumber.open(io.BytesIO(pdf.to_pdf(everything, context))) as document:
        assert len(document.pages) == 2
    fitted = generator.fit_to_page(everything, context)
    with pdfplumber.open(io.BytesIO(pdf.to_pdf(fitted, context))) as document:
        assert len(document.pages) == 1


def test_a_letter_pdf_is_the_letter_s_text_and_nothing_else() -> None:
    context = sample_context(language="ru")
    letter = "Здравствуйте!\n\nОткликаюсь на вакансию.\nЧто сделал:\n• Цикл оценки.\n\nС уважением"

    with pdfplumber.open(io.BytesIO(pdf.letter_to_pdf(letter, context))) as document:
        extracted = document.pages[0].extract_text() or ""

    assert _words(extracted) == _words(letter)


# ── selection ─────────────────────────────────────────────────────────


def test_the_main_job_shows_five_of_seven_bullets_the_strongest_first() -> None:
    """Not all seven: the two that answer the vacancy least make room for the rest."""
    llm = compose_fallback(sample_context())
    backend = compose_fallback(sample_context(track="backend"))

    assert len(llm.experience[0].bullets) == 5
    assert llm.experience[0].bullets[0] == 0, "the owner's lead bullet leads an LLM CV"
    assert 6 not in llm.experience[0].bullets, "mentoring is not what an LLM vacancy asks"
    assert backend.experience[0].bullets[0] == 4, "the backend bullet leads a backend CV"


def test_skills_are_what_the_vacancy_asks_for_and_what_the_page_shows() -> None:
    context = sample_context()
    arrangement = compose_fallback(context)
    shown = [choice.shown_as for choice in arrangement.skills]

    assert shown[:6] == ["Python", "LLM", "RAG", "LangGraph", "OpenAI API", "pgvector"]
    assert "FastAPI" in shown, "named by a shown bullet"
    for unasked in ("TypeScript", "React", "pandas", "Linux", "Git"):
        assert unasked not in shown
    assert len(shown) <= generator.MAX_SKILLS_SHOWN
    assert inspect(arrangement, context) == []


def test_pruning_never_goes_below_the_owner_s_own_count_rule() -> None:
    """A hard «at least N skills» rule wins over the ten-second read."""
    context = sample_context()
    rule = RuleSpec(
        id=str(uuid7()),
        scope="cv",
        severity="hard",
        params=SectionItemCountParams(section="Навыки", minimum=16),
        message="в навыках не меньше 16 пунктов",
    )
    arrangement = compose_fallback(context).model_copy(
        update={
            "skills": tuple(
                generator.SkillChoice(canonical_name=s.canonical_name, shown_as=s.spelling)
                for s in context.profile.skills
            )
        }
    )

    assert skills_floor([rule]) == 16
    assert len(prune_skills(arrangement, context, floor=16)) == 16
    assert len(prune_skills(arrangement, context)) < 16


def test_a_skill_named_only_in_the_description_is_asked_for() -> None:
    context = sample_context()

    assert asked_for("PostgreSQL", context), "in the text, not in the list"
    assert not asked_for("React", context)


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("Gates prompts at a 95,3% pass rate.", "en", "Gates prompts at a 95.3% pass rate."),
        ("Over 14 000 cards, 1,200 customers.", "en", "Over 14,000 cards, 1,200 customers."),
        ("p95 fell from 7,8 s to 2,9 s.", "en", "p95 fell from 7.8 s to 2.9 s."),
        ("Доля 95,3%, 14 000 карточек.", "ru", "Доля 95,3%, 14 000 карточек."),
    ],
)
def test_an_english_summary_writes_figures_the_english_way(
    text: str, language: str, expected: str
) -> None:
    assert written_for(text, language) == expected


def test_the_summary_s_figures_are_reformatted_and_still_checked() -> None:
    """Notation changes, values do not — and the guard still finds them in the profile."""
    context = sample_context()

    arrangement = compose_fallback(context)

    assert "95.3%" in arrangement.summary
    assert "95,3%" not in arrangement.summary
    assert guard.check_summary(arrangement.summary, context) == []
    assert BULLETS[2] in render.to_text(arrangement, context), "bullets stay verbatim"


def test_the_rule_based_summary_is_two_or_three_whole_sentences() -> None:
    summary = sample_context().profile.summary or ""

    trimmed = trimmed_summary(summary)

    assert trimmed.count(". ") + 1 <= generator.MAX_SUMMARY_SENTENCES
    assert summary.startswith(trimmed) and trimmed.endswith(".")
    assert "Before that" not in trimmed


def test_the_line_with_the_vacancy_s_first_requirements_leads() -> None:
    """The LLM line for an LLM vacancy, the backend line for a backend one."""
    llm = sample_context()
    backend = sample_context(track="backend")

    llm_lines = [line.text for line in render.layout(compose_fallback(llm), llm)]
    backend_lines = [line.text for line in render.layout(compose_fallback(backend), backend)]

    assert llm_lines[llm_lines.index("SKILLS") + 1].startswith("LLM & AI:")
    assert backend_lines[backend_lines.index("SKILLS") + 1].startswith("Backend:")
    lines = skill_groups.grouped(
        [
            ("python", "Python"),
            ("fastapi", "FastAPI"),
            ("django", "Django"),
            ("rag", "RAG"),
            ("pytest", "pytest"),
            ("figma", "Figma"),
            ("postgresql", "PostgreSQL"),
            ("docker", "Docker"),
        ],
        "en",
    )
    assert lines is not None
    assert ("Tools & Testing", ["pytest"]) not in lines, "a one-name line joins Other"


def test_project_bullets_print_in_the_resume_s_order() -> None:
    """A project's first bullet says what it is; the figure-led one follows it."""
    context = sample_context()

    arrangement = compose_fallback(context)

    assert arrangement.projects[0].bullets == (0, 1)


# ── the stored rows the layout reads ──────────────────────────────────


def test_the_company_note_and_degree_details_are_read_back_from_storage() -> None:
    (entry,) = experience_from_rows(
        [
            {
                "position": 0,
                "company": "Northwind",
                "title": "LLM Engineer",
                "company_note": " B2B SaaS ",
            }
        ]
    )
    (degree,) = education_from_rows(
        [{"institution": "NPU", "start_year": 2018, "end_year": True, "details": ["Honours", 3]}]
    )

    assert entry.company_note == "B2B SaaS"
    assert degree.start_year == 2018 and degree.end_year is None
    assert degree.details == ("Honours",)


def test_extraction_keeps_the_descriptor_and_the_degree_lines_verbatim() -> None:
    from app.resume.profile_builder import to_education, to_experience
    from app.schemas.llm import Education, ProfileExtraction, WorkPeriod

    extraction = ProfileExtraction(
        work_periods=[
            WorkPeriod(company="Northwind", title="LLM Engineer", company_note="  B2B  SaaS "),
            WorkPeriod(company="Acme", title="Dev", company_note="x" * 400),
        ],
        education=[Education(institution="NPU", details=["• Graduated with honours.", "", "ICPC"])],
    )

    first, second = to_experience(extraction)
    (degree,) = to_education(extraction)

    assert first.company_note == "B2B SaaS"
    assert second.company_note is None, "a paragraph is not a descriptor"
    assert degree["details"] == ["Graduated with honours.", "ICPC"]


def test_a_bullet_glyph_never_starts_a_label() -> None:
    """The renderer adds "• " itself; a label search starts after it."""
    assert re.match(r"^• ", render.BULLET)
    assert render.lead_in("• Evaluation loop. Built the set and the CI gate around it.") == ""


def test_the_pdf_is_what_is_sent_once_its_own_audit_passes() -> None:
    """The worker, the bot and the Mini App send ``preferred``: the PDF when offered."""
    from app.documents.review import NO_COVERAGE, DocumentReview, ReviewedDocument
    from app.documents.service import _audited_pdf

    context = sample_context()
    drawn = pdf.to_pdf(compose_fallback(context), context)
    offered = _audited_pdf(drawn, "Alex-Sample-Quillstack.docx", context.vacancy.vacancy_id)
    report = ats_audit.audit(drawn, source_format="pdf")
    document = ReviewedDocument(
        filename="Alex-Sample-Quillstack.docx",
        file_format="docx",
        text="",
        review=DocumentReview(ats=report, coverage=NO_COVERAGE),
        content=b"PK",
        pdf=offered,
    )

    assert offered == drawn
    assert document.preferred == ("Alex-Sample-Quillstack.pdf", drawn)
    assert document.model_copy(update={"pdf": None}).preferred == (
        "Alex-Sample-Quillstack.docx",
        b"PK",
    )
    assert "pdf" not in document.model_dump(), "never serialised into a response"


def test_markup_characters_in_the_owner_s_text_are_printed_not_parsed() -> None:
    """A bullet about "R&D" or "<200 ms" must not break the PDF or lose a character."""
    bullet = "Search & ranking. Cut p95 to <200 ms for R&D teams with a <b>cache</b> layer."
    job = sample_context().experience[0].model_copy(update={"achievements": (bullet, *BULLETS[1:])})
    context = sample_context(experience=(job,))
    arrangement = compose_fallback(context).model_copy(
        update={"experience": (JobChoice(ref=0, bullets=(0, 1)),)}
    )

    with pdfplumber.open(io.BytesIO(pdf.to_pdf(arrangement, context))) as document:
        extracted = document.pages[0].extract_text() or ""

    assert " ".join(_words(bullet)) in " ".join(_words(extracted))


def test_a_link_too_long_for_its_line_still_renders() -> None:
    """The title keeps a third of the line; the build never fails over a long link."""
    project = (
        sample_context()
        .projects[0]
        .model_copy(update={"link": "github.com/" + "very-long-organisation-name/" * 4 + "evalkit"})
    )
    context = sample_context(projects=(project,))

    assert pdf.to_pdf(compose_fallback(context), context).startswith(b"%PDF-")

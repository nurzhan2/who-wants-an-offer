"""Sorting a CV's skills into the labelled lines a recruiter scans.

The owner's rejected CV listed fourteen technologies on one comma-separated
line. Every reference the workshop research settled on (docs/WORKSHOP-RESEARCH.md,
rule C3, and ``app/workshop/seed/references.json``) writes the same list as three
to six labelled lines — "Backend: FastAPI, Django, …", "LLM & AI: RAG, …" — which
is what lets a reader find "do they know Postgres" in one glance instead of
reading the whole line.

**This is presentation, not a claim.** The grouping decides which line a skill is
printed on and nothing else: the set of skills, their spelling and their order
are the arrangement's, already checked by :mod:`app.documents.guard`. So it is
done here, deterministically, rather than asked of a model — a model choosing
labels would be one more place for an invented category ("Kubernetes expert")
to appear, and there is nothing for it to decide that a table cannot.

**Where the group comes from.** The project's skill dictionary
(``app/resume/skills_min.yaml``) already files every skill it knows under a coarse
group; those groups are mapped onto the CV's lines. The dictionary predates the
LLM work and does not know "RAG", "pgvector" or "Anthropic API", so a short list
of markers puts those on the AI line, which for the owner's track is the most
important line on the page. Anything else unknown goes on a last line, "Other".

**The order of the lines follows the arrangement.** The line holding most of
the arrangement's first :data:`LEADING_SKILLS` skills leads — and the
arrangement puts the vacancy's own requirements first, so that is the LLM line
for an LLM vacancy and the backend line for a backend one; the rest follow by
the same count, then by where their first skill stands. Within a line the
arrangement's order is kept.

**When grouping is not worth it.** Fewer than :data:`MIN_SKILLS_TO_GROUP` skills,
or skills that all land on one line, are printed as the single plain line they
always were: a "Other: Excel, Word" block is worse than no label at all.
"""

import re

from app.resume.skills import default_canonicalizer, normalize

#: Below this many skills, one plain line reads better than labelled ones.
MIN_SKILLS_TO_GROUP = 6

#: The CV's lines, in the order used when two groups first appear at the same
#: position (which cannot happen; it is the order of this tuple otherwise).
GROUPS: tuple[str, ...] = (
    "programming",
    "backend",
    "ai",
    "frontend",
    "data",
    "tools",
    "other",
)

#: What each line is called, per CV language. These labels are a contract with
#: ``app.workshop.sections``: a line "Backend: FastAPI, Django" there is part of
#: the skills section, not a heading of its own, and its values are counted one
#: by one. ``SKILL_GROUP_LABELS`` in that module must hold every label below,
#: folded; a test compares the two.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "programming": "Programming",
        "backend": "Backend",
        "ai": "LLM & AI",
        "frontend": "Frontend",
        "data": "Data & Infrastructure",
        "tools": "Tools & Testing",
        "other": "Other",
    },
    "ru": {
        "programming": "Языки программирования",
        "backend": "Бэкенд",
        "ai": "LLM и ИИ",
        "frontend": "Фронтенд",
        "data": "Данные и инфраструктура",
        "tools": "Инструменты и тесты",
        "other": "Другое",
    },
}

#: How many of the arrangement's first skills decide which line leads. The
#: arrangement puts the vacancy's own requirements first, and the first few of
#: those say what the job is; counting all of them would let a long line of
#: secondary tools outrank the line the role is named after.
LEADING_SKILLS = 5

#: Minor lines that join "Other" when they would hold a single name.
SINGLETON_INTO_OTHER: tuple[str, ...] = ("programming", "frontend", "tools", "data")

#: The skill dictionary's groups, mapped onto the CV's lines.
DICTIONARY_GROUPS: dict[str, str] = {
    "language": "programming",
    "backend": "backend",
    "frontend": "frontend",
    "database": "data",
    "data": "data",
    "queue": "data",
    "devops": "data",
    "cloud": "data",
    "ci": "data",
    "os": "data",
    "ml": "ai",
    "testing": "tools",
    "tool": "tools",
    "practice": "other",
}

#: Normalised fragments that mark a skill the dictionary does not know as AI
#: work: model APIs, retrieval, vector stores, agent tooling, evaluation.
AI_MARKERS: tuple[str, ...] = (
    "llm",
    "rag",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "langchain",
    "langgraph",
    "llamaindex",
    "embedding",
    "pgvector",
    "vector",
    "qdrant",
    "chroma",
    "faiss",
    "pinecone",
    "weaviate",
    "prompt",
    "agent",
    "mcp",
    "huggingface",
    "transformers",
    "whisper",
    "functioncalling",
    "eval",
    "ollama",
    "vllm",
    # Classic NLP libraries: the owner's own skills section has an NLP line,
    # and these sit beside the LLM work rather than under "Other".
    "spacy",
    "nltk",
    "gensim",
    "fasttext",
    "bert",
)

#: Normalised fragments that mark an unknown skill as data or infrastructure
#: work: analytical stores and the observability stack.
DATA_MARKERS: tuple[str, ...] = (
    "duckdb",
    "opentelemetry",
    "prometheus",
    "grafana",
    "sentry",
    "jaeger",
    "datadog",
)


def _bare_skill(name: str) -> str:
    """A skill without its qualifier or version: «PostgreSQL (JSONB, pg_trgm)» →
    «PostgreSQL», «Java 21» → «Java». The owner's resume writes skills that way,
    and the dictionary knows the bare name (22 Nov 2026: both landed in Other)."""
    bare = re.sub(r"\s*\(.*?\)\s*", " ", name)
    bare = re.sub(r"\s+v?\d+(?:\.\d+)*\s*$", "", bare.strip())
    return bare.strip()


def group_of(name: str) -> str:
    """Which CV line one skill belongs on, from its canonical name or spelling."""
    canonicalizer = default_canonicalizer()
    for candidate in dict.fromkeys((name, _bare_skill(name))):
        canonical = canonicalizer.canonicalize(candidate)
        if canonical is None:
            continue
        group = canonicalizer.group_of(canonical)
        if group is not None and group in DICTIONARY_GROUPS:
            return DICTIONARY_GROUPS[group]
    folded = normalize(name)
    if any(marker in folded for marker in AI_MARKERS):
        return "ai"
    if any(marker in folded for marker in DATA_MARKERS):
        return "data"
    return "other"


def grouped(skills: list[tuple[str, str]], language: str) -> list[tuple[str, list[str]]] | None:
    """``(label, [shown names])`` lines, or None when one plain line is better.

    ``skills`` is ``(canonical_name, shown_as)`` in the arrangement's order. The
    group is read off the canonical name first and the shown spelling second,
    because a renamed skill ("PostgreSQL" shown for "postgres") is still the same
    skill and either name may be the one the dictionary knows.
    """
    if len(skills) < MIN_SKILLS_TO_GROUP:
        return None
    labels = LABELS.get(language, LABELS["en"])
    lines: dict[str, list[str]] = {}
    weight: dict[str, int] = {}
    first: dict[str, int] = {}
    for position, (canonical, shown) in enumerate(skills):
        group = group_of(canonical)
        if group == "other":
            group = group_of(shown)
        lines.setdefault(group, []).append(shown)
        weight[group] = weight.get(group, 0) + (1 if position < LEADING_SKILLS else 0)
        first.setdefault(group, position)
    # One language alone is a thin line of its own ("Programming: Python"); it
    # leads the backend line instead, which is where a reader looks for it.
    if len(lines.get("programming", ())) == 1 and "backend" in lines:
        lines["backend"] = lines.pop("programming") + lines["backend"]
        weight["backend"] += weight.pop("programming")
        first["backend"] = min(first["backend"], first.pop("programming"))
    # Any other minor line holding a single name joins "Other": a label with
    # one value under it ("Frontend: React") reads as a gap.
    # A lone testing or tooling name ("pytest") is backend practice and closes
    # the backend line instead.
    for group in SINGLETON_INTO_OTHER:
        if len(lines.get(group, ())) == 1 and len(lines) > 2:
            into = "backend" if group == "tools" and "backend" in lines else "other"
            lines.setdefault(into, []).extend(lines.pop(group))
            weight[into] = weight.get(into, 0) + weight.pop(group)
            first[into] = min(first.get(into, len(skills)), first.pop(group))
    if len(lines) < 2:  # one group is just a plain line with a label on it
        return None
    # The line carrying most of what the arrangement put first leads — for an
    # LLM vacancy the LLM line, for a backend one the backend line — because
    # the arrangement puts the vacancy's own requirements first. "Other" last
    # whatever its weight: it is the line nobody looks for.
    ordered = sorted(
        lines.items(),
        key=lambda item: (item[0] == "other", -weight[item[0]], first[item[0]]),
    )
    return [(labels[group], names) for group, names in ordered]

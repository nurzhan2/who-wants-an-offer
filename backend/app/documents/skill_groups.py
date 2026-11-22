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

**The order of the lines follows the arrangement.** A group's position is the
position of its first skill in the arrangement's order, so the vacancy's
requirements — which the arrangement puts first — pull their lines up. Within a
line the arrangement's order is kept too.

**When grouping is not worth it.** Fewer than :data:`MIN_SKILLS_TO_GROUP` skills,
or skills that all land on one line, are printed as the single plain line they
always were: a "Other: Excel, Word" block is worse than no label at all.
"""

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
)


def group_of(name: str) -> str:
    """Which CV line one skill belongs on, from its canonical name or spelling."""
    canonicalizer = default_canonicalizer()
    canonical = canonicalizer.canonicalize(name)
    if canonical is not None:
        group = canonicalizer.group_of(canonical)
        if group is not None and group in DICTIONARY_GROUPS:
            return DICTIONARY_GROUPS[group]
    folded = normalize(name)
    if any(marker in folded for marker in AI_MARKERS):
        return "ai"
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
    for canonical, shown in skills:
        group = group_of(canonical)
        if group == "other":
            group = group_of(shown)
        lines.setdefault(group, []).append(shown)
    # One language alone is a thin line of its own ("Programming: Python"); it
    # leads the backend line instead, which is where a reader looks for it.
    if len(lines.get("programming", ())) == 1 and "backend" in lines:
        merged = {"backend": lines.pop("programming") + lines["backend"]}
        lines = merged | {group: names for group, names in lines.items() if group != "backend"}
    if len(lines) < 2:  # one group is just a plain line with a label on it
        return None
    # "Other" last whatever its first position: it is the line nobody looks
    # for, and leading with it would push the vacancy's own stack down.
    ordered = sorted(lines.items(), key=lambda item: item[0] == "other")
    return [(labels[group], names) for group, names in ordered]

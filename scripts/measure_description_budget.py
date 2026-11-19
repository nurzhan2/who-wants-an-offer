"""How many descriptions the reach rule buys for N profiles against one.

Phase 22 asks for the number: since several resumes are active, the embedding
pass walks once per profile and embeds the UNION of every profile's protected
top (``app/pipeline/embedding.py``). This script replays that walk without the
model, so the cost of six profiles against one can be read off a corpus.

Two modes:

* default — the live database. Needs vacancies that already have BOTH vectors
  (run ``make embed-backlog a=--until-drained`` first, otherwise the replay can
  only see the rows already embedded and undercounts) and the active profiles'
  vectors. Nothing is written.
* ``--synthetic N`` — a random corpus of N vacancies and random profiles, for
  the arithmetic alone. Its number says how the rule scales with the number of
  profiles on random geometry; it says nothing about a real corpus, where the
  tracks overlap (a backend and a fullstack resume want many of the same
  postings) and the union is smaller.

    uv run python scripts/measure_description_budget.py
    uv run python scripts/measure_description_budget.py --synthetic 3000 --profiles 6
"""

import argparse
import asyncio
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import settings
from app.db.models import CandidateProfile, Vacancy
from app.db.profile_order import DEFAULT_ORDER
from app.db.session import session_factory
from app.pipeline.embedding import _cosine, _Reach

RULE = "-" * 64


@dataclass(frozen=True, slots=True)
class Row:
    """One vacancy: its title vector and its description vector."""

    title: Sequence[float]
    description: Sequence[float]


@dataclass(frozen=True, slots=True)
class Profile:
    """One profile: its headline vector and its resume vector."""

    slug: str
    headline: Sequence[float]
    resume: Sequence[float]


def walk(
    rows: Sequence[Row], profile: Profile, skip: set[int], *, warmup: int, protect: int
) -> set[int]:
    """The rows one profile's walk would embed, given the ones already embedded."""
    reach = _Reach(profile_vector=profile.resume, warmup=warmup, protect=protect)
    order = sorted(range(len(rows)), key=lambda i: -_cosine(rows[i].title, profile.headline))
    embedded: set[int] = set()
    for index in order:
        if index in skip:
            # Already has a vector: not outstanding, so the real walk never sees it.
            continue
        title = _cosine(rows[index].title, profile.headline)
        if reach.out_of_reach(title, len(embedded)):
            break
        reach.record(title, rows[index].description)
        embedded.add(index)
    return embedded


def replay(
    rows: Sequence[Row], profiles: Sequence[Profile], *, warmup: int, protect: int
) -> list[tuple[str, int, int]]:
    """Per profile, in walk order: (slug, embedded by this walk, union so far)."""
    union: set[int] = set()
    report = []
    for profile in profiles:
        fresh = walk(rows, profile, union, warmup=warmup, protect=protect)
        union |= fresh
        report.append((profile.slug, len(fresh), len(union)))
    return report


def _unit(rng: random.Random, dim: int) -> list[float]:
    vector = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def synthetic(
    size: int, count: int, *, dim: int = 64, seed: int = 22
) -> tuple[list[Row], list[Profile]]:
    """A random corpus where a description sits near its own title."""
    rng = random.Random(seed)
    rows = []
    for _ in range(size):
        title = _unit(rng, dim)
        noise = _unit(rng, dim)
        rows.append(
            Row(
                title=title,
                description=[0.7 * t + 0.3 * n for t, n in zip(title, noise, strict=True)],
            )
        )
    profiles = []
    for index in range(count):
        headline = _unit(rng, dim)
        profiles.append(Profile(slug=f"p{index}", headline=headline, resume=headline))
    return rows, profiles


async def from_database() -> tuple[list[Row], list[Profile]]:
    """Vacancies with both vectors, and the active profiles with both."""
    async with session_factory() as session:
        vacancies = (
            await session.execute(
                select(Vacancy.title_embedding, Vacancy.embedding).where(
                    Vacancy.is_active.is_(True),
                    Vacancy.title_embedding.is_not(None),
                    Vacancy.embedding.is_not(None),
                )
            )
        ).all()
        people = (
            await session.execute(
                select(
                    CandidateProfile.slug,
                    CandidateProfile.headline_embedding,
                    CandidateProfile.embedding,
                )
                .where(
                    CandidateProfile.is_active.is_(True),
                    CandidateProfile.headline_embedding.is_not(None),
                    CandidateProfile.embedding.is_not(None),
                )
                .order_by(*DEFAULT_ORDER)
            )
        ).all()
    rows = [Row(title=list(t), description=list(d)) for t, d in vacancies]
    profiles = [Profile(slug=s, headline=list(h), resume=list(r)) for s, h, r in people]
    return rows, profiles


def show(
    rows: Sequence[Row], profiles: Sequence[Profile], *, warmup: int, protect: int, label: str
) -> None:
    print(RULE)
    print(f"БЮДЖЕТ ОПИСАНИЙ — {label}")
    print(f"  вакансий с обоими векторами {len(rows)}, warmup {warmup}, protect_top {protect}")
    print(RULE)
    if not profiles:
        print("  нет профилей с обоими векторами")
        return
    one = replay(rows, profiles[:1], warmup=warmup, protect=protect)[0][1]
    print(f"  1 профиль ({profiles[0].slug}): {one} описаний")
    for slug, fresh, union in replay(rows, profiles, warmup=warmup, protect=protect):
        print(f"  + {slug:<12} добавил {fresh:>5}, всего {union:>5}")
    total = replay(rows, profiles, warmup=warmup, protect=protect)[-1][2]
    if one:
        print(f"  {len(profiles)} профилей / 1 профиль = {total / one:.2f}×")
    print(RULE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--synthetic", type=int, default=None, metavar="N")
    parser.add_argument("--profiles", type=int, default=6)
    args = parser.parse_args()
    warmup, protect = settings.embedding_warmup_rows, settings.embedding_protect_top
    if args.synthetic:
        rows, profiles = synthetic(args.synthetic, args.profiles)
        show(rows, profiles, warmup=warmup, protect=protect, label="синтетический корпус")
    else:
        rows, profiles = asyncio.run(from_database())
        show(rows, profiles, warmup=warmup, protect=protect, label="живая база")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

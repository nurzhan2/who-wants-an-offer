"""What the worker does with a vacancy the moment it arrives.

Phase 22b, the owner's choices of 21 Nov 2026:

* every new vacancy is scored against every active resume at once;
* one whose best resume clears the queue's floor gets a letter and a tailored
  CV from that resume, within the day's model budget
  (``LLM_MAX_COST_PER_DAY_USD``) and document cap;
* the owner is told in Telegram straight away, with the CV as a file and the
  letter as text;
* then, by source: an **hh** vacancy waits in the agent's queue for the owner's
  confirmation (the agent runs on the owner's laptop, not here); a **Telegram**
  vacancy is written to the recruiter the post names, within the limits in
  :mod:`app.services.outreach`; anything else — LinkedIn and Indeed through the
  alert e-mails, the job boards — is applied to by hand with what was sent.

Nothing here applies on hh, and nothing here can: there is no browser and no hh
session on the server.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import MatchBucket
from app.db.models import Application, Match, Vacancy, VacancySource
from app.db.repositories.source_state import SourceStateRepository
from app.db.seed_rows import seed_only
from app.documents import service as documents
from app.letters import service as letters
from app.letters import store as letters_store
from app.llm import usage as usage_ledger
from app.matching import profile_vectors
from app.matching.best import best_profiles
from app.matching.scorer import ProfileNotReadyError, score_corpus
from app.services import notify, outreach
from app.services.outreach import Sender

logger = get_logger(__name__)

STATE_SLUG = "worker"


@dataclass(slots=True)
class Reaction:
    """What one vacancy got, for the log and the tests."""

    vacancy_id: UUID
    notified: bool = False
    letter: bool = False
    cv: bool = False
    action: str = ""


@dataclass(slots=True)
class Budget:
    """Today's spend and document count, persisted so a restart does not reset them."""

    day: str
    usd: float = 0.0
    documents: int = 0
    reacted: list[str] = field(default_factory=list)

    @property
    def can_write(self) -> bool:
        return (
            self.usd < settings.llm_max_cost_per_day_usd
            and self.documents < settings.worker_max_documents_per_day
        )


async def load_budget(session: AsyncSession) -> Budget:
    day = datetime.now(UTC).date().isoformat()
    stored = await SourceStateRepository(session).get(STATE_SLUG, f"budget:{day}") or {}
    return Budget(
        day=day, usd=float(stored.get("usd", 0.0)), documents=int(stored.get("documents", 0))
    )


async def save_budget(session: AsyncSession, budget: Budget) -> None:
    await SourceStateRepository(session).set(
        STATE_SLUG,
        f"budget:{budget.day}",
        {"usd": round(budget.usd, 4), "documents": budget.documents},
    )


async def react(
    session: AsyncSession,
    vacancy_ids: Sequence[UUID],
    *,
    sender: Sender | None = None,
) -> list[Reaction]:
    """Score these vacancies, then act on the ones worth acting on, best first."""
    if not vacancy_ids:
        return []
    # A profile uploaded on the server has no vectors until something computes
    # them, and without the headline's every title scores as unknown. Cheap
    # when they exist: one SELECT per active profile.
    note = await profile_vectors.ensure_profile_embedding(session, allowed=True)
    if note:
        logger.info("worker.profile_vectors", note=note)
    try:
        await score_corpus(session, vacancy_ids=vacancy_ids)
    except ProfileNotReadyError:
        logger.warning("worker.no_profile")
        return []
    await session.commit()
    best = await best_profiles(session, vacancy_ids)
    floor = Decimal(settings.agent_queue_min_score)
    candidates = sorted(
        (item for item in best.values() if item.score >= floor),
        key=lambda item: item.score,
        reverse=True,
    )
    budget = await load_budget(session)
    states = SourceStateRepository(session)
    reactions: list[Reaction] = []
    for item in candidates:
        key = f"reacted:{item.vacancy_id}"
        if await states.get(STATE_SLUG, key) is not None:
            continue
        facts = await _facts(session, item.vacancy_id, item.profile_id)
        if facts is None:
            continue
        reaction = await _one(session, item, facts, budget=budget, sender=sender)
        await states.set(STATE_SLUG, key, {"at": datetime.now(UTC).isoformat()})
        await save_budget(session, budget)
        await session.commit()
        reactions.append(reaction)
    return reactions


async def _facts(
    session: AsyncSession, vacancy_id: UUID, profile_id: UUID
) -> dict[str, Any] | None:
    """Title, company, source, link and the post's contact; None for a seed or a filtered row."""
    bucket = await session.scalar(
        select(Match.bucket).where(Match.vacancy_id == vacancy_id, Match.profile_id == profile_id)
    )
    if bucket == MatchBucket.FILTERED:
        return None
    if await session.scalar(select(seed_only(Vacancy.id)).where(Vacancy.id == vacancy_id)):
        return None
    vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None or not vacancy.is_active:
        return None
    source = (
        await session.execute(
            select(VacancySource.source_slug, VacancySource.url, VacancySource.raw)
            .where(VacancySource.vacancy_id == vacancy_id)
            .order_by(VacancySource.created_at)
            .limit(1)
        )
    ).first()
    if source is None:
        return None
    derived = source.raw.get("_derived") if isinstance(source.raw, dict) else None
    derived = derived if isinstance(derived, dict) else {}
    return {
        "title": vacancy.title,
        "company": vacancy.company,
        "slug": source.source_slug,
        "url": source.url,
        "contact": derived.get("apply_contact"),
        "channel": derived.get("channel"),
    }


async def _one(
    session: AsyncSession,
    item: Any,
    facts: dict[str, Any],
    *,
    budget: Budget,
    sender: Sender | None,
) -> Reaction:
    reaction = Reaction(vacancy_id=item.vacancy_id)
    letter_text: str | None = None
    cv: tuple[str, bytes] | None = None
    if budget.can_write:
        before = usage_ledger.ledger.invoiced_usd
        profile = await letters_store.load_profile_facts(session, item.profile_id)
        if profile is not None:
            await letters.write_letter(session, item.vacancy_id, profile)
            letter_text = await _letter(session, item.vacancy_id)
            reaction.letter = letter_text is not None
            written = await documents.write_cv(session, item.vacancy_id, profile)
            if written.document is not None:
                cv = (written.document.filename, written.document.content)
                reaction.cv = True
            budget.documents += 1
        budget.usd += max(0.0, usage_ledger.ledger.invoiced_usd - before)
        await session.commit()

    reaction.action = await _act(session, item, facts, letter_text, cv, sender=sender)
    margin = f", отрыв {item.margin:.0f}" if item.margin is not None else ""
    company = f" — {facts['company']}" if facts.get("company") else ""
    lines = [
        f"🆕 {facts['title']}{company}",
        f"Оценка {item.score:.0f} · резюме «{item.slug}»{margin}",
        f"{facts['slug']} · {facts['url']}",
        "",
        reaction.action,
    ]
    if not budget.can_write and letter_text is None:
        lines.append("Письмо и резюме не написаны: дневной бюджет модели или лимит документов.")
    if letter_text:
        lines += ["", "Письмо:", letter_text]
    reaction.notified = await notify.send_text("\n".join(lines))
    if cv is not None:
        await notify.send_document(cv[0], cv[1], caption=f"Резюме под «{facts['title']}»")
    return reaction


async def _act(
    session: AsyncSession,
    item: Any,
    facts: dict[str, Any],
    letter_text: str | None,
    cv: tuple[str, bytes] | None,
    *,
    sender: Sender | None,
) -> str:
    slug = facts["slug"]
    if slug == settings.agent_source_slug:
        return (
            "hh: в очереди агента. Подтвердите на дашборде — агент на ноутбуке отправит "
            f"отклик резюме «{item.slug}»."
        )
    if slug == "telegram":
        decision = await outreach.decide(
            session,
            contact=facts.get("contact"),
            vacancy_key=str(item.vacancy_id),
            score=float(item.score),
        )
        if not decision.allowed or sender is None or not letter_text or cv is None:
            # A message without the CV is half an application, and the contact
            # can be written to only once: better the owner sends both by hand.
            if not decision.allowed:
                reason = decision.reason
            elif cv is None:
                reason = "резюме под вакансию не собралось"
            else:
                reason = "нет письма или сессии"
            contact = facts.get("contact") or "контакт в посте"
            return f"Telegram: напишите сами {contact} ({reason})."
        channel = f" из канала @{facts['channel']}" if facts.get("channel") else ""
        text = (
            f"Здравствуйте! Пишу по вакансии «{facts['title']}»{channel}: "
            f"{facts['url']}\n\n{letter_text}"
        )
        return "Telegram: " + await outreach.send(
            session, sender, decision=decision, vacancy_key=str(item.vacancy_id), text=text, file=cv
        )
    return "Откликнитесь по ссылке: приложите резюме из файла ниже и вставьте письмо."


async def _letter(session: AsyncSession, vacancy_id: UUID) -> str | None:
    found = await session.scalar(
        select(Application.cover_letter)
        .where(Application.vacancy_id == vacancy_id, Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    return found if isinstance(found, str) else None

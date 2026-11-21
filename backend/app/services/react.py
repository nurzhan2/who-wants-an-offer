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
from app.db.models import Application, CandidateProfile, Match, Vacancy, VacancySource
from app.db.repositories.source_state import SourceStateRepository
from app.db.seed_rows import seed_only
from app.documents import service as documents
from app.letters import service as letters
from app.letters import store as letters_store
from app.llm import usage as usage_ledger
from app.matching import profile_vectors
from app.matching.best import best_profiles
from app.matching.scorer import ProfileNotReadyError, score_corpus
from app.services import bot, notify, outreach
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
    """Score these vacancies, then announce the ones the owner's filters let through."""
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
    prefs = await bot.load_prefs(session)
    floor = Decimal(settings.agent_queue_min_score)
    candidates = sorted(
        (item for item in best.values() if item.score >= floor),
        key=lambda item: item.score,
        reverse=True,
    )
    budget = await load_budget(session)
    states = SourceStateRepository(session)
    reactions: list[Reaction] = []
    by_track: dict[str, int] = {}
    outreach_lines: list[str] = []
    for item in candidates:
        key = f"reacted:{item.vacancy_id}"
        if await states.get(STATE_SLUG, key) is not None:
            continue
        facts = await _facts(session, item.vacancy_id, item.profile_id)
        if facts is None:
            continue
        seen: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "profile_id": str(item.profile_id),
            "slug": item.slug,
        }
        skipped = bot.why_not(
            prefs, slug=item.slug, score=item.score, published=facts.get("published")
        )
        if skipped is not None:
            await states.set(STATE_SLUG, key, {**seen, "skipped": skipped})
            await session.commit()
            continue
        reaction = Reaction(vacancy_id=item.vacancy_id)
        if facts["slug"] == "telegram" and prefs.outreach:
            reaction.action = await _outreach(session, item, facts, budget=budget, sender=sender)
            reaction.letter = await _letter(session, item.vacancy_id) is not None
            outreach_lines.append(f"• {facts['title']}: {reaction.action}")
        by_track[item.slug] = by_track.get(item.slug, 0) + 1
        await states.set(STATE_SLUG, key, seen)
        await save_budget(session, budget)
        await session.commit()
        reactions.append(reaction)
    if by_track:
        # One message a pass, not one per vacancy: on 21 Nov three days of
        # matches arrived as separate messages and buried the chat. The lists
        # behind the buttons show every vacancy, with its letter and its CV.
        summary = ", ".join(f"{slug} · {count}" for slug, count in by_track.items())
        text = "\n".join(["Новые вакансии: " + summary, *outreach_lines])
        buttons: notify.Buttons = [[(f"📋 {slug}", f"G:{slug}:0")] for slug in by_track]
        notified = await notify.send_text(text, buttons=buttons)
        for reaction in reactions:
            reaction.notified = notified
    return reactions


async def _facts(
    session: AsyncSession, vacancy_id: UUID, profile_id: UUID
) -> dict[str, Any] | None:
    """Title, company, source, link, date and contact; None for a seed or a filtered row."""
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
        "published": vacancy.published_at or vacancy.first_seen_at,
        "known_date": vacancy.published_at is not None,
    }


async def _write_letter(
    session: AsyncSession, vacancy_id: UUID, profile_id: UUID, budget: Budget
) -> str | None:
    """The letter for this vacancy, written now if there is none and the budget allows."""
    existing = await _letter(session, vacancy_id)
    if existing is not None or not budget.can_write:
        return existing
    profile = await letters_store.load_profile_facts(session, profile_id)
    if profile is None:
        return None
    before = usage_ledger.ledger.invoiced_usd
    await letters.write_letter(session, vacancy_id, profile)
    budget.documents += 1
    budget.usd += max(0.0, usage_ledger.ledger.invoiced_usd - before)
    await session.commit()
    return await _letter(session, vacancy_id)


async def _cv_for(
    session: AsyncSession, vacancy_id: UUID, profile_id: UUID
) -> tuple[str, bytes] | None:
    if settings.worker_attach_cv == "original":
        return await original_cv(session, profile_id)
    profile = await letters_store.load_profile_facts(session, profile_id)
    if profile is None:
        return None
    written = await documents.write_cv(session, vacancy_id, profile)
    await session.commit()
    if written.document is None:
        return None
    return (written.document.filename, written.document.content)


def _card(item: Any, facts: dict[str, Any], status: str) -> tuple[str, notify.Buttons]:
    company = f" — {facts['company']}" if facts.get("company") else ""
    when = bot.age_label(facts.get("published"))
    if not facts.get("known_date"):
        when = f"найдена {when}"
    lines = [
        f"{facts['title']}{company}",
        f"⭐ {item.score:.0f} · {item.slug} · {facts['slug']} · {when}",
        str(facts["url"]),
    ]
    if status:
        lines += ["", status]
    vid = str(item.vacancy_id)
    buttons: notify.Buttons = [[("✉️ Письмо", f"L:{vid}"), ("📄 Резюме", f"C:{vid}")]]
    if facts["slug"] == settings.agent_source_slug:
        buttons.append([("➕ В очередь hh", f"Q:{vid}")])
    return "\n".join(lines), buttons


async def _outreach(
    session: AsyncSession,
    item: Any,
    facts: dict[str, Any],
    *,
    budget: Budget,
    sender: Sender | None,
) -> str:
    """Write to the recruiter the post names, letter and CV both, or say why not."""
    decision = await outreach.decide(
        session,
        contact=facts.get("contact"),
        vacancy_key=str(item.vacancy_id),
        score=float(item.score),
    )
    contact = facts.get("contact") or "контакт в посте"
    if not decision.allowed:
        return f"Автоотклик: нет ({decision.reason}). Напишите сами: {contact}"
    if sender is None:
        return f"Автоотклик: нет сессии Telegram. Напишите сами: {contact}"
    cv = await _cv_for(session, item.vacancy_id, item.profile_id)
    if cv is None:
        # A message without the CV is half an application, and the contact can
        # be written to only once: better the owner sends both by hand.
        return (
            "Автоотклик: нет файла резюме (загрузите его заново через «Заменить файл»). "
            f"Напишите сами: {contact}"
        )
    letter_text = await _write_letter(session, item.vacancy_id, item.profile_id, budget)
    if not letter_text:
        return f"Автоотклик: письмо не написано (бюджет). Напишите сами: {contact}"
    channel = f" из канала @{facts['channel']}" if facts.get("channel") else ""
    text = (
        f"Здравствуйте! Пишу по вакансии «{facts['title']}»{channel}: "
        f"{facts['url']}\n\n{letter_text}"
    )
    return "Автоотклик: " + await outreach.send(
        session, sender, decision=decision, vacancy_key=str(item.vacancy_id), text=text, file=cv
    )


async def on_button(session: AsyncSession, data: str, *, sender: Sender | None = None) -> str:
    """A card's button: send the letter, the CV, or queue an hh vacancy. Returns a toast."""
    kind, _, raw_id = data.partition(":")
    try:
        vacancy_id = UUID(raw_id)
    except ValueError:
        return ""
    states = SourceStateRepository(session)
    seen = await states.get(STATE_SLUG, f"reacted:{vacancy_id}") or {}
    if not seen.get("profile_id"):
        # A vacancy opened from a list may predate the worker: find its winner.
        winner = (await best_profiles(session, [vacancy_id])).get(vacancy_id)
        if winner is not None:
            seen = {**seen, "profile_id": str(winner.profile_id), "slug": winner.slug}
            await states.set(STATE_SLUG, f"reacted:{vacancy_id}", seen)
            await session.commit()
    profile_raw = seen.get("profile_id")
    if not profile_raw:
        return "Не нашёл, с каким резюме совпала вакансия"
    profile_id = UUID(str(profile_raw))
    title = await session.scalar(select(Vacancy.title).where(Vacancy.id == vacancy_id)) or ""
    if kind == "V":
        winner = (await best_profiles(session, [vacancy_id])).get(vacancy_id)
        facts = await _facts(session, vacancy_id, profile_id)
        if winner is None or facts is None:
            return "Вакансия больше не активна"
        text, buttons = _card(
            winner, facts, f"Рекрутер: {facts['contact']}" if facts.get("contact") else ""
        )
        await notify.send_text(text, buttons=buttons)
        return ""
    budget = await load_budget(session)
    if kind in {"L", "Q"}:
        letter_text = await _write_letter(session, vacancy_id, profile_id, budget)
        await save_budget(session, budget)
        await session.commit()
        if letter_text is None:
            return "Письмо не написано: дневной бюджет модели исчерпан"
        if kind == "Q":
            await notify.send_text(
                f"«{title}» в очереди hh. Подтвердите на дашборде во вкладке «Отклики» — "
                "агент на ноутбуке отправит."
            )
            return "В очереди hh"
        await notify.send_text(f"Письмо для «{title}»:\n\n{letter_text}")
        return "Письмо отправлено"
    if kind == "C":
        cv = await _cv_for(session, vacancy_id, profile_id)
        if cv is None:
            return "Нет файла резюме: замените файл на дашборде"
        await notify.send_document(
            cv[0], cv[1], caption=f"Резюме «{seen.get('slug', '')}» для «{title}»"
        )
        return "Резюме отправлено"
    return ""


async def original_cv(session: AsyncSession, profile_id: UUID) -> tuple[str, bytes] | None:
    """The owner's own uploaded file for this profile, if it was kept (0019)."""
    row = (
        await session.execute(
            select(CandidateProfile.resume_filename, CandidateProfile.resume_file).where(
                CandidateProfile.id == profile_id
            )
        )
    ).first()
    if row is None or not row.resume_file:
        return None
    return (row.resume_filename or "resume.pdf", bytes(row.resume_file))


async def _letter(session: AsyncSession, vacancy_id: UUID) -> str | None:
    found = await session.scalar(
        select(Application.cover_letter)
        .where(Application.vacancy_id == vacancy_id, Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    return found if isinstance(found, str) else None

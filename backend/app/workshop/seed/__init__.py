"""Rules and reference documents from the 21 Nov 2026 research, loaded once.

The owner asked for the workshop to be filled from what good software-engineer
CVs and letters look like. The research behind it (38 sources read, 52 public
CVs reviewed; summarised in ``docs/WORKSHOP-RESEARCH.md``) is distilled into
two files next to this module:

* ``rules.json`` — checkable rules: clichés and self-deprecation forbidden in
  letters (hard), the same clichés in CVs (soft), length bounds;
* ``references.json`` — four anonymised composite documents (CV and letter, in
  English and Russian) shown to the model as examples of form. Their numbers
  are illustrative; the note on each says so, and the letter's own guard and
  prompt keep content to the candidate's profile.

Applied once per database, through the same service functions the dashboard
uses, so every rule passes the same truthfulness gate. Remembered in
``source_state`` under a versioned key: the owner can delete or switch off any
of these afterwards and they will not come back.
"""

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import ReferenceKind
from app.db.repositories.source_state import SourceStateRepository
from app.schemas.workshop import RuleCreate
from app.workshop import references as reference_documents
from app.workshop import service

logger = get_logger(__name__)

HERE = Path(__file__).parent
STATE_SLUG = "workshop"
SEED_KEY = "seed:research-2026-11-21"


def rules() -> list[RuleCreate]:
    raw = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
    return [RuleCreate.model_validate(item) for item in raw]


def references() -> list[dict[str, str]]:
    raw: list[dict[str, str]] = json.loads((HERE / "references.json").read_text(encoding="utf-8"))
    return raw


async def apply_seed(session: AsyncSession) -> str | None:
    """Store the seed once. Returns a line for the log, or None if already applied."""
    states = SourceStateRepository(session)
    if await states.get(STATE_SLUG, SEED_KEY) is not None:
        return None
    for rule in rules():
        await service.create_rule(session, rule)
    for item in references():
        await service.store_reference(
            session,
            kind=ReferenceKind(item["kind"]),
            title=item["title"],
            note=item["note"],
            text=reference_documents.accept_text(item["text"]),
        )
    await states.set(STATE_SLUG, SEED_KEY, {"rules": len(rules()), "references": len(references())})
    await session.commit()
    line = f"мастерская: {len(rules())} правил и {len(references())} образца из исследования"
    logger.info("workshop.seeded", rules=len(rules()), references=len(references()))
    return line

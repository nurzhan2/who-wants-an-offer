"""How one profile is chosen among several active ones, as SQL.

Two questions, both answered here and nowhere else so they cannot drift:

* **the default profile** — the one a single-profile question is answered from:
  the active profile with the lowest ``priority``, newest on a tie;
* **a vacancy's best profile** — the active profile whose ``match`` row scores
  highest, ties broken the same way. See :mod:`app.matching.best` for why it is
  computed and not stored.

In ``app/db`` rather than in ``app/matching`` because repositories build their
statements from it, and repositories must not import scoring.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, ScalarSelect, Select, select
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.db.models import CandidateProfile, Match

#: How ties between active profiles are broken, everywhere: the owner's
#: priority first, then the newest upload. One tuple so that the default
#: profile, the list of active ones and the best-profile choice cannot disagree.
DEFAULT_ORDER = (CandidateProfile.priority.asc(), CandidateProfile.created_at.desc())


def default_profile_id_stmt() -> Select[tuple[UUID]]:
    """``SELECT id`` of the default profile, for callers that only need the id."""
    return (
        select(CandidateProfile.id)
        .where(CandidateProfile.is_active.is_(True))
        .order_by(*DEFAULT_ORDER)
        .limit(1)
    )


def _ranked_column(
    column: str, vacancy_id: ColumnElement[Any] | InstrumentedAttribute[Any]
) -> Select[Any]:
    """One column of the vacancy's matches with active profiles, best first."""
    row = aliased(Match)
    selected = row.profile_id if column == "profile_id" else row.score
    return (
        select(selected)
        .join(CandidateProfile, CandidateProfile.id == row.profile_id)
        .where(row.vacancy_id == vacancy_id, CandidateProfile.is_active.is_(True))
        .order_by(row.score.desc(), *DEFAULT_ORDER)
    )


def best_profile_id(
    vacancy_id: ColumnElement[Any] | InstrumentedAttribute[Any],
) -> ScalarSelect[Any]:
    """The id of the active profile this vacancy scores best with, as SQL.

    Correlated on ``vacancy_id``; NULL when no active profile has scored it.
    """
    return _ranked_column("profile_id", vacancy_id).limit(1).scalar_subquery()


def runner_up_score(
    vacancy_id: ColumnElement[Any] | InstrumentedAttribute[Any],
) -> ScalarSelect[Any]:
    """The second-best active profile's score for this vacancy, as SQL. NULL if none."""
    return _ranked_column("score", vacancy_id).offset(1).limit(1).scalar_subquery()


def is_best_match() -> ColumnElement[bool]:
    """``match.profile_id`` is the best profile of ``match.vacancy_id``."""
    return Match.profile_id == best_profile_id(Match.vacancy_id)

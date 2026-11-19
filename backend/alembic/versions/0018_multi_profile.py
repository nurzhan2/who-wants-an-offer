"""Several resumes live at once: a slug, a track, a priority and hh's resume id.

Revision ID: 0018_multi_profile
Revises: 0017_dashboard_confirmation
Create Date: 2026-11-19

Until now an upload retired every other profile, so the owner's six resumes on
hh — one per track — could only be scored one at a time, by re-uploading. Each
profile now carries:

* ``slug`` — the owner's handle for it (``backend``, ``llm``), unique, used by
  ``POST /resume/upload?replace=<slug>`` to say which one a new file replaces;
* ``track`` — free text, what kind of job this resume is for;
* ``priority`` — who wins a tie between two equal scores, lower first;
* ``hh_resume_id`` — the 38-character hash of the same resume on hh, which the
  agent must select in hh's response form. Its shape is checked by the API
  schema, not by a constraint, like every other typed field here. Nullable: a resume that is not on hh
  is still worth scoring, it just cannot be applied with automatically.

``is_active`` keeps its column and changes meaning only in that several rows may
now hold it.

**Data.** The newest active profile — there should be exactly one — becomes
``priority = 0`` with slug ``main``; every other row gets ``priority = 100`` and
a slug derived from its id, so nothing has to be renamed by hand and nothing is
deleted. ``slug`` is filled before it becomes NOT NULL and UNIQUE.

Reversible: the downgrade drops the four columns. It cannot restore "only one
active" on its own, so it leaves the lowest-priority active row active and
retires the rest, which is the state the old code expects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_multi_profile"
down_revision: str | None = "0017_dashboard_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the columns, then fill them from what is there."""
    op.add_column("candidate_profile", sa.Column("slug", sa.String(50), nullable=True))
    op.add_column("candidate_profile", sa.Column("track", sa.String(50), nullable=True))
    op.add_column(
        "candidate_profile",
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
    )
    op.add_column("candidate_profile", sa.Column("hh_resume_id", sa.String(38), nullable=True))

    # Every row first gets a slug nobody typed, derived from its own id: unique
    # by construction and recognisable on the screen as "not named yet".
    op.execute(
        "UPDATE candidate_profile SET slug = 'cv-' || substr(replace(id::text, '-', ''), 25, 8)"
    )
    # Then the live one is named and ranked first. Newest active, the same rule
    # every "active profile" query used until this revision.
    op.execute(
        """
        UPDATE candidate_profile SET priority = 0, slug = 'main'
        WHERE id = (
            SELECT id FROM candidate_profile WHERE is_active
            ORDER BY created_at DESC LIMIT 1
        )
        """
    )
    op.alter_column("candidate_profile", "slug", nullable=False)
    op.create_unique_constraint(op.f("uq_candidate_profile_slug"), "candidate_profile", ["slug"])


def downgrade() -> None:
    """Back to one active profile: keep the first by priority, retire the rest."""
    op.execute(
        """
        UPDATE candidate_profile SET is_active = false
        WHERE is_active AND id <> (
            SELECT id FROM candidate_profile WHERE is_active
            ORDER BY priority, created_at DESC LIMIT 1
        )
        """
    )
    op.drop_constraint(op.f("uq_candidate_profile_slug"), "candidate_profile", type_="unique")
    op.drop_column("candidate_profile", "hh_resume_id")
    op.drop_column("candidate_profile", "priority")
    op.drop_column("candidate_profile", "track")
    op.drop_column("candidate_profile", "slug")

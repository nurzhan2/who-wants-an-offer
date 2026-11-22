"""Keep the resume's projects, so a CV can show them as the references do.

Revision ID: 0021_profile_projects
Revises: 0020_experience_achievements
Create Date: 2026-11-22

Personal, open-source and hackathon projects are what the workshop research
(docs/WORKSHOP-RESEARCH.md, 4.7) says carries weight when commercial
experience is short, and every reference CV has a PROJECTS section. The
extraction read none of it. A JSONB list on the profile, like ``education``:
a short list of flat records — name, link, stack, year and the resume's own
bullets, verbatim — read whole and never queried by field.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_profile_projects"
down_revision: str | None = "0020_experience_achievements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate_profile",
        sa.Column(
            "projects",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("candidate_profile", "projects")

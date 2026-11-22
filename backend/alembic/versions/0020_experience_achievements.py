"""Keep each job's achievement bullets, so a generated CV has something to say.

Revision ID: 0020_experience_achievements
Revises: 0019_resume_file
Create Date: 2026-11-22

Until now ``profile_experience`` held company, title, dates, stack and domains,
and a tailored CV could only print those: every job came out as a title and a
line of technologies, with the owner's own results — the part of a resume an
employer actually reads — left behind in the uploaded file. The bullets are
stored verbatim from the resume, one string each, in the resume's order; a
generated CV chooses among them by index and never writes one.

``server_default '[]'`` because rows parsed before this revision exist and the
column is ``NOT NULL``: they simply have no achievements until the resume is
re-uploaded, which is the truthful state of affairs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_experience_achievements"
down_revision: str | None = "0019_resume_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profile_experience",
        sa.Column(
            "achievements",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("profile_experience", "achievements")

"""Keep the resume's one-line description of each employer.

Revision ID: 0022_experience_company_note
Revises: 0021_profile_projects
Create Date: 2026-11-23

A strong one-page CV says in one italic line what a little-known employer is
("B2B marketplace for industrial parts, 40k SKUs"): a recruiter outside the
market has never heard of the company, and the line is what makes the bullets
under it legible. The owner's own resume has one under every job and the
extraction dropped it. A nullable column, quoted verbatim like the bullets and
never written by a model; a profile parsed before this migration simply has
none, and its CV prints the company line without one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_experience_company_note"
down_revision: str | None = "0021_profile_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("profile_experience", sa.Column("company_note", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("profile_experience", "company_note")

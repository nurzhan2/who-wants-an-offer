"""Keep the uploaded resume file, so the worker can attach the owner's own CV.

Revision ID: 0019_resume_file
Revises: 0018_multi_profile
Create Date: 2026-11-21

Until now the file was read once and deleted; only its text survived. On the
server the worker answers a vacancy with a message and a CV, and the owner's own
designed resume for the winning track is the document to send. Nullable: rows
uploaded before this revision have no file until the owner replaces it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_resume_file"
down_revision: str | None = "0018_multi_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("candidate_profile", sa.Column("resume_file", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("candidate_profile", "resume_file")

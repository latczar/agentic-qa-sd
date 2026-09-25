"""tickets.human_instruction

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26

A person can now correct the agent and have it try again, instead of only
accepting or overruling what it produced. The correction has to survive the
round trip to the queue and back, so it lives on the ticket.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: the overwhelming majority of tickets are never
    # corrected, and "" would be indistinguishable from "corrected with an
    # empty string" when the prompt builder decides whether to include it.
    op.add_column("tickets", sa.Column("human_instruction", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "human_instruction")

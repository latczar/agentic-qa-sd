"""approval_decision gains HANDLED

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23

REJECTED was carrying two meanings at once: "the agent got this wrong" and
"never mind, I will deal with it myself". Only the first is a verdict on the
agent, so recording both the same way made the approvals log read as if the
agent had been wrong every time a person simply chose to do the work.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres 12+ permits ADD VALUE inside a transaction provided the new
    # value is not used in that same transaction, which it is not here. IF NOT
    # EXISTS so a re-run on a database that already has it is a no-op.
    op.execute("ALTER TYPE approval_decision ADD VALUE IF NOT EXISTS 'HANDLED'")


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type. Doing it properly means
    # creating a replacement type, rewriting every column that uses it and
    # dropping the old one, which is a lot of risk to retire one unused label.
    # Leaving it behind costs nothing.
    pass

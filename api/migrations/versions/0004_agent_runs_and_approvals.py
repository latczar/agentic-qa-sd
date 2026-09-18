"""agent_runs + approvals

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

The tables Phase 8 needs: a per-attempt engineering record of what the AI did,
and a permanent record of human approval decisions.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    postgresql.ENUM("SUCCEEDED", "FAILED", name="agent_run_status").create(bind, checkfirst=True)
    postgresql.ENUM("APPROVED", "REJECTED", name="approval_decision").create(bind, checkfirst=True)

    # create_type=False: the types are made above, and create_table would
    # otherwise try to make them a second time with no checkfirst guard.
    agent_run_status = postgresql.ENUM(
        "SUCCEEDED", "FAILED", name="agent_run_status", create_type=False
    )
    approval_decision = postgresql.ENUM(
        "APPROVED", "REJECTED", name="approval_decision", create_type=False
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("status", agent_run_status, nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("retrieved_slugs", postgresql.JSONB(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("decision", approval_decision, nullable=False),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("agent_runs")

    bind = op.get_bind()
    postgresql.ENUM(name="approval_decision").drop(bind, checkfirst=True)
    postgresql.ENUM(name="agent_run_status").drop(bind, checkfirst=True)

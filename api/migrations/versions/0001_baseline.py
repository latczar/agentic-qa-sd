"""baseline: users, services, tickets, ticket_comments, audit_logs, processed_events

Revision ID: 0001
Revises:
Create Date: 2026-09-17

This captures the schema exactly as it already existed from Phases 2-3,
created until now via Base.metadata.create_all() in app/create_tables.py.
Adopting Alembic doesn't change anything for a database that already has
this schema — see the README for how to bring an existing dev database up
to date with `alembic stamp head` instead of re-running this migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Create each enum type explicitly first...
    postgresql.ENUM("END_USER", "AGENT", "ADMIN", name="user_role").create(bind, checkfirst=True)
    postgresql.ENUM("OPERATIONAL", "DEGRADED", "OUTAGE", name="service_status").create(bind, checkfirst=True)
    postgresql.ENUM(
        "NEW", "QUEUED", "PROCESSING", "AWAITING_APPROVAL", "RESOLVED", "ESCALATED", "FAILED",
        name="ticket_status",
    ).create(bind, checkfirst=True)
    postgresql.ENUM("LOW", "MEDIUM", "HIGH", "CRITICAL", name="ticket_priority").create(bind, checkfirst=True)

    # ...then build separate instances with create_type=False for use as
    # column types below. Without this, create_table tries to CREATE TYPE a
    # second time as a side effect of adding an enum column, with no
    # checkfirst guard on that second attempt - it fails against a real
    # database with "type already exists".
    user_role = postgresql.ENUM("END_USER", "AGENT", "ADMIN", name="user_role", create_type=False)
    service_status = postgresql.ENUM("OPERATIONAL", "DEGRADED", "OUTAGE", name="service_status", create_type=False)
    ticket_status = postgresql.ENUM(
        "NEW", "QUEUED", "PROCESSING", "AWAITING_APPROVAL", "RESOLVED", "ESCALATED", "FAILED",
        name="ticket_status", create_type=False,
    )
    ticket_priority = postgresql.ENUM(
        "LOW", "MEDIUM", "HIGH", "CRITICAL", name="ticket_priority", create_type=False
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("role", user_role, nullable=False, server_default="END_USER"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", service_status, nullable=False, server_default="OPERATIONAL"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submitted_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", ticket_status, nullable=False, server_default="NEW"),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("priority", ticket_priority, nullable=True),
        sa.Column("affected_service_id", sa.Integer(), sa.ForeignKey("services.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "processed_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_table("audit_logs")
    op.drop_table("ticket_comments")
    op.drop_table("tickets")
    op.drop_table("services")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_name in ("ticket_priority", "ticket_status", "service_status", "user_role"):
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)

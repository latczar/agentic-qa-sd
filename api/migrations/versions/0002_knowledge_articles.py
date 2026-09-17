"""knowledge_articles + the pgvector extension

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18

The first real schema change since adopting Alembic - the reason Alembic
exists in this project at all.
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 768


def upgrade() -> None:
    # pgvector ships in the Postgres image, but an extension still has to be
    # switched on per database before its types exist. Without this line the
    # next statement fails with: type "vector" does not exist.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(160), nullable=False, unique=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # No vector index yet. With a few dozen articles Postgres scans them all in
    # under a millisecond, and an ivfflat index built on an empty table would be
    # actively worse than none. It gets added in Phase 5, once there are real
    # queries to measure it against.


def downgrade() -> None:
    op.drop_table("knowledge_articles")
    # The extension is deliberately left in place. Dropping it would break any
    # other database object that came to depend on it, and CREATE EXTENSION IF
    # NOT EXISTS makes re-running the upgrade harmless either way.

"""HNSW index on knowledge_articles.embedding

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18

Deliberately a separate migration from 0002, which created the table without
any index. There was nothing to search then; now there is.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # HNSW rather than IVFFlat. IVFFlat picks its cluster centres from the rows
    # present when the index is built, so building one on an empty or nearly
    # empty table gives a bad index that stays bad until it's rebuilt. HNSW has
    # no such training step - it stays correct as rows are added, which suits a
    # knowledge base that grows an article at a time.
    #
    # vector_cosine_ops because nomic-embed-text returns unit-length vectors and
    # retrieval compares them with cosine distance (<=>). An index built for one
    # distance operator is simply ignored by queries using another, so this has
    # to match shared/retrieval.py.
    op.execute(
        "CREATE INDEX knowledge_articles_embedding_hnsw "
        "ON knowledge_articles USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_articles_embedding_hnsw")

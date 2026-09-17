"""Finding the knowledge articles most likely to help with a ticket.

This is the R in RAG. It is deliberately plain Python running a plain SQL
query - the LLM does not decide what to retrieve, it only gets handed what
this function found. Small local models are unreliable at open-ended tool
orchestration, so the orchestration stays here where it is testable.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from shared.embeddings import EmbeddingProvider
from shared.models import KnowledgeArticle

# How many articles to put in front of the model. Three is enough to cover a
# question asked slightly the wrong way round without burying the real answer
# in a context window that a 7B model handles poorly.
DEFAULT_LIMIT = 3

# Cosine distance above which a result is treated as "not actually relevant".
# 0 is identical, 1 is unrelated, 2 is opposite.
#
# Measured, not guessed. Across ten questions against the shipped knowledge base
# (eight with a known correct article, two deliberately off-topic), the correct
# article scored 0.207-0.370 and everything else 0.348-0.711. The two ranges
# overlap slightly, so no threshold separates them perfectly. 0.40 sits above
# every correct match, which means the cost of being wrong is occasionally
# admitting a near-miss into a list of three - not silently dropping the right
# answer, which would be far worse and much harder to notice.
#
# This is the mechanism behind the "no RAG hits" case in the spec: better to
# tell the model it has nothing than to hand it something irrelevant and invite
# a confident wrong answer. Re-measure this if the embedding model changes -
# the numbers belong to nomic-embed-text, not to the idea.
MAX_DISTANCE = 0.40


@dataclass(frozen=True)
class RetrievedArticle:
    slug: str
    title: str
    body: str
    distance: float

    @property
    def similarity(self) -> float:
        """Distance expressed the way people read it: 1.0 is a perfect match."""
        return 1.0 - self.distance


def search_knowledge(
    db: Session,
    provider: EmbeddingProvider,
    query: str,
    limit: int = DEFAULT_LIMIT,
    max_distance: float = MAX_DISTANCE,
) -> list[RetrievedArticle]:
    """Return the articles closest in meaning to `query`, nearest first.

    Raises EmbeddingError if the query itself can't be embedded - that's a
    failure to answer, not an empty result, and callers need to tell the two
    apart.
    """
    query_vector = provider.embed(query)

    # cosine_distance maps to pgvector's <=> operator, which is what the HNSW
    # index in migration 0003 was built for. Using a different distance
    # function here would silently fall back to scanning every row.
    distance = KnowledgeArticle.embedding.cosine_distance(query_vector).label("distance")

    rows = (
        db.query(KnowledgeArticle, distance)
        # An article whose embedding never got written is invisible to search
        # rather than sorting to some arbitrary position.
        .filter(KnowledgeArticle.embedding.isnot(None))
        .order_by(distance)
        .limit(limit)
        .all()
    )

    return [
        RetrievedArticle(
            slug=article.slug,
            title=article.title,
            body=article.body,
            distance=float(article_distance),
        )
        for article, article_distance in rows
        if article_distance <= max_distance
    ]

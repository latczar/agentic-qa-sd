"""Retrieval tests.

The ordering tests use hand-made vectors rather than a model. Real embeddings
would make these tests depend on what nomic-embed-text happens to think this
week; pointing vectors along known axes means the expected distances are
arithmetic, and a failure means the query is wrong rather than the model having
an off day. Whether the real model puts the right article first is a question
about retrieval quality, and that's the marked ollama test at the bottom plus
the Phase 11 evaluation suite.
"""

import math

import pytest
from sqlalchemy.orm import Session

from shared.embeddings import EmbeddingError, OllamaEmbeddingProvider
from shared.models import EMBEDDING_DIMENSIONS, KnowledgeArticle
from shared.retrieval import search_knowledge


def axis_vector(index: int) -> list[float]:
    """A unit vector pointing along one axis. Two different axes are exactly
    1.0 apart in cosine distance - as unrelated as two vectors can be without
    pointing in opposite directions."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def blend(first: int, second: int) -> list[float]:
    """Halfway between two axes: cosine distance 1 - 1/sqrt(2) = 0.293 to each."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[first] = vector[second] = 1.0 / math.sqrt(2)
    return vector


class StubProvider:
    """Returns one fixed vector, whatever it's asked to embed."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed(self, text: str) -> list[float]:
        return self.vector


def add_article(db: Session, slug: str, embedding: list[float] | None) -> KnowledgeArticle:
    article = KnowledgeArticle(
        slug=slug,
        title=slug.replace("-", " ").capitalize(),
        body=f"Body of {slug}.",
        content_hash=f"hash-{slug}",
        embedding=embedding,
    )
    db.add(article)
    db.commit()
    return article


@pytest.fixture
def articles(db_session: Session):
    add_article(db_session, "exact-match", axis_vector(0))
    add_article(db_session, "near-match", blend(0, 1))
    add_article(db_session, "unrelated", axis_vector(2))
    return db_session


def test_returns_nearest_article_first(articles: Session):
    results = search_knowledge(articles, StubProvider(axis_vector(0)), "anything")

    assert [r.slug for r in results] == ["exact-match", "near-match"]
    assert results[0].distance == pytest.approx(0.0, abs=1e-6)
    assert results[1].distance == pytest.approx(0.293, abs=1e-3)

    # "unrelated" is a full 1.0 away, past MAX_DISTANCE, so it is dropped
    # rather than padding the results out to the limit.
    assert "unrelated" not in [r.slug for r in results]


def test_similarity_reads_the_friendly_way_round(articles: Session):
    results = search_knowledge(articles, StubProvider(axis_vector(0)), "anything")

    assert results[0].similarity == pytest.approx(1.0, abs=1e-6)
    assert results[0].similarity > results[1].similarity


def test_limit_caps_the_number_of_results(articles: Session):
    results = search_knowledge(articles, StubProvider(axis_vector(0)), "anything", limit=1)

    assert [r.slug for r in results] == ["exact-match"]


def test_max_distance_can_be_loosened(articles: Session):
    results = search_knowledge(
        articles, StubProvider(axis_vector(0)), "anything", max_distance=2.0
    )

    assert [r.slug for r in results] == ["exact-match", "near-match", "unrelated"]


def test_nothing_relevant_returns_empty_not_the_least_bad_article(articles: Session):
    # A query pointing at an axis no article uses. Everything is 1.0 away.
    results = search_knowledge(articles, StubProvider(axis_vector(500)), "anything")

    assert results == []


def test_articles_without_an_embedding_are_invisible(db_session: Session):
    add_article(db_session, "never-embedded", None)

    results = search_knowledge(
        db_session, StubProvider(axis_vector(0)), "anything", max_distance=2.0
    )

    assert results == []


def test_empty_knowledge_base_returns_empty(db_session: Session):
    assert search_knowledge(db_session, StubProvider(axis_vector(0)), "anything") == []


def test_query_that_cannot_be_embedded_raises(articles: Session):
    class FailingProvider:
        def embed(self, text: str) -> list[float]:
            raise EmbeddingError("Could not reach Ollama")

    # An unanswerable query must not look like "no articles matched" - the
    # caller has to be able to tell a retrieval failure from an empty result.
    with pytest.raises(EmbeddingError):
        search_knowledge(articles, FailingProvider(), "anything")


@pytest.mark.ollama
def test_real_embeddings_retrieve_the_right_article(db_session: Session):
    """The quality check the synthetic tests deliberately don't make.

    Skipped automatically when Ollama isn't available, and excluded in CI.
    """
    provider = OllamaEmbeddingProvider()
    try:
        provider.embed("warm-up")
    except EmbeddingError as exc:
        pytest.skip(f"Ollama not available: {exc}")

    for slug, text in [
        ("account-lockout", "Account keeps locking out every few minutes.\nA stale password is being retried somewhere."),
        ("printer-queue", "Print jobs stuck in the queue.\nThe print spooler has jammed on a malformed job."),
        ("vpn-auth", "VPN client fails after a password change.\nCached credentials in Credential Manager."),
    ]:
        add_article(db_session, slug, provider.embed(text))

    results = search_knowledge(db_session, provider, "my account locks itself every ten minutes")

    assert results, "expected at least one article within MAX_DISTANCE"
    assert results[0].slug == "account-lockout"

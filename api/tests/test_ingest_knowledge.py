"""Ingestion tests - all run against the fake embedding provider.

Nothing here needs Ollama running. What's under test is the plumbing around the
model call: does a row get written, is the expensive call skipped when nothing
changed, does one bad article take the rest down with it.
"""

import pytest
from sqlalchemy.orm import Session

from app.ingest_knowledge import KNOWLEDGE_DIR, content_hash, ingest_directory, parse_article
from shared.embeddings import EmbeddingError, FakeEmbeddingProvider
from shared.models import EMBEDDING_DIMENSIONS, KnowledgeArticle

ARTICLE_ONE = "# Printer offline\n\nTurn the printer off, then on again.\n"
ARTICLE_TWO = "# VPN drops on wifi\n\nMove closer to the access point.\n"


class CountingProvider(FakeEmbeddingProvider):
    """A fake that also records how many times it was asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return super().embed(text)


class BrokenProvider(FakeEmbeddingProvider):
    """Fails for one named slug's text, works for everything else."""

    def __init__(self, fail_on: str) -> None:
        super().__init__()
        self.fail_on = fail_on

    def embed(self, text: str) -> list[float]:
        if self.fail_on in text:
            raise EmbeddingError("Could not reach Ollama at http://localhost:11434")
        return super().embed(text)


@pytest.fixture
def knowledge_dir(tmp_path):
    (tmp_path / "printer-offline.md").write_text(ARTICLE_ONE, encoding="utf-8")
    (tmp_path / "vpn-drops.md").write_text(ARTICLE_TWO, encoding="utf-8")
    return tmp_path


def test_fake_provider_is_deterministic_and_right_width():
    provider = FakeEmbeddingProvider()
    first = provider.embed("account locked out")
    second = provider.embed("account locked out")
    other = provider.embed("printer jammed")

    assert first == second
    assert first != other
    assert len(first) == EMBEDDING_DIMENSIONS


def test_ingest_creates_articles_with_embeddings(db_session: Session, knowledge_dir):
    result = ingest_directory(db_session, FakeEmbeddingProvider(), knowledge_dir)

    assert sorted(result.created) == ["printer-offline", "vpn-drops"]
    assert result.updated == [] and result.failed == []

    article = db_session.query(KnowledgeArticle).filter_by(slug="printer-offline").one()
    assert article.title == "Printer offline"
    assert "off, then on again" in article.body
    assert article.embedding is not None
    assert len(article.embedding) == EMBEDDING_DIMENSIONS


def test_reingesting_unchanged_articles_makes_no_embedding_calls(db_session: Session, knowledge_dir):
    provider = CountingProvider()
    ingest_directory(db_session, provider, knowledge_dir)
    assert len(provider.calls) == 2

    second_run = ingest_directory(db_session, provider, knowledge_dir)

    # The point of content_hash: the second run costs nothing.
    assert len(provider.calls) == 2
    assert sorted(second_run.unchanged) == ["printer-offline", "vpn-drops"]
    assert second_run.created == [] and second_run.updated == []


def test_edited_article_is_updated_in_place_not_duplicated(db_session: Session, knowledge_dir):
    provider = CountingProvider()
    ingest_directory(db_session, provider, knowledge_dir)
    before = db_session.query(KnowledgeArticle).filter_by(slug="printer-offline").one()
    original_id, original_hash = before.id, before.content_hash

    (knowledge_dir / "printer-offline.md").write_text(
        "# Printer offline\n\nCheck the network cable before power cycling.\n", encoding="utf-8"
    )
    result = ingest_directory(db_session, provider, knowledge_dir)

    assert result.updated == ["printer-offline"]
    assert result.unchanged == ["vpn-drops"]
    assert len(provider.calls) == 3  # two initial, one re-embed, nothing for the untouched article

    after = db_session.query(KnowledgeArticle).filter_by(slug="printer-offline").one()
    assert after.id == original_id
    assert after.content_hash != original_hash
    assert db_session.query(KnowledgeArticle).count() == 2


def test_one_failing_article_does_not_abandon_the_others(db_session: Session, knowledge_dir):
    result = ingest_directory(db_session, BrokenProvider(fail_on="VPN drops"), knowledge_dir)

    assert result.created == ["printer-offline"]
    assert [slug for slug, _ in result.failed] == ["vpn-drops"]
    assert "Could not reach Ollama" in result.failed[0][1]

    # The successful article is committed, the failed one simply isn't there.
    assert db_session.query(KnowledgeArticle).count() == 1


def test_article_left_without_an_embedding_is_retried(db_session: Session, knowledge_dir):
    ingest_directory(db_session, BrokenProvider(fail_on="VPN drops"), knowledge_dir)

    # Simulate the other way an article can end up embedding-less: the row is
    # written but the vector isn't. A matching content_hash must not be enough
    # to mark it unchanged, or it would never be repaired.
    db_session.add(
        KnowledgeArticle(
            slug="vpn-drops",
            title="VPN drops on wifi",
            body="Move closer to the access point.",
            content_hash=content_hash("VPN drops on wifi", "Move closer to the access point."),
            embedding=None,
        )
    )
    db_session.commit()

    result = ingest_directory(db_session, FakeEmbeddingProvider(), knowledge_dir)

    assert result.updated == ["vpn-drops"]
    repaired = db_session.query(KnowledgeArticle).filter_by(slug="vpn-drops").one()
    assert repaired.embedding is not None


def test_malformed_article_is_reported_not_raised(db_session: Session, knowledge_dir):
    (knowledge_dir / "no-heading.md").write_text("Just some text with no title line.\n", encoding="utf-8")

    result = ingest_directory(db_session, FakeEmbeddingProvider(), knowledge_dir)

    assert sorted(result.created) == ["printer-offline", "vpn-drops"]
    assert [slug for slug, _ in result.failed] == ["no-heading"]
    assert "must start with" in result.failed[0][1]


def test_shipped_knowledge_articles_all_parse():
    """Guards the real knowledge/ directory, so a malformed article fails CI."""
    paths = sorted(KNOWLEDGE_DIR.glob("*.md"))
    assert paths, f"no markdown articles found in {KNOWLEDGE_DIR}"

    slugs = []
    for path in paths:
        slug, title, body = parse_article(path)
        assert title and body
        slugs.append(slug)

    assert len(slugs) == len(set(slugs))

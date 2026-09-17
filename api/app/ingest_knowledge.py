"""Load the knowledge base from markdown files into Postgres, with embeddings.

Run it as often as you like: articles that haven't changed since last time are
left alone, and no embedding is requested for them.

    python -m app.ingest_knowledge
"""

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from shared.db import SessionLocal
from shared.embeddings import EmbeddingError, EmbeddingProvider, get_embedding_provider
from shared.models import KnowledgeArticle

# knowledge/ sits at the repo root, beside api/ and worker/, because it is
# source data for the whole system rather than something the API owns.
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"


@dataclass
class IngestResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.updated)} updated, "
            f"{len(self.unchanged)} unchanged, {len(self.failed)} failed"
        )


def content_hash(title: str, body: str) -> str:
    """Fingerprint of an article's text, used to decide whether to re-embed.

    Title and body are joined with a newline that can't appear in a title, so
    moving text between the two fields still changes the hash.
    """
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def parse_article(path: Path) -> tuple[str, str, str]:
    """Split a markdown file into (slug, title, body).

    The filename is the slug and the first `# ` line is the title. Keeping the
    slug tied to the filename is what makes re-ingestion an update rather than
    an insert - rename the file and you get a new article, which is the
    behaviour you'd want anyway.
    """
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()

    if not lines or not lines[0].startswith("# "):
        raise ValueError(f"{path.name} must start with a '# Title' heading line")

    title = lines[0][2:].strip()
    body = "\n".join(lines[1:]).strip()

    if not title:
        raise ValueError(f"{path.name} has an empty title")
    if not body:
        raise ValueError(f"{path.name} has a title but no body")

    return path.stem, title, body


def ingest_directory(
    db: Session,
    provider: EmbeddingProvider,
    directory: Path = KNOWLEDGE_DIR,
) -> IngestResult:
    result = IngestResult()

    for path in sorted(directory.glob("*.md")):
        try:
            slug, title, body = parse_article(path)
        except ValueError as exc:
            result.failed.append((path.stem, str(exc)))
            continue

        new_hash = content_hash(title, body)
        article = db.query(KnowledgeArticle).filter_by(slug=slug).first()

        # The embedding check matters as much as the hash check: an article left
        # with a NULL embedding by an earlier failed run has an up-to-date hash
        # but is still useless for retrieval, so it must be retried rather than
        # counted as unchanged.
        if article and article.content_hash == new_hash and article.embedding is not None:
            result.unchanged.append(slug)
            continue

        try:
            embedding = provider.embed(f"{title}\n{body}")
        except EmbeddingError as exc:
            # One unreachable model call shouldn't abandon the other articles.
            # The failure is recorded and reported at the end with a non-zero
            # exit code, so a half-finished run is visible rather than silent.
            result.failed.append((slug, str(exc)))
            continue

        if article:
            article.title = title
            article.body = body
            article.content_hash = new_hash
            article.embedding = embedding
            result.updated.append(slug)
        else:
            db.add(
                KnowledgeArticle(
                    slug=slug,
                    title=title,
                    body=body,
                    content_hash=new_hash,
                    embedding=embedding,
                )
            )
            result.created.append(slug)

    # One commit for the whole run, not one per article. Either the batch of
    # successful articles lands or none of it does, and there's no window where
    # another process sees a half-written knowledge base.
    db.commit()
    return result


def main() -> int:
    if not KNOWLEDGE_DIR.is_dir():
        print(f"No knowledge directory at {KNOWLEDGE_DIR}", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        result = ingest_directory(db, get_embedding_provider())
    finally:
        db.close()

    print(result.summary())
    for slug, reason in result.failed:
        print(f"  failed: {slug}: {reason}", file=sys.stderr)

    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

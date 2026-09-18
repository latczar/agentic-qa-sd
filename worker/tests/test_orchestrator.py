"""Orchestration tests: real Postgres, fake model.

Everything the model would decide is scripted, so what's under test is the
pipeline around it - what gets retrieved, what gets persisted, which status
the ticket lands in, and what happens when a step fails.
"""

import json
import uuid

import pytest
from sqlalchemy.orm import Session

from app import orchestrator
from shared.embeddings import EmbeddingError, FakeEmbeddingProvider
from shared.llm import FakeLLMProvider
from shared.models import (
    AgentRun,
    AgentRunStatus,
    AuditLog,
    KnowledgeArticle,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
    User,
    UserRole,
)

GOOD_RESPONSE = json.dumps(
    {
        "category": "Authentication",
        "priority": "HIGH",
        "affected_service": "Identity Service",
        "likely_root_cause": "Session not invalidated after reset",
        "recommended_resolution": "Invalidate existing sessions and retry sign-in",
        "confidence": 0.93,
        "sources": [{"document": "password-reset", "title": "Resetting your password"}],
    }
)

LOW_CONFIDENCE_RESPONSE = json.dumps(
    {
        "category": "Unknown",
        "priority": "LOW",
        "affected_service": None,
        "likely_root_cause": "Not enough information",
        "recommended_resolution": "Ask the user for more detail",
        "confidence": 0.2,
        "sources": [{"document": "password-reset", "title": "Resetting your password"}],
    }
)


TICKET_SUBJECT = "Cannot log in after password reset"
TICKET_DESCRIPTION = "I reset my password but still cannot log in."
# What orchestrator.run embeds to search with: subject and description joined.
# The seeded article is embedded from this exact string so that retrieval
# genuinely returns it under FakeEmbeddingProvider, which is deterministic on
# its input but gives unrelated vectors for any two different inputs.
TICKET_QUERY = f"{TICKET_SUBJECT}\n{TICKET_DESCRIPTION}"


def make_ticket(db: Session) -> Ticket:
    """Each call gets a genuinely unique user.

    Worker tests commit for real and never roll back, so hand-picked emails
    have to stay unique across the entire suite, not just within one file -
    a uuid removes that coordination problem entirely.
    """
    email = f"orch-{uuid.uuid4()}@example.com"
    user = User(name="Test User", email=email, role=UserRole.END_USER)
    db.add(user)
    db.commit()
    db.refresh(user)

    ticket = Ticket(
        submitted_by_id=user.id,
        subject=TICKET_SUBJECT,
        description=TICKET_DESCRIPTION,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@pytest.fixture
def seeded(db_session: Session):
    """Get-or-create, not insert.

    Worker tests commit for real and never roll back (see conftest), so a
    fixture that blindly inserted the same unique service and article would
    pass alone and fail as soon as a second test ran.
    """
    if not db_session.query(Service).filter_by(name="Identity Service").first():
        db_session.add(Service(name="Identity Service", status=ServiceStatus.OPERATIONAL))

    # Embedded with the *ticket's* query text, not the article's own words.
    # FakeEmbeddingProvider hashes its input, so two different strings give two
    # unrelated vectors and retrieval returns nothing at all - which is how
    # these tests used to run. That went unnoticed while the gate only asked
    # whether a citation existed; once it started checking citations against
    # what was actually retrieved, GOOD_RESPONSE was citing a document the
    # model had never been shown, and the ticket correctly stopped resolving.
    provider = FakeEmbeddingProvider()
    matching_embedding = provider.embed(TICKET_QUERY)

    article = db_session.query(KnowledgeArticle).filter_by(slug="password-reset").first()
    if article is None:
        db_session.add(
            KnowledgeArticle(
                slug="password-reset",
                title="Resetting your password",
                body="Use the self-service portal.",
                content_hash="hash-password-reset",
                embedding=matching_embedding,
            )
        )
    else:
        # Worker tests commit for real, so a row seeded by an earlier run
        # persists. Set it every time rather than only on insert.
        article.embedding = matching_embedding

    db_session.commit()
    return db_session


def test_confident_answer_resolves_the_ticket_and_records_the_run(seeded: Session):
    ticket = make_ticket(seeded)

    analysis = orchestrator.run(
        seeded,
        ticket,
        FakeEmbeddingProvider(),
        FakeLLMProvider([GOOD_RESPONSE]),
    )
    seeded.commit()

    assert analysis.category == "Authentication"
    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.priority.value == "HIGH"
    # The service name was matched back to a real row rather than stored as text.
    assert ticket.affected_service_id is not None

    run = seeded.query(AgentRun).filter_by(ticket_id=ticket.id).one()
    assert run.status == AgentRunStatus.SUCCEEDED
    assert run.confidence == pytest.approx(0.93)
    assert run.output["category"] == "Authentication"


def test_low_confidence_routes_to_human_approval(seeded: Session):
    ticket = make_ticket(seeded)

    orchestrator.run(
        seeded, ticket, FakeEmbeddingProvider(), FakeLLMProvider([LOW_CONFIDENCE_RESPONSE])
    )
    seeded.commit()

    assert ticket.status == TicketStatus.AWAITING_APPROVAL

    events = [a.event_type for a in seeded.query(AuditLog).filter_by(ticket_id=ticket.id).all()]
    assert "human_approval_requested" in events


def test_invented_service_name_is_not_forced_onto_the_ticket(seeded: Session):
    ticket = make_ticket(seeded)
    response = json.loads(GOOD_RESPONSE)
    response["affected_service"] = "Totally Made Up Service"

    orchestrator.run(
        seeded, ticket, FakeEmbeddingProvider(), FakeLLMProvider([json.dumps(response)])
    )
    seeded.commit()

    # No matching row, so the link is left empty rather than pointing at
    # something wrong or inventing a service.
    assert ticket.affected_service_id is None


def test_malformed_then_valid_response_still_succeeds(seeded: Session):
    ticket = make_ticket(seeded)

    analysis = orchestrator.run(
        seeded,
        ticket,
        FakeEmbeddingProvider(),
        FakeLLMProvider(["{not json at all", GOOD_RESPONSE]),
    )
    seeded.commit()

    assert analysis.category == "Authentication"
    assert ticket.status == TicketStatus.RESOLVED


def test_model_never_returning_valid_json_is_retryable_and_recorded(seeded: Session):
    ticket = make_ticket(seeded)

    with pytest.raises(orchestrator.RetryableOrchestrationError):
        orchestrator.run(
            seeded,
            ticket,
            FakeEmbeddingProvider(),
            FakeLLMProvider(["nope", "still nope", "nope again"]),
        )

    # Written on its own session, so it survives the caller's rollback.
    seeded.rollback()
    run = seeded.query(AgentRun).filter_by(ticket_id=ticket.id).one()
    assert run.status == AgentRunStatus.FAILED
    assert "valid analysis" in run.error


def test_retrieval_failure_is_retryable_not_an_empty_result(seeded: Session):
    ticket = make_ticket(seeded)

    class FailingEmbeddings:
        def embed(self, text: str) -> list[float]:
            raise EmbeddingError("Could not reach Ollama")

    with pytest.raises(orchestrator.RetryableOrchestrationError):
        orchestrator.run(seeded, ticket, FailingEmbeddings(), FakeLLMProvider([GOOD_RESPONSE]))

    seeded.rollback()
    run = seeded.query(AgentRun).filter_by(ticket_id=ticket.id).one()
    assert run.status == AgentRunStatus.FAILED
    assert "retrieval failed" in run.error

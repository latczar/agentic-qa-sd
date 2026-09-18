"""What the model is and isn't allowed to do through MCP.

The tools are called directly rather than over the protocol: what's under test
is the behaviour of each tool - what it returns, what it refuses, what it
clamps - not the MCP transport, which is the SDK's job.
"""

import uuid

import pytest

from app.server import get_service_status, get_ticket, search_knowledge_base
from shared.embeddings import FakeEmbeddingProvider
from shared.models import KnowledgeArticle, Service, ServiceStatus, Ticket, User, UserRole


def _ticket(db) -> Ticket:
    user = User(name="T", email=f"mcp-{uuid.uuid4()}@example.com", role=UserRole.END_USER)
    db.add(user)
    db.commit()
    db.refresh(user)
    ticket = Ticket(submitted_by_id=user.id, subject="Printer jammed", description="It is stuck.")
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def test_get_ticket_returns_the_ticket(db_session):
    ticket = _ticket(db_session)

    result = get_ticket(ticket.id)

    assert result["id"] == ticket.id
    assert result["subject"] == "Printer jammed"


def test_get_ticket_for_a_missing_id_reports_an_error_rather_than_raising(db_session):
    result = get_ticket(999_999)

    assert "error" in result


def test_unknown_service_returns_the_list_of_real_ones(db_session):
    name = f"Known Service {uuid.uuid4()}"
    db_session.add(Service(name=name, status=ServiceStatus.OPERATIONAL))
    db_session.commit()

    result = get_service_status("Totally Made Up Service")

    # This is what stops the model inventing service names: it gets told what
    # actually exists instead of a bare failure.
    assert "error" in result
    assert name in result["known_services"]


def test_known_service_reports_its_status(db_session):
    name = f"Status Service {uuid.uuid4()}"
    db_session.add(Service(name=name, status=ServiceStatus.DEGRADED))
    db_session.commit()

    assert get_service_status(name)["status"] == "DEGRADED"


@pytest.mark.parametrize("requested,expected_max", [(100, 10), (0, 10), (-5, 10)])
def test_search_limit_is_clamped(db_session, requested, expected_max):
    provider = FakeEmbeddingProvider()
    for i in range(12):
        slug = f"clamp-{uuid.uuid4()}"
        db_session.add(
            KnowledgeArticle(
                slug=slug,
                title=f"Article {i}",
                body="Body text.",
                content_hash=slug,
                embedding=provider.embed(slug),
            )
        )
    db_session.commit()

    results = search_knowledge_base("anything", limit=requested)

    # An unbounded limit from the model would quietly turn a bounded context
    # into the entire knowledge base.
    assert len(results) <= expected_max


def test_search_returning_nothing_is_an_empty_list_not_an_error(db_session):
    results = search_knowledge_base(f"completely unrelated {uuid.uuid4()}")

    assert isinstance(results, list)

"""Guarding an already-decided ticket against a duplicate publish.

Regression for something observed in the real system, not a hypothetical: the
same ticket was published twice under two different event ids. The consumer's
idempotency check is keyed on event_id, so both were legitimately new messages
and both ran - and the second turned a ticket that was AWAITING_APPROVAL into
an auto-recommended one. A duplicate publish silently cancelled a human review.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.processing import process_ticket
from shared.models import AuditLog, Ticket, TicketStatus, User, UserRole


def _ticket(db: Session, status: TicketStatus) -> Ticket:
    user = User(name="T", email=f"proc-{uuid.uuid4()}@example.com", role=UserRole.END_USER)
    db.add(user)
    db.commit()
    db.refresh(user)

    ticket = Ticket(
        submitted_by_id=user.id, subject="Already decided", description="...", status=status
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@pytest.mark.parametrize(
    "status",
    [TicketStatus.AWAITING_APPROVAL, TicketStatus.RESOLVED, TicketStatus.ESCALATED],
)
def test_an_already_decided_ticket_is_not_reanalysed(db_session: Session, status):
    ticket = _ticket(db_session, status)

    # No providers are configured here: if this tried to analyse, it would fail
    # trying to reach a model rather than returning quietly.
    process_ticket(db_session, ticket)
    db_session.commit()

    assert ticket.status == status
    events = [a.event_type for a in db_session.query(AuditLog).filter_by(ticket_id=ticket.id).all()]
    assert events == ["reanalysis_skipped"]

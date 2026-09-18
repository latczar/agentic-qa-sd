"""The approvals log.

Exists so a ticket list can distinguish "the AI recommended this" from "a
person signed this off". Both show as RESOLVED otherwise, and that is the one
distinction a human-in-the-loop system must not hide - it was invisible in the
UI until a screenshot made it obvious.
"""

import uuid

from shared.models import Ticket, TicketStatus, User, UserRole


def _awaiting(db_session) -> tuple[Ticket, User]:
    user = User(name="Dana Reeve", email=f"log-{uuid.uuid4()}@example.com", role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    ticket = Ticket(
        submitted_by_id=user.id,
        subject="Needs a decision",
        description="...",
        status=TicketStatus.AWAITING_APPROVAL,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket, user


def test_empty_when_nothing_has_been_decided(client):
    assert client.get("/approvals").status_code == 200


def test_an_approval_appears_with_the_decider_name_resolved(client, db_session):
    ticket, user = _awaiting(db_session)
    client.post(
        f"/tickets/{ticket.id}/approve",
        json={"decided_by_id": user.id, "reason": "Checked the lock status myself"},
    )

    entry = next(a for a in client.get("/approvals").json() if a["ticket_id"] == ticket.id)

    assert entry["decision"] == "APPROVED"
    # The name, not just the id - the list has to render it without a second lookup.
    assert entry["decided_by_name"] == "Dana Reeve"
    assert entry["reason"] == "Checked the lock status myself"


def test_an_anonymous_decision_still_appears(client, db_session):
    ticket, _ = _awaiting(db_session)
    client.post(f"/tickets/{ticket.id}/reject", json={})

    entry = next(a for a in client.get("/approvals").json() if a["ticket_id"] == ticket.id)

    # decided_by_id is optional, so the join must not drop the row.
    assert entry["decision"] == "REJECTED"
    assert entry["decided_by_name"] is None

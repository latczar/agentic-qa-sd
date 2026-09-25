"""Correcting the agent and sending the ticket round again.

The point of this route is that "the agent got it wrong" and "here is what it
should have said" are different things to be able to say. Reject records the
first and stops; instruct records the second and produces a new attempt.
"""

from unittest.mock import patch

from shared.models import AuditLog, Ticket, TicketStatus, User, UserRole


def _ticket(db_session, email: str, status: TicketStatus = TicketStatus.AWAITING_APPROVAL) -> Ticket:
    user = User(name="Test User", email=email, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    ticket = Ticket(
        submitted_by_id=user.id,
        subject="Needs a steer",
        description="...",
        status=status,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def test_instructing_stores_the_steer_and_requeues(client, db_session):
    ticket = _ticket(db_session, "steer@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/instruct",
        json={"instruction": "This is a network fault, not access", "instructed_by_id": ticket.submitted_by_id},
    )

    # 202, not 201: the ticket has been put back on the queue, and nothing is
    # finished until a worker picks it up.
    assert response.status_code == 202

    db_session.refresh(ticket)
    assert ticket.human_instruction == "This is a network fault, not access"
    # QUEUED specifically. The worker refuses to re-analyse anything sitting in
    # AWAITING_APPROVAL, so a ticket left in that state would be published and
    # then silently skipped.
    assert ticket.status == TicketStatus.QUEUED


def test_an_escalated_ticket_can_be_reopened_by_an_instruction(client, db_session):
    """Escalation used to be terminal. This is the way back out of it."""
    ticket = _ticket(db_session, "escalated@example.com", TicketStatus.ESCALATED)

    response = client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": "Try again, it is the VPN"})

    assert response.status_code == 202
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.QUEUED


def test_a_resolved_ticket_cannot_be_reopened_by_an_instruction(client, db_session):
    """Somebody decided this one. Quietly undoing that is not on offer."""
    ticket = _ticket(db_session, "closed@example.com", TicketStatus.RESOLVED)

    response = client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": "Have another go"})

    assert response.status_code == 409
    assert "RESOLVED" in response.json()["detail"]
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED


def test_an_empty_instruction_is_rejected(client, db_session):
    """Re-running with nothing changed costs a model call and changes nothing."""
    ticket = _ticket(db_session, "empty@example.com")

    assert client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": ""}).status_code == 422

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_APPROVAL


def test_the_latest_instruction_replaces_the_previous_one(client, db_session):
    ticket = _ticket(db_session, "twice@example.com")

    client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": "It is a network fault"})
    db_session.refresh(ticket)
    ticket.status = TicketStatus.AWAITING_APPROVAL  # as though the worker had come back round
    db_session.commit()
    client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": "Actually it is an access fault"})

    db_session.refresh(ticket)
    # Only the current steer goes in the prompt; contradictory corrections
    # stacked together would make the result harder to reason about.
    assert ticket.human_instruction == "Actually it is an access fault"

    # The history is not lost, it just lives where history belongs.
    said = [
        row.detail["instruction"]
        for row in db_session.query(AuditLog)
        .filter(AuditLog.ticket_id == ticket.id, AuditLog.event_type == "human_instructed")
        .all()
    ]
    assert said == ["It is a network fault", "Actually it is an access fault"]


def test_a_failed_publish_puts_the_ticket_back_rather_than_stranding_it(client, db_session):
    """The dangerous failure: QUEUED, but no message and therefore no worker.

    A ticket in that state is waiting for something that will never arrive,
    and nothing would ever route it back to a person.
    """
    ticket = _ticket(db_session, "broker-down@example.com")

    with patch(
        "app.routers.tickets.publish_ticket_created",
        side_effect=RuntimeError("broker unreachable"),
    ):
        response = client.post(f"/tickets/{ticket.id}/instruct", json={"instruction": "Try again"})

    assert response.status_code == 503
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.AWAITING_APPROVAL

    events = {
        row.event_type
        for row in db_session.query(AuditLog).filter(AuditLog.ticket_id == ticket.id).all()
    }
    assert "ticket_queue_failed" in events


def test_instructing_a_missing_ticket_is_404(client):
    assert client.post("/tickets/999999/instruct", json={"instruction": "hello"}).status_code == 404

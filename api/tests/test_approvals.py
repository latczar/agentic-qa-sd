"""Approval endpoints - the human half of human-in-the-loop."""

from shared.models import (
    AgentRun,
    AgentRunStatus,
    Approval,
    ApprovalDecision,
    AuditLog,
    Ticket,
    TicketStatus,
    User,
    UserRole,
)


def _awaiting_ticket(db_session, email: str) -> Ticket:
    user = User(name="Test User", email=email, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    ticket = Ticket(
        submitted_by_id=user.id,
        subject="Needs a human",
        description="...",
        status=TicketStatus.AWAITING_APPROVAL,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def test_approving_resolves_the_ticket_and_records_who_decided(client, db_session):
    ticket = _awaiting_ticket(db_session, "approver@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/approve",
        json={"decided_by_id": ticket.submitted_by_id, "reason": "Checked, looks right"},
    )

    assert response.status_code == 201
    assert response.json()["decision"] == "APPROVED"

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED
    approval = db_session.query(Approval).filter_by(ticket_id=ticket.id).one()
    assert approval.decision == ApprovalDecision.APPROVED
    assert approval.reason == "Checked, looks right"


def test_rejecting_escalates_rather_than_closing(client, db_session):
    ticket = _awaiting_ticket(db_session, "rejecter@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/reject", json={"reason": "Resolution would lock the user out"}
    )

    assert response.status_code == 201
    db_session.refresh(ticket)
    # A human disagreeing means the ticket still needs solving, by someone else.
    assert ticket.status == TicketStatus.ESCALATED


def test_handling_it_yourself_closes_the_ticket_without_blaming_the_agent(client, db_session):
    ticket = _awaiting_ticket(db_session, "handler@example.com")

    response = client.post(
        f"/tickets/{ticket.id}/handled",
        json={"decided_by_id": ticket.submitted_by_id, "reason": "Rang them, sorted in a minute"},
    )

    assert response.status_code == 201
    assert response.json()["decision"] == "HANDLED"

    db_session.refresh(ticket)
    # The work is done, so the ticket closes like an approval does.
    assert ticket.status == TicketStatus.RESOLVED
    approval = db_session.query(Approval).filter_by(ticket_id=ticket.id).one()
    # But it is not an approval: nobody said the agent's answer was right.
    assert approval.decision == ApprovalDecision.HANDLED
    assert approval.decision is not ApprovalDecision.APPROVED


def test_handling_is_recorded_separately_from_rejection(client, db_session):
    """The whole point of the third route: these two must stay distinguishable.

    Before this existed, closing a ticket by hand meant pressing Reject, which
    left a permanent record saying the agent was wrong when nobody had claimed
    that. The audit event has to separate them too, not just the enum.
    """
    handled = _awaiting_ticket(db_session, "sorted@example.com")
    rejected = _awaiting_ticket(db_session, "wrong@example.com")

    client.post(f"/tickets/{handled.id}/handled", json={})
    client.post(f"/tickets/{rejected.id}/reject", json={})

    db_session.refresh(handled)
    db_session.refresh(rejected)
    assert handled.status == TicketStatus.RESOLVED
    assert rejected.status == TicketStatus.ESCALATED

    events = {
        row.ticket_id: row.event_type
        for row in db_session.query(AuditLog)
        .filter(
            AuditLog.ticket_id.in_([handled.id, rejected.id]),
            AuditLog.event_type.like("human_%"),
        )
        .all()
    }
    assert events[handled.id] == "human_handled"
    assert events[rejected.id] == "human_rejected"


def test_cannot_handle_a_ticket_that_is_not_awaiting_approval(client, db_session):
    ticket = _awaiting_ticket(db_session, "alreadydone@example.com")
    ticket.status = TicketStatus.RESOLVED
    db_session.commit()

    # Same guard as approve and reject: a decided ticket cannot be decided again.
    assert client.post(f"/tickets/{ticket.id}/handled", json={}).status_code == 409


def test_cannot_approve_a_ticket_that_is_not_awaiting_approval(client, db_session):
    ticket = _awaiting_ticket(db_session, "wrongstate@example.com")
    ticket.status = TicketStatus.RESOLVED
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}/approve", json={})

    assert response.status_code == 409
    assert "RESOLVED" in response.json()["detail"]


def test_approving_a_missing_ticket_is_404(client):
    assert client.post("/tickets/999999/approve", json={}).status_code == 404


def test_analysis_endpoint_exposes_what_the_ai_did(client, db_session):
    ticket = _awaiting_ticket(db_session, "analysis@example.com")
    db_session.add(
        AgentRun(
            ticket_id=ticket.id,
            status=AgentRunStatus.SUCCEEDED,
            model="qwen2.5:7b-instruct",
            attempts=1,
            latency_ms=1234,
            retrieved_slugs={"slugs": ["vpn-connection-failures"]},
            output={"category": "Network", "confidence": 0.9},
            confidence=0.9,
        )
    )
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/analysis")

    assert response.status_code == 200
    runs = response.json()
    assert len(runs) == 1
    assert runs[0]["model"] == "qwen2.5:7b-instruct"
    assert runs[0]["retrieved_slugs"]["slugs"] == ["vpn-connection-failures"]

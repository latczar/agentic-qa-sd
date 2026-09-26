"""The overseer's endpoints, and one real round trip through RabbitMQ.

The stream endpoint itself is not driven through TestClient: an endless
response and a test client's disconnect handling are a poor match, and a test
that hangs is worse than none. The part worth proving is the pump - that an
event published by a worker really reaches a subscriber - and that is tested
directly against the real broker, the same one test_ticket_queue_integration
uses.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pika

from app.routers import overseer
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
from shared.progress import WORKER_PROGRESS_EXCHANGE, declare_progress_exchange, make_event
from shared.rabbitmq import get_connection


def test_the_page_and_its_assets_are_served(client):
    page = client.get("/overseer")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    # The script and styles live beside the page rather than inline, so the
    # page is only whole if both are actually served where it asks for them.
    assert '<script src="/static/overseer.js">' in page.text
    assert '<link rel="stylesheet" href="/static/overseer.css">' in page.text

    script = client.get("/static/overseer.js")
    assert script.status_code == 200
    assert 'new EventSource("/overseer/stream")' in script.text
    assert client.get("/static/overseer.css").status_code == 200


def test_the_pages_and_their_assets_are_revalidated_not_served_stale(client):
    """No build step fingerprints the file names, so every load must check
    for a newer copy, or a browser shows an old script with a new page."""
    for path in ("/", "/overseer", "/static/overseer.js", "/static/overseer.css"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path


def test_recent_returns_the_newest_rows_with_their_subject(client, db_session):
    user = User(name="Test User", email="overseer-recent@example.com", role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    ticket = Ticket(submitted_by_id=user.id, subject="Printer queue is stuck", description="...")
    db_session.add(ticket)
    db_session.commit()
    for event_type in ("ticket_created", "ticket_queued", "worker_started"):
        db_session.add(AuditLog(ticket_id=ticket.id, event_type=event_type))
    db_session.commit()

    rows = client.get("/overseer/recent?limit=3").json()

    assert [r["event_type"] for r in rows] == ["worker_started", "ticket_queued", "ticket_created"]
    assert {r["subject"] for r in rows} == {"Printer queue is stuck"}


def test_recent_refuses_an_unreasonable_limit(client):
    assert client.get("/overseer/recent?limit=5000").status_code == 422


def test_sse_frames_are_in_the_format_eventsource_expects():
    frame = overseer.sse("progress", {"step": "picked_up", "ticket_id": 3})

    assert frame.startswith("event: progress\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1]) == {"step": "picked_up", "ticket_id": 3}


def test_an_unreachable_broker_is_reported_rather_than_raised():
    pushed = []
    with patch.object(overseer, "get_connection", side_effect=pika.exceptions.AMQPConnectionError("down")):
        overseer.pump(threading.Event(), lambda event, data: pushed.append((event, data)))

    assert pushed[0][0] == "unavailable"
    assert "unreachable" in pushed[0][1]["detail"]


def test_a_published_progress_event_reaches_a_subscriber():
    """End to end through the real broker: worker publishes, pump relays."""
    pushed: list[tuple[str, object]] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=overseer.pump, args=(stop, lambda event, data: pushed.append((event, data))), daemon=True
    )
    thread.start()

    def wait_for(predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    try:
        # The first "queues" push means the private queue is declared and
        # bound. Publishing before that would go to nobody - it is a fanout,
        # and a fanout keeps nothing for subscribers who arrive late.
        assert wait_for(lambda: any(e == "queues" for e, _ in pushed)), f"pump never came up: {pushed}"

        marker = str(uuid.uuid4())
        connection = get_connection()
        try:
            channel = connection.channel()
            declare_progress_exchange(channel)
            channel.basic_publish(
                exchange=WORKER_PROGRESS_EXCHANGE,
                routing_key="",
                body=json.dumps(make_event("test-worker", "picked_up", 1, {"marker": marker})),
            )
        finally:
            connection.close()

        assert wait_for(
            lambda: any(e == "progress" and d["detail"].get("marker") == marker for e, d in pushed)
        ), f"event never arrived: {pushed}"
    finally:
        stop.set()
        thread.join(timeout=5)

    depths = next(d for e, d in pushed if e == "queues")
    assert {q["name"] for q in depths} == {"ticket.processing", "ticket.retry", "ticket.dead-letter"}
    assert not thread.is_alive(), "pump did not stop when told to"


# --- The board --------------------------------------------------------------

def _board_ticket(db_session, email, status, waited=timedelta(0)):
    user = User(name="Priya Raman", email=email, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    ticket = Ticket(
        submitted_by_id=user.id,
        subject=f"Board {email}",
        description="...",
        status=status,
        updated_at=datetime.now(timezone.utc) - waited,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket, user


def test_a_waiting_card_carries_everything_needed_to_decide_it(client, db_session):
    ticket, _ = _board_ticket(db_session, "card@example.com", TicketStatus.AWAITING_APPROVAL)
    db_session.add(
        AgentRun(
            ticket_id=ticket.id,
            status=AgentRunStatus.SUCCEEDED,
            model="qwen2.5:7b-instruct",
            latency_ms=900,
            output={
                "category": "Access",
                "priority": "MEDIUM",
                "confidence": 0.62,
                "likely_root_cause": "Not in the payroll group",
                "recommended_resolution": "Add the user to the payroll viewers group",
                "sources": [{"document": "payroll-portal-access-denied", "title": "Payroll portal access denied"}],
            },
            confidence=0.62,
        )
    )
    db_session.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="human_approval_requested",
            detail={"reason": "sensitive subject (payroll) always needs human approval"},
        )
    )
    db_session.commit()

    card = next(c for c in client.get("/overseer/board").json()["waiting"] if c["id"] == ticket.id)

    assert card["suggestion"] == "Add the user to the payroll viewers group"
    assert card["reason"].startswith("sensitive subject (payroll)")
    assert card["confidence"] == 0.62
    assert card["sources"][0]["title"] == "Payroll portal access denied"


def test_a_ticket_screened_before_analysis_has_a_reason_but_no_suggestion(client, db_session):
    ticket, _ = _board_ticket(db_session, "screened@example.com", TicketStatus.AWAITING_APPROVAL)
    db_session.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="human_approval_requested",
            detail={"reason": "ticket text contains analyser-directed instructions ('set confidence')"},
        )
    )
    db_session.commit()

    card = next(c for c in client.get("/overseer/board").json()["waiting"] if c["id"] == ticket.id)

    # No run happened, so there is nothing to approve - shown as such rather
    # than as an empty suggestion someone might wave through.
    assert card["suggestion"] is None
    assert "analyser-directed" in card["reason"]


def test_the_longest_waiting_ticket_comes_first(client, db_session):
    newer, _ = _board_ticket(db_session, "newer@example.com", TicketStatus.AWAITING_APPROVAL, timedelta(minutes=5))
    older, _ = _board_ticket(db_session, "older@example.com", TicketStatus.AWAITING_APPROVAL, timedelta(hours=2))

    ids = [c["id"] for c in client.get("/overseer/board").json()["waiting"]]

    assert ids.index(older.id) < ids.index(newer.id)


def test_solved_tickets_say_whether_a_person_or_the_agent_solved_them(client, db_session):
    by_agent, _ = _board_ticket(db_session, "auto@example.com", TicketStatus.RESOLVED)
    by_person, person = _board_ticket(db_session, "human@example.com", TicketStatus.RESOLVED)
    db_session.add(Approval(ticket_id=by_person.id, decision=ApprovalDecision.APPROVED, decided_by_id=person.id))
    db_session.commit()

    board = client.get("/overseer/board").json()
    decided = {o["id"]: o["decided_by"] for o in board["solved"]}

    assert decided[by_agent.id] is None
    assert decided[by_person.id] == "Priya Raman"
    assert board["solved_today"] == 2
    assert board["solved_automatically_today"] == 1


def test_all_time_totals_count_every_solved_ticket_not_just_today(client, db_session):
    _board_ticket(db_session, "last-week@example.com", TicketStatus.RESOLVED, waited=timedelta(days=6))
    helped, person = _board_ticket(db_session, "helped@example.com", TicketStatus.RESOLVED, waited=timedelta(days=9))
    db_session.add(Approval(ticket_id=helped.id, decision=ApprovalDecision.APPROVED, decided_by_id=person.id))
    # Not solved, so neither counts, approval or not.
    _board_ticket(db_session, "failed@example.com", TicketStatus.FAILED)
    waiting, waiter = _board_ticket(db_session, "waiting@example.com", TicketStatus.AWAITING_APPROVAL)
    db_session.add(Approval(ticket_id=waiting.id, decision=ApprovalDecision.APPROVED, decided_by_id=waiter.id))
    db_session.commit()

    board = client.get("/overseer/board").json()

    assert board["solved_today"] == 0
    assert board["solved_total"] == 2
    assert board["solved_automatically_total"] == 1

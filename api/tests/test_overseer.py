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
from unittest.mock import patch

import pika

from app.routers import overseer
from shared.models import AuditLog, Ticket, User, UserRole
from shared.progress import WORKER_PROGRESS_EXCHANGE, declare_progress_exchange, make_event
from shared.rabbitmq import get_connection


def test_the_page_is_served(client):
    response = client.get("/overseer")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "EventSource" in response.text


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

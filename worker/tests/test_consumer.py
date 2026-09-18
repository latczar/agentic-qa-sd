import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.consumer import MAX_RETRIES, on_message
from app.processing import RetryableProcessingError
from shared.models import ProcessedEvent, Ticket, TicketStatus, User, UserRole
from shared.rabbitmq import TICKET_DEAD_LETTER_QUEUE, TICKET_RETRY_QUEUE


def _make_ticket(db_session, email: str) -> Ticket:
    user = User(name="Test User", email=email, role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    ticket = Ticket(submitted_by_id=user.id, subject="Test ticket", description="...")
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _method():
    return SimpleNamespace(delivery_tag=1)


def _payload(ticket_id: int, retry_count: int = 0) -> dict:
    return {"event_id": str(uuid.uuid4()), "ticket_id": ticket_id, "retry_count": retry_count}


def test_happy_path_acks_and_records_processed_event(db_session):
    ticket = _make_ticket(db_session, "happy@example.com")
    payload = _payload(ticket.id)
    channel = MagicMock()

    # An explicit stand-in for the work itself: this test is about queue
    # plumbing (ack, idempotency record), not about the AI pipeline that the
    # real process_ticket now runs. Orchestration has its own tests.
    def mark_processing(db, t):
        t.status = TicketStatus.PROCESSING

    on_message(channel, _method(), None, json.dumps(payload).encode(), process_fn=mark_processing)

    channel.basic_ack.assert_called_once_with(1)
    channel.basic_publish.assert_not_called()

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.PROCESSING
    assert db_session.query(ProcessedEvent).filter_by(event_id=payload["event_id"]).first() is not None


def test_duplicate_event_is_skipped(db_session):
    ticket = _make_ticket(db_session, "duplicate@example.com")
    payload = _payload(ticket.id)
    db_session.add(ProcessedEvent(event_id=payload["event_id"], ticket_id=ticket.id))
    db_session.commit()

    channel = MagicMock()
    calls = []
    on_message(
        channel,
        _method(),
        None,
        json.dumps(payload).encode(),
        process_fn=lambda db, t: calls.append(t.id),
    )

    channel.basic_ack.assert_called_once_with(1)
    assert calls == []  # process_fn never ran — this is the point of idempotency


def test_ticket_not_found_is_dead_lettered_immediately(db_session):
    payload = _payload(ticket_id=999_999)
    channel = MagicMock()

    on_message(channel, _method(), None, json.dumps(payload).encode())

    channel.basic_ack.assert_called_once_with(1)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == TICKET_DEAD_LETTER_QUEUE
    assert json.loads(kwargs["body"])["failure_reason"] == "ticket_not_found"


def test_malformed_message_is_dead_lettered_without_crashing(db_session):
    channel = MagicMock()

    on_message(channel, _method(), None, b"not valid json")

    channel.basic_ack.assert_called_once_with(1)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == TICKET_DEAD_LETTER_QUEUE
    assert kwargs["body"] == b"not valid json"


def test_retryable_failure_schedules_a_retry_with_backoff(db_session):
    ticket = _make_ticket(db_session, "retry@example.com")
    payload = _payload(ticket.id, retry_count=0)
    channel = MagicMock()

    def failing(db, t):
        raise RetryableProcessingError("temporary glitch")

    on_message(channel, _method(), None, json.dumps(payload).encode(), process_fn=failing)

    channel.basic_ack.assert_called_once_with(1)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == TICKET_RETRY_QUEUE
    republished = json.loads(kwargs["body"])
    assert republished["retry_count"] == 1
    assert kwargs["properties"].expiration == "5000"  # BASE_DELAY_MS * 2^0


def test_retry_limit_exceeded_dead_letters_and_fails_ticket(db_session):
    ticket = _make_ticket(db_session, "exhausted@example.com")
    payload = _payload(ticket.id, retry_count=MAX_RETRIES)
    channel = MagicMock()

    def failing(db, t):
        raise RetryableProcessingError("still broken")

    on_message(channel, _method(), None, json.dumps(payload).encode(), process_fn=failing)

    channel.basic_ack.assert_called_once_with(1)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == TICKET_DEAD_LETTER_QUEUE

    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.FAILED


def test_unexpected_error_is_dead_lettered_not_retried(db_session):
    ticket = _make_ticket(db_session, "unexpected@example.com")
    payload = _payload(ticket.id, retry_count=0)
    channel = MagicMock()

    def broken(db, t):
        raise ValueError("bug, not a transient failure")

    on_message(channel, _method(), None, json.dumps(payload).encode(), process_fn=broken)

    channel.basic_ack.assert_called_once_with(1)
    _, kwargs = channel.basic_publish.call_args
    assert kwargs["routing_key"] == TICKET_DEAD_LETTER_QUEUE

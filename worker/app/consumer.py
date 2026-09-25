import json
import logging
from collections.abc import Callable

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from app.processing import RetryableProcessingError, process_ticket
from shared import db as shared_db
from shared import progress
from shared.models import AuditLog, ProcessedEvent, Ticket, TicketStatus
from shared.rabbitmq import (
    TICKET_DEAD_LETTER_QUEUE,
    TICKET_PROCESSING_QUEUE,
    TICKET_RETRY_QUEUE,
    declare_topology,
    get_connection,
)
from shared.progress import RabbitProgressReporter, declare_progress_exchange

logger = logging.getLogger("worker")

MAX_RETRIES = 3
BASE_DELAY_MS = 5_000
MAX_DELAY_MS = 60_000

ProcessFn = Callable[[object, Ticket], None]


def _schedule_retry(channel: BlockingChannel, payload: dict) -> None:
    """Republish onto ticket.retry with a TTL. When that TTL expires, RabbitMQ's
    dead-letter mechanism drops the message back onto ticket.processing on its
    own — no separate relay process needed."""
    payload["retry_count"] += 1
    delay_ms = min(BASE_DELAY_MS * (2 ** (payload["retry_count"] - 1)), MAX_DELAY_MS)
    channel.basic_publish(
        exchange="",
        routing_key=TICKET_RETRY_QUEUE,
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2, expiration=str(delay_ms)),
    )
    logger.warning(
        "scheduled retry %s for ticket %s in %sms", payload["retry_count"], payload.get("ticket_id"), delay_ms
    )
    progress.emit("retry_scheduled", attempt=payload["retry_count"], delay_ms=delay_ms)


def _dead_letter(channel: BlockingChannel, payload: dict, reason: str) -> None:
    payload = {**payload, "failure_reason": reason}
    channel.basic_publish(
        exchange="",
        routing_key=TICKET_DEAD_LETTER_QUEUE,
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    logger.error("dead-lettered ticket %s: %s", payload.get("ticket_id"), reason)
    progress.emit("dead_lettered", reason=reason)

    ticket_id = payload.get("ticket_id")
    db = shared_db.SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id) if ticket_id else None
        if ticket is not None:
            ticket.status = TicketStatus.FAILED
        db.add(
            AuditLog(
                # ticket_id has a real FK to tickets.id — a "ticket not found"
                # dead-letter has no valid ticket to point at, so it goes in
                # detail instead rather than violating referential integrity.
                ticket_id=ticket.id if ticket is not None else None,
                event_type="ticket_dead_lettered",
                detail={"reason": reason, "ticket_id": ticket_id},
            )
        )
        db.commit()
    finally:
        db.close()


def _already_processed(db, event_id: str) -> bool:
    return db.query(ProcessedEvent).filter_by(event_id=event_id).first() is not None


def on_message(
    channel: BlockingChannel,
    method: Basic.Deliver,
    properties: BasicProperties,
    body: bytes,
    process_fn: ProcessFn = process_ticket,
) -> None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.error("dropping unparseable message: %r", body)
        channel.basic_publish(exchange="", routing_key=TICKET_DEAD_LETTER_QUEUE, body=body)
        channel.basic_ack(method.delivery_tag)
        return

    event_id = payload.get("event_id")
    ticket_id = payload.get("ticket_id")
    retry_count = payload.get("retry_count", 0)

    db = shared_db.SessionLocal()
    try:
        if event_id and _already_processed(db, event_id):
            logger.info("event %s already processed, skipping", event_id)
            channel.basic_ack(method.delivery_tag)
            return

        ticket = db.get(Ticket, ticket_id) if ticket_id else None
        if ticket is None:
            _dead_letter(channel, payload, reason="ticket_not_found")
            channel.basic_ack(method.delivery_tag)
            return

        progress.emit("picked_up", ticket.id, subject=ticket.subject, retry_count=retry_count)
        process_fn(db, ticket)
        # Read before the commit: after it, this would be a fresh query, and
        # the overseer only needs to know where the worker left the ticket.
        final_status = ticket.status.value
        db.add(ProcessedEvent(event_id=event_id, ticket_id=ticket_id))
        db.commit()
        channel.basic_ack(method.delivery_tag)
        logger.info("processed ticket %s (event %s)", ticket_id, event_id)
        # Only after the commit. "finished" on the overseer means the record
        # is written, so it must not be sent while a rollback is still possible.
        progress.emit("finished", status=final_status)

    except RetryableProcessingError as exc:
        db.rollback()
        if retry_count >= MAX_RETRIES:
            _dead_letter(channel, payload, reason=f"retries exhausted: {exc}")
        else:
            _schedule_retry(channel, payload)
        channel.basic_ack(method.delivery_tag)

    except Exception as exc:
        # Unknown failure. Never leave the message stuck unacked forever, but
        # don't blindly retry something we don't understand either.
        db.rollback()
        _dead_letter(channel, payload, reason=f"unexpected error: {exc}")
        channel.basic_ack(method.delivery_tag)

    finally:
        progress.release()
        db.close()


# How often an idle worker says it is still there. A worker waiting for work
# and a worker that has died look identical from outside otherwise.
HEARTBEAT_SECONDS = 5


def _heartbeat(connection) -> None:
    # Only fires while the worker is idle in start_consuming: a callback that
    # is busy with a ticket blocks the I/O loop, so no heartbeat is sent during
    # a long model call. That is fine - the step events cover busy periods, and
    # the overseer shows how long the current step has been running instead.
    progress.emit("heartbeat", state="idle")
    connection.call_later(HEARTBEAT_SECONDS, lambda: _heartbeat(connection))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    connection = get_connection()
    channel = connection.channel()
    declare_topology(channel)
    declare_progress_exchange(channel)
    progress.set_reporter(RabbitProgressReporter(channel))
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=TICKET_PROCESSING_QUEUE, on_message_callback=on_message)
    logger.info("worker started, waiting for messages on %s", TICKET_PROCESSING_QUEUE)
    _heartbeat(connection)
    channel.start_consuming()


if __name__ == "__main__":
    main()

import json
import logging
from collections.abc import Callable

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from app.processing import RetryableProcessingError, process_ticket
from shared import db as shared_db
from shared.models import AuditLog, ProcessedEvent, Ticket, TicketStatus
from shared.rabbitmq import (
    TICKET_DEAD_LETTER_QUEUE,
    TICKET_PROCESSING_QUEUE,
    TICKET_RETRY_QUEUE,
    declare_topology,
    get_connection,
)

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


def _dead_letter(channel: BlockingChannel, payload: dict, reason: str) -> None:
    payload = {**payload, "failure_reason": reason}
    channel.basic_publish(
        exchange="",
        routing_key=TICKET_DEAD_LETTER_QUEUE,
        body=json.dumps(payload),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    logger.error("dead-lettered ticket %s: %s", payload.get("ticket_id"), reason)

    ticket_id = payload.get("ticket_id")
    db = shared_db.SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id) if ticket_id else None
        if ticket is not None:
            ticket.status = TicketStatus.FAILED
        db.add(AuditLog(ticket_id=ticket_id, event_type="ticket_dead_lettered", detail={"reason": reason}))
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

        process_fn(db, ticket)
        db.add(ProcessedEvent(event_id=event_id, ticket_id=ticket_id))
        db.commit()
        channel.basic_ack(method.delivery_tag)
        logger.info("processed ticket %s (event %s)", ticket_id, event_id)

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
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    connection = get_connection()
    channel = connection.channel()
    declare_topology(channel)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=TICKET_PROCESSING_QUEUE, on_message_callback=on_message)
    logger.info("worker started, waiting for messages on %s", TICKET_PROCESSING_QUEUE)
    channel.start_consuming()


if __name__ == "__main__":
    main()

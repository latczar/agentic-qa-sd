import json
import uuid

import pika
from pika.adapters.blocking_connection import BlockingChannel

from shared.config import RABBITMQ_URL

TICKET_PROCESSING_QUEUE = "ticket.processing"
TICKET_RETRY_QUEUE = "ticket.retry"
TICKET_DEAD_LETTER_QUEUE = "ticket.dead-letter"


def get_connection() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))


def declare_topology(channel: BlockingChannel) -> None:
    """Declare all three queues. Safe to call repeatedly — matches existing queues.

    ticket.retry has no consumer of its own. Its only job is to hold a message
    for `expiration` milliseconds (set per-message at publish time, so retry
    delay can grow with each attempt) and then let RabbitMQ's dead-letter
    mechanism drop it straight back onto ticket.processing — a delay queue,
    built out of a feature meant for actual dead-lettering.
    """
    channel.queue_declare(queue=TICKET_PROCESSING_QUEUE, durable=True)
    channel.queue_declare(
        queue=TICKET_RETRY_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": TICKET_PROCESSING_QUEUE,
        },
    )
    channel.queue_declare(queue=TICKET_DEAD_LETTER_QUEUE, durable=True)


def publish_ticket_created(ticket_id: int) -> str:
    """Publish a ticket-created event onto ticket.processing.

    Returns the event_id, which is also the idempotency key the worker checks
    against the processed_events table.
    """
    event_id = str(uuid.uuid4())
    body = json.dumps({"event_id": event_id, "ticket_id": ticket_id, "retry_count": 0})

    connection = get_connection()
    try:
        channel = connection.channel()
        declare_topology(channel)
        channel.basic_publish(
            exchange="",
            routing_key=TICKET_PROCESSING_QUEUE,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
    finally:
        connection.close()

    return event_id

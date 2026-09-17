"""Proves publish_ticket_created actually talks to a real RabbitMQ, not a mock.

The processing queue isn't rolled back like Postgres is, so it's purged first
to keep this test deterministic regardless of what earlier tests published.
"""

import json

from shared.models import User, UserRole
from shared.rabbitmq import TICKET_PROCESSING_QUEUE, declare_topology, get_connection


def test_creating_a_ticket_publishes_a_real_message_to_rabbitmq(client, db_session):
    user = User(name="Queue Test User", email="queue.test@example.com", role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    connection = get_connection()
    channel = connection.channel()
    declare_topology(channel)
    channel.queue_purge(TICKET_PROCESSING_QUEUE)

    response = client.post(
        "/tickets",
        json={"submitted_by_id": user.id, "subject": "Queue integration test", "description": "..."},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "QUEUED"

    method, _properties, message_body = channel.basic_get(TICKET_PROCESSING_QUEUE, auto_ack=True)
    assert method is not None, "expected a message on ticket.processing but the queue was empty"
    payload = json.loads(message_body)
    assert payload["ticket_id"] == body["id"]
    assert payload["retry_count"] == 0

    connection.close()

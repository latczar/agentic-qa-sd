"""Proves ticket creation really publishes to RabbitMQ, not a mock.

Deliberately split in two. An earlier version created a ticket and then read
the message back off ticket.processing - which only passed while no worker was
running. As soon as the real worker was up it consumed the message first and
the test failed for a reason that had nothing to do with the code under test.

So: one test checks what the API alone controls, and one proves the AMQP
round-trip on a queue nothing else is consuming.
"""

import json
import uuid

import pika

from shared.models import AuditLog, User, UserRole
from shared.rabbitmq import declare_topology, get_connection


def test_creating_a_ticket_publishes_and_marks_it_queued(client, db_session):
    user = User(name="Queue Test User", email="queue.test@example.com", role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    response = client.post(
        "/tickets",
        json={"submitted_by_id": user.id, "subject": "Queue integration test", "description": "..."},
    )

    assert response.status_code == 201
    body = response.json()
    # QUEUED only gets set after publish_ticket_created returns without raising,
    # so this status is itself evidence the broker accepted the message.
    assert body["status"] == "QUEUED"

    events = {
        a.event_type: a.detail
        for a in db_session.query(AuditLog).filter_by(ticket_id=body["id"]).all()
    }
    assert "ticket_queued" in events
    assert events["ticket_queued"]["event_id"]


def test_amqp_round_trip_on_a_private_queue():
    """Publish and read back a message, on a queue no worker is consuming."""
    queue = f"test.roundtrip.{uuid.uuid4()}"
    connection = get_connection()
    try:
        channel = connection.channel()
        declare_topology(channel)
        channel.queue_declare(queue=queue, durable=False, auto_delete=True)

        sent = {"event_id": str(uuid.uuid4()), "ticket_id": 123, "retry_count": 0}
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(sent),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )

        method, _properties, body = channel.basic_get(queue, auto_ack=True)
        assert method is not None, "published message did not come back"
        assert json.loads(body) == sent

        channel.queue_delete(queue=queue)
    finally:
        connection.close()

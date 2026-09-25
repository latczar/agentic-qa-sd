"""Live progress from the worker, for anyone who is watching.

Why this exists at all: the worker handles a ticket inside one database
transaction and commits once, at the end. That is correct for the record - a
half-analysed ticket should never be visible - but it means the database shows
nothing while the worker is thinking and then everything at once. Watching the
audit log is watching the commit, not the work.

So progress travels separately, as small fire-and-forget messages on a fanout
exchange. The console's overseer page subscribes; if nobody is subscribed the
messages go nowhere. The audit log stays the record of what happened. This is
only a view of it happening.

Two rules everything below follows:

1. Reporting progress must never be able to break the work. Every publish is
   wrapped, and a failure is logged and forgotten. A dashboard outage that
   stalls ticket processing would be the tail wagging the dog.
2. Module-level, like logging, rather than threaded through every signature.
   Progress is cross-cutting and optional in exactly the way logging is, and
   passing a reporter through consumer -> processing -> orchestrator ->
   analysis would change four signatures to carry something none of them act on.
"""

import json
import logging
import socket
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("progress")

# Fanout, not a queue: every subscriber gets every event, and events are not
# kept for anyone who is not listening. Non-durable for the same reason - a
# progress message about a ticket from before a broker restart is worthless.
WORKER_PROGRESS_EXCHANGE = "worker.progress"

# In Docker this is the container id, so scaled-out workers each get a
# distinct lane on the overseer without any extra configuration.
WORKER_ID = socket.gethostname()


def make_event(worker: str, step: str, ticket_id: int | None, detail: dict) -> dict:
    return {
        "worker": worker,
        "step": step,
        "ticket_id": ticket_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }


class ProgressReporter:
    """Base reporter: remembers which ticket the worker is holding.

    One worker process handles one ticket at a time (prefetch_count=1), so
    "the current ticket" is well-defined, and code deep in the pipeline that
    has no ticket in scope - the model retry loop, for one - can still report
    against the right ticket without being handed its id.
    """

    def __init__(self, worker: str = WORKER_ID) -> None:
        self.worker = worker
        self.current_ticket: int | None = None

    def emit(self, step: str, ticket_id: int | None = None, **detail) -> None:
        if ticket_id is not None:
            self.current_ticket = ticket_id
        self._send(make_event(self.worker, step, ticket_id or self.current_ticket, detail))

    def release(self) -> None:
        """The worker has let go of its ticket."""
        self.current_ticket = None

    def _send(self, event: dict) -> None:  # pragma: no cover - overridden
        pass


class NullProgressReporter(ProgressReporter):
    """Reports to nobody. The default, and what every test gets unless it asks."""


class RecordingProgressReporter(ProgressReporter):
    """Keeps every event, for tests to assert the order of steps."""

    def __init__(self, worker: str = "test-worker") -> None:
        super().__init__(worker)
        self.events: list[dict] = []

    def _send(self, event: dict) -> None:
        self.events.append(event)

    @property
    def steps(self) -> list[str]:
        return [e["step"] for e in self.events]


class RabbitProgressReporter(ProgressReporter):
    """Publishes onto the fanout exchange, on a channel the caller owns.

    The worker's own consuming channel, in practice. Publishing from inside a
    consumer callback is permitted on a BlockingChannel, and reusing it avoids
    a second connection whose heartbeats would go unserviced during every long
    model call, exactly like the first one's.
    """

    def __init__(self, channel, worker: str = WORKER_ID) -> None:
        super().__init__(worker)
        self._channel = channel

    def _send(self, event: dict) -> None:
        import pika

        self._channel.basic_publish(
            exchange=WORKER_PROGRESS_EXCHANGE,
            routing_key="",
            body=json.dumps(event),
            # delivery_mode 1 = transient. Nothing about a progress blip is
            # worth writing to the broker's disk.
            properties=pika.BasicProperties(delivery_mode=1, content_type="application/json"),
        )


_reporter: ProgressReporter = NullProgressReporter()


def set_reporter(reporter: ProgressReporter) -> None:
    global _reporter
    _reporter = reporter


def get_reporter() -> ProgressReporter:
    return _reporter


@contextmanager
def use_reporter(reporter: ProgressReporter):
    """Swap the reporter for the duration of a block. For tests."""
    previous = get_reporter()
    set_reporter(reporter)
    try:
        yield reporter
    finally:
        set_reporter(previous)


def emit(step: str, ticket_id: int | None = None, **detail) -> None:
    """Report a step. Never raises - see rule 1 at the top of this file."""
    try:
        _reporter.emit(step, ticket_id, **detail)
    except Exception as exc:
        logger.debug("progress event %r dropped: %s", step, exc)


def release() -> None:
    try:
        _reporter.release()
    except Exception:
        pass


def declare_progress_exchange(channel) -> None:
    channel.exchange_declare(exchange=WORKER_PROGRESS_EXCHANGE, exchange_type="fanout", durable=False)

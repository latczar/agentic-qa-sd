"""The progress reporter itself, separately from what reports through it."""

from unittest.mock import MagicMock

from shared import progress
from shared.progress import (
    WORKER_PROGRESS_EXCHANGE,
    RabbitProgressReporter,
    RecordingProgressReporter,
    use_reporter,
)


def test_events_publish_to_the_fanout_exchange_as_transient_messages():
    channel = MagicMock()
    reporter = RabbitProgressReporter(channel, worker="w-1")

    reporter.emit("picked_up", 7, subject="VPN drops")

    kwargs = channel.basic_publish.call_args.kwargs
    assert kwargs["exchange"] == WORKER_PROGRESS_EXCHANGE
    assert kwargs["routing_key"] == ""
    # Transient: nothing about a progress blip is worth the broker's disk.
    assert kwargs["properties"].delivery_mode == 1


def test_a_publish_that_fails_is_swallowed():
    """Rule one of the module: watching must never break the work."""
    channel = MagicMock()
    channel.basic_publish.side_effect = ConnectionError("broker went away")

    with use_reporter(RabbitProgressReporter(channel)):
        progress.emit("calling_model", 7, attempt=1)  # must not raise


def test_later_events_inherit_the_ticket_the_worker_is_holding():
    reporter = RecordingProgressReporter()

    reporter.emit("picked_up", 12)
    reporter.emit("calling_model", attempt=1)

    assert [e["ticket_id"] for e in reporter.events] == [12, 12]


def test_releasing_the_ticket_stops_it_being_attached_to_heartbeats():
    reporter = RecordingProgressReporter()
    reporter.emit("picked_up", 12)

    reporter.release()
    reporter.emit("heartbeat", state="idle")

    assert reporter.events[-1]["ticket_id"] is None


def test_use_reporter_restores_whatever_was_there_before():
    before = progress.get_reporter()

    with use_reporter(RecordingProgressReporter()):
        assert progress.get_reporter() is not before

    assert progress.get_reporter() is before

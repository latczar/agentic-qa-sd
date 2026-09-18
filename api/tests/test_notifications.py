"""The n8n notifier.

The behaviour that matters here is what happens when n8n is down: a ticket
that needs a human needs one whether or not the notification was delivered,
so this must never raise into the orchestration path.
"""

import urllib.error
from unittest.mock import patch

from shared import notifications


def test_disabled_when_no_url_is_configured():
    with patch.object(notifications, "N8N_WEBHOOK_URL", ""):
        assert notifications.notify_approval_needed({"ticket_id": 1}) is False


def test_successful_post_reports_true():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.object(notifications, "N8N_WEBHOOK_URL", "http://n8n.invalid/webhook"):
        with patch.object(notifications.urllib.request, "urlopen", return_value=Response()):
            assert notifications.notify_approval_needed({"ticket_id": 1}) is True


def test_unreachable_n8n_returns_false_rather_than_raising():
    with patch.object(notifications, "N8N_WEBHOOK_URL", "http://n8n.invalid/webhook"):
        with patch.object(
            notifications.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            # Not pytest.raises: the whole point is that this failure is
            # swallowed so a downed notifier can't fail the ticket.
            assert notifications.notify_approval_needed({"ticket_id": 1}) is False

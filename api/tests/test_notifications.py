"""The approval notifiers.

The behaviour that matters here is what happens when a channel is down: a
ticket that needs a human needs one whether or not the notification was
delivered, so none of this may raise into the orchestration path.
"""

import urllib.error
from unittest.mock import patch

from shared import notifications


def test_n8n_disabled_when_no_url_is_configured():
    with patch.object(notifications, "N8N_WEBHOOK_URL", ""):
        assert notifications.notify_n8n({"ticket_id": 1}) is False


def test_successful_post_reports_true():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.object(notifications, "N8N_WEBHOOK_URL", "http://n8n.invalid/webhook"):
        with patch.object(notifications.urllib.request, "urlopen", return_value=Response()):
            assert notifications.notify_n8n({"ticket_id": 1}) is True


def test_unreachable_n8n_returns_false_rather_than_raising():
    with patch.object(notifications, "N8N_WEBHOOK_URL", "http://n8n.invalid/webhook"):
        with patch.object(
            notifications.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            # Not pytest.raises: the whole point is that this failure is
            # swallowed so a downed notifier can't fail the ticket.
            assert notifications.notify_n8n({"ticket_id": 1}) is False


def test_telegram_is_skipped_when_the_allowlist_is_empty():
    """An empty allowlist means nobody, and must report nobody.

    The fake client accepts every message, so going through it would return
    True and write "telegram: true" into the audit log for a message that was
    never sent to anyone.
    """
    with patch.object(notifications, "TELEGRAM_PROVIDER", "http"):
        with patch.object(notifications, "TELEGRAM_ALLOWED_CHAT_IDS", frozenset()):
            assert notifications.notify_telegram({"ticket_id": 1}) is False


def test_telegram_is_skipped_when_not_configured():
    with patch.object(notifications, "TELEGRAM_PROVIDER", "fake"):
        with patch.object(notifications, "TELEGRAM_ALLOWED_CHAT_IDS", frozenset({99})):
            assert notifications.notify_telegram({"ticket_id": 1}) is False


def test_fan_out_reports_each_channel_separately():
    """One channel failing must not be reported as the other failing too."""
    with patch.object(notifications, "N8N_WEBHOOK_URL", ""):
        with patch.object(notifications, "TELEGRAM_PROVIDER", "fake"):
            assert notifications.notify_approval_needed({"ticket_id": 1}) == {
                "n8n": False,
                "telegram": False,
            }

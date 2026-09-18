"""Telling n8n that something needs a human.

One direction only: we push events to n8n, n8n pushes decisions back through
the API's /approve and /reject endpoints. n8n never touches Postgres or
RabbitMQ itself, so business logic stays in one place and n8n stays thin
glue - notifications, waiting for a human, escalation.
"""

import json
import logging
import urllib.error
import urllib.request

from shared.config import N8N_WEBHOOK_TIMEOUT_SECONDS, N8N_WEBHOOK_URL

logger = logging.getLogger("notifications")


def notify_approval_needed(payload: dict) -> bool:
    """Fire the approval webhook. Returns whether it got through.

    Never raises. A ticket that needs human approval needs it whether or not
    the notification was delivered - failing the whole analysis because a
    notification service was down would be the wrong trade. The caller records
    the outcome in the audit log either way, so a silently unnotified ticket is
    still visible.
    """
    if not N8N_WEBHOOK_URL:
        return False

    request = urllib.request.Request(
        N8N_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=N8N_WEBHOOK_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("approval webhook to %s failed: %s", N8N_WEBHOOK_URL, exc)
        return False

"""Telling somebody that a ticket needs a human.

Two channels now, so this file is a fan-out rather than a single webhook call.

n8n is push-only: we send it events, it sends decisions back through the API's
/approve, /reject and /handled endpoints. Telegram works exactly the same way -
the bot calls those same endpoints. Neither one touches Postgres or RabbitMQ
directly, so there is precisely one place a ticket can be decided, and the
status guard, the approvals record and the audit trail apply to a decision
made from a phone as much as one made in a browser.
"""

import json
import logging
import urllib.error
import urllib.request

from shared.config import (
    N8N_WEBHOOK_TIMEOUT_SECONDS,
    N8N_WEBHOOK_URL,
    TELEGRAM_ALLOWED_CHAT_IDS,
    TELEGRAM_PROVIDER,
)

logger = logging.getLogger("notifications")


def notify_n8n(payload: dict) -> bool:
    """Fire the approval webhook. Returns whether it got through."""
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


def notify_telegram(payload: dict) -> bool:
    """Send the approval request to every allowlisted chat."""
    # Checked before building a client, not after. get_telegram_client returns
    # the fake when Telegram is not configured, and the fake accepts every
    # message happily - so going through it anyway would write "telegram: true"
    # into the audit log for a message that was never sent. An audit trail that
    # overstates delivery is worse than no audit trail.
    if TELEGRAM_PROVIDER != "http" or not TELEGRAM_ALLOWED_CHAT_IDS:
        return False

    # Imported here rather than at module scope so that a project running
    # without Telegram never constructs a client it has no token for.
    from shared.telegram import TelegramError, get_telegram_client, send_approval_request

    try:
        client = get_telegram_client()
    except TelegramError as exc:
        logger.warning("Telegram is enabled but unusable: %s", exc)
        return False

    return send_approval_request(client, TELEGRAM_ALLOWED_CHAT_IDS, payload)


def notify_approval_needed(payload: dict) -> dict[str, bool]:
    """Tell every configured channel. Returns which ones got through.

    Never raises. A ticket that needs human approval needs it whether or not
    the notification was delivered - failing the whole analysis because a chat
    app was down would be the wrong trade. The caller records this result in
    the audit log, so a ticket nobody was told about is still visible as one.

    A dict rather than a bool because "notified" stopped being a yes/no the
    moment there were two channels: n8n succeeding and Telegram failing is a
    different situation from both working, and the audit log should say which.
    """
    return {"n8n": notify_n8n(payload), "telegram": notify_telegram(payload)}

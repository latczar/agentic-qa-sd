"""The long-polling loop.

Deliberately thin. Everything worth testing lives in dispatch.py; this file
owns only the things that need a real network - asking Telegram for updates,
advancing the offset, and not falling over when either fails.

Long polling rather than a webhook, because a webhook needs a public HTTPS URL
that Telegram can reach and this runs on a laptop. getUpdates needs nothing
but outbound HTTP: no tunnel, no port forwarding, nothing exposed.
"""

import logging
import time

from shared.config import TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_POLL_SECONDS, TELEGRAM_PROVIDER
from shared.db import SessionLocal
from shared.telegram import TelegramError, get_telegram_client
from telegram_bot.app.api_client import ServiceDeskApi
from telegram_bot.app.dispatch import handle_update

logger = logging.getLogger("telegram_bot")

# How long to wait after a failed poll before trying again. Telegram being
# briefly unreachable is ordinary; hammering it while it is down is not.
BACKOFF_SECONDS = 5


def poll_once(telegram, api, offset: int) -> int:
    """Fetch and handle one batch. Returns the offset to ask for next.

    One database session per batch rather than per process: a long-lived
    session on an idle connection is exactly what goes stale overnight, and
    this loop spends almost all of its life idle.
    """
    updates = telegram.get_updates(offset=offset, timeout=TELEGRAM_POLL_SECONDS)
    if not updates:
        return offset

    db = SessionLocal()
    try:
        for update in updates:
            try:
                handle_update(db, telegram, api, update)
            except Exception:
                # One bad update must not take the loop down with it. It has
                # already been marked as handled by dispatch, so the next poll
                # moves past it rather than replaying the same crash forever.
                logger.exception("could not handle update %s", update.get("update_id"))
                db.rollback()
    finally:
        db.close()

    # offset = highest seen + 1 is how getUpdates acknowledges. Telegram keeps
    # redelivering anything below it, which is what makes the idempotency
    # check in dispatch worth having.
    return max(u.get("update_id", 0) for u in updates) + 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if TELEGRAM_PROVIDER != "http":
        # Exiting rather than idling. A container that sits there logging
        # nothing looks healthy, and "the bot is running but was never given a
        # token" is a confusing thing to debug an hour later.
        logger.error("TELEGRAM_PROVIDER is %r, not 'http' - nothing to poll.", TELEGRAM_PROVIDER)
        raise SystemExit(1)

    if not TELEGRAM_ALLOWED_CHAT_IDS:
        # Not fatal: this is the state you are in while setting up, and the
        # only way to find your own chat id is to message the bot and read the
        # id out of the refusal it logs. Loud, but still running.
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty, so every message will be refused. "
            "Message the bot and this log will show the chat id to add."
        )

    telegram = get_telegram_client()
    api = ServiceDeskApi()
    offset = 0
    logger.info("telegram bot started, long-polling every %ss", TELEGRAM_POLL_SECONDS)

    while True:
        try:
            offset = poll_once(telegram, api, offset)
        except TelegramError as exc:
            logger.warning("poll failed: %s", exc)
            time.sleep(BACKOFF_SECONDS)
        except KeyboardInterrupt:
            logger.info("stopping")
            return


if __name__ == "__main__":
    main()

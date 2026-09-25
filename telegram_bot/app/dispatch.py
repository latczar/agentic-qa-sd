"""Deciding what to do with one Telegram update.

Kept separate from the polling loop so the whole of it can be tested without a
network: hand it an update dict, a fake Telegram client and a real database
session, and assert on what came back. The loop in bot.py is then thin enough
to read in one go.
"""

import html
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.config import TELEGRAM_ALLOWED_CHAT_IDS
from shared.models import ProcessedEvent, User
from shared.telegram import TelegramClient, TelegramError
from telegram_bot.app.api_client import ServiceDeskApi

logger = logging.getLogger("telegram_bot.dispatch")

# The actions a button can carry, mapped to the API route that performs them.
# A lookup rather than a free-form string, so a malformed callback_data cannot
# be turned into an arbitrary request path.
CALLBACK_ACTIONS = {
    "approve": "approve",
    "handled": "handled",
    "reject": "reject",
}

HELP = (
    "<b>Service desk</b>\n\n"
    "Send me any message and I will raise it as a ticket.\n\n"
    "/link you@example.com - tell me who you are\n"
    "/status - what the pipeline is doing right now\n"
    "/instruct 42 this is a network fault - correct the agent and re-run ticket 42\n"
    "/help - this message"
)


def _allowed(chat_id: int) -> bool:
    return chat_id in TELEGRAM_ALLOWED_CHAT_IDS


def _already_handled(db: Session, update_id: int) -> bool:
    """Has this exact update been acted on before?

    Telegram redelivers everything below the offset until the next getUpdates
    confirms it, so a crash between "approved the ticket" and "advanced the
    offset" would otherwise approve it twice. Same table and same reasoning as
    the RabbitMQ consumer's idempotency check.
    """
    return db.query(ProcessedEvent).filter_by(event_id=f"tg-{update_id}").first() is not None


def _mark_handled(db: Session, update_id: int) -> None:
    db.add(ProcessedEvent(event_id=f"tg-{update_id}"))
    db.commit()


def _linked_user(db: Session, chat_id: int) -> User | None:
    return db.query(User).filter_by(telegram_chat_id=chat_id).first()


def _link(db: Session, chat_id: int, email: str) -> str:
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None:
        return f"I have no user with the email {html.escape(email)}."

    existing = _linked_user(db, chat_id)
    if existing is not None and existing.id != user.id:
        # Re-linking is allowed, but the old link has to go first. Every
        # decision made from this chat is attributed to whoever it points at,
        # and the unique constraint would reject the second link anyway.
        existing.telegram_chat_id = None
        db.flush()

    user.telegram_chat_id = chat_id
    try:
        db.commit()
    except IntegrityError:
        # This account is already linked to a different chat. Allowing it
        # would make "who approved this" ambiguous the moment either chat
        # tapped a button.
        db.rollback()
        return "That account is already linked to a different Telegram chat."
    return f"Linked. I will file your tickets as {html.escape(user.name)}."


def _status(api: ServiceDeskApi) -> str:
    result = api.overview()
    if not result.ok:
        return f"Could not read the pipeline: {html.escape(result.detail)}"

    lines = ["<b>Pipeline</b>", ""]
    for stage in result.body.get("stages", []):
        # Empty stages are noise, except the two worth seeing a zero for:
        # "nothing waiting on you" and "nothing broken" are both answers.
        if stage["count"] or stage["key"] in ("needs_you", "failed"):
            lines.append(f"{stage['count']:>3}  {html.escape(stage['label'])}")
    lines += ["", f"{result.body.get('needs_you', 0)} waiting on you."]
    return "\n".join(lines)


def _raise_ticket(db: Session, api: ServiceDeskApi, chat_id: int, text: str) -> str:
    user = _linked_user(db, chat_id)
    if user is None:
        return "Tell me who you are first: /link you@example.com"

    # The first line becomes the subject, the whole message stays as the
    # description. Nothing is lost, and a one-line message reads sensibly in
    # both fields rather than being truncated into nonsense.
    subject = text.strip().splitlines()[0]
    result = api.create_ticket(user.id, subject, text.strip())
    if not result.ok:
        return f"I could not raise that: {html.escape(result.detail)}"

    return (
        f"Raised ticket <b>#{result.body['id']}</b>.\n"
        "I will message you here if it needs a decision."
    )


def _instruct(db: Session, api: ServiceDeskApi, chat_id: int, argument: str) -> str:
    user = _linked_user(db, chat_id)
    parts = argument.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        return "Use: /instruct 42 what the agent should have said"

    ticket_id, instruction = int(parts[0]), parts[1]
    result = api.instruct(ticket_id, instruction, user.id if user else None)
    if not result.ok:
        return f"Ticket #{ticket_id}: {html.escape(result.detail)}"
    return f"Told the agent, and put ticket <b>#{ticket_id}</b> back through the pipeline."


def _handle_message(db: Session, telegram: TelegramClient, api: ServiceDeskApi, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if not _allowed(chat_id):
        # Logged at warning so the operator can find their own chat id during
        # setup - this log line is the intended way to discover it. The reply
        # itself says nothing about what the bot does or who may use it.
        logger.warning("ignoring message from chat %s, which is not on the allowlist", chat_id)
        telegram.send_message(chat_id, "This bot is not available to this account.")
        return

    if not text:
        telegram.send_message(chat_id, "I can only read text messages.")
        return

    command, _, argument = text.partition(" ")
    command = command.lower()

    if command in ("/start", "/help"):
        telegram.send_message(chat_id, HELP)
    elif command == "/link":
        telegram.send_message(chat_id, _link(db, chat_id, argument))
    elif command == "/status":
        telegram.send_message(chat_id, _status(api))
    elif command == "/instruct":
        telegram.send_message(chat_id, _instruct(db, api, chat_id, argument))
    elif command.startswith("/"):
        telegram.send_message(chat_id, f"I do not know {html.escape(command)}.\n\n{HELP}")
    else:
        telegram.send_message(chat_id, _raise_ticket(db, api, chat_id, text))


def _handle_callback(db: Session, telegram: TelegramClient, api: ServiceDeskApi, callback: dict) -> None:
    callback_id = callback["id"]
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    data = callback.get("data") or ""

    if chat_id is None or not _allowed(chat_id):
        logger.warning("ignoring button press from chat %s, which is not on the allowlist", chat_id)
        telegram.answer_callback(callback_id, "Not available to this account.")
        return

    action, _, raw_id = data.partition(":")
    if action not in CALLBACK_ACTIONS or not raw_id.isdigit():
        telegram.answer_callback(callback_id, "I do not understand that button.")
        return

    ticket_id = int(raw_id)
    user = _linked_user(db, chat_id)
    result = api.decide(ticket_id, CALLBACK_ACTIONS[action], user.id if user else None)

    if result.ok:
        outcome = {
            "approve": "Approved",
            "handled": "Marked as handled by you",
            "reject": "Recorded as the agent getting it wrong",
        }[action]
        telegram.answer_callback(callback_id, outcome)
        summary = f"{outcome} - ticket #{ticket_id}"
    else:
        # A 409 is the normal case here, not a bug: somebody already decided
        # this ticket, probably in the browser. Say so plainly rather than
        # showing an error.
        telegram.answer_callback(
            callback_id, "Already decided" if result.status == 409 else "That did not work"
        )
        summary = f"Ticket #{ticket_id}: {html.escape(result.detail)}"

    # Rewrite the original message without its buttons, whatever the outcome.
    # Leaving a live Approve button on a decided ticket invites a second tap
    # that can only ever fail.
    message_id = message.get("message_id")
    if message_id is not None:
        original = message.get("text") or ""
        try:
            telegram.edit_message_text(
                chat_id, message_id, f"{html.escape(original)}\n\n<b>{summary}</b>"
            )
        except TelegramError as exc:
            logger.warning("could not update message %s after a decision: %s", message_id, exc)


def handle_update(db: Session, telegram: TelegramClient, api: ServiceDeskApi, update: dict) -> None:
    """Act on one update, at most once."""
    update_id = update.get("update_id")
    if update_id is not None and _already_handled(db, update_id):
        logger.info("update %s has already been handled - skipping", update_id)
        return

    try:
        if "callback_query" in update:
            _handle_callback(db, telegram, api, update["callback_query"])
        elif "message" in update:
            _handle_message(db, telegram, api, update["message"])
        else:
            logger.debug("ignoring update with nothing to act on: %s", sorted(update))
    finally:
        # Marked in a finally, so an update that blew up is not retried
        # forever. One bad message must not wedge the queue behind it; the
        # traceback is in the log and the person can simply send it again.
        if update_id is not None:
            _mark_handled(db, update_id)

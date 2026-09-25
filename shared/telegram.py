"""Talking to Telegram.

Same real/fake provider shape as shared/llm.py and shared/embeddings.py, for
the same reason: tests must never need a bot token or a network. The fake
records what would have been sent and hands back scripted updates, so the
whole dispatch path gets exercised offline.

Long polling, not webhooks. A webhook needs a public HTTPS URL that Telegram
can reach; this project runs on a laptop behind a router. getUpdates only
needs outbound HTTP, so there is no tunnel, no port forwarding and nothing
exposed to the internet in order to run the demo.

urllib rather than requests, matching the rest of the repo - the whole client
is a few POSTs and adding a dependency to make them prettier is not worth it.
"""

import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from shared.config import (
    TELEGRAM_API_BASE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_PROVIDER,
    TELEGRAM_TIMEOUT_SECONDS,
)

logger = logging.getLogger("telegram")


class TelegramError(RuntimeError):
    """Telegram could not be reached, or refused what we sent."""


# Telegram caps callback_data at 64 bytes, so the button payload has to stay
# small. "approve:12" leaves plenty of room; anything richer would need a
# lookup table keyed by a short id, which this does not yet warrant.
def decision_button(label: str, action: str, ticket_id: int) -> dict:
    return {"text": label, "callback_data": f"{action}:{ticket_id}"}


class TelegramClient(Protocol):
    def send_message(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> int | None: ...

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def get_updates(self, offset: int, timeout: int) -> list[dict]: ...


class HttpTelegramClient:
    """The real thing: Telegram's HTTP Bot API."""

    def __init__(
        self,
        token: str = TELEGRAM_BOT_TOKEN,
        api_base: str = TELEGRAM_API_BASE,
        timeout: float = TELEGRAM_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is empty. Set it, or set TELEGRAM_PROVIDER=fake "
                "to run without Telegram at all."
            )
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _call(self, method: str, payload: dict, timeout: float | None = None) -> dict:
        request = urllib.request.Request(
            f"{self._api_base}/bot{self._token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Telegram puts a human-readable reason in the body even on a 4xx,
            # and it is almost always the useful part ("chat not found",
            # "message is not modified"). Surface it rather than just the code.
            detail = exc.read().decode(errors="replace")[:300]
            raise TelegramError(f"Telegram {method} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Could not reach Telegram for {method}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram {method} returned a response that wasn't JSON: {exc}") from exc

        if not body.get("ok"):
            raise TelegramError(f"Telegram {method} refused: {body.get('description')!r}")
        return body

    def send_message(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        result = self._call("sendMessage", payload).get("result") or {}
        return result.get("message_id")

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        # No reply_markup, which is the point: editing without it strips the
        # buttons, so a decided ticket cannot be decided again by tapping the
        # same message a second time. The API's 409 would catch that anyway,
        # but a button that does nothing is a bad thing to leave on screen.
        self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        # Telegram shows a spinner on the tapped button until this is called.
        # Skipping it leaves the button looking stuck even when the work
        # succeeded, so it is not optional politeness.
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        # The HTTP timeout has to outlast the long-poll timeout, or urllib
        # gives up on a perfectly healthy connection that is simply waiting.
        body = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + self._timeout,
        )
        return body.get("result") or []


class FakeTelegramClient:
    """Records what would have been sent, replays scripted updates.

    Tests assert against .sent and .edits rather than a mock framework, so a
    failure shows the actual message text a person would have received.
    """

    def __init__(self, updates: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answered: list[tuple[str, str]] = []
        self._updates = list(updates) if updates else []
        self._next_message_id = 1000

    def send_message(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        self._next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": self._next_message_id})
        return self._next_message_id

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        # Hands back everything at or after the offset, then nothing, so a
        # polling loop in a test terminates instead of spinning forever.
        due = [u for u in self._updates if u.get("update_id", 0) >= offset]
        self._updates = [u for u in self._updates if u.get("update_id", 0) < offset]
        return due


def get_telegram_client(name: str = TELEGRAM_PROVIDER) -> TelegramClient:
    """Pick a client by name, so switching is an env var and not a code change."""
    if name == "http":
        return HttpTelegramClient()
    if name == "fake":
        return FakeTelegramClient()
    raise ValueError(f"Unknown TELEGRAM_PROVIDER '{name}' - expected 'http' or 'fake'")


def format_approval_request(payload: dict) -> tuple[str, list[list[dict]]]:
    """Turn an approval payload into a message and its buttons.

    Everything interpolated here is escaped, because parse_mode is HTML and
    the subject is whatever a member of the public typed into the ticket form.
    An unescaped "<b>" would be the least of it: this is the same untrusted
    text the prompt builder fences, arriving at a different output channel,
    and it needs handling at both.
    """
    ticket_id = payload.get("ticket_id")
    subject = html.escape(str(payload.get("subject") or "(no subject)"))
    reason = html.escape(str(payload.get("reason") or "needs a human"))

    lines = [f"<b>Ticket #{ticket_id}</b> needs you", "", subject, "", f"<i>Why: {reason}</i>"]

    category = payload.get("category")
    priority = payload.get("priority")
    confidence = payload.get("confidence")
    if category or priority:
        bits = [html.escape(str(b)) for b in (category, priority) if b]
        lines.append("Agent says: " + " / ".join(bits))
    if isinstance(confidence, (int, float)):
        lines.append(f"Confidence: {confidence:.0%}")

    resolution = payload.get("recommended_resolution")
    if resolution:
        lines += ["", html.escape(str(resolution))]

    buttons = [
        [
            decision_button("Approve", "approve", ticket_id),
            decision_button("I handled it", "handled", ticket_id),
        ],
        [decision_button("Agent was wrong", "reject", ticket_id)],
    ]
    return "\n".join(lines), buttons


def send_approval_request(client: TelegramClient, chat_ids, payload: dict) -> bool:
    """Send one approval request to every approver. True if any got through.

    Never raises, for the same reason the n8n webhook never raises: a ticket
    needs a human whether or not the message announcing it was delivered.
    One chat being unreachable must not stop the others being told, so each
    send is attempted and failures are logged rather than propagated.
    """
    text, buttons = format_approval_request(payload)
    delivered = False
    for chat_id in chat_ids:
        try:
            client.send_message(chat_id, text, buttons)
            delivered = True
        except TelegramError as exc:
            logger.warning("could not send approval request to chat %s: %s", chat_id, exc)
    return delivered

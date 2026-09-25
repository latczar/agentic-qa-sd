"""The bot's client for the service desk API.

The bot never writes ticket state straight to Postgres. Every decision goes
through the same endpoints the browser and n8n use, so the AWAITING_APPROVAL
guard, the approvals record and the audit trail apply identically whether a
ticket was decided at a desk or on a phone on a train.

Its own bookkeeping - which update it has already seen, which chat belongs to
which person - is a different matter and does use the database directly. That
is the bot's private state, not ticket state, and no other process owns it.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from shared.config import API_BASE_URL

logger = logging.getLogger("telegram_bot.api")


@dataclass
class ApiResult:
    status: int
    body: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def detail(self) -> str:
        """The API's own explanation, which is usually the useful sentence."""
        return str(self.body.get("detail") or self.body)


class ServiceDeskApi:
    def __init__(self, base_url: str = API_BASE_URL, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> ApiResult:
        request = urllib.request.Request(
            f"{self._base}{path}",
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return ApiResult(response.status, json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            # A 409 here is not an error to hide - it is the API correctly
            # refusing to decide an already-decided ticket, which happens
            # whenever someone taps an old message. The caller turns it into a
            # plain sentence, so the body has to survive this far.
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"detail": raw.decode(errors="replace")[:300]}
            return ApiResult(exc.code, body)
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("service desk API unreachable for %s %s: %s", method, path, exc)
            return ApiResult(0, {"detail": f"the service desk API is unreachable ({exc})"})

    def create_ticket(self, submitted_by_id: int, subject: str, description: str) -> ApiResult:
        return self._request(
            "POST",
            "/tickets",
            {
                "submitted_by_id": submitted_by_id,
                # The column is 200 wide; a chat message has no such limit, so
                # the subject is trimmed here and the full text always survives
                # in the description below.
                "subject": subject[:200],
                "description": description,
                "source": "TELEGRAM",
            },
        )

    def decide(self, ticket_id: int, action: str, decided_by_id: int | None, reason: str | None = None) -> ApiResult:
        return self._request(
            "POST",
            f"/tickets/{ticket_id}/{action}",
            {"decided_by_id": decided_by_id, "reason": reason},
        )

    def instruct(self, ticket_id: int, instruction: str, instructed_by_id: int | None) -> ApiResult:
        return self._request(
            "POST",
            f"/tickets/{ticket_id}/instruct",
            {"instruction": instruction, "instructed_by_id": instructed_by_id},
        )

    def overview(self) -> ApiResult:
        return self._request("GET", "/overview")

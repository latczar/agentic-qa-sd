"""Fixtures for the Telegram bot.

Same Postgres harness as the other suites - one transaction per test, rolled
back at the end. The two things that would otherwise reach the network, the
Telegram API and the service desk API, are both replaced with fakes that
record what they were asked to do.
"""

import pytest

from shared.telegram import FakeTelegramClient
from shared.testing import _schema, db_session  # noqa: F401 - re-exported as pytest fixtures
from telegram_bot.app import dispatch as dispatch_module
from telegram_bot.app.api_client import ApiResult

ALLOWED_CHAT = 555001
BLOCKED_CHAT = 999002


class FakeApi:
    """Stands in for the service desk API.

    Records every call and returns whatever the test scripted, so the bot's
    handling of a 409 or an unreachable API can be exercised without needing
    either condition to actually occur.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.next_result: ApiResult | None = None

    def _result(self, default: ApiResult) -> ApiResult:
        result, self.next_result = self.next_result or default, None
        return result

    def create_ticket(self, submitted_by_id, subject, description):
        self.calls.append(("create_ticket", submitted_by_id, subject, description))
        return self._result(ApiResult(201, {"id": 42}))

    def decide(self, ticket_id, action, decided_by_id, reason=None):
        self.calls.append(("decide", ticket_id, action, decided_by_id))
        return self._result(ApiResult(201, {"id": 1, "decision": action.upper()}))


@pytest.fixture
def telegram():
    return FakeTelegramClient()


@pytest.fixture
def fake_api():
    return FakeApi()


@pytest.fixture(autouse=True)
def allowlist(monkeypatch):
    """One known-good chat and nothing else.

    Patched on the dispatch module rather than on shared.config, because
    dispatch imported the value by name at import time - patching the config
    module would leave the old frozenset in place and every allowlist test
    would pass for the wrong reason.
    """
    monkeypatch.setattr(dispatch_module, "TELEGRAM_ALLOWED_CHAT_IDS", frozenset({ALLOWED_CHAT}))

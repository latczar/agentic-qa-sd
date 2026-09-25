"""What the bot does with an incoming update.

The allowlist tests are the ones that matter most. Anyone can find a bot by
its username and start typing at it, and the buttons this bot sends resolve
real tickets, so "who is allowed to press these" is not a detail.
"""

from shared.models import ProcessedEvent, User, UserRole
from telegram_bot.app.api_client import ApiResult
from telegram_bot.app.dispatch import handle_update
from telegram_bot.tests.conftest import ALLOWED_CHAT, BLOCKED_CHAT


def _linked_user(db_session, chat_id: int, email: str = "linked@example.com") -> User:
    user = User(name="Sam Okafor", email=email, role=UserRole.AGENT, telegram_chat_id=chat_id)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _message(chat_id: int, text: str, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _callback(chat_id: int, data: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "data": data,
            "message": {"message_id": 77, "chat": {"id": chat_id}, "text": "Ticket #42 needs you"},
        },
    }


def test_a_chat_that_is_not_on_the_allowlist_gets_nothing_done(db_session, telegram, fake_api):
    _linked_user(db_session, BLOCKED_CHAT, "stranger@example.com")

    handle_update(db_session, telegram, fake_api, _message(BLOCKED_CHAT, "my laptop is broken"))

    # The refusal is sent, but no ticket is raised and the API is never called.
    assert fake_api.calls == []
    assert "not available" in telegram.sent[0]["text"].lower()


def test_a_button_press_from_an_unknown_chat_decides_nothing(db_session, telegram, fake_api):
    """The important half of the allowlist: the buttons resolve real tickets."""
    handle_update(db_session, telegram, fake_api, _callback(BLOCKED_CHAT, "approve:42"))

    assert fake_api.calls == []
    assert telegram.answered == [("cb-1", "Not available to this account.")]
    # No message edit either: nothing happened, so nothing is reported as having.
    assert telegram.edits == []


def test_an_allowlisted_message_raises_a_ticket(db_session, telegram, fake_api):
    user = _linked_user(db_session, ALLOWED_CHAT)

    handle_update(
        db_session, telegram, fake_api, _message(ALLOWED_CHAT, "VPN keeps dropping\nsince this morning")
    )

    assert fake_api.calls == [
        ("create_ticket", user.id, "VPN keeps dropping", "VPN keeps dropping\nsince this morning")
    ]
    # First line as the subject, whole message as the description: nothing is
    # lost, and the subject is not a truncated fragment.
    assert "#42" in telegram.sent[0]["text"]


def test_an_unlinked_chat_is_asked_who_it_is(db_session, telegram, fake_api):
    handle_update(db_session, telegram, fake_api, _message(ALLOWED_CHAT, "printer is jammed"))

    # Allowlisted, but nobody knows who they are, so the ticket has no author
    # to file it against.
    assert fake_api.calls == []
    assert "/link" in telegram.sent[0]["text"]


def test_linking_binds_the_chat_to_a_person(db_session, telegram, fake_api):
    user = User(name="Priya Raman", email="priya@example.com", role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()

    handle_update(db_session, telegram, fake_api, _message(ALLOWED_CHAT, "/link priya@example.com"))

    db_session.refresh(user)
    assert user.telegram_chat_id == ALLOWED_CHAT
    assert "Priya Raman" in telegram.sent[0]["text"]


def test_linking_an_unknown_email_changes_nothing(db_session, telegram, fake_api):
    handle_update(db_session, telegram, fake_api, _message(ALLOWED_CHAT, "/link nobody@example.com"))

    assert db_session.query(User).filter_by(telegram_chat_id=ALLOWED_CHAT).first() is None
    assert "no user" in telegram.sent[0]["text"].lower()


def test_approving_from_a_button_calls_the_api_and_strips_the_buttons(db_session, telegram, fake_api):
    user = _linked_user(db_session, ALLOWED_CHAT)

    handle_update(db_session, telegram, fake_api, _callback(ALLOWED_CHAT, "approve:42"))

    # Attributed to the linked person, not anonymous.
    assert fake_api.calls == [("decide", 42, "approve", user.id)]
    assert telegram.answered == [("cb-1", "Approved")]
    # The edit carries no reply_markup, which is what removes the buttons. A
    # live Approve button on a decided ticket can only ever fail.
    assert "Approved - ticket #42" in telegram.edits[0]["text"]


def test_a_ticket_someone_already_decided_is_reported_plainly(db_session, telegram, fake_api):
    """Tapping an old message is normal, not an error to show a stack trace for."""
    _linked_user(db_session, ALLOWED_CHAT)
    fake_api.next_result = ApiResult(409, {"detail": "ticket is RESOLVED, not AWAITING_APPROVAL"})

    handle_update(db_session, telegram, fake_api, _callback(ALLOWED_CHAT, "approve:42"))

    assert telegram.answered == [("cb-1", "Already decided")]
    assert "RESOLVED" in telegram.edits[0]["text"]


def test_a_malformed_button_payload_is_not_turned_into_a_request(db_session, telegram, fake_api):
    _linked_user(db_session, ALLOWED_CHAT)

    handle_update(db_session, telegram, fake_api, _callback(ALLOWED_CHAT, "delete:../../admin"))

    assert fake_api.calls == []
    assert telegram.answered == [("cb-1", "I do not understand that button.")]


def test_the_same_update_is_never_acted_on_twice(db_session, telegram, fake_api):
    """Telegram redelivers until the offset moves, so this has to hold.

    Without it, a crash between "approved the ticket" and "advanced the
    offset" approves the same ticket again on the next poll.
    """
    user = _linked_user(db_session, ALLOWED_CHAT)
    update = _callback(ALLOWED_CHAT, "approve:42", update_id=900)

    handle_update(db_session, telegram, fake_api, update)
    handle_update(db_session, telegram, fake_api, update)

    assert fake_api.calls == [("decide", 42, "approve", user.id)]
    assert db_session.query(ProcessedEvent).filter_by(event_id="tg-900").count() == 1


def test_an_update_that_blows_up_is_still_marked_as_seen(db_session, telegram, fake_api):
    """One bad message must not wedge the queue behind it forever."""
    _linked_user(db_session, ALLOWED_CHAT)

    def explode(*args, **kwargs):
        raise RuntimeError("something went wrong")

    fake_api.decide = explode

    try:
        handle_update(db_session, telegram, fake_api, _callback(ALLOWED_CHAT, "approve:42", update_id=901))
    except RuntimeError:
        pass

    assert db_session.query(ProcessedEvent).filter_by(event_id="tg-901").count() == 1


def test_instruct_passes_the_correction_through(db_session, telegram, fake_api):
    user = _linked_user(db_session, ALLOWED_CHAT)

    handle_update(
        db_session, telegram, fake_api, _message(ALLOWED_CHAT, "/instruct 42 it is a network fault")
    )

    assert fake_api.calls == [("instruct", 42, "it is a network fault", user.id)]


def test_instruct_without_a_ticket_number_explains_itself(db_session, telegram, fake_api):
    _linked_user(db_session, ALLOWED_CHAT)

    handle_update(db_session, telegram, fake_api, _message(ALLOWED_CHAT, "/instruct fix it please"))

    assert fake_api.calls == []
    assert "/instruct 42" in telegram.sent[0]["text"]

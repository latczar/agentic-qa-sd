"""The numbers behind the console.

What matters here is that the stage states are derived from the counts rather
than set by hand anywhere, because the console paints its dots from them: a
stage reporting "idle" while holding work would show a green board over a
stuck pipeline, which is the one thing an overview must not do.
"""

from shared.models import Ticket, TicketSource, TicketStatus, User, UserRole


def _user(db_session, email: str) -> User:
    user = User(name="Test User", email=email, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _ticket(db_session, user_id: int, status: TicketStatus, source=TicketSource.WEB) -> Ticket:
    ticket = Ticket(
        submitted_by_id=user_id,
        subject="Something",
        description="...",
        status=status,
        source=source,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def _stages(body) -> dict:
    return {s["key"]: s for s in body["stages"]}


def test_counts_and_stage_states_follow_the_tickets(client, db_session):
    user = _user(db_session, "overview@example.com")
    _ticket(db_session, user.id, TicketStatus.AWAITING_APPROVAL)
    _ticket(db_session, user.id, TicketStatus.AWAITING_APPROVAL)
    _ticket(db_session, user.id, TicketStatus.QUEUED)
    _ticket(db_session, user.id, TicketStatus.FAILED)

    body = client.get("/overview").json()
    stages = _stages(body)

    assert body["needs_you"] == 2
    assert stages["needs_you"]["count"] == 2
    # Amber, not green: two tickets are sitting on a person.
    assert stages["needs_you"]["state"] == "waiting"
    # Work in the queue is the system running normally.
    assert stages["queued"]["state"] == "flowing"
    # A failed ticket is not "work in progress", it is something broken.
    assert stages["failed"]["state"] == "stuck"
    # Nothing in analysis, so nothing to report.
    assert stages["analysing"]["count"] == 0
    assert stages["analysing"]["state"] == "idle"


def test_an_empty_pipeline_reports_every_stage_idle(client, db_session):
    body = client.get("/overview").json()
    assert body["needs_you"] == 0
    assert {s["state"] for s in body["stages"]} == {"idle"}


def test_every_stage_says_what_it_is_for(client, db_session):
    """The console prints this text, and a stage with no explanation is a
    number nobody can act on."""
    body = client.get("/overview").json()
    assert all(s["does"].strip() for s in body["stages"])
    assert all(s["label"].strip() for s in body["stages"])


def test_the_channel_split_is_reported(client, db_session):
    user = _user(db_session, "channels@example.com")
    _ticket(db_session, user.id, TicketStatus.NEW, TicketSource.TELEGRAM)
    _ticket(db_session, user.id, TicketStatus.NEW, TicketSource.WEB)
    _ticket(db_session, user.id, TicketStatus.NEW, TicketSource.WEB)

    body = client.get("/overview").json()

    assert body["by_source"] == {"WEB": 2, "TELEGRAM": 1}
    # Both keys are always present, so the console never has to guess whether
    # a missing channel means zero or means the field was dropped.
    assert set(body["by_source"]) == {"WEB", "TELEGRAM"}


def test_open_total_excludes_finished_tickets(client, db_session):
    user = _user(db_session, "open@example.com")
    _ticket(db_session, user.id, TicketStatus.QUEUED)
    _ticket(db_session, user.id, TicketStatus.ESCALATED)
    _ticket(db_session, user.id, TicketStatus.RESOLVED)
    _ticket(db_session, user.id, TicketStatus.FAILED)

    body = client.get("/overview").json()

    # Escalated counts as open - it is still somebody's problem. Resolved and
    # failed do not: one is finished and the other has stopped.
    assert body["open_total"] == 2
    assert body["escalated_open"] == 1


def test_health_reports_every_dependency_without_raising(client):
    """Whatever is or is not running locally, this must answer.

    A health endpoint that 500s when a dependency is down tells you nothing
    at exactly the moment you need it to.
    """
    response = client.get("/overview/health")

    assert response.status_code == 200
    names = {d["name"] for d in response.json()}
    assert {"Postgres", "RabbitMQ", "Ollama", "MCP server", "Telegram"} <= names
    assert all(isinstance(d["ok"], bool) and d["detail"] for d in response.json())


def test_telegram_switched_off_is_reported_as_off_not_down(client):
    """Not configuring an optional feature is not an outage."""
    telegram = next(d for d in client.get("/overview/health").json() if d["name"] == "Telegram")

    assert telegram["state"] == "off"

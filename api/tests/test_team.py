"""The overseer's team and routines view.

Two kinds of test. The drift guards read the files the routines are defined
in, so the page cannot go on describing a schedule that has since changed.
The rest check that the evidence is read correctly - above all that a routine
which has stopped leaving traces is reported as quiet, because a routines
page that shows green over a stopped chaser is worse than no page.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.routers import team
from shared.models import AuditLog, KnowledgeArticle, Ticket, TicketComment, TicketStatus, User, UserRole

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The health checks cross the network; these tests are about the data."""
    monkeypatch.setattr(team, "_n8n_up", lambda: (True, "stubbed"))
    monkeypatch.setattr(team, "_port_open", lambda url, port: (True, "stubbed"))


def _routine(client, key):
    return next(r for r in client.get("/overseer/team").json()["routines"] if r["key"] == key)


def _member(client, key):
    return next(m for m in client.get("/overseer/team").json()["members"] if m["key"] == key)


def _waiting_ticket(db_session, email, waited: timedelta) -> Ticket:
    user = User(name="Test User", email=email, role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    ticket = Ticket(
        submitted_by_id=user.id,
        subject="Waiting",
        description="...",
        status=TicketStatus.AWAITING_APPROVAL,
        updated_at=datetime.now(timezone.utc) - waited,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def _chase(db_session, ticket, at: datetime):
    db_session.add(
        TicketComment(
            ticket_id=ticket.id,
            body=f"⏰ Reminder: still awaiting approval. {team.SLA_COMMENT_MARK}.",
            created_at=at,
        )
    )
    db_session.commit()


# --- Drift guards -----------------------------------------------------------


def test_the_chaser_schedule_matches_the_workflow_file():
    workflow = json.loads((REPO / "n8n/workflows/sla-chaser.json").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in workflow["nodes"]}

    interval = nodes["Every 15 minutes"]["parameters"]["rule"]["interval"][0]["minutesInterval"]
    code = next(n["parameters"]["jsCode"] for n in workflow["nodes"] if "jsCode" in n.get("parameters", {}))
    chase_at = json.loads(re.search(r"CHASE_AT_MINUTES = (\[[^\]]+\])", code).group(1))
    run_every = int(re.search(r"RUN_EVERY_MINUTES = (\d+)", code).group(1))

    assert team.SLA_RUN_EVERY_MINUTES == interval == run_every
    assert list(team.SLA_CHASE_AT_MINUTES) == chase_at


def test_the_comment_marks_are_what_the_workflows_actually_post():
    """These marks are how "last ran" is found. A reworded comment would
    otherwise make a healthy routine look as though it had stopped."""
    chaser = (REPO / "n8n/workflows/sla-chaser.json").read_text(encoding="utf-8")
    notifier = json.loads((REPO / "n8n/workflows/ticket-approval.json").read_text(encoding="utf-8"))
    bodies = json.dumps(notifier, ensure_ascii=False)

    assert team.SLA_COMMENT_MARK in chaser
    for mark in team.NOTIFIER_COMMENT_MARKS:
        assert mark in bodies, mark


def test_the_retry_description_matches_the_worker():
    consumer = (REPO / "worker/app/consumer.py").read_text(encoding="utf-8")

    assert int(re.search(r"^MAX_RETRIES = (\d+)", consumer, re.M).group(1)) == team.RETRY_MAX_ATTEMPTS
    base_ms = int(re.search(r"^BASE_DELAY_MS = ([\d_]+)", consumer, re.M).group(1).replace("_", ""))
    assert base_ms // 1000 == team.RETRY_BASE_SECONDS


# --- The SLA chaser ---------------------------------------------------------


def test_a_ticket_short_of_reminders_makes_the_chaser_quiet(client, db_session):
    """Three hours of waiting should have produced three reminders (30m, 1h, 2h)."""
    ticket = _waiting_ticket(db_session, "short@example.com", timedelta(hours=3))
    _chase(db_session, ticket, datetime.now(timezone.utc) - timedelta(hours=2))

    chaser = _routine(client, "sla_chaser")

    assert chaser["state"] == "quiet"
    assert f"#{ticket.id}" in chaser["note"]
    assert "3 reminders" in chaser["note"] and "has had 1" in chaser["note"]


def test_a_ticket_with_all_its_reminders_leaves_the_chaser_ok(client, db_session):
    ticket = _waiting_ticket(db_session, "chased@example.com", timedelta(hours=3))
    now = datetime.now(timezone.utc)
    for minutes_ago in (150, 120, 60):
        _chase(db_session, ticket, now - timedelta(minutes=minutes_ago))

    chaser = _routine(client, "sla_chaser")

    assert chaser["state"] == "ok"
    assert chaser["note"] is None


def test_a_reminder_is_not_counted_missing_until_a_whole_run_has_passed(client, db_session):
    """At 35 minutes the 30-minute reminder may simply not have run yet."""
    _waiting_ticket(db_session, "fresh@example.com", timedelta(minutes=35))

    assert _routine(client, "sla_chaser")["state"] != "quiet"


def test_with_nothing_to_chase_the_chaser_is_unknown_not_ok(client, db_session):
    """No evidence either way is not the same as evidence that it works."""
    assert _routine(client, "sla_chaser")["state"] == "unknown"


# --- The approval notifier --------------------------------------------------


def _approval_request(db_session, notified, minutes_ago: int) -> Ticket:
    ticket = _waiting_ticket(db_session, f"notify{minutes_ago}{notified!r}@example.com".replace(" ", ""), timedelta(minutes=minutes_ago))
    db_session.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="human_approval_requested",
            detail={"reason": "confidence 0.50 is below 0.85", "notified": notified},
            created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
    )
    db_session.commit()
    return ticket


def test_a_request_n8n_never_answered_makes_the_notifier_quiet(client, db_session):
    ticket = _approval_request(db_session, {"n8n": True, "telegram": False}, minutes_ago=10)

    notifier = _routine(client, "approval_notifier")

    assert notifier["state"] == "quiet"
    assert f"#{ticket.id}" in notifier["note"]


def test_a_request_the_worker_could_not_deliver_is_reported_as_such(client, db_session):
    ticket = _approval_request(db_session, {"n8n": False, "telegram": False}, minutes_ago=10)

    notifier = _routine(client, "approval_notifier")

    assert notifier["state"] == "quiet"
    assert "could not reach n8n" in notifier["note"] and f"#{ticket.id}" in notifier["note"]


def test_older_rows_that_stored_a_plain_bool_are_still_read(client, db_session):
    """Rows from before the Telegram channel store notified as True/False."""
    _approval_request(db_session, False, minutes_ago=10)

    assert "could not reach n8n" in _routine(client, "approval_notifier")["note"]


def test_a_request_answered_with_a_note_leaves_the_notifier_ok(client, db_session):
    ticket = _approval_request(db_session, {"n8n": True}, minutes_ago=10)
    db_session.add(
        TicketComment(
            ticket_id=ticket.id,
            body="Queued for agent review (MEDIUM, confidence 0.5).",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=9),
        )
    )
    db_session.commit()

    notifier = _routine(client, "approval_notifier")

    assert notifier["state"] == "ok"
    assert notifier["evidence"] == f"Last note posted on #{ticket.id}"


# --- Knowledge and the team -------------------------------------------------


def test_articles_without_embeddings_make_loading_quiet(client, db_session):
    db_session.add(KnowledgeArticle(slug="team-no-vector", title="T", body="b", content_hash="h1"))
    db_session.commit()

    loading = _routine(client, "ingestion")

    assert loading["state"] == "quiet"
    assert "cannot be searched" in loading["note"]


def test_the_chart_is_honest_about_what_each_member_is(client):
    members = client.get("/overseer/team").json()["members"]
    keys = {m["key"] for m in members}

    assert {m["kind"] for m in members} == {"person", "agent", "tool", "bot", "automation"}
    # One person at the top, everyone else reporting to someone who exists.
    assert [m["key"] for m in members if m["reports_to"] is None] == ["you"]
    assert all(m["reports_to"] in keys for m in members if m["reports_to"])


def test_telegram_that_was_never_set_up_is_off(client):
    assert _member(client, "telegram")["state"] == "off"


def test_every_routine_belongs_to_a_member(client):
    body = client.get("/overseer/team").json()
    keys = {m["key"] for m in body["members"]}

    assert all(r["owner"] in keys for r in body["routines"])

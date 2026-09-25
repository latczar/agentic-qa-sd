"""The overseer's team view: who does the work, and the routines that keep it moving.

Borrowed from Paperclip's idea of an org chart for agents, and kept honest in
one specific way: every member here is a real, running part of this system,
described as what it is. A person, an AI agent, a tool, a bot and an
automation are different things. Drawing them all as "employees" with names
and personalities would make the chart look busier and claim more than the
system does, and the first question in any demo would be what the difference
between two of them actually is.

Everything reported comes from our own database, from a health check that
needs no credentials, or not at all. Where this page cannot know something -
whether an n8n workflow is active, whether the Telegram bot's container is
running - it says so rather than painting a green dot over the gap.
"""

import urllib.error
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.routers.overseer import _start_of_today
from app.routers.overview import _port_open
from app.schemas import RoutineOut, TeamMemberOut, TeamOut
from shared.config import MCP_SERVER_URL, N8N_URL, TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_PROVIDER
from shared.db import get_db
from shared.models import (
    AgentRun,
    AgentRunStatus,
    Approval,
    AuditLog,
    KnowledgeArticle,
    Ticket,
    TicketComment,
    TicketSource,
    TicketStatus,
    User,
)

router = APIRouter(prefix="/overseer", tags=["overseer"])

# Copied from the definitions they describe. api/tests/test_team.py reads those
# files and fails if these drift, so this page cannot go on describing a
# schedule that has since changed.
SLA_RUN_EVERY_MINUTES = 15  # n8n/workflows/sla-chaser.json, the schedule trigger
SLA_CHASE_AT_MINUTES = (30, 60, 120, 240, 480)  # same file, CHASE_AT_MINUTES
RETRY_BASE_SECONDS = 5  # worker/app/consumer.py, BASE_DELAY_MS
RETRY_MAX_ATTEMPTS = 3  # worker/app/consumer.py, MAX_RETRIES

# How each n8n workflow signs the comments it posts. They are the only trace
# those workflows leave in our data, so they are how "last ran" is known.
SLA_COMMENT_MARK = "Chased automatically by the SLA workflow"
NOTIFIER_COMMENT_MARKS = ("Queued for agent review", "🚨 Paged on-call")

# A notification is posted a moment after the request that triggers it. Only
# call the notifier quiet once a request has gone unanswered for longer than
# that, or every ticket would briefly look like a failure.
NOTIFIER_GRACE_MINUTES = 2


def _n8n_up() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{N8N_URL.rstrip('/')}/healthz", timeout=1.5) as response:
            return 200 <= response.status < 300, f"{N8N_URL} answered /healthz"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{N8N_URL} unreachable ({exc})"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _duration(minutes: float) -> str:
    """"3 days", "5 hours", "40 minutes" - for sentences a person reads."""
    if minutes >= 2 * 24 * 60:
        return _plural(int(minutes // (24 * 60)), "day")
    if minutes >= 120:
        return _plural(int(minutes // 60), "hour")
    return _plural(max(int(minutes), 1), "minute")


def _reached_n8n(detail: dict | None) -> bool:
    """Whether a human_approval_requested row says n8n took the request.

    Rows written before the Telegram channel store a plain bool; later rows
    store one entry per channel. Both are the record, so both are read.
    """
    notified = (detail or {}).get("notified")
    if isinstance(notified, dict):
        return bool(notified.get("n8n"))
    return bool(notified)


# --- Members ------------------------------------------------------------------


def _you(db: Session, since: datetime) -> TeamMemberOut:
    waiting = db.query(func.count(Ticket.id)).filter(Ticket.status == TicketStatus.AWAITING_APPROVAL).scalar() or 0
    decided_today = db.query(func.count(Approval.id)).filter(Approval.created_at >= since).scalar() or 0
    last_decision = db.query(func.max(Approval.created_at)).scalar()
    return TeamMemberOut(
        key="you",
        name="You",
        kind="person",
        role="Decides anything the AI should not decide alone",
        reports_to=None,
        state="waiting" if waiting else "up",
        status=f"{_plural(waiting, 'ticket')} waiting for you" if waiting else "Nothing is waiting for you",
        facts=[f"{_plural(decided_today, 'decision')} today"],
        tech=[
            "Decides through /approve, /handled, /reject and /instruct",
            "Every decision is kept in approvals and audit_logs",
        ],
        last_active=last_decision,
    )


def _triage(db: Session, since: datetime) -> TeamMemberOut:
    runs_today = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since).scalar() or 0
    average_ms = (
        db.query(func.avg(AgentRun.latency_ms))
        .filter(AgentRun.created_at >= since, AgentRun.status == AgentRunStatus.SUCCEEDED)
        .scalar()
    )
    solved_alone = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.event_type == "auto_recommended", AuditLog.created_at >= since)
        .scalar()
        or 0
    )
    sent_to_you = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.event_type == "human_approval_requested", AuditLog.created_at >= since)
        .scalar()
        or 0
    )
    last_run = db.query(AgentRun).order_by(AgentRun.id.desc()).first()

    facts = [f"{_plural(runs_today, 'ticket')} analysed today"]
    if runs_today:
        facts.append(f"Solved {solved_alone} on its own, sent {sent_to_you} to you")
    if average_ms:
        facts.append(f"About {round(average_ms / 1000)} seconds a ticket")

    return TeamMemberOut(
        key="triage",
        name="Triage agent",
        kind="agent",
        role="Reads each ticket, looks up the help articles and proposes a fix",
        reports_to="you",
        # What it is doing right now comes from the live stream on the page,
        # which knows far better than a database query can. This is only the
        # starting point before the first event arrives.
        state="idle",
        status="Waiting for work",
        facts=facts,
        tech=[
            f"Model: {last_run.model}" if last_run else "Model: not used yet",
            "Consumes ticket.processing on RabbitMQ, one ticket at a time",
            "Scale out with: docker compose up -d --scale worker=3",
        ],
        last_active=last_run.created_at if last_run else None,
    )


def _knowledge(db: Session, since: datetime) -> TeamMemberOut:
    reachable, detail = _port_open(MCP_SERVER_URL, 8080)
    total = db.query(func.count(KnowledgeArticle.id)).scalar() or 0
    searchable = db.query(func.count(KnowledgeArticle.id)).filter(KnowledgeArticle.embedding.is_not(None)).scalar() or 0
    searches_today = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.event_type == "rag_documents_retrieved", AuditLog.created_at >= since)
        .scalar()
        or 0
    )
    last_search = (
        db.query(func.max(AuditLog.created_at)).filter(AuditLog.event_type == "rag_documents_retrieved").scalar()
    )
    if not reachable:
        status = "Not answering, so the triage agent cannot look anything up"
    elif searchable < total:
        status = f"Only {searchable} of {total} help articles are searchable"
    else:
        status = f"Ready: {_plural(total, 'help article')}, all searchable"
    return TeamMemberOut(
        key="knowledge",
        name="Knowledge search",
        kind="tool",
        role="Finds the help articles that match a ticket",
        reports_to="triage",
        state="up" if reachable else "down",
        status=status,
        facts=[f"{_plural(searches_today, 'search', 'searches')} today"],
        tech=[
            "MCP server over streamable HTTP",
            "Tools: search_knowledge_base, get_ticket, get_service_status",
            "pgvector cosine search with a relevance cut-off",
            detail,
        ],
        last_active=last_search,
    )


def _telegram(db: Session, since: datetime) -> TeamMemberOut:
    total = db.query(func.count(Ticket.id)).filter(Ticket.source == TicketSource.TELEGRAM).scalar() or 0
    today = (
        db.query(func.count(Ticket.id))
        .filter(Ticket.source == TicketSource.TELEGRAM, Ticket.created_at >= since)
        .scalar()
        or 0
    )
    linked = db.query(func.count(User.id)).filter(User.telegram_chat_id.is_not(None)).scalar() or 0
    last_ticket = db.query(func.max(Ticket.created_at)).filter(Ticket.source == TicketSource.TELEGRAM).scalar()

    if TELEGRAM_PROVIDER != "http":
        state, status = "off", "Not set up. Give it a bot token to switch it on"
    elif not TELEGRAM_ALLOWED_CHAT_IDS:
        state, status = "down", "Has a token, but nobody is on the allowlist, so it answers no one"
    else:
        state, status = "up", f"Set up for {_plural(len(TELEGRAM_ALLOWED_CHAT_IDS), 'chat')}"

    return TeamMemberOut(
        key="telegram",
        name="Telegram concierge",
        kind="bot",
        role="Takes tickets from a chat and brings your decisions back",
        reports_to="you",
        state=state,
        status=status,
        facts=[f"{_plural(total, 'ticket')} raised from Telegram ({today} today)", f"{_plural(linked, 'person', 'people')} linked"],
        tech=[
            "Long-polls getUpdates, so it needs no public URL",
            "Runs in sd-telegram-bot, behind the 'telegram' Compose profile",
            "Whether that container is running is not visible from here",
        ],
        last_active=last_ticket,
    )


def _automations(db: Session, since: datetime) -> TeamMemberOut:
    up, detail = _n8n_up()
    today = [
        log.detail
        for log in db.query(AuditLog).filter(
            AuditLog.event_type == "human_approval_requested", AuditLog.created_at >= since
        )
    ]
    sent = len([d for d in today if _reached_n8n(d)])
    last_comment = (
        db.query(func.max(TicketComment.created_at))
        .filter(
            TicketComment.body.contains(SLA_COMMENT_MARK)
            | TicketComment.body.startswith(NOTIFIER_COMMENT_MARKS[0])
            | TicketComment.body.startswith(NOTIFIER_COMMENT_MARKS[1])
        )
        .scalar()
    )
    return TeamMemberOut(
        key="automations",
        name="Automations",
        kind="automation",
        role="Tells you when something needs you, and chases you if it waits",
        reports_to="you",
        state="up" if up else "down",
        status="n8n is up" if up else "n8n is not answering",
        facts=[f"{_plural(sent, 'approval request')} handed to it today"],
        tech=[
            "n8n workflows: Ticket approval, SLA chaser, Error handler",
            "Defined in n8n/workflows/ and imported with the n8n CLI",
            "Which workflows are active is only visible inside n8n",
            detail,
        ],
        last_active=last_comment,
    )


# --- Routines -----------------------------------------------------------------


def _approval_notifier(db: Session) -> RoutineOut:
    last_request = (
        db.query(AuditLog).filter(AuditLog.event_type == "human_approval_requested").order_by(AuditLog.id.desc()).first()
    )
    last_reply = (
        db.query(TicketComment)
        .filter(
            TicketComment.body.startswith(NOTIFIER_COMMENT_MARKS[0])
            | TicketComment.body.startswith(NOTIFIER_COMMENT_MARKS[1])
        )
        .order_by(TicketComment.id.desc())
        .first()
    )
    now = datetime.now(timezone.utc)

    state, note = "ok", None
    if last_request is None:
        state = "ok" if last_reply else "unknown"
        note = None if last_reply else "Nothing has needed a person yet, so there is nothing to check."
    elif not _reached_n8n(last_request.detail):
        state = "quiet"
        note = f"The last time a ticket needed you (#{last_request.ticket_id}), the triage agent could not reach n8n."
    elif (last_reply is None or last_reply.created_at < last_request.created_at) and (
        now - last_request.created_at
    ).total_seconds() > NOTIFIER_GRACE_MINUTES * 60:
        # n8n accepted the request but never posted its note back, which is
        # what an imported-but-inactive workflow looks like from here.
        state = "quiet"
        note = (
            f"#{last_request.ticket_id} was handed to n8n but no note came back. "
            "Check the Ticket approval workflow is active in n8n."
        )

    return RoutineOut(
        key="approval_notifier",
        name="Approval notifier",
        owner="automations",
        trigger="When a ticket is sent to you",
        does="Posts a note on the ticket saying why it is waiting and what the AI proposed. Critical tickets page on-call first.",
        state=state,
        last_evidence=last_reply.created_at if last_reply else None,
        evidence=f"Last note posted on #{last_reply.ticket_id}" if last_reply else None,
        note=note,
        tech=[
            "n8n workflow 'Ticket approval', webhook /webhook/ticket-approval",
            "Called by the worker, calls back into POST /tickets/{id}/comments",
        ],
    )


def _sla_chaser(db: Session) -> RoutineOut:
    waiting = db.query(Ticket.id, Ticket.updated_at).filter(Ticket.status == TicketStatus.AWAITING_APPROVAL).all()
    chases = db.query(TicketComment.ticket_id, TicketComment.created_at).filter(
        TicketComment.body.contains(SLA_COMMENT_MARK)
    )
    chases_by_ticket: dict[int, list[datetime]] = {}
    for ticket_id, created_at in chases:
        chases_by_ticket.setdefault(ticket_id, []).append(created_at)
    last_chase = db.query(TicketComment).filter(TicketComment.body.contains(SLA_COMMENT_MARK)).order_by(
        TicketComment.id.desc()
    ).first()

    # The chaser keeps no state: each run reminds about any ticket that crossed
    # a milestone in the window that run covers. So how many reminders a
    # waiting ticket should have had is arithmetic on how long it has waited,
    # and fewer than that means runs did not happen. A milestone only counts as
    # missed once a whole run has passed after it.
    now = datetime.now(timezone.utc)
    worst = None
    # Working is only claimed on evidence from now: a waiting ticket that was
    # due reminders and got every one. A reminder posted last week says the
    # chaser worked last week, not that it is working today.
    checked = False
    for ticket_id, since in waiting:
        waited = (now - since).total_seconds() / 60
        due = sum(1 for m in SLA_CHASE_AT_MINUTES if waited >= m + SLA_RUN_EVERY_MINUTES)
        got = len([c for c in chases_by_ticket.get(ticket_id, []) if c >= since])
        if got < due and (worst is None or due - got > worst[1] - worst[2]):
            worst = (ticket_id, due, got, waited)
        if due and got >= due:
            checked = True

    if worst:
        ticket_id, due, got, waited = worst
        state = "quiet"
        note = (
            f"#{ticket_id} has waited {_duration(waited)} and should have had "
            f"{_plural(due, 'reminder')} by now, but has had {got}. "
            # Two causes look identical from here, so both are named: the
            # workflow is not active in n8n, or nothing was running at all
            # while that time passed. Blaming n8n alone would be a guess.
            "Either the chaser is not active in n8n, or the stack was not running "
            "while that time passed."
        )
    elif checked:
        state, note = "ok", None
    else:
        state, note = "unknown", "Nothing has waited long enough to need a reminder, so there is nothing to check."

    return RoutineOut(
        key="sla_chaser",
        name="SLA chaser",
        owner="automations",
        trigger=f"Every {SLA_RUN_EVERY_MINUTES} minutes",
        does="Reminds you about tickets that have waited 30 minutes, 1, 2, 4 and 8 hours, most urgent first, then stops.",
        state=state,
        last_evidence=last_chase.created_at if last_chase else None,
        evidence=f"Last reminder posted on #{last_chase.ticket_id}" if last_chase else None,
        note=note,
        tech=[
            f"n8n workflow 'SLA chaser', schedule trigger every {SLA_RUN_EVERY_MINUTES} min",
            "Reads GET /tickets, writes POST /tickets/{id}/comments",
            "Stores nothing: backs off by milestone arithmetic on updated_at",
        ],
    )


def _error_handler() -> RoutineOut:
    return RoutineOut(
        key="error_handler",
        name="Error handler",
        owner="automations",
        trigger="When either n8n workflow fails",
        does="Records the failure. It never retries, so a failed reminder is never mistaken for a failed ticket.",
        state="unknown",
        last_evidence=None,
        evidence=None,
        note="Its runs only appear in n8n's own run history, which this page cannot read without an n8n API key.",
        tech=["n8n workflow 'Error handler', set as errorWorkflow on the other two"],
    )


def _retry(db: Session) -> RoutineOut:
    last = db.query(AuditLog).filter(AuditLog.event_type == "ticket_dead_lettered").order_by(AuditLog.id.desc()).first()
    delays = ", ".join(f"{RETRY_BASE_SECONDS * 2**i} s" for i in range(RETRY_MAX_ATTEMPTS))
    if last:
        reason = (last.detail or {}).get("reason", "unknown reason")
        where = f"#{last.ticket_id}" if last.ticket_id else "a message with no ticket"
        evidence, note = f"Last gave up on {where}: {reason}", None
    else:
        evidence, note = None, "Nothing has had to be given up on."
    return RoutineOut(
        key="retry",
        name="Retry with back-off",
        owner="triage",
        trigger="When a step fails in a way worth trying again",
        does=f"Tries again after {delays}. After {RETRY_MAX_ATTEMPTS} retries it gives up and marks the ticket failed.",
        # Retries themselves are not written anywhere; giving up is. The floor
        # shows the live count of tickets backing off, straight from the broker.
        state="ok",
        last_evidence=last.created_at if last else None,
        evidence=evidence,
        note=note,
        tech=[
            "ticket.retry holds each message for its TTL, then dead-letters it back to ticket.processing",
            "Gives up to ticket.dead-letter and marks the ticket FAILED",
        ],
    )


def _ingestion(db: Session) -> RoutineOut:
    total = db.query(func.count(KnowledgeArticle.id)).scalar() or 0
    searchable = db.query(func.count(KnowledgeArticle.id)).filter(KnowledgeArticle.embedding.is_not(None)).scalar() or 0
    last = db.query(func.max(KnowledgeArticle.updated_at)).scalar()
    if total == 0:
        state, note = "unknown", "No help articles have been loaded yet."
    elif searchable < total:
        state = "quiet"
        note = f"{_plural(total - searchable, 'article')} cannot be searched yet. Run it again with Ollama running."
    else:
        state, note = "ok", None
    return RoutineOut(
        key="ingestion",
        name="Knowledge loading",
        owner="knowledge",
        trigger="When someone runs it",
        does="Reads the help articles in knowledge/ and makes them searchable. Skips any that have not changed.",
        state=state,
        last_evidence=last,
        evidence=f"{searchable} of {_plural(total, 'article')} searchable, last loaded" if total else None,
        note=note,
        tech=[
            "docker compose exec api python -m app.ingest_knowledge",
            "SHA-256 content hash per article; nomic-embed-text, 768 dimensions",
        ],
    )


@router.get("/team", response_model=TeamOut)
def overseer_team(db: Session = Depends(get_db)) -> TeamOut:
    since = _start_of_today()
    return TeamOut(
        members=[
            _you(db, since),
            _triage(db, since),
            _knowledge(db, since),
            _telegram(db, since),
            _automations(db, since),
        ],
        routines=[
            _approval_notifier(db),
            _sla_chaser(db),
            _retry(db),
            _ingestion(db),
            _error_handler(),
        ],
    )

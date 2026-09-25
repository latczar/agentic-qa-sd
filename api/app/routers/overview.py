"""Numbers for the console.

Two endpoints rather than one, split by how expensive they are. /overview is
pure SQL against the local database, so it is cheap enough for the console to
poll every few seconds. /overview/health reaches out to other processes over
the network, so it is slower, can fail, and is polled far less often. Folding
them together would have made the fast one as slow as the slow one.
"""

import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.schemas import DependencyOut, OverviewOut, StageOut
from shared.config import (
    MCP_SERVER_URL,
    OLLAMA_URL,
    RABBITMQ_URL,
    TELEGRAM_ALLOWED_CHAT_IDS,
    TELEGRAM_PROVIDER,
)
from shared.db import get_db, ping_database
from shared.models import Ticket, TicketSource, TicketStatus

router = APIRouter(prefix="/overview", tags=["overview"])

# The pipeline as a person would describe it, in the order work moves through
# it. "does" is shown in the console next to each stage: the point of the view
# is that someone who has never seen the system can read what each queue is
# for without being told.
STAGES: list[tuple[str, TicketStatus, str, str]] = [
    ("intake", TicketStatus.NEW, "Intake", "Ticket accepted, not yet announced to the queue"),
    ("queued", TicketStatus.QUEUED, "Queue", "On RabbitMQ, waiting for a free worker"),
    ("analysing", TicketStatus.PROCESSING, "Analysis", "Worker is retrieving knowledge and asking the model"),
    ("needs_you", TicketStatus.AWAITING_APPROVAL, "Needs you", "The confidence gate routed this to a person"),
    ("escalated", TicketStatus.ESCALATED, "Escalated", "A person overruled the agent; still open"),
    ("failed", TicketStatus.FAILED, "Failed", "The pipeline could not produce an answer"),
    ("resolved", TicketStatus.RESOLVED, "Resolved", "Closed, by the agent or by a person"),
]

# Which stages mean a human is blocked, and which mean something is broken.
# Kept as data rather than an if-tree so the console's colour and this list
# cannot drift apart.
WAITING_ON_A_PERSON = {"needs_you", "escalated"}
BROKEN = {"failed"}


def _state(key: str, count: int) -> str:
    if count == 0:
        return "idle"
    if key in BROKEN:
        return "stuck"
    if key in WAITING_ON_A_PERSON:
        return "waiting"
    return "flowing"


@router.get("", response_model=OverviewOut)
def get_overview(db: Session = Depends(get_db)) -> OverviewOut:
    # One grouped query rather than seven counts, because the console polls
    # this and seven round trips per poll adds up for no benefit.
    counts = dict(
        db.query(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status).all()
    )
    sources = dict(
        db.query(Ticket.source, func.count(Ticket.id)).group_by(Ticket.source).all()
    )

    stages = [
        StageOut(key=key, label=label, does=does, count=counts.get(status, 0), state=_state(key, counts.get(status, 0)))
        for key, status, label, does in STAGES
    ]

    # Midnight UTC, not local midnight. The containers run in UTC and the
    # browser may not, so "today" is defined once here rather than differently
    # in every caller.
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    resolved_today = (
        db.query(func.count(Ticket.id))
        .filter(Ticket.status == TicketStatus.RESOLVED, Ticket.updated_at >= since)
        .scalar()
        or 0
    )

    open_statuses = [
        TicketStatus.NEW,
        TicketStatus.QUEUED,
        TicketStatus.PROCESSING,
        TicketStatus.AWAITING_APPROVAL,
        TicketStatus.ESCALATED,
    ]

    return OverviewOut(
        needs_you=counts.get(TicketStatus.AWAITING_APPROVAL, 0),
        stages=stages,
        by_source={s.value: sources.get(s, 0) for s in TicketSource},
        open_total=sum(counts.get(s, 0) for s in open_statuses),
        resolved_today=resolved_today,
        escalated_open=counts.get(TicketStatus.ESCALATED, 0),
    )


def _port_open(url: str, default_port: int, timeout: float = 1.5) -> tuple[bool, str]:
    """Is anything listening? Deliberately not a protocol handshake.

    A full AMQP or MCP handshake on every health poll costs more than the
    answer is worth here, so this reports exactly what it checked - the port
    is open - and the wording says so rather than implying more.
    """
    parsed = urllib.parse.urlsplit(url)
    host, port = parsed.hostname, parsed.port or default_port
    if not host:
        return False, f"could not read a host out of {url!r}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"port {port} open on {host}"
    except OSError as exc:
        return False, f"{host}:{port} unreachable ({exc})"


@router.get("/health", response_model=list[DependencyOut])
def get_health() -> list[DependencyOut]:
    results: list[DependencyOut] = []

    try:
        ping_database()
        results.append(DependencyOut(name="Postgres", ok=True, detail="query answered"))
    except Exception as exc:
        results.append(DependencyOut(name="Postgres", ok=False, detail=str(exc)[:200]))

    ok, detail = _port_open(RABBITMQ_URL, 5672)
    results.append(DependencyOut(name="RabbitMQ", ok=ok, detail=detail))

    try:
        with urllib.request.urlopen(f"{OLLAMA_URL.rstrip('/')}/api/tags", timeout=2) as response:
            ok = 200 <= response.status < 300
        results.append(DependencyOut(name="Ollama", ok=ok, detail="model API answered"))
    except (urllib.error.URLError, OSError) as exc:
        results.append(DependencyOut(name="Ollama", ok=False, detail=f"unreachable ({exc})"))

    ok, detail = _port_open(MCP_SERVER_URL, 8080)
    results.append(DependencyOut(name="MCP server", ok=ok, detail=detail))

    # Reported from configuration, not by calling Telegram. The console polls
    # this, and hitting someone else's API on a timer to render a status dot
    # is not a reasonable thing to do to them.
    if TELEGRAM_PROVIDER != "http":
        telegram_detail = "not configured (TELEGRAM_PROVIDER is not 'http')"
    elif not TELEGRAM_ALLOWED_CHAT_IDS:
        telegram_detail = "token set, but the allowlist is empty so the bot answers nobody"
    else:
        telegram_detail = f"token set, {len(TELEGRAM_ALLOWED_CHAT_IDS)} chat(s) allowed"
    telegram_ok = TELEGRAM_PROVIDER == "http" and bool(TELEGRAM_ALLOWED_CHAT_IDS)
    results.append(
        DependencyOut(
            name="Telegram",
            ok=telegram_ok,
            detail=telegram_detail,
            # Not configured at all is "off"; configured but refusing everyone
            # is a real problem and stays "down".
            state="up" if telegram_ok else "off" if TELEGRAM_PROVIDER != "http" else "down",
        )
    )

    for result in results:
        if result.name != "Telegram":
            result.state = "up" if result.ok else "down"
    return results

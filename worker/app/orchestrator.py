"""The AI pipeline for one ticket.

Order of operations is decided here, in plain Python, not by the model:
retrieve, build a bounded prompt, ask for a structured answer, validate it,
apply the approval rules, persist everything. The model's only job is the
judgement in the middle.
"""

import logging
import time

from sqlalchemy.orm import Session

from app import mcp_client, rules
from app.mcp_client import MCPError
from app.prompt import build_prompt
from shared import db as shared_db
from shared import progress
from shared.ai_analysis import TicketAnalysis
from shared.analysis import AnalysisError, analyze_ticket
from shared.config import GENERATION_MODEL, RETRIEVAL_MODE
from shared.embeddings import EmbeddingError, EmbeddingProvider
from shared.llm import LLMProvider
from shared.notifications import notify_approval_needed
from shared.models import (
    AgentRun,
    AgentRunStatus,
    AuditLog,
    Service,
    Ticket,
    TicketStatus,
)
from shared.retrieval import RetrievedArticle, search_knowledge

logger = logging.getLogger("worker.orchestrator")


class OrchestrationError(Exception):
    """The pipeline could not produce a usable analysis."""


class RetryableOrchestrationError(OrchestrationError):
    """...but it's worth trying again (model down, database blip)."""


def _retrieve(db: Session, provider: EmbeddingProvider, query: str) -> list[RetrievedArticle]:
    """Find knowledge articles, through MCP's controlled tool by default.

    RETRIEVAL_MODE="direct" exists so orchestration tests don't need a running
    MCP server to exercise everything downstream of retrieval. Production runs
    in "mcp" mode, which is the path that actually matters: the same tool the
    model would use, with the same clamped limits.
    """
    if RETRIEVAL_MODE == "direct":
        return search_knowledge(db, provider, query)

    rows = mcp_client.search_knowledge_base(query)
    return [
        RetrievedArticle(
            slug=row["document"],
            title=row["title"],
            body=row["body"],
            distance=1.0 - float(row["similarity"]),
        )
        for row in rows
    ]


def _known_service_names(db: Session) -> list[str]:
    return [name for (name,) in db.query(Service.name).order_by(Service.name).all()]


def _resolve_service_id(db: Session, name: str | None) -> int | None:
    if not name:
        return None
    service = db.query(Service).filter(Service.name == name).first()
    return service.id if service else None


def _escalate_unanalysed(db: Session, ticket: Ticket, gate: rules.Gate) -> None:
    """Park a ticket for a human without having asked the model anything.

    No AgentRun row is written, because no run happened - a row naming a
    model that was never called would be a lie in the audit trail. The
    AuditLog entry carries the reason instead.
    """
    ticket.status = TicketStatus.AWAITING_APPROVAL
    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="ticket_screened_before_analysis",
            detail={"reason": gate.reason, "requires_human_approval": True},
        )
    )
    notified = notify_approval_needed(
        {
            "ticket_id": ticket.id,
            "subject": ticket.subject,
            "reason": gate.reason,
            "approve_url": f"/tickets/{ticket.id}/approve",
            "reject_url": f"/tickets/{ticket.id}/reject",
        }
    )
    # Same event as the post-analysis path: a screened ticket is waiting on a
    # person for the same reason any other one is, and anything reading the
    # audit trail should find it without knowing which route it took.
    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="human_approval_requested",
            detail={"reason": gate.reason, "notified": notified},
        )
    )


def _apply(db: Session, ticket: Ticket, analysis: TicketAnalysis, gate: rules.Gate) -> None:
    ticket.category = analysis.category
    ticket.priority = analysis.priority
    ticket.affected_service_id = _resolve_service_id(db, analysis.affected_service)
    ticket.status = (
        TicketStatus.AWAITING_APPROVAL if gate.requires_human_approval else TicketStatus.RESOLVED
    )


def run(
    db: Session,
    ticket: Ticket,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
) -> TicketAnalysis | None:
    """Run the pipeline for one ticket.

    Returns the analysis, or None when the ticket was screened out before
    the model was ever asked - in that case no analysis exists, and
    inventing one would misreport what happened.
    """
    started = time.monotonic()
    ticket_context = f"{ticket.subject} {ticket.description}"

    # Cheapest check first, and before anything is spent. It needs only the
    # ticket's own words, and a ticket that trips it goes to a human
    # whatever the model would have said - so there is nothing to gain by
    # retrieving and generating first.
    progress.emit("screening", ticket.id)
    screened = rules.screen_ticket_text(ticket_context)
    if screened:
        progress.emit("screened_out", reason=screened.reason)
        _escalate_unanalysed(db, ticket, screened)
        return None

    query = f"{ticket.subject}\n{ticket.description}"
    articles: list[RetrievedArticle] = []

    progress.emit("retrieving", via=RETRIEVAL_MODE)
    try:
        articles = _retrieve(db, embedding_provider, query)
    except (EmbeddingError, MCPError) as exc:
        progress.emit("run_failed", error=f"retrieval failed: {exc}")
        # Couldn't even ask the question - that's a failure to retrieve, not a
        # finding of "nothing relevant". Worth retrying; Ollama may be back.
        _record_failure(db, ticket, articles, 0, started, f"retrieval failed: {exc}")
        raise RetryableOrchestrationError(f"retrieval failed: {exc}") from exc

    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="rag_documents_retrieved",
            detail={"slugs": [a.slug for a in articles], "count": len(articles)},
        )
    )

    # Titles as well as slugs: the overseer shows people "VPN connection failures",
    # not "vpn-connection-failures", and only the worker has both to hand.
    progress.emit("retrieved", slugs=[a.slug for a in articles], titles=[a.title for a in articles])

    prompt = build_prompt(ticket, articles, _known_service_names(db))
    progress.emit(
        "prompt_built",
        articles=len(articles),
        # Worth showing: a steered run is answering a person's correction, and
        # watching it without knowing that would misread what it is doing.
        steered=bool((ticket.human_instruction or "").strip()),
    )

    try:
        analysis = analyze_ticket(llm_provider, prompt)
    except AnalysisError as exc:
        progress.emit("run_failed", error=str(exc))
        _record_failure(db, ticket, articles, 0, started, str(exc))
        # The model being unreachable or never returning valid JSON is worth
        # another go later; it is not a permanent property of this ticket.
        raise RetryableOrchestrationError(str(exc)) from exc

    # The gate judges sensitivity from what the user actually reported, not
    # from how the model summarised it, and verifies citations against what
    # retrieval actually returned rather than trusting the model to only name
    # documents it was given.
    progress.emit(
        "analysed",
        category=analysis.category,
        priority=analysis.priority.value,
        confidence=analysis.confidence,
    )
    gate = rules.evaluate(
        analysis,
        ticket_context=ticket_context,
        retrieved_slugs=[a.slug for a in articles],
    )
    progress.emit("gate", requires_human=gate.requires_human_approval, reason=gate.reason)
    _apply(db, ticket, analysis, gate)

    db.add(
        AgentRun(
            ticket_id=ticket.id,
            status=AgentRunStatus.SUCCEEDED,
            model=GENERATION_MODEL,
            attempts=1,
            latency_ms=int((time.monotonic() - started) * 1000),
            retrieved_slugs={"slugs": [a.slug for a in articles]},
            output=analysis.model_dump(mode="json"),
            confidence=analysis.confidence,
        )
    )
    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="ai_result_generated",
            detail={
                "category": analysis.category,
                "priority": analysis.priority.value,
                "confidence": analysis.confidence,
                "requires_human_approval": gate.requires_human_approval,
                "reason": gate.reason,
            },
        )
    )
    if gate.requires_human_approval:
        notified = notify_approval_needed(
            {
                "ticket_id": ticket.id,
                "subject": ticket.subject,
                "category": analysis.category,
                "priority": analysis.priority.value,
                "confidence": analysis.confidence,
                "reason": gate.reason,
                "recommended_resolution": analysis.recommended_resolution,
                "approve_url": f"/tickets/{ticket.id}/approve",
                "reject_url": f"/tickets/{ticket.id}/reject",
            }
        )
        db.add(
            AuditLog(
                ticket_id=ticket.id,
                event_type="human_approval_requested",
                detail={"reason": gate.reason, "notified": notified},
            )
        )
        progress.emit("notified", channels=notified)
    else:
        db.add(
            AuditLog(
                ticket_id=ticket.id,
                event_type="auto_recommended",
                detail={"reason": gate.reason},
            )
        )

    logger.info(
        "ticket %s analysed: %s/%s confidence=%.2f approval=%s (%s)",
        ticket.id,
        analysis.category,
        analysis.priority.value,
        analysis.confidence,
        gate.requires_human_approval,
        gate.reason,
    )
    return analysis


def _record_failure(
    db: Session,
    ticket: Ticket,
    articles: list[RetrievedArticle],
    attempts: int,
    started: float,
    error: str,
) -> None:
    """Write the failed run so a failure is as visible as a success.

    Deliberately on its own session and committed immediately: the caller
    rolls back when this raises, and a rolled-back failure record would leave
    no trace of the attempt at all.
    """
    ticket_id = ticket.id
    slugs = [a.slug for a in articles]
    latency_ms = int((time.monotonic() - started) * 1000)

    failure_db = shared_db.SessionLocal()
    try:
        failure_db.add(
            AgentRun(
                ticket_id=ticket_id,
                status=AgentRunStatus.FAILED,
                model=GENERATION_MODEL,
                attempts=attempts,
                latency_ms=latency_ms,
                retrieved_slugs={"slugs": slugs},
                error=error,
            )
        )
        failure_db.add(
            AuditLog(ticket_id=ticket_id, event_type="ai_run_failed", detail={"error": error})
        )
        failure_db.commit()
    finally:
        failure_db.close()

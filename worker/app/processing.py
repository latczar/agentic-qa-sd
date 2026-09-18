import logging

from sqlalchemy.orm import Session

from app import orchestrator
from shared.embeddings import get_embedding_provider
from shared.llm import get_llm_provider
from shared.models import AuditLog, Ticket, TicketStatus

logger = logging.getLogger("worker.processing")


class RetryableProcessingError(Exception):
    """Raised when processing fails in a way that's worth trying again."""


# A ticket in one of these states has already been decided - in the first two
# cases possibly by a person. Re-analysing it would overwrite that decision.
ALREADY_DECIDED = (
    TicketStatus.AWAITING_APPROVAL,
    TicketStatus.RESOLVED,
    TicketStatus.ESCALATED,
)


def process_ticket(db: Session, ticket: Ticket) -> None:
    """Run the AI pipeline for one ticket.

    Providers are built here rather than passed in, because this is the entry
    point the queue consumer calls. Tests drive orchestrator.run directly with
    fakes instead of going through this.
    """
    # Idempotency in the consumer is keyed on event_id, so it stops the same
    # *message* being handled twice - not the same *ticket* being published
    # twice under two different event ids. That happened for real, and the
    # second run turned a ticket that was awaiting human approval into an
    # auto-recommended one: a duplicate publish silently cancelling a human
    # review. The event check can't catch it, so the state does.
    if ticket.status in ALREADY_DECIDED:
        logger.info(
            "ticket %s is already %s - not re-analysing", ticket.id, ticket.status.value
        )
        db.add(
            AuditLog(
                ticket_id=ticket.id,
                event_type="reanalysis_skipped",
                detail={"status": ticket.status.value},
            )
        )
        return

    ticket.status = TicketStatus.PROCESSING
    db.add(AuditLog(ticket_id=ticket.id, event_type="worker_started", detail=None))
    db.flush()

    try:
        orchestrator.run(db, ticket, get_embedding_provider(), get_llm_provider())
    except orchestrator.RetryableOrchestrationError as exc:
        # Translate into the error the queue consumer already knows how to
        # retry and eventually dead-letter.
        raise RetryableProcessingError(str(exc)) from exc

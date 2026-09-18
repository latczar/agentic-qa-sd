from sqlalchemy.orm import Session

from app import orchestrator
from shared.embeddings import get_embedding_provider
from shared.llm import get_llm_provider
from shared.models import AuditLog, Ticket, TicketStatus


class RetryableProcessingError(Exception):
    """Raised when processing fails in a way that's worth trying again."""


def process_ticket(db: Session, ticket: Ticket) -> None:
    """Run the AI pipeline for one ticket.

    Providers are built here rather than passed in, because this is the entry
    point the queue consumer calls. Tests drive orchestrator.run directly with
    fakes instead of going through this.
    """
    ticket.status = TicketStatus.PROCESSING
    db.add(AuditLog(ticket_id=ticket.id, event_type="worker_started", detail=None))
    db.flush()

    try:
        orchestrator.run(db, ticket, get_embedding_provider(), get_llm_provider())
    except orchestrator.RetryableOrchestrationError as exc:
        # Translate into the error the queue consumer already knows how to
        # retry and eventually dead-letter.
        raise RetryableProcessingError(str(exc)) from exc

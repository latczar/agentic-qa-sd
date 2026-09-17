from sqlalchemy.orm import Session

from shared.models import AuditLog, Ticket, TicketStatus


class RetryableProcessingError(Exception):
    """Raised when processing fails in a way that's worth trying again."""


def process_ticket(db: Session, ticket: Ticket) -> None:
    """Do the actual work for a ticket.

    This is a placeholder until Phase 8 (agent orchestration) replaces its
    body with real RAG + LLM classification. For now it only proves the
    async pipeline works end to end: a worker really did pick this ticket up
    off the queue and do something with it.
    """
    ticket.status = TicketStatus.PROCESSING
    db.add(AuditLog(ticket_id=ticket.id, event_type="worker_started", detail=None))

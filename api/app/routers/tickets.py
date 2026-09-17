import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.schemas import CommentCreate, CommentOut, TicketCreate, TicketOut, TicketUpdate
from shared.db import get_db
from shared.models import AuditLog, Ticket, TicketComment, TicketStatus
from shared.rabbitmq import publish_ticket_created

logger = logging.getLogger("api.tickets")

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_ticket_or_404(ticket_id: int, db: Session) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket


@router.post("", response_model=TicketOut, status_code=201)
def create_ticket(payload: TicketCreate, db: Session = Depends(get_db)) -> Ticket:
    ticket = Ticket(
        submitted_by_id=payload.submitted_by_id,
        subject=payload.subject,
        description=payload.description,
    )
    db.add(ticket)
    db.flush()  # assigns ticket.id so the audit log row below can reference it
    db.add(AuditLog(ticket_id=ticket.id, event_type="ticket_created", detail={"subject": ticket.subject}))
    db.commit()
    db.refresh(ticket)

    # Publish only after the ticket is safely committed. If this fails, the
    # ticket still exists as NEW rather than being announced before it's real.
    # It just won't move forward on its own — there's no automatic recovery
    # for this yet, which is a known limitation (see README).
    try:
        event_id = publish_ticket_created(ticket.id)
        ticket.status = TicketStatus.QUEUED
        db.add(AuditLog(ticket_id=ticket.id, event_type="ticket_queued", detail={"event_id": event_id}))
    except Exception as exc:
        logger.error("failed to publish ticket_created for ticket %s: %s", ticket.id, exc)
        db.add(AuditLog(ticket_id=ticket.id, event_type="ticket_queue_failed", detail={"error": str(exc)}))

    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("", response_model=list[TicketOut])
def list_tickets(db: Session = Depends(get_db)) -> list[Ticket]:
    return list(db.query(Ticket).order_by(Ticket.created_at.desc()).all())


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(get_db)) -> Ticket:
    return _get_ticket_or_404(ticket_id, db)


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(ticket_id: int, payload: TicketUpdate, db: Session = Depends(get_db)) -> Ticket:
    ticket = _get_ticket_or_404(ticket_id, db)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(ticket, field, value)

    if updates:
        db.add(AuditLog(ticket_id=ticket.id, event_type="ticket_updated", detail=updates))
    db.commit()
    db.refresh(ticket)
    return ticket


@router.post("/{ticket_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(ticket_id: int, payload: CommentCreate, db: Session = Depends(get_db)) -> TicketComment:
    _get_ticket_or_404(ticket_id, db)

    comment = TicketComment(ticket_id=ticket_id, author_id=payload.author_id, body=payload.body)
    db.add(comment)
    db.add(AuditLog(ticket_id=ticket_id, event_type="comment_added", detail={"author_id": payload.author_id}))
    db.commit()
    db.refresh(comment)
    return comment

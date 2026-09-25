import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.schemas import (
    AgentRunOut,
    ApprovalOut,
    ApprovalRequest,
    CommentCreate,
    CommentOut,
    InstructRequest,
    TicketCreate,
    TicketOut,
    TicketUpdate,
)
from shared.db import get_db
from shared.models import (
    AgentRun,
    Approval,
    ApprovalDecision,
    AuditLog,
    Ticket,
    TicketComment,
    TicketStatus,
)
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
        source=payload.source,
    )
    db.add(ticket)
    db.flush()  # assigns ticket.id so the audit log row below can reference it
    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="ticket_created",
            detail={"subject": ticket.subject, "source": ticket.source.value},
        )
    )
    db.commit()
    db.refresh(ticket)

    # Publish only after the ticket is safely committed. If this fails, the
    # ticket still exists as NEW rather than being announced before it's real.
    # It just won't move forward on its own — there's no automatic recovery
    # for this yet, which is a known limitation (see README).
    try:
        event_id = publish_ticket_created(ticket.id)
        # Compare-and-set, not a plain assignment. Between the publish above and
        # this commit, a worker can consume the message, run the whole analysis
        # and write its own status. An unconditional "status = QUEUED" would
        # then overwrite that result and strand the ticket forever, because the
        # message has already been acked and nothing will redeliver it. Only
        # advance NEW -> QUEUED; if something already moved it on, leave it be.
        db.query(Ticket).filter(
            Ticket.id == ticket.id, Ticket.status == TicketStatus.NEW
        ).update({Ticket.status: TicketStatus.QUEUED}, synchronize_session=False)
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


def _decide(
    ticket_id: int,
    payload: ApprovalRequest,
    decision: ApprovalDecision,
    new_status: TicketStatus,
    db: Session,
) -> Approval:
    ticket = _get_ticket_or_404(ticket_id, db)

    # Only a ticket actually waiting on a human can be decided. Without this,
    # an approval could silently overwrite a RESOLVED or FAILED ticket.
    if ticket.status is not TicketStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"ticket is {ticket.status.value}, not {TicketStatus.AWAITING_APPROVAL.value}",
        )

    approval = Approval(
        ticket_id=ticket_id,
        decision=decision,
        decided_by_id=payload.decided_by_id,
        reason=payload.reason,
    )
    db.add(approval)
    ticket.status = new_status
    db.add(
        AuditLog(
            ticket_id=ticket_id,
            event_type=f"human_{decision.value.lower()}",
            detail={"decided_by_id": payload.decided_by_id, "reason": payload.reason},
        )
    )
    db.commit()
    db.refresh(approval)
    return approval


@router.post("/{ticket_id}/approve", response_model=ApprovalOut, status_code=201)
def approve_ticket(
    ticket_id: int, payload: ApprovalRequest, db: Session = Depends(get_db)
) -> Approval:
    return _decide(ticket_id, payload, ApprovalDecision.APPROVED, TicketStatus.RESOLVED, db)


@router.post("/{ticket_id}/reject", response_model=ApprovalOut, status_code=201)
def reject_ticket(
    ticket_id: int, payload: ApprovalRequest, db: Session = Depends(get_db)
) -> Approval:
    # Rejection escalates rather than closing: a human disagreeing with the AI
    # means the ticket still needs solving, by someone else. Use this only when
    # the agent was actually wrong - /handled is the route for a person who is
    # simply doing the work themselves.
    return _decide(ticket_id, payload, ApprovalDecision.REJECTED, TicketStatus.ESCALATED, db)


@router.post("/{ticket_id}/handled", response_model=ApprovalOut, status_code=201)
def handle_ticket_manually(
    ticket_id: int, payload: ApprovalRequest, db: Session = Depends(get_db)
) -> Approval:
    """A person dealt with this themselves, passing no judgement on the agent.

    Closes the ticket like an approval, because the work is done either way,
    but records a different decision. Without this route the only way to close
    a ticket by hand is to press Reject, which writes "the agent was wrong"
    into the permanent record whether or not it was.
    """
    return _decide(ticket_id, payload, ApprovalDecision.HANDLED, TicketStatus.RESOLVED, db)


# A correction can be given while the ticket is waiting on a person, and also
# once it has been escalated. Escalation was previously a dead end - the ticket
# stopped there and nothing moved it again - so letting an instruction reopen
# it turns "the agent got this wrong" into something recoverable rather than
# terminal. RESOLVED is deliberately not here: that ticket is closed, and
# reopening it silently would undo somebody's decision.
INSTRUCTABLE = (TicketStatus.AWAITING_APPROVAL, TicketStatus.ESCALATED)


@router.post("/{ticket_id}/instruct", response_model=TicketOut, status_code=202)
def instruct_ticket(
    ticket_id: int, payload: InstructRequest, db: Session = Depends(get_db)
) -> Ticket:
    """Correct the agent and send the ticket back round.

    202 rather than 201: nothing is finished when this returns. The ticket has
    been put back on the queue and the worker will pick it up, so the honest
    answer is "accepted", not "done".
    """
    ticket = _get_ticket_or_404(ticket_id, db)

    if ticket.status not in INSTRUCTABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"ticket is {ticket.status.value}; an instruction only applies to "
                f"{' or '.join(s.value for s in INSTRUCTABLE)}"
            ),
        )

    # Latest instruction wins. The previous one has been superseded, and
    # feeding the model a pile of contradictory corrections would make the
    # result harder to reason about, not better. The full history stays in
    # audit_logs, and each attempt still gets its own agent_runs row.
    ticket.human_instruction = payload.instruction
    # Back to QUEUED before publishing, and this is load-bearing. The worker
    # refuses to re-analyse a ticket sitting in AWAITING_APPROVAL or ESCALATED,
    # precisely so a stray duplicate message cannot cancel a human review. This
    # re-run is deliberate, so it goes through that guard by moving the ticket
    # rather than by weakening it.
    ticket.status = TicketStatus.QUEUED
    db.add(
        AuditLog(
            ticket_id=ticket.id,
            event_type="human_instructed",
            detail={
                "instructed_by_id": payload.instructed_by_id,
                "instruction": payload.instruction,
            },
        )
    )
    db.commit()
    db.refresh(ticket)

    try:
        event_id = publish_ticket_created(ticket.id)
        db.add(
            AuditLog(
                ticket_id=ticket.id,
                event_type="ticket_queued",
                detail={"event_id": event_id, "reason": "human_instruction"},
            )
        )
    except Exception as exc:
        # The instruction is already saved and the ticket already says QUEUED,
        # so leaving it there would strand it: no message, no worker, no way
        # back to a person. Put it back where it was and say so.
        logger.error("failed to re-publish ticket %s after instruction: %s", ticket.id, exc)
        ticket.status = TicketStatus.AWAITING_APPROVAL
        db.add(
            AuditLog(
                ticket_id=ticket.id,
                event_type="ticket_queue_failed",
                detail={"error": str(exc), "reason": "human_instruction"},
            )
        )
        db.commit()
        db.refresh(ticket)
        raise HTTPException(
            status_code=503, detail=f"could not re-queue ticket: {exc}"
        ) from exc

    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("/{ticket_id}/analysis", response_model=list[AgentRunOut])
def get_ticket_analysis(ticket_id: int, db: Session = Depends(get_db)) -> list[AgentRun]:
    """What the AI actually did for this ticket, newest first."""
    _get_ticket_or_404(ticket_id, db)
    return list(
        db.query(AgentRun).filter_by(ticket_id=ticket_id).order_by(AgentRun.created_at.desc()).all()
    )

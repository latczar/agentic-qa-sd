from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.schemas import ApprovalLogOut
from shared.db import get_db
from shared.models import Approval, User

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalLogOut])
def list_approvals(db: Session = Depends(get_db)) -> list[ApprovalLogOut]:
    """Every human decision, newest first.

    Exists so a list of tickets can show which ones a person decided rather
    than the AI. Without it, a RESOLVED ticket looks identical whether it was
    auto-recommended or approved by an agent - which is the single distinction
    a human-in-the-loop system most needs to make visible.

    One query with a join rather than a lookup per ticket: the caller is
    rendering a whole list and would otherwise make one request per row.
    """
    rows = (
        db.query(Approval, User.name)
        .outerjoin(User, User.id == Approval.decided_by_id)
        .order_by(Approval.created_at.desc())
        .all()
    )
    return [
        ApprovalLogOut(
            id=approval.id,
            ticket_id=approval.ticket_id,
            decision=approval.decision,
            decided_by_id=approval.decided_by_id,
            decided_by_name=name,
            reason=approval.reason,
            created_at=approval.created_at,
        )
        for approval, name in rows
    ]

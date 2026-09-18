from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shared.models import (
    AgentRunStatus,
    ApprovalDecision,
    Priority,
    ServiceStatus,
    TicketStatus,
    UserRole,
)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: UserRole
    created_at: datetime


class ServiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    status: ServiceStatus
    created_at: datetime


class TicketCreate(BaseModel):
    submitted_by_id: int
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    priority: Priority | None = None
    category: str | None = None
    affected_service_id: int | None = None
    assigned_to_id: int | None = None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    submitted_by_id: int
    assigned_to_id: int | None
    subject: str
    description: str
    status: TicketStatus
    category: str | None
    priority: Priority | None
    affected_service_id: int | None
    created_at: datetime
    updated_at: datetime


class CommentCreate(BaseModel):
    body: str = Field(min_length=1)
    author_id: int | None = None


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    author_id: int | None
    body: str
    created_at: datetime


class ApprovalRequest(BaseModel):
    decided_by_id: int | None = None
    reason: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    decision: ApprovalDecision
    decided_by_id: int | None
    reason: str | None
    created_at: datetime


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    status: AgentRunStatus
    model: str
    attempts: int
    latency_ms: int
    retrieved_slugs: dict | None
    output: dict | None
    confidence: float | None
    error: str | None
    created_at: datetime


class ApprovalLogOut(BaseModel):
    """An approval with the decider's name resolved, for list views."""

    id: int
    ticket_id: int
    decision: ApprovalDecision
    decided_by_id: int | None
    decided_by_name: str | None
    reason: str | None
    created_at: datetime

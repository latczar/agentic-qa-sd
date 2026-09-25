from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shared.models import (
    AgentRunStatus,
    ApprovalDecision,
    Priority,
    ServiceStatus,
    TicketSource,
    TicketStatus,
    UserRole,
)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: UserRole
    telegram_chat_id: int | None = None
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
    # Defaulted rather than required, so every existing caller (the web form,
    # the tests, curl in the README) keeps working untouched.
    source: TicketSource = TicketSource.WEB


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
    source: TicketSource
    human_instruction: str | None
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


class InstructRequest(BaseModel):
    """A correction aimed at the agent, not a verdict on it.

    min_length=1 because an empty instruction would re-run the ticket with
    nothing changed, burn a model call and produce the same answer, which
    looks like a broken button rather than a no-op.
    """

    instruction: str = Field(min_length=1, max_length=2000)
    instructed_by_id: int | None = None


class StageOut(BaseModel):
    """One stage of the pipeline, as the console draws it."""

    key: str
    label: str
    does: str
    count: int
    # "flowing" | "waiting" | "idle" | "stuck" - what colour the dot is.
    state: str


class OverviewOut(BaseModel):
    needs_you: int
    stages: list[StageOut]
    by_source: dict[str, int]
    open_total: int
    resolved_today: int
    escalated_open: int


class DependencyOut(BaseModel):
    name: str
    ok: bool
    detail: str
    # "up", "down" or "off". "off" is for optional parts nobody has switched
    # on: painting those red would make a correctly configured stack look
    # broken, and a red dot is the first thing anyone watching asks about.
    state: str = "up"


class AuditEventOut(BaseModel):
    """An audit_log row with its ticket's subject, for the overseer's record."""

    id: int
    ticket_id: int | None
    subject: str | None
    event_type: str
    detail: dict | None
    created_at: datetime

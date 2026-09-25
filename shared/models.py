import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# nomic-embed-text returns 768 numbers per piece of text. The column width is
# fixed at table-creation time, so changing embedding model later means a
# migration and a re-embed of every article - not a config tweak.
EMBEDDING_DIMENSIONS = 768


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    END_USER = "END_USER"
    AGENT = "AGENT"
    ADMIN = "ADMIN"


class ServiceStatus(str, enum.Enum):
    OPERATIONAL = "OPERATIONAL"
    DEGRADED = "DEGRADED"
    OUTAGE = "OUTAGE"


class TicketStatus(str, enum.Enum):
    NEW = "NEW"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class Priority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TicketSource(str, enum.Enum):
    """How the ticket got here.

    Worth storing rather than inferring: once a ticket can arrive from a chat
    app as well as the web form, "who can I reply to, and where" stops being
    obvious from the row. It also lets the console show the split, which is
    the honest way to answer "is anyone actually using the Telegram bot".
    """

    WEB = "WEB"
    TELEGRAM = "TELEGRAM"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"), default=UserRole.END_USER)
    # Which Telegram chat belongs to this person, once they have linked one.
    # Unique so a chat cannot be bound to two accounts, which would make
    # "who decided this" ambiguous the moment either of them tapped Approve.
    # Nullable because most users never link one, and BigInteger because
    # Telegram ids for groups already exceed a 32-bit int.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ServiceStatus] = mapped_column(
        SAEnum(ServiceStatus, name="service_status"), default=ServiceStatus.OPERATIONAL
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    submitted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status"), default=TicketStatus.NEW
    )
    source: Mapped[TicketSource] = mapped_column(
        SAEnum(TicketSource, name="ticket_source"), default=TicketSource.WEB, server_default="WEB"
    )
    # The latest steer from a person: "wrong category, this is Network".
    # Latest wins rather than accumulating, because a correction that has been
    # superseded is noise in the prompt - the history lives in audit_logs,
    # where it belongs, and every re-run still gets its own agent_runs row.
    human_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    priority: Mapped[Priority | None] = mapped_column(SAEnum(Priority, name="ticket_priority"), nullable=True)
    affected_service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    comments: Mapped[list["TicketComment"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["Ticket"] = relationship(back_populates="comments")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedEvent(Base):
    """Idempotency record: one row per successfully processed queue event.

    If RabbitMQ redelivers a message (e.g. the worker crashed after committing
    but before acking), the worker checks this table by event_id and skips
    reprocessing rather than doing the work twice.
    """

    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRunStatus(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ApprovalDecision(str, enum.Enum):
    """What a person decided about the agent's recommendation.

    Three outcomes rather than two, because REJECTED was doing two jobs. It
    meant both "the agent got this wrong" and "never mind, I will deal with it
    myself", and those are different claims: the first is a verdict on the
    answer, the second says nothing about the answer at all. Recording them
    identically made the approvals log overstate how often the agent was wrong,
    and this log is the only evidence a later review has.
    """

    APPROVED = "APPROVED"  # do what the agent proposed
    REJECTED = "REJECTED"  # the agent's recommendation was wrong
    HANDLED = "HANDLED"  # a person dealt with it, no verdict on the agent


class AgentRun(Base):
    """One row per AI orchestration attempt, successful or not.

    This is the engineering/debugging record - what was retrieved, what the
    model said, how long it took, how many attempts it needed. Distinct from
    audit_logs, which is the human-readable lifecycle trail. Different
    consumers, different shapes: this one feeds the Phase 11 evaluation suite.

    No chain-of-thought is stored, only operational metadata and the final
    structured output.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    status: Mapped[AgentRunStatus] = mapped_column(SAEnum(AgentRunStatus, name="agent_run_status"))
    model: Mapped[str] = mapped_column(String(120))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int] = mapped_column(Integer)
    # Slugs of the knowledge articles put in front of the model, so a bad
    # answer can be traced back to what it was actually given.
    retrieved_slugs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    """A human's decision on an AI recommendation.

    Kept separate from ticket.status: status is the ticket's current state,
    this is the permanent record of who decided what and why.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    decision: Mapped[ApprovalDecision] = mapped_column(
        SAEnum(ApprovalDecision, name="approval_decision")
    )
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeArticle(Base):
    """A knowledge base article plus the vector that makes it searchable by meaning.

    One article = one embedding. No chunking yet: these articles are short enough
    to embed whole, and chunking only earns its place once retrieval quality shows
    it's needed (Phase 5).
    """

    __tablename__ = "knowledge_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identifier taken from the source filename, so re-ingesting the same
    # file updates its row instead of inserting a duplicate.
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    # SHA-256 of title + body. If it hasn't changed since last time, ingestion
    # skips the embedding call entirely - that's the expensive part.
    content_hash: Mapped[str] = mapped_column(String(64))
    # Nullable on purpose: a row can exist with its text stored but no embedding
    # yet, which is exactly the state we're in if Ollama was unreachable partway
    # through ingestion. Phase 5 retrieval ignores rows where this is NULL.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

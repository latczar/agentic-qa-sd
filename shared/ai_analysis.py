"""The shape of an AI analysis result.

Defined before any code that produces one, so "does the model's output match
what we actually need" is a Pydantic validation question, not a guess.
"""

from pydantic import BaseModel, ConfigDict, Field

from shared.models import Priority


class Source(BaseModel):
    """Which knowledge article the analysis leaned on, so a human can check it."""

    document: str  # KnowledgeArticle.slug
    title: str


class TicketAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    priority: Priority
    affected_service: str | None
    likely_root_cause: str
    recommended_resolution: str
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source]

    # Deliberately no `requires_human_approval` field here. Whether a human
    # must review this is a business rule applied to the analysis afterward
    # (confidence threshold, sensitive-action check) - not something the model
    # gets to decide about its own output. See the human-in-the-loop rules in
    # the README.

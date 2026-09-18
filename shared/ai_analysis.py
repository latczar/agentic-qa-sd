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
    # Nullable because "I don't know" is a legitimate answer. Evaluation showed
    # the model returning null for both of these on out-of-scope tickets - it
    # genuinely has no root cause for "what is the capital of France" - and a
    # non-null requirement was demanding it invent one to pass validation.
    # Forcing a model to fill a field is a good way to manufacture a
    # hallucination.
    likely_root_cause: str | None
    recommended_resolution: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source]

    # Deliberately no `requires_human_approval` field here. Whether a human
    # must review this is a business rule applied to the analysis afterward
    # (confidence threshold, sensitive-action check) - not something the model
    # gets to decide about its own output. See the human-in-the-loop rules in
    # the README.

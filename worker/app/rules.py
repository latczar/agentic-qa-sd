"""Deciding whether a human has to look at this before anything happens.

Plain if/else on purpose. This is the one place in the system where the
question "is the AI allowed to proceed on its own" gets answered, and it
should be readable by someone who doesn't write Python. The model gets no
say in it - it reports a confidence, this decides what that's worth.
"""

from dataclasses import dataclass

from shared.ai_analysis import TicketAnalysis
from shared.models import Priority

# Below this, a human reviews it. Set deliberately high: the cost of a needless
# human review is a minute of someone's time, the cost of an unreviewed wrong
# answer on a real incident is much higher.
CONFIDENCE_THRESHOLD = 0.85

# Categories where a wrong automated answer does real damage, regardless of how
# confident the model claims to be. Matched case-insensitively against the
# model's own category and its recommended resolution.
SENSITIVE_KEYWORDS = (
    "security",
    "breach",
    "data loss",
    "delete",
    "wipe",
    "payroll",
    "permission",
    "access grant",
    "admin rights",
)


@dataclass(frozen=True)
class Gate:
    requires_human_approval: bool
    reason: str


def evaluate(analysis: TicketAnalysis) -> Gate:
    """Decide whether this analysis can stand as an automatic recommendation."""
    haystack = f"{analysis.category} {analysis.recommended_resolution}".lower()
    sensitive = [word for word in SENSITIVE_KEYWORDS if word in haystack]

    if sensitive:
        return Gate(True, f"sensitive topic ({', '.join(sensitive)}) always needs human approval")

    if not analysis.sources:
        # No cited evidence means the answer came from the model's own memory
        # rather than our knowledge base, however confident it sounds.
        return Gate(True, "no supporting knowledge articles were cited")

    if analysis.confidence < CONFIDENCE_THRESHOLD:
        return Gate(
            True, f"confidence {analysis.confidence:.2f} is below {CONFIDENCE_THRESHOLD:.2f}"
        )

    if analysis.priority is Priority.CRITICAL:
        return Gate(True, "CRITICAL priority always needs human approval")

    return Gate(False, f"confidence {analysis.confidence:.2f} with cited evidence")

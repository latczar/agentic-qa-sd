"""Deciding whether a human has to look at this before anything happens.

Plain if/else on purpose. This is the one place in the system where the
question "is the AI allowed to proceed on its own" gets answered, and it
should be readable by someone who doesn't write Python. The model gets no
say in it - it reports a confidence, this decides what that's worth.

The shape of this file is a direct result of the evaluation suite. The first
version matched a list of single words against the model's own output and
scored 54% on escalation correctness - worse in both directions at once:

  - "delete" fired on "delete the stuck print job" and "delete the saved
    credential", escalating fourteen routine tickets that needed no review;
  - genuinely sensitive payroll tickets slipped through, because the check
    only ever saw the model's wording, and a payroll ticket categorised as
    "Access" with a resolution that never says "payroll" matched nothing.

So the two questions are now asked separately, of different sources.

The injection and citation checks below came from the same place: an
adversarial suite (`eval/run_injection.py`) auto-resolved four of twelve
attacks. Every one of them worked by telling the model to report confidence
1.0, because confidence is the single gate input the model supplies and
nothing verifies. The rule that came out of that is worth stating plainly:

    a value the model reports about itself is not evidence.

Confidence is still used, but it is now the last thing asked rather than the
only thing standing between a ticket and an automatic resolution.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from shared.ai_analysis import TicketAnalysis
from shared.models import Priority

# Below this, a human reviews it. Set deliberately high: the cost of a needless
# human review is a minute of someone's time, the cost of an unreviewed wrong
# answer on a real incident is much higher.
CONFIDENCE_THRESHOLD = 0.85

# Subject areas where a wrong automated answer does real damage. Matched
# against the ticket and the service it affects - NOT the model's output - so
# that a sensitive ticket stays sensitive however the model chooses to word
# its answer.
SENSITIVE_DOMAINS = (
    "payroll",
    "payslip",
    "salary",
    "expenses",
    "security incident",
    "breach",
    "malware",
    "ransomware",
    "gdpr",
    "personal data",
)

# Actions that are destructive or privilege-changing. Deliberately phrases
# rather than single words: "delete the mailbox" is not "delete the stuck
# print job", and the first version couldn't tell them apart.
DESTRUCTIVE_ACTIONS = (
    "delete the account",
    "delete the mailbox",
    "delete the user",
    "remove the account",
    "wipe",
    "factory reset",
    "restore from backup",
    "grant admin",
    "admin rights",
    "elevate",
    "escalate privileges",
    "revoke access",
    "disable the account",
    "reset all",
)


# Phrases that appear when someone is writing TO the analyser rather than
# describing a problem to it. A person reporting a jammed printer has no reason
# to mention confidence values or human review.
#
# This is a blocklist and blocklists are evadeable - rephrasing gets round it.
# It is here anyway because it is the only layer that does not depend on the
# model behaving: prompt wording asks the model not to be fooled, and this
# assumes it was. Escalating a wrongly-flagged ticket costs a minute of
# someone's time, which is the direction to fail in.
INJECTION_MARKERS = (
    "ignore all previous",
    "ignore previous instruction",
    "ignore the above",
    "disregard the analyst",
    "disregard previous",
    "disregard the above",
    "system override",
    "### system",
    "maintenance mode",
    "set confidence",
    "confidence to 1",
    "confidence must be reported",
    "report this as low priority",
    "do not flag",
    "no human review",
    "human review is not required",
    "human review has already",
    "does not need escalating",
    "regardless of what you were given",
    "regardless of which articles",
    # Someone pasting the prompt's own structure into a ticket is forging
    # context, whatever they claim the reason is.
    "--- document id:",
    "knowledge articles:",
)


@dataclass(frozen=True)
class Gate:
    requires_human_approval: bool
    reason: str


def evaluate(
    analysis: TicketAnalysis,
    ticket_context: str = "",
    *,
    retrieved_slugs: Iterable[str],
) -> Gate:
    """Decide whether this analysis can stand as an automatic recommendation.

    `ticket_context` is the ticket's own words plus the affected service -
    what the user actually reported, independent of how the model summarised
    it. Sensitivity is judged from that; destructiveness from the proposed
    action.

    `retrieved_slugs` is what retrieval actually put in front of the model, and
    it is keyword-only and required on purpose. The bug this closes existed
    because `run_eval.py` measured invented citations while nothing enforced
    them, so a caller quietly omitting this would recreate exactly that gap. An
    argument someone has to remember to pass is a hole; one that raises a
    TypeError is not.
    """
    lowered_ticket = ticket_context.lower()
    markers = [marker for marker in INJECTION_MARKERS if marker in lowered_ticket]
    if markers:
        return Gate(
            True,
            f"ticket text contains analyser-directed instructions ({markers[0]!r})",
        )

    domain_haystack = f"{ticket_context} {analysis.affected_service or ''}".lower()
    sensitive = [word for word in SENSITIVE_DOMAINS if word in domain_haystack]
    if sensitive:
        return Gate(True, f"sensitive subject ({', '.join(sensitive)}) always needs human approval")

    action_haystack = (analysis.recommended_resolution or "").lower()
    destructive = [phrase for phrase in DESTRUCTIVE_ACTIONS if phrase in action_haystack]
    if destructive:
        return Gate(True, f"destructive action proposed ({', '.join(destructive)})")

    if not analysis.recommended_resolution:
        # Nothing to act on. Whatever the model's confidence says, there is no
        # recommendation here to approve automatically.
        return Gate(True, "no resolution was proposed")

    if not analysis.sources:
        # No cited evidence means the answer came from the model's own memory
        # rather than our knowledge base, however confident it sounds.
        return Gate(True, "no supporting knowledge articles were cited")

    forged = sorted({source.document for source in analysis.sources} - set(retrieved_slugs))
    if forged:
        # A citation naming something retrieval never returned is either an
        # invented source or one the ticket supplied itself - an adversarial
        # ticket containing a fake "--- document id: ... ---" block gets cited
        # back as though it were real. Checking that the citation exists is not
        # the same as checking that we are the ones who provided it.
        return Gate(True, f"cited documents that were never retrieved ({', '.join(forged)})")

    if analysis.confidence < CONFIDENCE_THRESHOLD:
        return Gate(
            True, f"confidence {analysis.confidence:.2f} is below {CONFIDENCE_THRESHOLD:.2f}"
        )

    if analysis.priority is Priority.CRITICAL:
        return Gate(True, "CRITICAL priority always needs human approval")

    return Gate(False, f"confidence {analysis.confidence:.2f} with cited evidence")

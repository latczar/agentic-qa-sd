"""Building the prompt handed to the model.

Kept separate from the orchestration so the exact wording is one readable
thing that can be diffed, reviewed, and (Phase 11) evaluated against.
"""

from shared.models import Ticket
from shared.retrieval import RetrievedArticle

SYSTEM_RULES = """You are an IT service desk analyst. Analyse the ticket below using ONLY the \
knowledge articles provided. Respond with ONLY a JSON object - no prose, no markdown fences.

Required JSON shape:
{
  "category": string,
  "priority": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "affected_service": string or null,
  "likely_root_cause": string,
  "recommended_resolution": string,
  "confidence": number between 0 and 1,
  "sources": [{"document": string, "title": string}]
}

Rules:
- "affected_service" MUST be one of the known services listed below, or null if none fit. \
Do not invent a service name.
- "sources" MUST only cite documents from the knowledge articles below, using their exact \
document id. Cite nothing if you used nothing.
- "confidence" reflects how well the knowledge articles actually support your answer. If \
they do not address this ticket, say so with a low confidence rather than guessing.
"""


def build_prompt(ticket: Ticket, articles: list[RetrievedArticle], service_names: list[str]) -> str:
    """Assemble the bounded context the model gets.

    Only the retrieved articles go in - never the whole knowledge base. That
    keeps the context small enough for a 7B model to actually use, and makes
    a wrong answer traceable to a specific retrieval result.
    """
    services = "\n".join(f"- {name}" for name in service_names) or "- (none configured)"

    if articles:
        knowledge = "\n\n".join(
            f"--- document id: {a.slug} | title: {a.title} ---\n{a.body}" for a in articles
        )
    else:
        # Said explicitly rather than left as an empty section. A model handed
        # a blank space tends to fall back on its own memory; a model told
        # plainly that it has nothing is far more likely to return low
        # confidence, which is what should happen here.
        knowledge = (
            "(No relevant knowledge articles were found for this ticket. You have no "
            "supporting evidence. Return a low confidence and cite no sources.)"
        )

    return (
        f"{SYSTEM_RULES}\n"
        f"Known services:\n{services}\n\n"
        f"Ticket subject: {ticket.subject}\n"
        f"Ticket description: {ticket.description}\n\n"
        f"Knowledge articles:\n{knowledge}\n"
    )

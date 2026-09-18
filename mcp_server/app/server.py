"""The only database surface the LLM is ever allowed to see.

The worker is trusted code and talks to Postgres directly for its own
bookkeeping. The *model* does not: everything model-facing goes through the
narrow, named tools below. There is no "run this SQL" tool, and there never
will be - that's the whole point. If a tool isn't here, the model can't do it.

Transport is streamable HTTP rather than stdio because this runs as its own
container, not as a child process of the worker.
"""

import logging

from mcp.server.fastmcp import FastMCP

from shared.db import SessionLocal
from shared.embeddings import EmbeddingError, get_embedding_provider
from shared.models import Service, Ticket
from shared.retrieval import search_knowledge

logger = logging.getLogger("mcp_server")

mcp = FastMCP("service-desk", host="0.0.0.0", port=8080)

# A tool returning "every ticket that ever mentioned a password" would hand the
# model a pile of other people's data to leak into an answer. Capped here.
MAX_SIMILAR_TICKETS = 5


@mcp.tool()
def get_ticket(ticket_id: int) -> dict:
    """Look up one ticket by its id.

    Returns the ticket's own fields only - no comments, no other users' data.
    """
    db = SessionLocal()
    try:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            return {"error": f"no ticket with id {ticket_id}"}
        return {
            "id": ticket.id,
            "subject": ticket.subject,
            "description": ticket.description,
            "status": ticket.status.value,
            "category": ticket.category,
            "priority": ticket.priority.value if ticket.priority else None,
        }
    finally:
        db.close()


@mcp.tool()
def search_knowledge_base(query: str, limit: int = 3) -> list[dict]:
    """Find knowledge articles whose meaning is closest to `query`.

    Returns an empty list when nothing is relevant enough - that is a real
    answer meaning "we have no supporting evidence", not a failure. Do not
    fill the gap from memory.
    """
    db = SessionLocal()
    try:
        # Clamped rather than trusted: `limit` arrives from the model, and an
        # unbounded value would quietly turn a bounded context into the whole
        # knowledge base.
        limit = max(1, min(limit, 10))
        try:
            articles = search_knowledge(db, get_embedding_provider(), query, limit=limit)
        except EmbeddingError as exc:
            return [{"error": f"could not search: {exc}"}]

        return [
            {
                "document": a.slug,
                "title": a.title,
                "body": a.body,
                "similarity": round(a.similarity, 3),
            }
            for a in articles
        ]
    finally:
        db.close()


@mcp.tool()
def get_service_status(name: str) -> dict:
    """Check whether a named service is currently healthy.

    Also the list of services that exist - use it to avoid inventing a service
    name that isn't real.
    """
    db = SessionLocal()
    try:
        service = db.query(Service).filter(Service.name == name).first()
        if service is None:
            known = [n for (n,) in db.query(Service.name).order_by(Service.name).all()]
            return {"error": f"no service named '{name}'", "known_services": known}
        return {
            "name": service.name,
            "status": service.status.value,
            "description": service.description,
        }
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("MCP server starting on :8080 (streamable-http)")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

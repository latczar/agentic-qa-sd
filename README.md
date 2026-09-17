# AI Service Desk Agent

A local, no-paid-APIs AI incident management system: submit a support ticket, and an
AI worker classifies it, retrieves relevant knowledge (RAG over pgvector), proposes a
resolution via a local LLM (Ollama), and routes it for human approval when confidence
is low or the action is sensitive. Built to show production-style patterns: async
processing over a real queue, retries and dead-lettering, idempotency, structured
output validation, and human-in-the-loop control — not just "call an LLM".

## Status

[![CI](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml/badge.svg)](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml)

Phase 2 of 12 — PostgreSQL schema and ticket API. See [Architecture](#architecture) for
the full target design and [Phases](#phases) for what's built so far.

## Architecture (target — built incrementally)

```
User
  │
  ▼
FastAPI  ──────────────► PostgreSQL (tickets, users, services, comments,
  │                        knowledge_articles+embeddings, agent_runs,
  │ publish event           approvals, audit_logs, processed_events)
  ▼
RabbitMQ (ticket.processing → ticket.retry → ticket.dead-letter)
  │
  ▼
Worker (AI orchestrator — trusted code, direct DB access for its own bookkeeping)
  ├── deterministic retrieval via MCP client ──► MCP Server ──► Postgres
  │                                                (read-mostly, controlled tools only)
  ├── pgvector similarity search (bounded context, never the whole knowledge base)
  ├── Ollama (generation + embeddings)
  ├── Pydantic validation (reject/retry on malformed JSON)
  └── confidence/risk rules ──► AWAITING_APPROVAL or auto-recommend
        │
        ▼
      n8n (notifications, approval webhook, escalation) ──► FastAPI approve/reject
        │
        ▼
      Ticket updated → Audit log
```

**Design decisions worth knowing:**
- The LLM never touches Postgres directly. All model-facing data access goes through
  MCP's controlled tools. The worker itself, as trusted code, writes directly to
  Postgres for its own bookkeeping (status, audit log, run history).
- Retrieval (which knowledge articles, which similar tickets) is decided by plain
  Python code, not by the LLM choosing tool calls in an open loop — small local
  models are unreliable at open-ended tool orchestration. The LLM's job is judgement
  on bounded, already-assembled context.
- n8n only ever talks to FastAPI over HTTP. It never touches Postgres or RabbitMQ
  directly, so there's one source of truth for business logic.
- Local models: `qwen2.5:7b-instruct` for generation (tool-calling capable in
  Ollama), `nomic-embed-text` (768 dimensions) for embeddings. Adjust down if your
  hardware can't run the 7B model.

## Phases

1. **Repository skeleton** — Postgres, RabbitMQ, and a FastAPI `/health` endpoint that
   proves the API can actually reach the database.
2. **PostgreSQL schema and FastAPI ticket API** ← you are here — `tickets`, `users`,
   `services`, `ticket_comments`, `audit_logs`; ticket CRUD, comments, user/service
   lookups.
3. RabbitMQ queue and worker
4. Knowledge ingestion + pgvector
5. RAG retrieval
6. Ollama integration
7. MCP server
8. Agent orchestration
9. Human approval + n8n
10. Failure handling / retries / DLQ
11. Evaluation suite
12. Documentation and demo

## Local setup

```bash
cp .env.example .env
docker compose up --build
docker compose exec api python -m app.create_tables
docker compose exec api python -m app.seed
curl http://localhost:8000/health
curl http://localhost:8000/services
```

RabbitMQ management UI: http://localhost:15672 (login from your `.env`).

Schema note: there's no Alembic yet. Phase 2 is the *first* schema, and Alembic's whole
job is managing changes to one — it earns its place at Phase 4, when pgvector tables
give us an actual second migration to write.

## Testing

Ticket/user/service tests hit a real Postgres — no SQLite stand-in — using a separate
`service_desk_test` database (created automatically) and one transaction per test,
rolled back afterwards, so nothing leaks between tests or touches your dev data.

```bash
docker compose up -d postgres
cd api
pip install -r requirements.txt
pytest
```

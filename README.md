# AI Service Desk Agent

A local, no-paid-APIs AI incident management system: submit a support ticket, and an
AI worker classifies it, retrieves relevant knowledge (RAG over pgvector), proposes a
resolution via a local LLM (Ollama), and routes it for human approval when confidence
is low or the action is sensitive. Built to show production-style patterns: async
processing over a real queue, retries and dead-lettering, idempotency, structured
output validation, and human-in-the-loop control — not just "call an LLM".

## Status

[![CI](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml/badge.svg)](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml)

Phase 3 of 12 — RabbitMQ queue and worker. See [Architecture](#architecture) for
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
- `models.py`, `db.py`, `config.py`, and the RabbitMQ topology live in a top-level
  `shared/` package, not inside `api/`. It exists because the worker (Phase 3) needs
  the exact same models and database access as the API — it was deliberately *not*
  created in Phase 1/2, before there was a second real consumer to justify it.

**Queue design (Phase 3):**
- `ticket.processing` — the main work queue. The worker consumes here.
- `ticket.retry` — has no consumer. A failed message is republished here with a
  per-message TTL (`expiration`) that grows with each attempt (5s, 10s, 20s, capped at
  60s). When that TTL expires, RabbitMQ's own dead-letter mechanism drops the message
  straight back onto `ticket.processing` — a delay queue built out of a feature meant
  for actual dead-lettering, so no separate relay process is needed.
- `ticket.dead-letter` — terminal. Messages land here after `MAX_RETRIES` (3) failed
  attempts, or immediately for anything that can never succeed (ticket ID doesn't
  exist, message body isn't valid JSON). Nothing consumes it automatically — that's
  the point of a DLQ, a human inspects it.
- **Idempotency**: every published event carries a fresh `event_id`. The worker checks
  it against a `processed_events` table before doing any work, so a RabbitMQ
  redelivery (e.g. the worker crashed after committing but before acking) can't cause
  the same ticket to be processed twice.
- **Publish-after-commit**: `POST /tickets` commits the ticket to Postgres first, then
  publishes. If publishing fails, the ticket stays `NEW` rather than being announced
  before it verifiably exists — but there's no automatic recovery yet for a ticket
  stuck at `NEW` because RabbitMQ was briefly unreachable. That's a known gap, not an
  oversight; revisit if Phase 10 needs it.
- Core retry/backoff/DLQ/idempotency plumbing is built now because the worker needs it
  from day one, independent of AI. Phase 10 extends failure handling to the AI-specific
  cases (Ollama unavailable, malformed LLM JSON, low confidence) once those pieces
  exist.

## Phases

1. **Repository skeleton** — Postgres, RabbitMQ, and a FastAPI `/health` endpoint that
   proves the API can actually reach the database.
2. **PostgreSQL schema and FastAPI ticket API** — `tickets`, `users`, `services`,
   `ticket_comments`, `audit_logs`; ticket CRUD, comments, user/service lookups.
3. **RabbitMQ queue and worker** ← you are here — ticket creation publishes an event;
   a separate worker process consumes it with acknowledgements, exponential-backoff
   retries, a dead-letter queue, and idempotency via `processed_events`. The worker
   doesn't do anything AI-shaped yet (that's Phase 8) — it proves the async pipeline
   itself works: pick up a ticket, mark it `PROCESSING`, ack.
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
docker compose up --build   # postgres, rabbitmq, api, worker
docker compose exec api python -m app.create_tables
docker compose exec api python -m app.seed
curl http://localhost:8000/health
curl http://localhost:8000/services

# Create a ticket, then watch the worker log pick it up and mark it PROCESSING:
curl -X POST http://localhost:8000/tickets \
  -H "Content-Type: application/json" \
  -d '{"submitted_by_id": 1, "subject": "Cannot log in", "description": "..."}'
docker compose logs -f worker
```

RabbitMQ management UI: http://localhost:15672 (login from your `.env`) — watch
`ticket.processing`, `ticket.retry`, and `ticket.dead-letter` fill and drain there.

Schema note: there's no Alembic yet. Phase 2 is the *first* schema, and Alembic's whole
job is managing changes to one — it earns its place at Phase 4, when pgvector tables
give us an actual second migration to write.

## Testing

API tests (ticket/user/service CRUD) hit a real Postgres — no SQLite stand-in — using a
separate `service_desk_test` database (created automatically) and one transaction per
test, rolled back afterwards, so nothing leaks between tests or touches your dev data.
One test also hits a real RabbitMQ to prove `POST /tickets` actually publishes.

Known limitation: tests use a dedicated Postgres database but share the *same*
RabbitMQ instance and queues as local dev — there's no test vhost. Running the test
suite repeatedly against your local `docker compose` RabbitMQ can leave a few
dead-lettered messages referencing ticket IDs that only exist in the test database.
That's not silent corruption — it's the dead-letter queue correctly doing its job for
an unrecognised ticket reference — just noise you can clear from the management UI.
Worth fixing with a separate vhost if this ever needed to run somewhere test/dev
isolation actually mattered; not worth the complexity for a local portfolio project.

Worker tests use a different isolation strategy: `on_message` manages its own database
session per call (there's no per-request scope like FastAPI's `Depends` to swap out),
so the worker's database connection is pointed at the real test database for the whole
test run instead, and tests use unique data per test rather than a rollback. Retry,
dead-letter, and idempotency logic is tested directly against `on_message` with a fake
RabbitMQ channel — no live RabbitMQ needed for those, only Postgres.

```bash
docker compose up -d postgres rabbitmq

cd api
pip install -r requirements.txt
pytest

cd ../worker
pip install -r requirements.txt
pytest
```

# AI Service Desk Agent

A local, no-paid-APIs AI incident management system: submit a support ticket, and an
AI worker classifies it, retrieves relevant knowledge (RAG over pgvector), proposes a
resolution via a local LLM (Ollama), and routes it for human approval when confidence
is low or the action is sensitive. Built to show production-style patterns: async
processing over a real queue, retries and dead-lettering, idempotency, structured
output validation, and human-in-the-loop control — not just "call an LLM".

## Status

[![CI](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml/badge.svg)](https://github.com/latczar/agentic-qa-sd/actions/workflows/ci.yml)

Phase 9 of 12 — the full pipeline runs end to end: a ticket is queued, analysed
by a local model against retrieved knowledge, gated on confidence and risk, and
either auto-recommended or routed to a human for approval. See
[Architecture](#architecture) for the design and [Phases](#phases) for what's built.

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

**Knowledge base and embeddings (Phase 4):**
- Articles are plain markdown in `knowledge/` — filename is the slug, the first
  `# ` line is the title. Source data lives at the repo root rather than inside
  `api/` because it belongs to the whole system, not to one service.
- One article = one embedding. No chunking: these articles are short enough to embed
  whole, and chunking only earns its place if Phase 5 shows retrieval needs it.
- `content_hash` (SHA-256 of title + body) is what makes re-ingestion cheap. Change one
  article out of forty and exactly one embedding call is made, not forty. An article
  whose embedding is NULL is always retried, even when its hash matches — otherwise a
  run that failed partway could never repair itself.
- `EMBEDDING_PROVIDER` picks between the real Ollama client and a deterministic fake
  (`shared/embeddings.py`). The fake produces stable, correctly-shaped vectors that
  carry no meaning — enough to test ingestion plumbing, which is why the whole suite
  runs in CI with no model present. Judging retrieval *quality* needs the real model
  and belongs in the Phase 11 evaluation suite.
- No vector index yet. With a few dozen articles Postgres scans them all in well under
  a millisecond, and an ivfflat index built on a near-empty table is worse than none.
  It arrives in Phase 5, alongside real queries to measure it against.
- Ollama runs on the host rather than in Compose — it wants GPU access and is shared
  with other projects on the same machine. Containers reach it via
  `host.docker.internal`.

**Retrieval (Phase 5):**
- `shared/retrieval.py` is plain Python running one SQL query. The LLM does not choose
  what to retrieve; it is handed what this found. That keeps the retrieval step
  testable and keeps a 7B model away from a job it is bad at.
- Cosine distance (`<=>`), because `nomic-embed-text` returns unit-length vectors.
  The HNSW index in migration 0003 is built with `vector_cosine_ops` to match — an
  index built for one distance operator is ignored by queries using another.
- HNSW rather than IVFFlat: IVFFlat picks its cluster centres from whatever rows exist
  when the index is built, so building one on a nearly empty table produces a bad index
  that stays bad until rebuilt. HNSW needs no training step.
- `MAX_DISTANCE` (0.40) is the cutoff that makes "no relevant articles" a real answer,
  and it was measured rather than guessed. Across ten questions against the shipped
  knowledge base — eight with a known correct article, two deliberately off-topic — the
  correct article scored 0.207–0.370 and everything else 0.348–0.711. The ranges overlap
  slightly, so no threshold is perfect; 0.40 sits above every correct match, so the cost
  of being wrong is occasionally letting a near-miss into a list of three rather than
  silently dropping the right answer. Re-measure if the embedding model changes.
  Returning nothing beats handing the model an irrelevant article and inviting a
  confident wrong answer — this is the "no RAG hits" path the spec calls for.
- A failure to embed the *question* raises rather than returning an empty list. A
  caller has to be able to tell "nothing matched" from "retrieval broke".

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
3. **RabbitMQ queue and worker** — ticket creation publishes an event;
   a separate worker process consumes it with acknowledgements, exponential-backoff
   retries, a dead-letter queue, and idempotency via `processed_events`. The worker
   doesn't do anything AI-shaped yet (that's Phase 8) — it proves the async pipeline
   itself works: pick up a ticket, mark it `PROCESSING`, ack.
4. **Knowledge ingestion + pgvector** — markdown knowledge articles
   in `knowledge/` are parsed, embedded with `nomic-embed-text`, and stored in a
   `knowledge_articles` table with a `vector(768)` column. Re-running ingestion is
   cheap: a SHA-256 content hash per article means unchanged articles are skipped
   without an embedding call. Nothing searches them yet — that's Phase 5.
5. **RAG retrieval** — `search_knowledge()` embeds a question and
   returns the nearest articles by cosine distance, with a relevance cutoff so an
   unanswerable question returns nothing rather than the least bad article. An
   HNSW index arrives with it. Nothing calls it yet — the worker starts using it
   at Phase 8, through the MCP tools built at Phase 7.
6. **Ollama integration** — real/fake provider split for the generation model,
   Pydantic-validated structured output, retry with the validation error fed back.
7. **MCP server** — three narrow tools over streamable HTTP. The worker really
   routes retrieval through it; there is no raw-SQL tool by design.
8. **Agent orchestration** — retrieve, prompt, validate, gate, persist. Order
   decided in Python; the model only supplies the judgement in the middle.
9. **Human approval + n8n** ← you are here — `/approve` and `/reject`, plus a
   webhook to n8n that branches on priority.
10. Failure handling / retries / DLQ
11. Evaluation suite
12. Documentation and demo

## Local setup

```bash
cp .env.example .env
docker compose up --build   # postgres, rabbitmq, api, worker, mcp-server, n8n
docker compose exec api alembic upgrade head
docker compose exec api python -m app.seed
docker compose exec api python -m app.ingest_knowledge   # needs Ollama, see below
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
n8n: http://localhost:5679 (import `n8n/workflows/ticket-approval.json`).

Host ports are deliberately shifted where the sibling `ai-auto` project already
uses the default: Postgres on 5433, n8n on 5679. Both stacks can run at once.

Ollama runs on the host rather than in Compose — it wants GPU access and is
shared with other projects. Containers reach it via `host.docker.internal`.

```bash
ollama pull nomic-embed-text      # embeddings, 768 dimensions
ollama pull qwen2.5:7b-instruct   # generation, tool-calling capable
docker compose exec api python -m app.ingest_knowledge
```

### Watching one ticket go through

```bash
curl -X POST http://localhost:8000/tickets -H "Content-Type: application/json"   -d '{"submitted_by_id": 1, "subject": "VPN keeps dropping after I changed my password",
       "description": "The VPN disconnects every few minutes since my password change."}'

curl http://localhost:8000/tickets/1            # NEW -> QUEUED -> PROCESSING -> AWAITING_APPROVAL
curl http://localhost:8000/tickets/1/analysis   # what the model actually said, and why
curl -X POST http://localhost:8000/tickets/1/approve   -H "Content-Type: application/json" -d '{"decided_by_id": 2, "reason": "Checked, correct"}'
```

## How a decision gets made

The model reports a confidence. It does not decide whether that is good enough —
[`worker/app/rules.py`](worker/app/rules.py) does, in plain if/else:

| Condition | Outcome |
| --- | --- |
| Sensitive topic (security, payroll, delete, permissions…) | Always human approval |
| No knowledge articles cited | Always human approval — the answer came from the model's memory, not our evidence |
| Confidence < 0.85 | Human approval |
| CRITICAL priority | Human approval |
| Otherwise | Auto-recommended |

Approve resolves the ticket; reject escalates it, because a human disagreeing
means it still needs solving by someone else.

**Known rough edge:** the sensitive-topic check is naive substring matching, so
a resolution saying "delete the saved credential entry" trips the `delete`
keyword and asks for approval it doesn't need. It errs toward human review,
which is the safe direction, but it fires more often than it should.

Postgres is published on host port **5433**, not 5432, so this project can run at the
same time as another local Postgres. Inside the Compose network it's still 5432.
Override with `POSTGRES_HOST_PORT` in `.env` if 5433 is taken too.

Ingestion needs Ollama running on the host with the embedding model pulled:

```bash
ollama pull nomic-embed-text
```

Re-run `python -m app.ingest_knowledge` whenever you edit anything in `knowledge/`.
It prints a `created / updated / unchanged / failed` summary and exits non-zero if any
article failed, so it's safe to run on a schedule or in a script.

Migrations now run through Alembic (`api/migrations/`), introduced at Phase 4 once
there was an actual second schema change to manage. `0001_baseline.py` captures the
Phase 2-3 schema exactly as it was — adopting Alembic didn't change anything for
existing databases. If you had a dev database from before this phase, run
`alembic stamp head` instead of `alembic upgrade head` once, so Alembic knows that
schema already exists rather than trying to recreate it. A fresh database (or CI) just
runs `alembic upgrade head` normally.

Note: test databases (`service_desk_test`) still get their schema from
`Base.metadata.create_all()` directly (see `shared/testing.py`), not from Alembic —
tests are checking application logic against the current schema, not testing the
migrations themselves, so the faster, simpler path is the right one there. One
consequence worth knowing: the test database has no HNSW index, because that index is
created by a migration rather than declared on the model. Retrieval results are
identical either way (an index changes how Postgres finds rows, not which rows match),
so the tests stay honest — but they are not measuring index behaviour, and aren't
meant to.

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
pytest                   # includes the marked ollama tests if Ollama is running
pytest -m "not ollama"   # what CI runs

cd ../worker
pip install -r requirements.txt
pytest
```

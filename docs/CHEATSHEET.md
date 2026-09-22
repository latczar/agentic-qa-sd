# Cheat sheet

One page. For revision before an interview, or for getting the stack back up
after a week away.

---

## The one-paragraph answer

A local AI IT service desk. A ticket arrives over HTTP, is queued to RabbitMQ so
the web request never waits on the model, and a worker retrieves relevant
knowledge articles from Postgres via pgvector, prompts a local Ollama model for
structured JSON, validates that JSON against a schema, then runs a
**deterministic gate** that decides auto-resolve or human review. The model
never touches the database — it reaches everything through three MCP tools.
Retries are a TTL queue with a dead-letter exchange, not retry code. A 40-case
evaluation suite caught three real bugs; a 14-case adversarial suite found a
genuine prompt-injection hole, now closed.

---

## The path of a ticket

```
form/POST -> API saves to Postgres -> publish to ticket.processing -> 200 OK
                                              |
                                           worker
                                              |
   embed the ticket -> pgvector similarity search -> top articles
                                              |
   build prompt (rules + services + <ticket> fenced + articles)
                                              |
   Ollama qwen2.5:7b-instruct -> JSON -> validate against schema (retry if not)
                                              |
                                          THE GATE
                                         /         \
                              all checks pass    any check fails
                                    |                   |
                                RESOLVED        AWAITING_APPROVAL
                             (auto, logged)    -> n8n notifies
                                                -> person clicks
                                                -> audit log: who + why
```

---

## The gate, in order

Plain Python. No AI. First match wins and sends it to a human.

| # | Check | Verifiable without trusting the model? |
|---|-------|:--:|
| 1 | Ticket text contains analyst-directed instructions (`INJECTION_MARKERS`) | yes |
| 2 | Subject/service is sensitive (`SENSITIVE_DOMAINS` — payroll, GDPR, breach…) | yes |
| 3 | Proposed action is destructive or privilege-changing (`DESTRUCTIVE_ACTIONS`) | yes |
| 4 | No resolution proposed | yes |
| 5 | No sources cited | yes |
| 6 | Cited a document retrieval never returned (`retrieved_slugs`) | yes |
| 7 | `confidence < 0.85` | **no — self-reported** |
| 8 | Priority is CRITICAL | yes |

Seven of eight are facts. One is the model's opinion of itself. That is exactly
where the attack went.

`evaluate()` takes `retrieved_slugs` as a **required keyword-only argument**, so
check 6 cannot be skipped by forgetting to pass it — it raises `TypeError`.

---

## Why each piece exists

| Piece | Job | Why not simpler |
|---|---|---|
| Postgres + pgvector | Tickets, users, services, audit log, articles **and** their vectors | One store for records and search; nothing to keep in sync |
| RabbitMQ | Carries "ticket N needs work" | Model takes 2–15s; the web request must not wait. Crash-safe |
| API (FastAPI) | Form, REST, approvals, serves the demo UI | — |
| Worker | Retrieval -> model -> gate | Own container: fails, scales, deploys independently |
| MCP server | 3 tools: `get_ticket`, `search_knowledge_base`, `get_service_status` | The model gets three narrow doors, not a DB connection |
| n8n | Notify, approval webhook, SLA chaser, error handler | Thin glue over HTTP; never touches Postgres or RabbitMQ |
| Ollama (host) | `qwen2.5:7b-instruct` + `nomic-embed-text` | Local, free, no egress. Containers reach it via `host.docker.internal` |

---

## Retry without retry code

```
ticket.processing   <- worker consumes (1 consumer)
ticket.retry        <- TTL + x-dead-letter-routing-key: ticket.processing
ticket.dead-letter  <- 0 consumers, by design
```

Failure publishes to `ticket.retry`. The TTL expires, RabbitMQ dead-letters the
message, and the routing key sends it back to the work queue. **The broker does
the waiting.** After three attempts it lands in the dead-letter queue, which
nothing reads because nothing should — those need a person.

Idempotency: a `processed_events` table, so the same message arriving twice does
the work once.

---

## Numbers worth knowing

| | |
|---|---|
| Tests | 109 (api 52, worker 49, mcp_server 8) — all against real Postgres, no SQLite |
| Knowledge articles | 20 |
| Eval set | 40 cases (32 with a known correct article, 8 deliberately out of scope) |
| Retrieval hit rate | 96.9% |
| Recall@1 | 90.6% |
| Correctly returned nothing when out of scope | 100% |
| Structured output valid within retry limit | 100% |
| Citations supported by what was retrieved | 100% (unsupported rate 0%) |
| Median retrieval + analysis | ~3.8s |
| Adversarial suite | 12 attacks + 2 controls: **4/12 got through, now 0/12** |
| Confidence threshold | 0.85 |

Escalation correctness swings between runs (87.5% / 75.0%) with no code change.
Measured reason: the model only ever emits 0.70 / 0.80 / 0.85 / 0.90 / 1.00, and
the threshold is 0.85, so cases flip on quantisation. An A/B of the old vs new
prompt over 32 cases gave a mean delta of −0.009. **Don't read that swing as a
regression.**

---

## The injection story

12 synthetic attacks. Four got through. None of them forged anything — each
cited a real retrieved article and proposed a sensible resolution. The only
thing the attacker supplied was `confidence: 1.0`.

The attack did not make the system do harm. It chose **whether a human looked**.

Fixed at three layers:

1. **Input screening** (deterministic) — refuse to auto-resolve tickets whose
   text is written *to* the analyst
2. **Prompt hardening** (probabilistic) — fence ticket text in `<ticket>`
   markers, label it untrusted, escape anything trying to close the fence
3. **Output verification** — every citation checked against `retrieved_slugs`

Result: 0/12, controls unaffected.

**Two things to say before you're asked:**

- Layer 1 did all the work. Prompt hardening showed no measurable effect on that
  run. Claiming three fixes all worked would be untrue.
- `INJECTION_MARKERS` is a blocklist. Rephrasing gets past it. It raises the cost
  of an attack; it is not a boundary.

---

## The n8n workflows

| Workflow | What it does |
|---|---|
| `ticket-approval` | Receives the worker's webhook on `AWAITING_APPROVAL`, notifies, calls back into the API to approve or reject |
| `sla-chaser` | Every 15 min, finds stale approvals, chases at 30/60/120/240/480 minutes, highest priority first, then stops |
| `error-handler` | `errorWorkflow` target — catches failures in the other two |

The chaser **stores no state**. It derives "is a chase due?" from the milestone
list and the run interval:

```js
const due = CHASE_AT_MINUTES.some(m => m > waiting - RUN_EVERY && m <= waiting);
```

That is the answer to "how does it back off without remembering anything?"

---

## Running it

```bash
docker compose start
```

`start` brings back stopped containers. `up -d` is the fallback or first run.
`stop` at the end of a session keeps containers; `down` would remove them. The
volumes (`postgres_data`, `rabbitmq_data`, `n8n_data`) survive both.

| Thing | URL |
|---|---|
| Demo UI | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| MCP server | http://localhost:8080 |
| RabbitMQ management | http://localhost:15672 |
| n8n | http://localhost:5679 |
| Postgres (host) | `localhost:5433` |

Ports 5433 and 5679 rather than 5432/5678 because the sibling `ai-auto` project
holds the defaults. Inside Compose they are the normal ports.

Ollama runs on the **host**, not in Compose — it wants the GPU and is shared with
other projects.

```bash
docker compose exec rabbitmq rabbitmqctl list_queues name messages consumers arguments
```

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/sla-chaser.json
```

---

## Decisions you should be able to defend

- **Why RabbitMQ?** The model takes seconds. Queue it, ack it, retry it, and the
  submitter never waits. Also: the API and worker fail independently.
- **Why pgvector and not a vector database?** One store, one backup, one
  migration path. A separate vector DB would be a second source of truth for 20
  articles.
- **Why MCP?** The model gets three named tools instead of a database
  connection. Permissions become a property of the tool list, not of the prompt.
- **Why no LangChain?** Hand-rolled retrieval and orchestration — fewer moving
  parts, and every line is defensible.
- **Why isn't it a tool-calling loop?** Deliberate. Small local models are
  unreliable at open-ended orchestration, so the retrieval sequence is decided by
  Python and the model's agentic responsibility is narrowed to one bounded
  choice. **This is the biggest remaining gap** and worth naming yourself before
  someone else does.
- **What happens when retrieval returns nothing?** The prompt says so explicitly
  rather than leaving a blank — a model handed empty space falls back on its own
  memory, a model told plainly it has no evidence returns low confidence. Then
  the gate does the rest. "I don't know" is a first-class outcome.
- **Why separate containers for API and worker?** They fail, scale and deploy
  independently — and it proves the async boundary is real, not decorative.

---

## Known rough edges — say these first

- The eval set still only references the original 8 articles; the 12 added later
  are not exercised by it.
- Category accuracy moved 87.5% -> 81.2% between runs with no code change. That
  is single-run variance; a serious evaluation would average several runs per
  case.
- `INJECTION_MARKERS` is a blocklist, not a boundary.
- Prompt hardening showed no measurable effect on the run that mattered.
- The adversarial set is 12 cases, one run each. Enough to find a real bug, not
  enough to claim a rate.
- No auth or RBAC — local demo only.

---
---

# Part 2 — Interview drill

## The spine

Use the same shape for every project. Ten beats, no thinking required.

```
Problem
-> Why AI was useful
-> Architecture
-> What I built
-> How context/data gets in
-> What the model returns
-> Validation
-> Failure/fallback
-> Human review
-> Result
```

**This project, filled in:**

| Beat | One line |
|---|---|
| Problem | Service desk tickets need triage, and most are variations on things already documented |
| Why AI | Free-text in, structured decision out — the bit a rules engine can't do |
| Architecture | FastAPI -> RabbitMQ -> worker -> pgvector retrieval -> Ollama -> gate -> Postgres, with MCP as the model's only door out and n8n for the human-facing glue |
| What I built | All of it — schema, queue topology, retrieval, prompt, gate, MCP server, eval suite, adversarial suite |
| How context gets in | Ticket embedded, cosine search over 20 articles, top 3, anything above 0.40 distance dropped |
| What the model returns | One JSON object: category, priority, service, root cause, resolution, confidence, sources |
| Validation | Pydantic schema with `extra="forbid"`, then eight gate checks in plain Python |
| Failure/fallback | Invalid JSON -> retry; failed message -> TTL queue -> back to the work queue; three strikes -> dead-letter queue |
| Human review | Anything the gate flags waits for a person; the audit log records who and why |
| Result | Retrieval hit rate 96.9%, structured output 100%, unsupported citations 0%, 4/12 injection attacks closed to 0/12 |

For the other two projects, fill in the same ten rows before you go in. If a row
is blank, that's the question they'll ask.

---

## Cold questions — short answers, anchored in real code

Two or three sentences each. Anchor every one in something you actually built.

**Why MCP?**
The model gets three named tools, not a database connection. Permissions become a
property of the tool list rather than something you ask the prompt nicely for.
It also keeps the context small — the model pulls what it needs rather than being
handed everything every time.

**Why RAG?**
The model's memory doesn't contain our internal policies, and I need the answer
to be checkable. Retrieval gives it evidence and gives me citations I can verify
against what was actually retrieved.

**RAG vs MCP?**
Not alternatives. RAG is a pattern for getting evidence into the context; MCP is
a protocol for exposing capabilities to a client. An MCP tool can expose a RAG
search — mine does, it's called `search_knowledge_base`.

**What is a vector database?**
Storage plus an index for embeddings, with nearest-neighbour search. I didn't use
a separate one — pgvector makes Postgres do it.

**Why pgvector?**
One store, one backup, one migration path. Twenty articles doesn't justify a
second source of truth to keep in sync. The HNSW index is migration 0003.

**What is Pydantic doing?**
Turning the expected output into an enforceable contract. `extra="forbid"` so an
invented field fails rather than being silently dropped, and
`confidence: float = Field(ge=0.0, le=1.0)` so the number is in range before
anything reads it. The model's output is untrusted until it passes.

**What happens when the LLM returns invalid JSON?**
Validation fails and it retries, a bounded number of times. If it still fails the
ticket escalates rather than proceeding. In the 40-case eval, structured-output
success within the retry limit is 100%.

**What happens when retrieval is bad?**
A measured distance threshold (0.40 cosine) drops weak matches, and if nothing
survives, the prompt says so explicitly instead of leaving a blank — a model
handed empty space falls back on its own memory. It returns low confidence, and
the gate escalates.

**How do you evaluate an LLM workflow?**
A fixed dataset with known answers, and separate metrics per stage rather than
one overall score: retrieval hit rate, Recall@1, structured-output validity,
citation support, escalation correctness. Plus an adversarial set. And you have
to know your noise floor — mine moves ±12% between identical runs.

**How do you reduce hallucination?**
Three things, in order of how much they help: give it evidence, tell it plainly
when there is none, and then verify the citations against what retrieval actually
returned. Unsupported-answer rate is 0%.

**How do you handle retries?**
A TTL queue with a dead-letter exchange pointing back at the work queue. The
broker does the waiting; there's no retry code. Three attempts, then a terminal
queue nothing consumes.

**How do you prevent infinite agent loops?**
I don't have an open-ended loop — deliberately, because small local models are
unreliable at orchestration. If I did: a maximum iteration count, a step budget,
and a terminal state the loop has to reach.

**How do you handle sensitive internal data?**
Nothing leaves the machine — the model runs locally, no API key, no egress. On
top of that the gate escalates anything matching sensitive subjects before it can
auto-resolve, and that check runs against the ticket, not the model's output, so
it can't be worded around.

**How do you decide when human approval is required?**
Risk, not confidence alone. Low-risk with strong evidence can go automatically;
sensitive subject, destructive or privilege-changing action, weak evidence, or
CRITICAL priority goes to a person. Eight checks, first match wins.

**How would you reduce latency?**
Measure first — median is about 3.8 seconds. Then a smaller model, fewer articles
in the prompt, or streaming. But the submitter never waits anyway, because the
work is queued and the HTTP request returns immediately.

**How would you change model providers?**
New class behind the `LLMProvider` protocol and a config value. The queue, gate,
audit log and MCP boundary don't know or care. The real work is re-measuring —
every eval number belongs to the old model, and the 0.85 threshold was tuned to
its confidence distribution.

**Why n8n instead of pure Python?**
Thin glue for the things it's genuinely good at: notification, branching, waiting
on a person. Business rules stay in one place — n8n only calls the API over HTTP
and never touches Postgres or RabbitMQ.

**When would you NOT use AI?**
When the rule is writable. The gate is plain if/else precisely because that
decision has to be identical every time and legible afterwards in an audit. Also
anywhere a wrong answer is expensive and you have no way to check it.

---

## Vocabulary — and whether you actually did it

Know all of these. **Only claim the ones in the "yes" rows for this project.**
Getting caught claiming reranking you didn't build is worse than not mentioning
it.

| Term | Plain meaning | In this project? |
|---|---|---|
| Chunking | Splitting documents into useful pieces | **No** — whole articles, they're short enough |
| Embeddings | Numbers representing meaning, for similarity search | Yes — `nomic-embed-text`, 768 dimensions |
| Top-K | Take the K closest | Yes — K = 3 |
| Distance threshold | Reject weak matches instead of feeding the model rubbish | Yes — 0.40 cosine, **measured not guessed** |
| Grounding | Answer from retrieved evidence, not model memory | Yes — and citations verified against retrieval |
| Fallback | If evidence isn't good enough, don't guess | Yes — explicit "you have no evidence" text in the prompt |
| Metadata filtering | Narrow retrieval by product/team/type before searching | **No** — would be the next thing at more articles |
| Hybrid search | Semantic plus exact keyword | **No** |
| Reranking | Reorder retrieved results with a second, better model | **No** |

If asked about the three "no" rows, the good answer is: *"Not needed at 20
articles — semantic search over that corpus already gets Recall@1 of 90.6%.
Metadata filtering would be the first one I'd add, because it cuts the search
space rather than improving the ranking."* That shows you know **when**, not just
**what**.

---

## Two phrasings to fix before you go in

**1. Don't use "deterministic validation" as a catch-all.** It sounds like a
phrase you learned. Say what it actually was:

> I treat model output as untrusted until the application validates it. What that
> means depends on the workflow — here it's schema validation, then checking the
> citations against what retrieval actually returned, then a set of rules over
> the ticket itself that the model can't influence, and human approval for
> anything sensitive or destructive.

Concrete beats abstract every time. **"Deterministic gate" is still the right
name for the thing** — just don't let it do all the work in the sentence.

**2. "What if the generated output is wrong?"** — your strongest answer, because
it's about not being fooled by something that looks right:

> Passing isn't the same as being correct. The gate exists because a
> correct-sounding answer with no supporting evidence is indistinguishable from
> a confident wrong one — I've got a screenshot of the model producing exactly
> the right resolution on a payslip ticket, with 30% confidence and nothing
> backing it, and the system still refused to act on it.

---

## Five-minute brush-up

**Pydantic** — the contract for untrusted output.
```python
class TicketAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    priority: Priority
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source]
```
`extra="forbid"` is the bit worth pointing at: a model that invents a field
fails validation rather than having it silently dropped. Drift is a reason to
retry, not to shrug.

**FastAPI** — request model -> service layer -> retrieval/model -> response model.
Your real endpoints:

```
POST   /tickets                      GET  /tickets            GET /tickets/{id}
PATCH  /tickets/{id}                 POST /tickets/{id}/comments
POST   /tickets/{id}/approve         POST /tickets/{id}/reject
GET    /tickets/{id}/analysis        GET  /approvals
GET    /services   GET /users   GET /users/{id}   GET /health   GET /docs
```

Note approve and reject hang off the **ticket**, not off an approval id — the
thing being decided is the ticket. `GET /approvals` is the joined read the list
page uses to show who decided what, one query for the whole list rather than a
lookup per row.

The OpenAPI page is generated from the same Pydantic models used for validation —
one definition doing two jobs.

**n8n** — know the node types by name: Webhook, HTTP Request, IF/Switch, Code,
Schedule Trigger, Split In Batches. Plus expressions, `errorWorkflow`, and the
items model (a node runs once per input item). Your shape:
```
Webhook -> validate -> IF gate flagged -> notify -> wait for human -> HTTP back to API -> log
```

**MCP** — the sentence that shows you understand it:
> MCP is the protocol that lets a client discover and call tools in a controlled
> way. The model still reasons about which tool to call — MCP isn't the model,
> and it isn't the reasoning. It's the interface and the permission boundary.

---

## If you only remember three things

1. **The gate had eight checks. Seven were verifiable facts, one was the model's
   opinion of itself, and that's exactly the one the attack went for.**
2. **The queue topology *is* the retry policy** — there's no retry code anywhere.
3. **Every number I quote belongs to this model.** Swap the model and the
   architecture survives, but the measurements have to be earned again.

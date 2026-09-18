# Demo walkthrough

Screenshots of the running system, and what each one is evidence of. Captured
with `docs/capture_screenshots.py` against a live stack, so they can be
regenerated after a UI change instead of quietly going stale.

Every ticket below was analysed by `qwen2.5:7b-instruct` running locally. No
paid API, no network egress, no API key.

---

## 1. The ticket list

![Ticket list](media/01-ticket-list.png)

**What you're looking at:** every ticket and the state it reached. The form on
the left submits a new one.

**Point at:** the badges, and the line under each one. `RESOLVED`,
`AWAITING_APPROVAL`, `ESCALATED` — the same ticket type does not always land in
the same place, because where it lands depends on evidence and confidence, not
on the category.

Then point at the difference between *"Approved by Priya Nandakumar — ill see
what i can do"* and *"Auto-recommended — no human review required"*. Both are
`RESOLVED`. Only one of them was decided by a person.

**Talking point — "who actually made this decision?"**
This line was added because a screenshot made its absence obvious: the list
showed `RESOLVED` identically whether the AI recommended it or an agent signed
it off, and that is the single distinction a human-in-the-loop system must not
hide. It comes from `GET /approvals`, one joined query for the whole list
rather than a lookup per row.

**Talking point — "why is this asynchronous?"**
A local model takes 2-15 seconds to answer. Doing that inside the HTTP request
would make the person raising a ticket wait for it. Instead the API saves the
ticket, publishes to RabbitMQ and returns immediately; a separate worker
process picks it up whenever it is free. The list updates on its own because
the state genuinely changes in the background. That is also why the API and the
worker are separate containers rather than one process: they fail, scale and
deploy independently.

---

## 2. An answer the system refused to act on

![Analysis panel](media/02-analysis-resolved.png)

**What you're looking at:** ticket #14 — *"i need to see other employee's
payslip"* — with its analysis expanded.

**Point at, in this order:**

- **Resolution:** *"Deny the request as it violates company policy on data
  privacy."* The model was right.
- **Confidence: 30%.** It was not sure.
- **Sources: none cited.** Nothing in the knowledge base supported it.
- **retrieved nothing.** Retrieval returned zero articles, because nothing was
  close enough to be relevant.

**Talking point — "what stops it hallucinating?"**
This is the strongest single frame in the project. The model produced a correct,
sensible answer, and the system still refused to act on it automatically,
because a correct-sounding answer with no supporting evidence is
indistinguishable from a confident wrong one. Three separate rules each
independently forced a human review here: the subject is sensitive (payroll,
payslip), no sources were cited, and confidence was below threshold.

It is marked `RESOLVED` because a human then approved it — the audit log
records `human_approved` with the agent's id and their reason. The AI did not
close this ticket.

**Follow-up worth volunteering:** the retrieval trace (`retrieved …`) is shown
next to the citations deliberately. "What it cited" and "what it was actually
shown" being different is how you catch invented evidence. Across the 40-case
evaluation set, the rate of citing a document that was never retrieved is 0%.

---

## 3. Held back for a human

![Awaiting approval](media/03-awaiting-approval.png)

**What you're looking at:** ticket #13 — *"vpn's chair is broken lmao"* — a
deliberately out-of-scope question, typed casually.

**Point at:** confidence **0%**, **none cited**, and yet the two retrieved
articles are about VPN connections. The embedding matched on the word "vpn",
retrieval handed over the nearest things it had, and the model correctly
declined to pretend they were relevant.

**Talking point — "what happens when retrieval returns nothing useful?"**
The prompt says so explicitly rather than leaving an empty section: a model
handed a blank space falls back on its own memory, whereas a model told plainly
that it has no supporting evidence returns low confidence. Then the gate does
the rest. "I don't know" is a first-class outcome here, not a failure — the
approval buttons and the reason field are what turn it into a human decision
rather than a dead end.

---

## 4. The API

![API docs](media/04-api-docs.png)

**What you're looking at:** the OpenAPI explorer at `/docs`, generated from the
code rather than written.

**Talking point — "how do you know the model's output is the right shape?"**
The `TicketAnalysis` schema documented on this page is the same Pydantic model
the model's JSON is validated against. One definition serves validation and
documentation both. It sets `extra="forbid"`, so a model that invents an extra
field fails validation rather than having that field silently dropped — drift is
a reason to retry, not something to shrug at.

---

## Screens worth adding yourself

Two need a login this project deliberately doesn't hold:

**RabbitMQ — http://localhost:15672** (credentials in your `.env`) → *Queues*.
Three queues: `ticket.processing` (the work), `ticket.retry` (a delay queue —
messages sit here on a TTL and RabbitMQ drops them back automatically), and
`ticket.dead-letter` (terminal, nothing consumes it by design, which is the
point of a DLQ). Screenshot the Queues tab as `media/05-rabbitmq-queues.png`.

**n8n — http://localhost:5679** → *Ticket approval* workflow, and its
*Executions* tab. Screenshot as `media/06-n8n-workflow.png`.

**Talking point for n8n — "why n8n and not just more Python?"**
It only ever receives a webhook from the worker and calls back into the API to
approve or reject. It never touches Postgres or RabbitMQ. That keeps the
business rules in one place and n8n as thin glue for the things it is genuinely
good at: notification, branching, waiting on a person.

---

## Known rough edges

Worth saying out loud rather than being caught by them:

- The evaluation set still only references the original 8 knowledge articles,
  so the 12 added later are not exercised by it.
- Category accuracy moved 87.5% -> 81.2% between two runs with no change to
  categorisation code. That is single-run variance, and a serious evaluation
  would average several runs per case.

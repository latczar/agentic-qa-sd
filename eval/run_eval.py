"""Measure the agent instead of eyeballing it.

Two modes, because they answer different questions and cost very different
amounts of time:

    python run_eval.py --retrieval-only   # seconds - is retrieval finding the right article?
    python run_eval.py                    # ~15s per case - is the whole pipeline right?

Retrieval-only needs the embedding model; the full run needs the generation
model too. Neither writes to the tickets table: cases are run through the same
functions the worker uses, not through the queue, so a run is repeatable and
doesn't fill the database with fake tickets.
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from app import rules  # noqa: E402  (worker/app)
from app.prompt import build_prompt  # noqa: E402
from shared.analysis import AnalysisError, analyze_ticket  # noqa: E402
from shared.db import SessionLocal  # noqa: E402
from shared.embeddings import get_embedding_provider  # noqa: E402
from shared.llm import get_llm_provider  # noqa: E402
from shared.models import Service, Ticket  # noqa: E402
from shared.retrieval import search_knowledge  # noqa: E402

DATASET = Path(__file__).with_name("dataset.json")


@dataclass
class CaseResult:
    case_id: str
    retrieved: list[str]
    expected_document: str | None
    latency_ms: int
    # Full-pipeline fields, left unset in retrieval-only mode.
    structured_ok: bool | None = None
    category: str | None = None
    confidence: float | None = None
    cited: list[str] = field(default_factory=list)
    escalated: bool | None = None
    expected_escalation: bool | None = None
    category_hit: bool | None = None
    error: str | None = None

    @property
    def retrieval_hit(self) -> bool:
        """Did the expected article come back at all?

        For a negative case (expected_document is null) the correct behaviour
        is retrieving nothing, so an empty result counts as a hit.
        """
        if self.expected_document is None:
            return not self.retrieved
        return self.expected_document in self.retrieved

    @property
    def retrieval_rank_1(self) -> bool:
        if self.expected_document is None:
            return not self.retrieved
        return bool(self.retrieved) and self.retrieved[0] == self.expected_document

    @property
    def citations_supported(self) -> bool:
        """Every cited document was actually one we put in front of the model.

        A citation that wasn't retrieved means the model invented a source,
        which is worse than not citing at all - it looks like evidence.
        """
        return all(doc in self.retrieved for doc in self.cited)


def _percent(count: int, total: int) -> str:
    return f"{(count / total * 100):.1f}%" if total else "n/a"


def run_case(db, case: dict, embed_provider, llm_provider, retrieval_only: bool) -> CaseResult:
    query = f"{case['subject']}\n{case['description']}"
    started = time.monotonic()

    articles = search_knowledge(db, embed_provider, query)
    retrieved = [a.slug for a in articles]

    result = CaseResult(
        case_id=case["id"],
        retrieved=retrieved,
        expected_document=case["expected_document"],
        latency_ms=int((time.monotonic() - started) * 1000),
        expected_escalation=case["expected_escalation"],
    )

    if retrieval_only:
        return result

    ticket = Ticket(subject=case["subject"], description=case["description"], submitted_by_id=0)
    service_names = [n for (n,) in db.query(Service.name).order_by(Service.name).all()]
    prompt = build_prompt(ticket, articles, service_names)

    try:
        analysis = analyze_ticket(llm_provider, prompt)
    except AnalysisError as exc:
        result.structured_ok = False
        result.error = str(exc)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    gate = rules.evaluate(analysis, ticket_context=f"{case['subject']} {case['description']}")
    keywords = [k.lower() for k in case["expected_category_keywords"]]

    result.structured_ok = True
    result.category = analysis.category
    result.confidence = analysis.confidence
    result.cited = [s.document for s in analysis.sources]
    result.escalated = gate.requires_human_approval
    result.category_hit = (
        any(k in analysis.category.lower() for k in keywords) if keywords else None
    )
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def report(results: list[CaseResult], retrieval_only: bool) -> str:
    total = len(results)
    positives = [r for r in results if r.expected_document is not None]
    negatives = [r for r in results if r.expected_document is None]

    lines = [
        "# Evaluation results",
        "",
        f"{total} cases ({len(positives)} with a known correct article, "
        f"{len(negatives)} deliberately outside the knowledge base)",
        "",
        "## Retrieval",
        "",
        f"- Hit rate (expected article retrieved at all): "
        f"{_percent(sum(r.retrieval_hit for r in positives), len(positives))}",
        f"- Recall@1 (expected article ranked first): "
        f"{_percent(sum(r.retrieval_rank_1 for r in positives), len(positives))}",
        f"- Correctly returned nothing for out-of-scope questions: "
        f"{_percent(sum(r.retrieval_hit for r in negatives), len(negatives))}",
        f"- Median retrieval+analysis latency: "
        f"{statistics.median(r.latency_ms for r in results):.0f}ms",
    ]

    if retrieval_only:
        lines += ["", "_Retrieval-only run: the generation model was not called._"]
        return "\n".join(lines)

    scored = [r for r in results if r.structured_ok is not None]
    ok = [r for r in scored if r.structured_ok]
    categorised = [r for r in ok if r.category_hit is not None]
    escalation_scored = [r for r in ok if r.escalated is not None]
    cited_anything = [r for r in ok if r.cited]

    lines += [
        "",
        "## Answer quality",
        "",
        f"- Structured output success (valid JSON matching the schema, within the retry limit): "
        f"{_percent(len(ok), len(scored))}",
        f"- Category accuracy (matched an acceptable keyword): "
        f"{_percent(sum(bool(r.category_hit) for r in categorised), len(categorised))}",
        f"- Escalation decisions matching expectation: "
        f"{_percent(sum(r.escalated == r.expected_escalation for r in escalation_scored), len(escalation_scored))}",
        f"- Citations supported by what was actually retrieved: "
        f"{_percent(sum(r.citations_supported for r in cited_anything), len(cited_anything))}",
        f"- Unsupported-answer rate (cited a document that was never retrieved): "
        f"{_percent(sum(not r.citations_supported for r in cited_anything), len(cited_anything))}",
        "",
        "## Failures worth looking at",
        "",
    ]

    problems = [
        r
        for r in results
        if not r.retrieval_hit
        or r.structured_ok is False
        or (r.escalated is not None and r.escalated != r.expected_escalation)
    ]
    if not problems:
        lines.append("_None._")
    else:
        for r in problems:
            why = []
            if not r.retrieval_hit:
                why.append(f"expected `{r.expected_document}`, got {r.retrieved or 'nothing'}")
            if r.structured_ok is False:
                why.append(f"no valid output ({r.error})")
            if r.escalated is not None and r.escalated != r.expected_escalation:
                why.append(f"escalation {r.escalated}, expected {r.expected_escalation}")
            lines.append(f"- **{r.case_id}**: {'; '.join(why)}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-only", action="store_true", help="skip the generation model")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--out", type=Path, help="write the report to a file as well as stdout")
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[: args.limit]

    db = SessionLocal()
    embed_provider = get_embedding_provider()
    llm_provider = None if args.retrieval_only else get_llm_provider()

    results = []
    try:
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['id']}", file=sys.stderr)
            results.append(run_case(db, case, embed_provider, llm_provider, args.retrieval_only))
    finally:
        db.close()

    text = report(results, args.retrieval_only)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")

    # Non-zero if retrieval is clearly broken, so this can gate a change rather
    # than just being a document someone reads.
    positives = [r for r in results if r.expected_document is not None]
    hit_rate = sum(r.retrieval_hit for r in positives) / len(positives) if positives else 1.0
    return 0 if hit_rate >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())


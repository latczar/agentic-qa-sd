"""Getting a validated TicketAnalysis out of the model, even when it doesn't
cooperate on the first try.

A model returning "valid JSON" is not the same as it being *correct* -
matching our schema, a real priority value, a confidence number in range.
This is the retry loop that turns "the model tried" into "the model produced
something we can act on, or we gave up and said so honestly."
"""

import json

from pydantic import ValidationError

from shared import progress
from shared.ai_analysis import TicketAnalysis
from shared.llm import LLMError, LLMProvider

MAX_ATTEMPTS = 3


class AnalysisError(RuntimeError):
    """Raised when the model never produced a valid TicketAnalysis in time."""


def analyze_ticket(
    provider: LLMProvider, prompt: str, max_attempts: int = MAX_ATTEMPTS
) -> TicketAnalysis:
    """Call the model, validate its answer, and retry with feedback if it's wrong.

    Each retry tells the model exactly what was wrong with its last attempt,
    rather than just asking again and hoping - a local model is far more
    likely to fix a named problem than to get it right blind a second time.
    """
    current_prompt = prompt
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        # The slowest step in the whole pipeline by a distance, so the one most
        # worth seeing live: seconds of silence from a 7B model look like a hang
        # unless something says the worker is waiting on it.
        progress.emit("calling_model", attempt=attempt, model=getattr(provider, "model", None))
        try:
            raw = provider.generate(current_prompt)
        except LLMError as exc:
            raise AnalysisError(f"model unreachable on attempt {attempt}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"response was not valid JSON: {exc}"
        else:
            try:
                return TicketAnalysis.model_validate(data)
            except ValidationError as exc:
                last_error = f"response didn't match the required schema: {exc}"

        progress.emit("invalid_answer", attempt=attempt, error=last_error[:200], retrying=attempt < max_attempts)
        current_prompt = (
            f"{prompt}\n\n"
            f"Your previous answer was invalid: {last_error}\n"
            f"Return ONLY the corrected JSON object, matching the schema exactly."
        )

    raise AnalysisError(
        f"model failed to produce a valid analysis after {max_attempts} attempts: {last_error}"
    )

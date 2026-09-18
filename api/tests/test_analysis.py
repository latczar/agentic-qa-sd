"""The validate-and-retry loop. No database, no model - pure logic, so these
run in milliseconds and cover every branch."""

import json

import pytest

from shared.analysis import MAX_ATTEMPTS, AnalysisError, analyze_ticket
from shared.llm import FakeLLMProvider, LLMError
from shared.models import Priority

VALID = json.dumps(
    {
        "category": "Authentication",
        "priority": "HIGH",
        "affected_service": "Identity Service",
        "likely_root_cause": "Session not invalidated",
        "recommended_resolution": "Invalidate sessions",
        "confidence": 0.9,
        "sources": [{"document": "password-reset", "title": "Resetting your password"}],
    }
)


def test_valid_first_response_is_used_as_is():
    provider = FakeLLMProvider([VALID])

    analysis = analyze_ticket(provider, "prompt")

    assert analysis.priority is Priority.HIGH
    assert len(provider.prompts) == 1


def test_malformed_json_is_retried_and_the_error_is_fed_back():
    provider = FakeLLMProvider(["not json at all", VALID])

    analysis = analyze_ticket(provider, "original prompt")

    assert analysis.category == "Authentication"
    assert len(provider.prompts) == 2
    # The retry tells the model what was wrong rather than just asking again -
    # a local model fixes a named problem far more reliably than a blind retry.
    assert "was not valid JSON" in provider.prompts[1]
    assert "original prompt" in provider.prompts[1]


def test_schema_violation_is_retried_with_the_validation_error():
    missing_field = json.dumps({"category": "Authentication", "confidence": 0.9})
    provider = FakeLLMProvider([missing_field, VALID])

    analyze_ticket(provider, "prompt")

    assert "didn't match the required schema" in provider.prompts[1]


def test_extra_field_is_rejected_not_silently_dropped():
    with_extra = json.loads(VALID)
    with_extra["explanation"] = "I think this is right but am not sure"
    provider = FakeLLMProvider([json.dumps(with_extra), VALID])

    analyze_ticket(provider, "prompt")

    # extra="forbid" earning its place: a model adding fields it wasn't asked
    # for is drifting, and drift is a reason to retry rather than accept.
    assert len(provider.prompts) == 2


def test_confidence_outside_zero_to_one_is_rejected():
    impossible = json.loads(VALID)
    impossible["confidence"] = 1.5
    provider = FakeLLMProvider([json.dumps(impossible), VALID])

    analyze_ticket(provider, "prompt")

    assert len(provider.prompts) == 2


def test_giving_up_after_the_retry_limit_raises_rather_than_returning_junk():
    provider = FakeLLMProvider(["nope"] * MAX_ATTEMPTS)

    with pytest.raises(AnalysisError) as exc:
        analyze_ticket(provider, "prompt")

    assert f"{MAX_ATTEMPTS} attempts" in str(exc.value)
    assert len(provider.prompts) == MAX_ATTEMPTS


def test_unreachable_model_fails_immediately_without_burning_retries():
    class Unreachable:
        def generate(self, prompt: str) -> str:
            raise LLMError("Could not reach Ollama")

    with pytest.raises(AnalysisError) as exc:
        analyze_ticket(Unreachable(), "prompt")

    # Retrying a connection error inside this loop would just stack delays on
    # top of the queue's own backoff, which already handles "come back later".
    assert "unreachable" in str(exc.value)

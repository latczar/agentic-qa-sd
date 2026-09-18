"""The approval gate. Pure logic, no database, no model - so these are fast
and exhaustive, which matters because this is the code that decides whether a
human sees something before it's acted on."""

import pytest

from app.rules import CONFIDENCE_THRESHOLD, evaluate
from shared.ai_analysis import Source, TicketAnalysis
from shared.models import Priority


def analysis(**overrides) -> TicketAnalysis:
    defaults = dict(
        category="Authentication",
        priority=Priority.HIGH,
        affected_service="Identity Service",
        likely_root_cause="Session not invalidated after reset",
        recommended_resolution="Invalidate existing sessions and retry sign-in",
        confidence=0.95,
        sources=[Source(document="password-reset-self-service", title="Resetting your password")],
    )
    defaults.update(overrides)
    return TicketAnalysis(**defaults)


def test_confident_evidenced_answer_can_proceed_without_a_human():
    gate = evaluate(analysis())

    assert gate.requires_human_approval is False


def test_low_confidence_needs_a_human():
    gate = evaluate(analysis(confidence=CONFIDENCE_THRESHOLD - 0.01))

    assert gate.requires_human_approval is True
    assert "confidence" in gate.reason


def test_no_cited_sources_needs_a_human_however_confident():
    gate = evaluate(analysis(confidence=1.0, sources=[]))

    assert gate.requires_human_approval is True
    assert "cited" in gate.reason


@pytest.mark.parametrize(
    "field,value",
    [
        ("category", "Security incident"),
        ("recommended_resolution", "Delete the affected mailbox and restore from backup"),
        ("category", "Payroll access"),
        ("recommended_resolution", "Grant admin rights to the user"),
    ],
)
def test_sensitive_topics_always_need_a_human(field, value):
    gate = evaluate(analysis(confidence=1.0, **{field: value}))

    assert gate.requires_human_approval is True
    assert "sensitive" in gate.reason


def test_critical_priority_always_needs_a_human():
    gate = evaluate(analysis(confidence=1.0, priority=Priority.CRITICAL))

    assert gate.requires_human_approval is True
    assert "CRITICAL" in gate.reason

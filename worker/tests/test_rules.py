"""The approval gate. Pure logic, no database, no model - so these are fast
and exhaustive, which matters because this is the code that decides whether a
human sees something before it's acted on.

Several of these exist because the evaluation suite caught the first version
getting them wrong in both directions at once. They are regression tests for
measured failures, not hypotheticals.
"""

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
    assert evaluate(analysis(), "I cannot sign in").requires_human_approval is False


def test_low_confidence_needs_a_human():
    gate = evaluate(analysis(confidence=CONFIDENCE_THRESHOLD - 0.01), "I cannot sign in")

    assert gate.requires_human_approval is True
    assert "confidence" in gate.reason


def test_no_cited_sources_needs_a_human_however_confident():
    gate = evaluate(analysis(confidence=1.0, sources=[]), "I cannot sign in")

    assert gate.requires_human_approval is True
    assert "cited" in gate.reason


def test_no_proposed_resolution_needs_a_human():
    gate = evaluate(analysis(recommended_resolution=None), "I cannot sign in")

    # Nothing to act on, so there is nothing to approve automatically.
    assert gate.requires_human_approval is True
    assert "no resolution" in gate.reason


def test_critical_priority_always_needs_a_human():
    gate = evaluate(analysis(confidence=1.0, priority=Priority.CRITICAL), "I cannot sign in")

    assert gate.requires_human_approval is True
    assert "CRITICAL" in gate.reason


@pytest.mark.parametrize(
    "ticket_text",
    [
        "I cannot open the payroll portal",
        "My payslip is missing from the finance system",
        "We think this is a security incident, files are encrypted",
        "Suspected malware on my laptop",
    ],
)
def test_sensitive_subjects_escalate_even_when_the_model_words_it_blandly(ticket_text):
    # Regression: the first version only looked at the model's own output, so a
    # payroll ticket the model labelled "Access" with no mention of payroll in
    # the resolution sailed straight through unreviewed. Evaluation caught it.
    gate = evaluate(analysis(confidence=1.0, category="Access"), ticket_text)

    assert gate.requires_human_approval is True
    assert "sensitive subject" in gate.reason


@pytest.mark.parametrize(
    "resolution",
    [
        "Delete the account and recreate it",
        "Grant admin rights to the user",
        "Wipe the device and re-enrol it",
        "Revoke access to the shared drive",
    ],
)
def test_destructive_actions_escalate(resolution):
    gate = evaluate(analysis(confidence=1.0, recommended_resolution=resolution), "Routine request")

    assert gate.requires_human_approval is True
    assert "destructive action" in gate.reason


@pytest.mark.parametrize(
    "resolution",
    [
        "Delete the stuck print job from the queue and restart the spooler",
        "Open Credential Manager and delete the saved entry beginning with vpn.",
        "Clear the cached credentials and sign in again",
        "Check the user's group membership is correct",
    ],
)
def test_benign_resolutions_that_merely_mention_deleting_do_not_escalate(resolution):
    # Regression: fourteen routine tickets escalated because a bare "delete" or
    # "permission" appeared in an ordinary instruction. Phrases, not words.
    gate = evaluate(analysis(confidence=0.95, recommended_resolution=resolution), "Printer stuck")

    assert gate.requires_human_approval is False

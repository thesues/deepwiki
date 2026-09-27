"""Compatibility checks for the Hermes 0.19 smart-approval contract.

These tests are skipped under the repository's bare stdlib interpreter and run
in the pinned Hermes environment used by the image validation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


approval = pytest.importorskip("tools.approval")
auxiliary = pytest.importorskip("agent.auxiliary_client")


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("APPROVE", "approve"), ("DENY", "deny"), ("ESCALATE", "escalate"),
     ("I am not sure", "escalate")],
)
def test_smart_approval_verdicts(answer, expected, monkeypatch):
    calls = []

    def call_llm(**kwargs):
        calls.append(kwargs)
        return _response(answer)

    monkeypatch.setattr(auxiliary, "call_llm", call_llm)
    assert approval._smart_approve("echo ok", "script execution") == expected
    assert calls[0]["task"] == "approval"


def test_smart_approval_model_failure_escalates_to_a_human(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("auxiliary model unavailable")

    monkeypatch.setattr(auxiliary, "call_llm", fail)
    assert approval._smart_approve("echo ok", "script execution") == "escalate"

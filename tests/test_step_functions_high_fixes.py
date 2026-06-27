"""Step Functions adapter — advisory contract + count hygiene.

Standard->Express migration savings are execution-shape dependent and Step
Functions exposes no states-per-execution / memory-consumed signal, so every rec
is a $0 Counted=False advisory. These render (so the lever is visible) but must
not inflate the counted rec-count headline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.contracts import ServiceFindings

import services.adapters.step_functions as adapter_mod
from services.adapters.step_functions import StepFunctionsModule


def _patched_scan(monkeypatch: pytest.MonkeyPatch, recs: list[dict]) -> ServiceFindings:
    monkeypatch.setattr(adapter_mod, "get_enhanced_step_functions_checks", lambda ctx: {"recommendations": recs})
    return StepFunctionsModule().scan(SimpleNamespace(pricing_multiplier=1.0, region="us-east-1"))


def test_recs_are_zero_advisory_and_excluded_from_count(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {"StateMachineArn": "arn:...:sm1", "Recommendation": "High volume", "CheckCategory": "Express Migration"},
        {"StateMachineArn": "arn:...:sm2", "Recommendation": "High volume", "CheckCategory": "Express Migration"},
    ]
    findings = _patched_scan(monkeypatch, recs)

    assert findings.total_monthly_savings == 0.0
    rendered = findings.sources["enhanced_checks"].recommendations
    assert len(rendered) == 2  # both render
    for rec in rendered:
        assert rec["Counted"] is False
        assert rec["EstimatedMonthlySavings"] == 0.0
        assert rec["EstimatedSavings"].startswith("$0.00/month")
    # Count hygiene: advisory recs excluded from the rec-count headline.
    assert findings.total_recommendations == 0


def test_empty_scan_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = _patched_scan(monkeypatch, [])
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0

"""Tests for lesson C8: an upper bound is counted only if billing corroborates it.

The invariant these tests defend, stated once:

    Removing evidence must never INCREASE counted savings.

A missing ``ce:GetCostAndUsage`` permission silently inflated the RDS snapshot tab
by $719.58/mo on a live account (bnc). The same shape existed unguarded in the AMI
tab ($161.60/mo of uncorroborated upper bounds).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services._reconcile import reconcile_against_billed


def _recs(n: int, each: float) -> list[dict]:
    return [{"ImageId": f"ami-{i}", "EstimatedMonthlySavings": each} for i in range(n)]


def test_unreadable_billing_demotes_every_bound() -> None:
    out, counted = reconcile_against_billed(_recs(3, 10.0), None, pool_label="EBS snapshot storage")
    assert counted == 0.0
    assert all(r["Counted"] is False for r in out)
    assert all(r["EstimatedMonthlySavings"] == 0.0 for r in out)
    assert all(r["PotentialMonthlySavings"] == 10.0 for r in out)
    assert all(r["EstimatedSavings"].startswith("up to $10.00/month — advisory") for r in out)
    assert "no Cost Explorer actual" in out[0]["ReconciliationBasis"]


def test_zero_billed_pool_demotes_every_bound() -> None:
    # CE answered and nothing is billed -> there is nothing to save.
    out, counted = reconcile_against_billed(_recs(3, 10.0), 0.0, pool_label="EBS snapshot storage")
    assert counted == 0.0
    assert all(r["Counted"] is False for r in out)
    assert "bills $0.00/mo" in out[0]["ReconciliationBasis"]


def test_bound_capped_at_billed_pool() -> None:
    out, counted = reconcile_against_billed(_recs(4, 10.0), 25.0, pool_label="EBS snapshot storage")
    assert counted == pytest.approx(25.0, abs=0.02)   # 40.0 upper bound -> capped to 25.0
    assert all(r["Reconciled"] for r in out)
    assert out[0]["ReconciliationFactor"] == pytest.approx(0.625)
    assert out[0]["UpperBoundBeforeReconciliation"] == 10.0
    assert "reconciled to actual billed" in out[0]["EstimatedSavings"]


def test_bound_below_pool_is_counted_in_full() -> None:
    out, counted = reconcile_against_billed(_recs(2, 10.0), 500.0, pool_label="EBS snapshot storage")
    assert counted == pytest.approx(20.0)
    assert all(r.get("Counted") is not False for r in out)
    assert out[0]["ActualBilledPool"] == 500.0
    assert "not capped" in out[0]["ReconciliationBasis"]


def test_zero_value_recs_pass_through_untouched() -> None:
    recs = [{"ImageId": "a", "EstimatedMonthlySavings": 0.0, "Counted": False}]
    out, counted = reconcile_against_billed(recs, None, pool_label="x")
    assert out == recs and counted == 0.0


def test_input_is_not_mutated() -> None:
    recs = _recs(2, 10.0)
    reconcile_against_billed(recs, None, pool_label="x")
    assert all("Counted" not in r for r in recs)


@pytest.mark.parametrize("billed", [None, 0.0, 5.0, 20.0, 1000.0])
def test_invariant_removing_evidence_never_raises_counted(billed) -> None:
    """The C8 invariant itself: counted(no evidence) <= counted(any evidence)."""
    recs = _recs(5, 10.0)  # $50 upper bound
    _, with_evidence = reconcile_against_billed(recs, billed, pool_label="x")
    _, without = reconcile_against_billed(recs, None, pool_label="x")
    assert without <= with_evidence
    assert without == 0.0
    assert with_evidence <= 50.0 + 0.02   # never exceeds the upper bound either
    if billed is not None:
        assert with_evidence <= billed + 0.02   # nor the billed pool


# --------------------------------------------------------------------------- #
# bnc live regression (2026-07-10): a fail-closed ceiling is only safe if the
# billing query is RIGHT. A wrong CE service filter returned $0 and looked
# identical to "nothing billed", falsely zeroing $161.60/mo of real AMI savings.
# --------------------------------------------------------------------------- #
def test_zero_pool_with_priced_recs_raises_a_contradiction_warning() -> None:
    warnings: list[str] = []
    out, counted = reconcile_against_billed(
        _recs(3, 10.0), 0.0, pool_label="EBS snapshot storage",
        on_contradiction=warnings.append,
    )
    assert counted == 0.0                       # still fails closed
    assert all(r["Counted"] is False for r in out)
    assert len(warnings) == 1
    msg = warnings[0]
    assert "$0.00/mo billed EBS snapshot storage" in msg
    assert "$30.00/mo" in msg                    # the contradicted total
    assert "verify the billing query" in msg


def test_no_contradiction_warning_when_pool_unreadable() -> None:
    # None means "we could not read it" — not a contradiction, just unsubstantiated.
    warnings: list[str] = []
    reconcile_against_billed(_recs(3, 10.0), None, pool_label="x", on_contradiction=warnings.append)
    assert warnings == []


def test_no_contradiction_warning_when_nothing_priced() -> None:
    warnings: list[str] = []
    reconcile_against_billed([], 0.0, pool_label="x", on_contradiction=warnings.append)
    assert warnings == []


def test_advisory_message_blames_permissions_only_when_ce_unreadable() -> None:
    unreadable, _ = reconcile_against_billed(_recs(1, 10.0), None, pool_label="P", grant_hint="grant ce:X")
    assert "grant ce:X" in unreadable[0]["EstimatedSavings"]
    # CE answered with $0 — do NOT tell the operator to grant a permission they have.
    zero, _ = reconcile_against_billed(_recs(1, 10.0), 0.0, pool_label="P", grant_hint="grant ce:X")
    assert "grant ce:X" not in zero[0]["EstimatedSavings"]
    assert "P bills $0.00/mo" in zero[0]["EstimatedSavings"]

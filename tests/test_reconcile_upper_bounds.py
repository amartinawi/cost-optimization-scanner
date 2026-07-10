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
    out, counted = reconcile_against_billed(_recs(3, 10.0), None, pool_label="EBS snapshot storage", pool_share=1.0)
    assert counted == 0.0
    assert all(r["Counted"] is False for r in out)
    assert all(r["EstimatedMonthlySavings"] == 0.0 for r in out)
    assert all(r["PotentialMonthlySavings"] == 10.0 for r in out)
    assert all(r["EstimatedSavings"].startswith("up to $10.00/month — advisory") for r in out)
    assert "no Cost Explorer actual" in out[0]["ReconciliationBasis"]


def test_zero_billed_pool_demotes_every_bound() -> None:
    # CE answered and nothing is billed -> there is nothing to save.
    out, counted = reconcile_against_billed(_recs(3, 10.0), 0.0, pool_label="EBS snapshot storage", pool_share=1.0)
    assert counted == 0.0
    assert all(r["Counted"] is False for r in out)
    assert "bills $0.00/mo" in out[0]["ReconciliationBasis"]


def test_bound_capped_at_billed_pool() -> None:
    out, counted = reconcile_against_billed(_recs(4, 10.0), 25.0, pool_label="EBS snapshot storage", pool_share=1.0)
    assert counted == pytest.approx(25.0, abs=0.02)   # 40.0 upper bound -> capped to 25.0
    assert all(r["Reconciled"] for r in out)
    assert out[0]["ReconciliationFactor"] == pytest.approx(0.625)
    assert out[0]["UpperBoundBeforeReconciliation"] == 10.0
    assert "share of the" in out[0]["EstimatedSavings"]


def test_bound_below_pool_is_counted_in_full() -> None:
    out, counted = reconcile_against_billed(_recs(2, 10.0), 500.0, pool_label="EBS snapshot storage", pool_share=1.0)
    assert counted == pytest.approx(20.0)
    assert all(r.get("Counted") is not False for r in out)
    assert out[0]["ActualBilledPool"] == 500.0
    assert "not capped" in out[0]["ReconciliationBasis"]


def test_zero_value_recs_pass_through_untouched() -> None:
    recs = [{"ImageId": "a", "EstimatedMonthlySavings": 0.0, "Counted": False}]
    out, counted = reconcile_against_billed(recs, None, pool_label="x", pool_share=1.0)
    assert out == recs and counted == 0.0


def test_input_is_not_mutated() -> None:
    recs = _recs(2, 10.0)
    reconcile_against_billed(recs, None, pool_label="x", pool_share=1.0)
    assert all("Counted" not in r for r in recs)


@pytest.mark.parametrize("billed", [None, 0.0, 5.0, 20.0, 1000.0])
def test_invariant_removing_evidence_never_raises_counted(billed) -> None:
    """The C8 invariant itself: counted(no evidence) <= counted(any evidence)."""
    recs = _recs(5, 10.0)  # $50 upper bound
    _, with_evidence = reconcile_against_billed(recs, billed, pool_label="x", pool_share=1.0)
    _, without = reconcile_against_billed(recs, None, pool_label="x", pool_share=1.0)
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
        _recs(3, 10.0), 0.0, pool_label="EBS snapshot storage", pool_share=1.0,
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
    reconcile_against_billed(_recs(3, 10.0), None, pool_label="x", pool_share=1.0, on_contradiction=warnings.append)
    assert warnings == []


def test_no_contradiction_warning_when_nothing_priced() -> None:
    warnings: list[str] = []
    reconcile_against_billed([], 0.0, pool_label="x", pool_share=1.0, on_contradiction=warnings.append)
    assert warnings == []


def test_advisory_message_blames_permissions_only_when_ce_unreadable() -> None:
    unreadable, _ = reconcile_against_billed(_recs(1, 10.0), None, pool_label="P", pool_share=1.0, grant_hint="grant ce:X")
    assert "grant ce:X" in unreadable[0]["EstimatedSavings"]
    # CE answered with $0 — do NOT tell the operator to grant a permission they have.
    zero, _ = reconcile_against_billed(_recs(1, 10.0), 0.0, pool_label="P", pool_share=1.0, grant_hint="grant ce:X")
    assert "grant ce:X" not in zero[0]["EstimatedSavings"]
    assert "P bills $0.00/mo" in zero[0]["EstimatedSavings"]


# --------------------------------------------------------------------------- #
# afs-prod live audit (2026-07-10): the billed pool covers EVERY resource of its
# kind, not just the flagged ones. Capping at the whole pool asserts the flagged
# subset IS the pool. 317 unused AMIs held 744 of 3,003 snapshots (23.7% of the
# estate) yet were credited 100% of the $5,124.78/mo bill — $3,911.50/mo that
# survives deleting all of them, because 2,259 other snapshots keep billing.
# --------------------------------------------------------------------------- #
def test_ceiling_is_the_subset_share_of_the_pool_not_the_whole_pool() -> None:
    recs = _recs(317, 6823.59 / 317)          # $6,823.59 upper bound
    out, counted = reconcile_against_billed(
        recs, 5124.78, pool_label="EBS snapshot storage", pool_share=0.2367
    )
    assert counted == pytest.approx(5124.78 * 0.2367, abs=2.0)   # ~$1,213 (per-rec rounding)
    assert counted < 5124.78                                      # never the whole pool
    assert all(r["Reconciled"] for r in out)
    assert out[0]["AttributableShare"] == pytest.approx(0.2367)
    assert out[0]["AttributableCeiling"] == pytest.approx(5124.78 * 0.2367, abs=0.02)
    assert "23.7% share" in out[0]["EstimatedSavings"]


def test_unmeasurable_share_demotes_rather_than_claiming_the_pool() -> None:
    # We know the pool, but not what fraction of it these resources represent.
    # Counting the whole pool would credit spend that survives the action.
    out, counted = reconcile_against_billed(
        _recs(3, 10.0), 500.0, pool_label="EBS snapshot storage", pool_share=None
    )
    assert counted == 0.0
    assert all(r["Counted"] is False for r in out)
    assert all(r["PotentialMonthlySavings"] == 10.0 for r in out)
    assert "share of EBS snapshot storage could not be measured" in out[0]["EstimatedSavings"]


def test_share_is_clamped_to_unit_interval() -> None:
    _, over = reconcile_against_billed(_recs(2, 50.0), 100.0, pool_label="p", pool_share=1.7)
    assert over == pytest.approx(100.0)   # never more than the pool
    _, under = reconcile_against_billed(_recs(2, 50.0), 100.0, pool_label="p", pool_share=-0.5)
    assert under == 0.0                   # a negative share is no share


def test_bound_below_its_share_is_counted_in_full() -> None:
    # Upper bound $20 is under the $50 share of a $500 pool -> counted as-is.
    out, counted = reconcile_against_billed(_recs(2, 10.0), 500.0, pool_label="p", pool_share=0.1)
    assert counted == pytest.approx(20.0)
    assert all(r.get("Counted") is not False for r in out)
    assert "10.0% share" in out[0]["ReconciliationBasis"]

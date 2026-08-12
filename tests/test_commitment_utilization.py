"""SP/RI utilization parsing against REAL Cost Explorer response shapes.

Live-pinned on bnc (2026-08-09): GetSavingsPlansUtilization returns
Total.Utilization.{UtilizationPercentage,UnusedCommitment,...} — the adapter
previously read a nonexistent "SavingsPlansUtilizations" top key (rate stuck
at 0.0) and a misspelled "SavingsPlansUtilizationsDetails" list key (per-SP
under-utilization recs never emitted; bnc: $342.49/mo unused commitment
unreported).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.adapters.commitment_analysis import CommitmentAnalysisModule


class _UtilCe:
    """CE stub speaking the real (probed) response shapes."""

    def get_savings_plans_utilization(self, **kwargs):
        return {"Total": {"Utilization": {
            "TotalCommitment": "1953.2352", "UsedCommitment": "1610.7411",
            "UnusedCommitment": "342.4941", "UtilizationPercentage": "82.4653"}}}

    def get_savings_plans_utilization_details(self, **kwargs):
        return {"SavingsPlansUtilizationDetails": [
            {"SavingsPlanArn": "arn:aws:savingsplans::784852663902:savingsplan/sp-1",
             "Utilization": {"TotalCommitment": "1953.2352", "UsedCommitment": "1610.7411",
                             "UnusedCommitment": "342.4941", "UtilizationPercentage": "82.4653"},
             "AmortizedCommitment": {"TotalAmortizedCommitment": "1953.2352"}},
        ]}

    def get_reservation_utilization(self, **kwargs):
        return {"Total": {"UtilizationPercentage": "0", "PurchasedHours": "0"}}


def _mod_and_ctx():
    ctx = SimpleNamespace(region="ap-southeast-1", warn=MagicMock(), permission_issue=MagicMock())
    return CommitmentAnalysisModule(), ctx


def test_sp_utilization_rate_parsed_from_real_shape():
    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_sp_utilization(ctx, _UtilCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert rate == pytest.approx(0.8247, abs=0.001)   # was 0.0 before the fix


def test_sp_underutilization_rec_emitted_with_unused_commitment_dollars():
    mod, ctx = _mod_and_ctx()
    recs, _ = mod._check_sp_utilization(ctx, _UtilCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert len(recs) == 1
    rec = recs[0]
    # Waste = the window's UnusedCommitment (30-day tp ~= monthly), not an
    # hourly reconstruction from fields that do not exist. The figure is real
    # and still published — as an ADVISORY, see the sunk-cost tests below.
    assert rec["AdvisoryEstimate"] == pytest.approx(342.49, abs=0.01)
    assert "82.5%" in rec["current_value"] or "82.4" in rec["current_value"]


# --------------------------------------------------------------------------- #
# bnc live regression (2026-08-12): unused commitment is a SUNK cost
#
# A Savings Plan bills its hourly commitment for the whole 1- or 3-year term
# whether or not usage consumes it, and "if you don't use all the benefits,
# then AWS doesn't apply them to the next hour" — the waste is unrecoverable.
# A plan can only be returned within 7 days of purchase, in the same calendar
# month, at a commitment of $100/hr or less. So no action available to the
# operator this month reduces the bill by the unused-commitment figure, and it
# must not sit in a headline that reads "potential monthly savings".
#
# Observed on bnc / ap-southeast-1: $390.32/mo counted across 4 plans (13.8% of
# the headline). The plans are r5/t3/r6a EC2-Instance SPs, while the account's
# only uncovered on-demand is r4.large and m6i.large — so even the charitable
# "shift usage onto the plan" reading has provably $0 of in-family usage to
# shift. Same shape as the RI lever below.
#
# The figure is still published (AdvisoryEstimate) because the waste is real
# and worth acting on at renewal — this mirrors FSx SSD->HDD and the AWS Backup
# AMI retarget: real lever, not realizable here, so $0 counted.
# --------------------------------------------------------------------------- #
def test_sp_underutilization_is_advisory_not_counted():
    mod, ctx = _mod_and_ctx()
    recs, _ = mod._check_sp_utilization(ctx, _UtilCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    rec = recs[0]
    assert rec["Counted"] is False, "unused SP commitment cannot be recovered"
    assert rec["monthly_savings"] == 0.0
    assert rec["AdvisoryEstimate"] == pytest.approx(342.49, abs=0.01)
    # The card must say why it is $0, not silently drop the dollar.
    assert "sunk" in rec["reason"].lower() or "cannot be recovered" in rec["reason"].lower()


def test_sp_underutilization_excluded_from_tab_total():
    """End-to-end: the waste must not reach total_monthly_savings."""
    mod = CommitmentAnalysisModule()
    ctx = SimpleNamespace(
        region="ap-southeast-1", warn=MagicMock(), permission_issue=MagicMock(),
        client=lambda name, region=None: _UtilCe() if name == "ce" else None,
        commitment_coverage=None,
    )
    findings = mod.scan(ctx)
    util = list(findings.sources["sp_utilization"].recommendations)
    assert len(util) == 1 and util[0]["Counted"] is False
    assert findings.total_monthly_savings == 0.0


class _RiUtilCe(_UtilCe):
    """RI utilization with a measured RICostForUnusedHours."""

    def get_reservation_utilization(self, **kwargs):
        return {
            "UtilizationsByTime": [{"Groups": [{
                "Value": "ri-abc123",
                "Attributes": {"subscriptionId": "ri-abc123"},
                "Utilization": {"UtilizationPercentage": "40.0",
                                "RICostForUnusedHours": "215.00",
                                "TotalAmortizedFee": "358.33"},
            }]}],
            "Total": {"UtilizationPercentage": "40.0", "PurchasedHours": "744"},
        }


def test_ri_underutilization_is_advisory_not_counted():
    """Reserved Instances are equally non-cancellable — same sunk cost.

    Did not fire on bnc (no RIs held), so this fix carries a $0 delta there;
    it closes the identical phantom for any account that does hold RIs.
    """
    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_ri_utilization(ctx, _RiUtilCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["monthly_savings"] == 0.0
    assert rec["AdvisoryEstimate"] == pytest.approx(215.00, abs=0.01)
    assert rate == pytest.approx(0.40, abs=0.01)


def test_sp_rate_none_when_no_commitment_held():
    class _Empty(_UtilCe):
        def get_savings_plans_utilization(self, **kwargs):
            return {"Total": {"Utilization": {"TotalCommitment": "0", "UtilizationPercentage": "0"}}}

        def get_savings_plans_utilization_details(self, **kwargs):
            return {"SavingsPlansUtilizationDetails": []}

    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_sp_utilization(ctx, _Empty(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert rate is None and recs == []                 # n/a, never a fabricated 0%


def test_ri_rate_none_when_no_purchased_hours():
    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_ri_utilization(ctx, _UtilCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert rate is None                                # zero PurchasedHours -> n/a


class _CovCe(_UtilCe):
    """Coverage shapes live-pinned on bnc (SP rows) / Jarir (RI Total)."""

    def get_savings_plans_coverage(self, **kwargs):
        return {"SavingsPlansCoverages": [
            {"Attributes": {"SERVICE": "AWS Lambda"},
             "Coverage": {"SpendCoveredBySavingsPlans": "0", "OnDemandCost": "354.36",
                          "TotalCost": "354.36", "CoveragePercentage": "0.0"}},
            {"Attributes": {"SERVICE": "Amazon Elastic Compute Cloud - Compute"},
             "Coverage": {"SpendCoveredBySavingsPlans": "1610.74", "OnDemandCost": "200.00",
                          "TotalCost": "1810.74", "CoveragePercentage": "88.9"}},
        ]}

    def get_reservation_coverage(self, **kwargs):
        return {"Total": {"CoverageHours": {
            "OnDemandHours": "16169.20", "ReservedHours": "0",
            "TotalRunningHours": "16169.20", "CoverageHoursPercentage": "0"}}}


def test_sp_coverage_rate_uses_real_covered_key():
    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_sp_coverage(ctx, _CovCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    # covered/(od+covered) = 1610.74/(554.36+1610.74) ~= 0.744 (was 0.0 via "CoveredCost")
    assert rate == pytest.approx(1610.74 / (554.36 + 1610.74), abs=0.01)
    # LS-2 — the per-service gap CARD is gone (it could carry no defensible
    # dollar), but both services' spend must still reach the rate above: the
    # denominator includes AWS Lambda's $354.36.
    assert recs == []


def test_ri_coverage_rate_from_coverage_hours():
    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_ri_coverage(ctx, _CovCe(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert rate == pytest.approx(0.0)                  # real 0% (hours ran, none reserved)


def test_ri_coverage_none_when_no_hours():
    class _NoHours(_CovCe):
        def get_reservation_coverage(self, **kwargs):
            return {"Total": {"CoverageHours": {"TotalRunningHours": "0"}}}

    mod, ctx = _mod_and_ctx()
    recs, rate = mod._check_ri_coverage(ctx, _NoHours(), {"Start": "2026-07-10", "End": "2026-08-09"})
    assert rate is None


# --------------------------------------------------------------------------- #
# H3 — expiring Savings Plans come from savingsplans:DescribeSavingsPlans
# --------------------------------------------------------------------------- #
def test_expiring_sp_emitted_from_describe_savings_plans():
    """The old implementation read a MISSPELLED CE key
    (SavingsPlansUtilizationsDetails) and an EndDateTime field that
    SavingsPlansUtilizationDetail does not have, so the check could never fire
    on any account. End dates live on savingsplans:DescribeSavingsPlans."""
    from datetime import datetime, timedelta, UTC

    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat().replace("+00:00", "Z")
    later = (datetime.now(UTC) + timedelta(days=200)).isoformat().replace("+00:00", "Z")
    sp = MagicMock()
    sp.describe_savings_plans.return_value = {"savingsPlans": [
        {"savingsPlanId": "sp-expiring", "savingsPlanType": "Compute", "end": soon},
        {"savingsPlanId": "sp-far", "savingsPlanType": "Compute", "end": later},
    ]}
    ctx = SimpleNamespace(
        region="us-east-1", warn=MagicMock(), permission_issue=MagicMock(),
        client=lambda name, region=None: sp,
    )
    recs = CommitmentAnalysisModule()._check_expiring(ctx, MagicMock(), {"Start": "x", "End": "y"})

    assert [r["resource_id"] for r in recs] == ["sp-expiring"]  # >90d not flagged
    rec = recs[0]
    assert rec["severity"] == "HIGH"          # <= 30 days
    assert rec["Counted"] is False            # an expiry date is not a saving (D4)
    assert rec["monthly_savings"] == 0.0
    assert "expires in" in rec["reason"]


def test_expiring_sp_no_client_is_silent():
    ctx = SimpleNamespace(region="us-east-1", warn=MagicMock(), permission_issue=MagicMock())
    # No .client attribute at all -> abstain, no crash.
    assert CommitmentAnalysisModule()._check_expiring(ctx, MagicMock(), {"Start": "x", "End": "y"}) == []

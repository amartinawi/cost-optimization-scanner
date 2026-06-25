"""Tests for the Fargate-isolated Compute Savings Plan view.

Covers the pure per-leg SP math (discount, rightsized baseline, coverage),
the adapter's _check_fargate_savings_plan driven by fake CE + SavingsPlans
clients, and the reporter rendering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.commitment_analysis import CommitmentAnalysisModule
from services.commitment_logic import fargate_sp_analysis, fargate_sp_cell


# Real May-2026 ap-south-1 Fargate legs (od $, qty hours) and 3yr No-Upfront rates.
_LEGS = {
    "APS3-Fargate-vCPU-Hours:perCPU": {"od": 1259.77, "qty": 29592.0},
    "APS3-Fargate-GB-Hours": {"od": 296.33, "qty": 66666.0},
    "APS3-Fargate-ARM-vCPU-Hours:perCPU": {"od": 48.35, "qty": 2028.0},
    "APS3-Fargate-ARM-GB-Hours": {"od": 10.59, "qty": 4053.0},
}
_RATES_3YR_NOUP = {
    "APS3-Fargate-vCPU-Hours:perCPU": 0.023408,
    "APS3-Fargate-GB-Hours": 0.002560,
    "APS3-Fargate-ARM-vCPU-Hours:perCPU": 0.012874,
    "APS3-Fargate-ARM-GB-Hours": 0.001404,
}


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def test_cell_discount_and_full_coverage():
    cell = fargate_sp_cell(_LEGS, _RATES_3YR_NOUP, rightsizing_monthly=0.0, coverage_ratio=1.0)
    assert cell["eligible_od"] == pytest.approx(1615.04, abs=0.01)
    assert cell["discount_pct"] == pytest.approx(44.6, abs=1.0)  # ~45% Fargate 3yr NoUp
    # Full coverage, no rightsizing → ceiling == recommended.
    assert cell["recommended_saving"] == pytest.approx(cell["ceiling_saving"], abs=0.01)


def test_coverage_ratio_scales_recommended():
    full = fargate_sp_cell(_LEGS, _RATES_3YR_NOUP, coverage_ratio=1.0)["ceiling_saving"]
    cell = fargate_sp_cell(_LEGS, _RATES_3YR_NOUP, coverage_ratio=0.708)
    assert cell["recommended_saving"] == pytest.approx(round(full * 0.708, 2), abs=0.02)


def test_rightsizing_reduces_baseline_and_savings():
    base = fargate_sp_cell(_LEGS, _RATES_3YR_NOUP, rightsizing_monthly=0.0, coverage_ratio=1.0)
    rs = fargate_sp_cell(_LEGS, _RATES_3YR_NOUP, rightsizing_monthly=300.0, coverage_ratio=1.0)
    assert rs["rightsized_od"] == pytest.approx(base["eligible_od"] - 300.0, abs=0.01)
    # Discount % is rate-driven (unchanged); ceiling saving scales with baseline.
    assert rs["discount_pct"] == base["discount_pct"]
    assert rs["ceiling_saving"] < base["ceiling_saving"]


def test_analysis_sorts_cells_by_recommended():
    matrix = {
        ("3yr", "No Upfront"): _RATES_3YR_NOUP,
        ("1yr", "No Upfront"): {k: v * 1.8 for k, v in _RATES_3YR_NOUP.items()},  # worse discount
    }
    out = fargate_sp_analysis(matrix and _LEGS, matrix, coverage_ratio=0.7)
    assert [c["term"] for c in out["cells"]][0] == "3yr"  # higher saving first


def test_analysis_empty_legs():
    out = fargate_sp_analysis({}, {("3yr", "No Upfront"): _RATES_3YR_NOUP})
    assert out["eligible_od"] == 0.0
    assert out["cells"][0]["recommended_saving"] == 0.0


# --------------------------------------------------------------------------- #
# Adapter — _check_fargate_savings_plan with fakes
# --------------------------------------------------------------------------- #
def _fake_ce():
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [{"Groups": [
            {"Keys": ["APS3-Fargate-vCPU-Hours:perCPU"], "Metrics": {"UnblendedCost": {"Amount": "1259.77"}, "UsageQuantity": {"Amount": "29592"}}},
            {"Keys": ["APS3-Fargate-GB-Hours"], "Metrics": {"UnblendedCost": {"Amount": "296.33"}, "UsageQuantity": {"Amount": "66666"}}},
            {"Keys": ["APS3-Fargate-EphemeralStorage-GB-Hours"], "Metrics": {"UnblendedCost": {"Amount": "1.47"}, "UsageQuantity": {"Amount": "11535"}}},  # excluded
            {"Keys": ["APS3-DataTransfer-Regional-Bytes"], "Metrics": {"UnblendedCost": {"Amount": "3.72"}, "UsageQuantity": {"Amount": "372"}}},  # excluded
        ]}]
    }
    ce.get_savings_plans_purchase_recommendation.return_value = {
        "SavingsPlansPurchaseRecommendation": {"SavingsPlansPurchaseRecommendationSummary": {
            "HourlyCommitmentToPurchase": "2.573", "CurrentOnDemandSpend": "2615.0"}}}
    return ce


def _fake_sp():
    sp = MagicMock()

    def offering(savingsPlanPaymentOptions, **kw):
        pay = savingsPlanPaymentOptions[0]
        rate_mult = {"No Upfront": 1.0, "Partial Upfront": 0.94, "All Upfront": 0.90}[pay]
        results = []
        for dur in (31536000, 94608000):  # 1yr, 3yr
            base = {"APS3-Fargate-vCPU-Hours:perCPU": 0.03, "APS3-Fargate-GB-Hours": 0.0033} if dur == 31536000 \
                else {"APS3-Fargate-vCPU-Hours:perCPU": 0.023408, "APS3-Fargate-GB-Hours": 0.002560}
            for ut, rate in base.items():
                results.append({"savingsPlanOffering": {"durationSeconds": dur}, "usageType": ut, "rate": str(rate * rate_mult)})
        return {"searchResults": results}

    sp.describe_savings_plans_offering_rates.side_effect = offering
    return sp


def _ctx(fargate_rightsizing=0.0):
    ce = _fake_ce()
    sp = _fake_sp()
    ns = SimpleNamespace(region="ap-south-1", fargate_rightsizing_monthly=fargate_rightsizing)
    ns.client = lambda name, region=None: {"ce": ce, "savingsplans": sp}.get(name)
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    return ns, ce, sp


def test_adapter_builds_fargate_sp_cells():
    ctx, ce, sp = _ctx()
    recs, extras = CommitmentAnalysisModule()._check_fargate_savings_plan(ctx, ce, {"Start": "2026-05-01", "End": "2026-06-01"})
    # Ephemeral + data transfer excluded → eligible = 1259.77 + 296.33.
    assert extras["eligible_od"] == pytest.approx(1556.10, abs=0.01)
    # 6 cells (2 terms x 3 payments), all advisory ($0 counted).
    assert len(recs) == 6
    assert all(r["Counted"] is False and r["monthly_savings"] == 0.0 for r in recs)
    assert all(r["check_category"] == "Fargate Savings Plan" for r in recs)
    # 3yr discount > 1yr discount.
    by = {(r["term"], r["payment"]): r["discount_pct"] for r in recs}
    assert by[("3yr", "No Upfront")] > by[("1yr", "No Upfront")]


def test_adapter_applies_rightsizing_handoff():
    ctx, ce, sp = _ctx(fargate_rightsizing=200.0)
    _, extras = CommitmentAnalysisModule()._check_fargate_savings_plan(ctx, ce, {"Start": "x", "End": "y"})
    assert extras["rightsizing_monthly"] == 200.0
    assert extras["rightsized_od"] == pytest.approx(extras["eligible_od"] - 200.0, abs=0.01)


def test_adapter_falls_back_to_estimator_when_no_handoff(monkeypatch):
    # No ctx.fargate_rightsizing_monthly (isolated --scan-only commitment_analysis)
    # → the adapter computes a live ECS-only estimate.
    import services.containers as containers_shim

    monkeypatch.setattr(containers_shim, "estimate_fargate_rightsizing_monthly", lambda ctx: 175.0)
    ce = _fake_ce()
    sp = _fake_sp()
    ns = SimpleNamespace(region="ap-south-1")  # NOTE: no fargate_rightsizing_monthly attr
    ns.client = lambda name, region=None: {"ce": ce, "savingsplans": sp}.get(name)
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    _, extras = CommitmentAnalysisModule()._check_fargate_savings_plan(ns, ce, {"Start": "x", "End": "y"})
    assert extras["rightsizing_monthly"] == 175.0
    assert extras["rightsized_od"] == pytest.approx(extras["eligible_od"] - 175.0, abs=0.01)


def test_handoff_takes_precedence_over_estimator(monkeypatch):
    import services.containers as containers_shim

    called = {"n": 0}
    monkeypatch.setattr(containers_shim, "estimate_fargate_rightsizing_monthly",
                        lambda ctx: called.__setitem__("n", called["n"] + 1) or 999.0)
    ctx, ce, sp = _ctx(fargate_rightsizing=120.0)  # hand-off present
    _, extras = CommitmentAnalysisModule()._check_fargate_savings_plan(ctx, ce, {"Start": "x", "End": "y"})
    assert extras["rightsizing_monthly"] == 120.0
    assert called["n"] == 0  # estimator NOT called when hand-off present


def test_quantify_fargate_rightsizing_pure():
    from services.containers_logic import quantify_fargate_rightsizing
    # 2 vCPU / 8 GB (2048/8192) x2 → target 1 vCPU / 2 GB.
    q = quantify_fargate_rightsizing(2048, 8192, 2, 0.04048, 0.004445)
    assert q is not None
    assert (q["target_cpu_units"], q["target_mem_mb"]) == (1024, 2048)
    cur = 2 * 0.04048 + 8 * 0.004445
    tgt = 1 * 0.04048 + 2 * 0.004445
    assert q["saving"] == pytest.approx((cur - tgt) * 730 * 2)
    # Already smallest size → None.
    assert quantify_fargate_rightsizing(256, 512, 1, 0.04048, 0.004445) is None


def test_adapter_no_fargate_returns_empty():
    ctx, ce, sp = _ctx()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
    recs, extras = CommitmentAnalysisModule()._check_fargate_savings_plan(ctx, ce, {"Start": "x", "End": "y"})
    assert recs == [] and extras == {}


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #
def test_ri_utilization_uses_subscription_id_groupby():
    ce = MagicMock()
    captured = {}

    def util(**kw):
        captured.update(kw)
        return {"UtilizationsByTime": [{"Groups": [
            {"Attributes": {"subscriptionId": "ri-abc"},
             "Utilization": {"UtilizationPercentage": "40", "TotalAmortizedCost": "100"}},
        ]}], "Total": {"UtilizationPercentage": "40"}}

    ce.get_reservation_utilization.side_effect = util
    ctx = SimpleNamespace()
    ctx.warn = MagicMock(); ctx.permission_issue = MagicMock()
    recs, rate = CommitmentAnalysisModule()._check_ri_utilization(ctx, ce, {"Start": "x", "End": "y"})
    assert captured["GroupBy"] == [{"Type": "DIMENSION", "Key": "SUBSCRIPTION_ID"}]
    assert recs[0]["resource_id"] == "ri-abc" and recs[0]["monthly_savings"] == 60.0  # 100 * (1-0.4)
    assert rate == 0.40


def test_ri_coverage_no_groupby_returns_overall_rate():
    ce = MagicMock()
    captured = {}

    def cov(**kw):
        captured.update(kw)
        return {"Total": {"CoveragePercentage": "85"}}

    ce.get_reservation_coverage.side_effect = cov
    ctx = SimpleNamespace()
    ctx.warn = MagicMock(); ctx.permission_issue = MagicMock()
    recs, rate = CommitmentAnalysisModule()._check_ri_coverage(ctx, ce, {"Start": "x", "End": "y"})
    assert "GroupBy" not in captured  # SERVICE groupBy removed (API rejects it)
    assert rate == 0.85 and recs == []


def test_cost_hub_commitment_recs_are_advisory_not_counted():
    # CoH RI/SP purchase recs overlap per-service rightsizing -> advisory, not
    # summed into the commitment tab total (which then reflects realized waste).
    coh = [
        {"actionType": "PurchaseReservedInstances", "currentResourceType": "RdsReservedInstances",
         "estimatedMonthlySavings": 4768.53, "region": "eu-west-1"},
        {"actionType": "PurchaseReservedInstances", "currentResourceType": "ElastiCacheReservedInstances",
         "estimatedMonthlySavings": 367.01, "region": "eu-west-1"},
    ]
    ce = MagicMock()
    # No SP/RI utilization data, no purchase scenarios (DataUnavailable account).
    ce.get_savings_plans_utilization.side_effect = Exception("DataUnavailable")
    ce.get_savings_plans_utilization_details.side_effect = Exception("DataUnavailable")
    ce.get_savings_plans_coverage.return_value = {"Total": {"CoveragePercentage": "0"}}
    ce.get_reservation_utilization.return_value = {"UtilizationsByTime": [], "Total": {}}
    ce.get_reservation_coverage.return_value = {"Total": {"CoveragePercentage": "0"}}
    ce.get_savings_plans_purchase_recommendation.return_value = {"SavingsPlansPurchaseRecommendation": {}}
    ce.get_reservation_purchase_recommendation.return_value = {"Recommendations": []}
    sp = MagicMock()
    sp.describe_savings_plans_offering_rates.return_value = {"searchResults": []}
    ctx = SimpleNamespace(region="eu-west-1", pricing_multiplier=1.0, fast_mode=False,
                          cost_hub_splits={"commitment_analysis": coh}, fargate_rightsizing_monthly=0.0)
    ctx.client = lambda n, region=None: {"ce": ce, "savingsplans": sp}.get(n)
    ctx.warn = MagicMock(); ctx.permission_issue = MagicMock()
    ctx.pricing_engine = MagicMock()
    f = CommitmentAnalysisModule().scan(ctx)
    # CoH RI recs present and rendered, but advisory (excluded from the total).
    assert f.sources["cost_optimization_hub"].count == 2
    assert all(r.get("Counted") is False for r in f.sources["cost_optimization_hub"].recommendations)
    assert f.total_monthly_savings == 0.0  # no existing-commitment waste on this account


def test_render_fargate_sp_matrix():
    from reporter_phase_b import _render_fargate_savings_plan
    recs = [
        {"term": "3yr", "payment": "No Upfront", "discount_pct": 45.0, "recommended_saving": 515.0, "ceiling_saving": 727.37},
        {"term": "1yr", "payment": "No Upfront", "discount_pct": 20.0, "recommended_saving": 229.0, "ceiling_saving": 323.72},
    ]
    sd = {"sources": {"fargate_savings_plan": {"extras": {
        "eligible_od": 1615.04, "rightsized_od": 1315.04, "rightsizing_monthly": 300.0, "coverage_ratio": 0.708}}}}
    html = _render_fargate_savings_plan(recs, "fargate_savings_plan", sd)
    assert "Fargate Compute Savings Plan" in html
    assert "$1,615.04/mo" in html and "$1,315.04/mo" in html
    assert "45.0%" in html and "$515.00/mo" in html and "$727.37/mo" in html
    assert "Rightsize first" in html

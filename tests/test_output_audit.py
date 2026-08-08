"""Tests for tools/output_audit.py — the output-audit invariant harness.

Each test seeds ONE violation class from docs/audits/prompts/_LIVE_AUDIT_LESSONS.md
into a synthetic scan JSON and asserts the matching sweep flags it, plus a clean
report that must pass every sweep with zero FAILs.
"""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.output_audit import (  # noqa: E402
    load_report,
    rec_dollar,
    run_sweeps,
    sweep_advisory_leak,
    sweep_counted_zero,
    sweep_headline,
    sweep_html_render,
    sweep_negative,
    sweep_shared_snapshots,
    sweep_string_numeric,
    sweep_summary_semantics,
    sweep_warnings,
    triage_log,
)


def _counted(dollars: float, **extra) -> dict:
    """A well-formed counted rec: string and numeric agree to the cent."""
    return {
        "Counted": True,
        "EstimatedMonthlySavings": dollars,
        "EstimatedSavings": f"${dollars:,.2f}/month",
        **extra,
    }


def _advisory(**extra) -> dict:
    """A well-formed $0 advisory: zero numeric, potential carries the figure."""
    return {
        "Counted": False,
        "EstimatedMonthlySavings": 0.0,
        "EstimatedSavings": "$0.00/month",
        "PotentialMonthlySavings": 42.0,
        **extra,
    }


def make_report() -> dict:
    """Minimal internally-consistent scan JSON matching ScanResultBuilder shape."""
    services = {
        "ami": {
            "service_name": "AMI",
            "total_recommendations": 2,
            "total_monthly_savings": 30.0,
            "sources": {
                "unused_amis": {
                    "count": 2,
                    "recommendations": [
                        _counted(10.0, ImageId="ami-aaa", SnapshotIds=["snap-0a1a1a1a"]),
                        _counted(20.0, ImageId="ami-bbb", SnapshotIds=["snap-0b2b2b2b"]),
                    ],
                }
            },
        },
        "ebs": {
            "service_name": "EBS",
            "total_recommendations": 1,
            "total_monthly_savings": 5.0,
            "sources": {
                "snapshots": {
                    "count": 2,
                    "recommendations": [
                        _counted(5.0, SnapshotId="snap-0c3c3c3c"),
                        _advisory(SnapshotId="snap-0d4d4d4d"),
                    ],
                }
            },
        },
    }
    return {
        "account_id": "111122223333",
        "region": "eu-west-1",
        "profile": "test",
        "scan_warnings": [],
        "permission_issues": [],
        "services": services,
        "summary": {
            "total_services_scanned": 2,
            "total_recommendations": 3,
            "total_monthly_savings": 35.0,
        },
    }


def _fails(findings):
    return [f for f in findings if f["severity"] == "FAIL"]


# ---------------------------------------------------------------- clean pass


def test_clean_report_has_no_fails():
    findings = run_sweeps(make_report())
    assert _fails(findings) == [], _fails(findings)


# ------------------------------------------------------------- S1 headline


def test_headline_mismatch_flagged():
    data = make_report()
    data["summary"]["total_monthly_savings"] = 99.0
    assert _fails(sweep_headline(data))


# -------------------------------------------------------- S2 advisory leak


def test_advisory_leak_flagged():
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][1][
        "EstimatedMonthlySavings"
    ] = 13.30
    leaks = _fails(sweep_advisory_leak(data))
    assert leaks and "ebs" in leaks[0]["service"]


def test_advisory_leak_snake_case_field_flagged():
    """B1 subtlety (i): the numeric hides under snake_case monthly_savings."""
    data = make_report()
    rec = data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][1]
    rec["monthly_savings"] = 7.5
    assert _fails(sweep_advisory_leak(data))


def test_commitment_projection_advisory_exempt():
    """B1 subtlety (ii): SP/RI what-if projections legitimately carry a numeric."""
    data = make_report()
    data["services"]["commitment_analysis"] = {
        "service_name": "Commitment Analysis",
        "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {
            "purchase_recommendations": {
                "count": 1,
                "recommendations": [
                    {"Counted": False, "monthly_savings": 500.0, "EstimatedSavings": ""}
                ],
            }
        },
    }
    data["summary"]["total_services_scanned"] = 3
    assert _fails(sweep_advisory_leak(data)) == []


# ------------------------------------------------------- S3 counted-but-$0


def test_counted_zero_warned():
    data = make_report()
    data["services"]["ami"]["sources"]["unused_amis"]["recommendations"].append(
        {"Counted": True, "EstimatedMonthlySavings": 0.0, "EstimatedSavings": ""}
    )
    warns = [f for f in sweep_counted_zero(data) if f["severity"] == "WARN"]
    assert warns


# -------------------------------------------------- S4 shared snapshot A3


def test_shared_snapshot_across_amis_flagged():
    data = make_report()
    recs = data["services"]["ami"]["sources"]["unused_amis"]["recommendations"]
    recs[1]["SnapshotIds"] = ["snap-0a1a1a1a"]  # same snap as rec 0
    assert _fails(sweep_shared_snapshots(data))


def test_snapshot_counted_in_ami_and_ebs_flagged():
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][0][
        "SnapshotId"
    ] = "snap-0a1a1a1a"  # also counted under ami-aaa
    assert _fails(sweep_shared_snapshots(data))


def test_advisory_snapshot_reference_not_flagged():
    """A $0 advisory referencing a counted snap is the CORRECT dedup output."""
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][1][
        "SnapshotId"
    ] = "snap-0a1a1a1a"
    assert _fails(sweep_shared_snapshots(data)) == []


# --------------------------------------------- S5 string/numeric agreement


def test_string_numeric_disagreement_flagged():
    data = make_report()
    rec = data["services"]["ami"]["sources"]["unused_amis"]["recommendations"][0]
    rec["EstimatedSavings"] = "$4.84/month"  # numeric stays 10.0 — B2
    assert _fails(sweep_string_numeric(data))


def test_counted_string_without_numeric_flagged():
    """B3: a counted rec with only a string breaks counted == rendered."""
    data = make_report()
    recs = data["services"]["ami"]["sources"]["unused_amis"]["recommendations"]
    recs[0]["EstimatedMonthlySavings"] = 0.0
    recs[0]["EstimatedSavings"] = "$10.00/month"
    assert _fails(sweep_string_numeric(data))


def test_percent_only_string_not_flagged():
    data = make_report()
    rec = data["services"]["ami"]["sources"]["unused_amis"]["recommendations"][0]
    rec["EstimatedSavings"] = "Up to 75%"
    findings = sweep_string_numeric(data)
    assert all("ami" not in f.get("service", "") for f in _fails(findings))


# --------------------------------------------- S7 summary count semantics


def test_service_counted_only_mismatch_flagged():
    data = make_report()
    data["services"]["ebs"]["total_recommendations"] = 2  # includes the advisory — D4
    assert _fails(sweep_summary_semantics(data))


def test_scanned_services_mismatch_flagged():
    data = make_report()
    data["summary"]["total_services_scanned"] = 7
    assert _fails(sweep_summary_semantics(data))


# ------------------------------------------------------- S8 negative / NaN


def test_negative_savings_flagged():
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][0][
        "EstimatedMonthlySavings"
    ] = -5.0
    assert _fails(sweep_negative(data))


# ----------------------------------------------------- S9 HTML render D2


def _wrap_html(data: dict, tabs: list[str]) -> str:
    payload = base64.b64encode(json.dumps(data).encode()).decode()
    tags = "".join(f'<a id="tab-{k}"></a><div id="panel-{k}"></div>' for k in tabs)
    return f'<html>{tags}<a href="data:application/json;base64,{payload}">json</a></html>'


def test_missing_tab_for_rendered_service_flagged():
    data = make_report()
    html = _wrap_html(data, tabs=["ami"])  # ebs renders cards but has no tab — D2
    assert _fails(sweep_html_render(data, html))


def test_all_tabs_present_passes():
    data = make_report()
    html = _wrap_html(data, tabs=["ami", "ebs"])
    assert _fails(sweep_html_render(data, html)) == []


def test_load_report_from_html(tmp_path):
    data = make_report()
    p = tmp_path / "r.html"
    p.write_text(_wrap_html(data, tabs=["ami", "ebs"]))
    assert load_report(p) == data


# ------------------------------------------------ S11/S12 warnings + perms


def test_dropped_coh_bucket_flagged():
    data = make_report()
    data["scan_warnings"] = [
        {"service": "cost_hub", "message": "2 recommendation type(s) had no service bucket and were dropped"}
    ]
    assert _fails(sweep_warnings(data))


def test_ce_permission_gap_warned():
    data = make_report()
    data["permission_issues"] = [
        {"service": "rds", "action": "ce:GetCostAndUsage", "message": "AccessDenied"}
    ]
    findings = sweep_warnings(data)
    assert any("ce:GetCostAndUsage" in f["message"] for f in findings)


# ------------------------------------------------------- S10 log triage


def test_log_triage_classifies():
    log = (
        "⚠️ Warning: Cost Hub recommends deleting vol-001 but it is currently 'in-use', "
        "not unattached — dropping the stale recommendation.\n"
        "⚠️ Warning: Skipped IOPS rightsizing for vol-002: no CloudWatch IOPS data in the 14-day window.\n"
        "⚠️ Warning: S3 endpoint unreachable for region me-south-1; skipping all remaining buckets\n"
        "📊 Found 694 optimization opportunities across 34 services\n"
    )
    findings = triage_log(log)
    cats = {f["sweep"] for f in findings}
    assert "log-coverage-gap" in cats  # unreachable endpoint = absent savings
    assert any(f["sweep"] == "log-summary" and "694" in f["message"] for f in findings)


# ----------------------------------------------------------- rec_dollar F1/F2


def test_rec_dollar_reads_all_field_shapes():
    assert rec_dollar({"EstimatedMonthlySavings": 3.0}) == 3.0
    assert rec_dollar({"estimatedMonthlySavings": 4.0}) == 4.0  # CoH camelCase — F1
    assert rec_dollar({"EstimatedMonthlyCost": 5.0}) == 5.0  # unattached EBS — F2
    assert rec_dollar({"monthly_savings": 6.0}) == 6.0
    assert rec_dollar({"EstimatedSavings": "$7.50/month"}) == 7.5
    assert rec_dollar({}) == 0.0


# ------------------------------------------------- S13 tab reconciliation


def test_tab_total_mismatch_flagged():
    from tools.output_audit import sweep_tab_reconcile

    data = make_report()
    data["services"]["ami"]["total_monthly_savings"] = 96.0  # recs sum to 30
    assert _fails(sweep_tab_reconcile(data))


def test_tab_reconcile_skips_placeholder_sources():
    from tools.output_audit import sweep_tab_reconcile

    data = make_report()
    data["services"]["ami"]["sources"]["placeholder"] = {"count": 5, "recommendations": []}
    data["services"]["ami"]["total_monthly_savings"] = 96.0
    assert _fails(sweep_tab_reconcile(data)) == []


# ------------------------------------------- F5 CO nested savings shape


def test_rec_dollar_reads_compute_optimizer_nested():
    rec = {
        "volumeArn": "arn:aws:ec2:eu-west-1:1:volume/vol-0aaaaaaaa",
        "finding": "NotOptimized",
        "volumeRecommendationOptions": [
            {"savingsOpportunity": {"estimatedMonthlySavings": {"currency": "USD", "value": 22.0}}}
        ],
    }
    assert rec_dollar(rec) == 22.0


def test_eks_node_group_projection_exempt():
    """Born-advisory Spot/Graviton what-ifs are projections, not B1 leaks."""
    data = make_report()
    data["services"]["eks_cost"] = {
        "service_name": "EKS",
        "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {
            "node_group_optimization": {
                "count": 1,
                "recommendations": [
                    {"Counted": False, "monthly_savings": 250.19, "check_type": "node_group"}
                ],
            }
        },
    }
    data["summary"]["total_services_scanned"] = 3
    assert _fails(sweep_advisory_leak(data)) == []


# --------------------------------------------- rate-assertion extraction


def test_extract_rate_assertions_from_audit_basis():
    from tools.output_audit import extract_rate_assertions

    data = make_report()
    rec = data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][0]
    rec["AuditBasis"] = {
        "rate_basis": "Aurora backup storage $/GB-month (live Pricing API)",
        "rate": 0.021,
        "region": "eu-west-1",
        "engine": "aurora-mysql",
        "formula": "9GB x $0.0210/GB-mo",
    }
    rows = extract_rate_assertions(data)
    rate_rows = [r for r in rows if r["field"] == "rate"]
    assert rate_rows and rate_rows[0]["value"] == 0.021
    assert "aurora-mysql" in rate_rows[0]["context"]


def test_extract_rate_assertions_from_pricing_basis():
    from tools.output_audit import extract_rate_assertions

    data = make_report()
    rec = data["services"]["ami"]["sources"]["unused_amis"]["recommendations"][0]
    rec["PricingBasis"] = "$1.8640->$0.9320/hr Windows on-demand (r6i.4xlarge->r6i.2xlarge) x 730h"
    rows = [r for r in extract_rate_assertions(data) if r["field"] == "PricingBasis"]
    assert rows and "$1.8640" in rows[0]["context"]


def test_extract_rate_assertions_dedupes_identical_claims():
    from tools.output_audit import extract_rate_assertions

    data = make_report()
    for rec in data["services"]["ami"]["sources"]["unused_amis"]["recommendations"]:
        rec["AuditBasis"] = {"rate": 0.05, "region": "eu-west-1"}
    rows = [r for r in extract_rate_assertions(data) if r["field"] == "rate"]
    assert len(rows) == 1  # two recs, one distinct claim


def test_extract_rate_assertions_skips_non_rate_numerics():
    from tools.output_audit import extract_rate_assertions

    data = make_report()
    rec = data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][0]
    rec["AuditBasis"] = {"rate": 0.095, "age_days": 130, "size_gb": 9, "region": "x"}
    fields = {r["field"] for r in extract_rate_assertions(data)}
    assert "rate" in fields and "age_days" not in fields and "size_gb" not in fields


# ------------------------------------------ S14 projected commitment reconcile


def test_projected_commitment_reconciles():
    from tools.output_audit import sweep_projected_commitment

    data = make_report()
    data["services"]["commitment_analysis"] = {
        "service_name": "Commitment Analysis", "total_recommendations": 0,
        "total_monthly_savings": 0.0,
        "sources": {"purchase_recommendations": {"count": 1, "recommendations": [
            {"card_kind": "ri_type", "service": "RDS", "instance_type": "x",
             "region": "eu-west-1", "Counted": False, "monthly_savings": 1000.0,
             "scenarios": [], "recommended_scenario": 0}]}},
    }
    data["summary"]["projected_commitment_monthly_savings"] = 1000.0
    assert _fails(sweep_projected_commitment(data)) == []
    data["summary"]["projected_commitment_monthly_savings"] = 999999.0
    assert _fails(sweep_projected_commitment(data))


# ----------------------- B1-iii: commitment-demotion advisories (Jarir J-2)


def test_demotion_marked_advisory_exempt_from_leak_sweep():
    """A Counted=False rec carrying the demotion markers (CommitmentCoverageNote
    / AdvisoryEstimate) keeps its indicative numeric BY DESIGN (B1-iii) — the
    renderer shows it beside the coverage note; nothing sums it."""
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][1].update(
        {"estimatedMonthlySavings": 332.296, "AdvisoryEstimate": 332.296,
         "CommitmentCoverageNote": "Covered by an active c6a EC2-Instance Savings Plan"})
    assert _fails(sweep_advisory_leak(data)) == []


def test_markerless_demoted_rec_still_flagged():
    data = make_report()
    data["services"]["ebs"]["sources"]["snapshots"]["recommendations"][1][
        "estimatedMonthlySavings"] = 13.30  # no markers -> genuine B1 leak
    assert _fails(sweep_advisory_leak(data))


def test_network_asg_born_advisory_source_exempt():
    data = make_report()
    data["services"]["network"] = {
        "service_name": "Network", "total_recommendations": 0, "total_monthly_savings": 0.0,
        "sources": {"auto_scaling_groups": {"count": 1, "recommendations": [
            {"Counted": False, "EstimatedMonthlySavings": 118.96,
             "CheckCategory": "Oversized ASG Instances"}]}},
    }
    data["summary"]["total_services_scanned"] = 3
    assert _fails(sweep_advisory_leak(data)) == []

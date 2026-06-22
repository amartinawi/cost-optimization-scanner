"""Tests for the EC2 audit-remediation fixes.

Covers the OS-aware pricing helpers, the spot guard, cron/batch mutual
exclusivity, expanded previous-generation coverage, and — most importantly —
the cross-source de-duplication in the EC2 adapter that stops the same
instance's savings being counted by Cost Optimization Hub, Compute Optimizer,
and the heuristic checks simultaneously.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.ec2 as ec2_adapter
from services.adapters.ec2 import (
    EC2Module,
    _co_instance_id,
    _coh_instance_id,
    _coh_is_renderable,
)
from services.ec2 import (
    _instance_pricing_os,
    _is_spot_instance,
    get_advanced_ec2_checks,
    get_enhanced_ec2_checks,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "platform, expected",
    [
        ("Linux/UNIX", "Linux"),
        ("Red Hat Enterprise Linux", "RHEL"),
        ("SUSE Linux", "SUSE"),
        ("Windows", "Windows"),
        ("Windows BYOL", "Windows"),
        ("Windows with SQL Server Standard", "Windows"),  # prefix fallback
        ("", "Linux"),  # absent -> conservative Linux
        ("Some Future Platform", "Linux"),
    ],
)
def test_instance_pricing_os(platform, expected):
    assert _instance_pricing_os({"PlatformDetails": platform}) == expected


def test_is_spot_instance():
    assert _is_spot_instance({"InstanceLifecycle": "spot"}) is True
    assert _is_spot_instance({"InstanceLifecycle": "scheduled"}) is False
    assert _is_spot_instance({}) is False


def test_coh_is_renderable():
    assert _coh_is_renderable({"resourceId": "i-1"}) is True
    assert _coh_is_renderable({"actionType": "PurchaseReservedInstances"}) is False
    assert _coh_is_renderable({"actionType": "Rightsize", "resourceId": "N/A"}) is False
    assert (
        _coh_is_renderable({"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}}})
        is False
    )


def test_co_and_coh_instance_id():
    assert _coh_instance_id({"resourceId": "i-abc"}) == "i-abc"
    assert _co_instance_id({"instanceArn": "arn:aws:ec2:us-east-1:1:instance/i-xyz"}) == "i-xyz"
    assert _co_instance_id({"instanceArn": "i-plain"}) == "i-plain"


# --------------------------------------------------------------------------- #
# Heuristic checks: cron/batch exclusivity & expanded prev-gen
# --------------------------------------------------------------------------- #
def _fake_ctx(instances: list[dict], hourly: float = 0.10):
    """Build a minimal ScanContext stand-in driving describe_instances."""
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Reservations": [{"Instances": instances}]}]
    ec2_client = MagicMock()
    ec2_client.get_paginator.return_value = paginator

    pricing_engine = MagicMock()
    pricing_engine.get_ec2_hourly_price.return_value = hourly

    return SimpleNamespace(
        region="us-east-1",
        fast_mode=True,  # skip CloudWatch / describe_volumes enrichment
        pricing_multiplier=1.0,
        pricing_engine=pricing_engine,
        client=lambda name, region=None: ec2_client,
        warn=MagicMock(),
        permission_issue=MagicMock(),
    )


def test_cron_and_batch_are_mutually_exclusive():
    """An instance named to match both patterns yields exactly one finding."""
    ctx = _fake_ctx(
        [
            {
                "InstanceId": "i-1",
                "InstanceType": "m5.large",
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "batch-job-runner"}],
            }
        ]
    )
    recs = get_advanced_ec2_checks(ctx, 1.0, fast_mode=True)["recommendations"]
    categories = [r["CheckCategory"] for r in recs]
    # "batch-job-runner" has no cron/scheduler token -> batch only, never both.
    assert categories == ["Batch Job Instances"]


def test_cron_token_wins_over_batch():
    ctx = _fake_ctx(
        [
            {
                "InstanceId": "i-2",
                "InstanceType": "m5.large",
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "cron-batch-host"}],
            }
        ]
    )
    recs = get_advanced_ec2_checks(ctx, 1.0, fast_mode=True)["recommendations"]
    assert [r["CheckCategory"] for r in recs] == ["Cron Job Instances"]


def test_previous_generation_covers_more_than_t2():
    """m4 (not just t2) is detected and mapped to a current-gen target."""
    ctx = _fake_ctx(
        [
            {
                "InstanceId": "i-3",
                "InstanceType": "m4.large",
                "State": {"Name": "running"},
                "PlatformDetails": "Linux/UNIX",
                "Tags": [],
            }
        ]
    )
    recs = get_enhanced_ec2_checks(ctx, 1.0, fast_mode=True)["recommendations"]
    prevgen = [r for r in recs if r["CheckCategory"] == "Previous Generation Migration"]
    assert len(prevgen) == 1
    assert "m6i.large" in prevgen[0]["EstimatedSavings"]
    # Transparency fields: OS + pricing basis recorded for audit.
    assert prevgen[0]["OS"] == "Linux"
    assert "on-demand" in prevgen[0]["PricingBasis"]
    assert "730h" in prevgen[0]["PricingBasis"]


def test_windows_pricing_basis_records_os():
    """A Windows instance records Windows in both the OS field and pricing basis."""
    ctx = _fake_ctx(
        [
            {
                "InstanceId": "i-win",
                "InstanceType": "m4.large",
                "State": {"Name": "running"},
                "PlatformDetails": "Windows",
                "Tags": [],
            }
        ],
        hourly=0.30,
    )
    recs = get_enhanced_ec2_checks(ctx, 1.0, fast_mode=True)["recommendations"]
    prevgen = [r for r in recs if r["CheckCategory"] == "Previous Generation Migration"][0]
    assert prevgen["OS"] == "Windows"
    assert "Windows" in prevgen["PricingBasis"]
    assert "$0.3000/hr" in prevgen["PricingBasis"]


def test_spot_instances_are_skipped():
    ctx = _fake_ctx(
        [
            {
                "InstanceId": "i-spot",
                "InstanceType": "m4.large",
                "State": {"Name": "running"},
                "InstanceLifecycle": "spot",
                "Tags": [],
            }
        ]
    )
    recs = get_enhanced_ec2_checks(ctx, 1.0, fast_mode=True)["recommendations"]
    assert recs == []


# --------------------------------------------------------------------------- #
# Cross-source de-duplication (the CRITICAL fix)
# --------------------------------------------------------------------------- #
def test_cross_source_dedup_counts_each_instance_once(monkeypatch):
    """Same instance in CoH + CO + heuristics is counted once; highest heuristic wins."""
    coh = [{"resourceId": "i-AAA", "estimatedMonthlySavings": 100.0}]
    co = [
        {
            "instanceArn": "arn:.../i-AAA",  # duplicate of CoH -> dropped
            "recommendationOptions": [
                {"rank": 1, "savingsOpportunity": {"estimatedMonthlySavings": {"value": 80.0}}}
            ],
        },
        {
            "instanceArn": "arn:.../i-BBB",  # unique -> kept (50)
            "recommendationOptions": [
                {"rank": 1, "savingsOpportunity": {"estimatedMonthlySavings": {"value": 50.0}}}
            ],
        },
    ]
    enhanced = [
        {"InstanceId": "i-AAA", "EstimatedSavings": "$90.00/month", "CheckCategory": "Idle Instances"},
        {"InstanceId": "i-CCC", "EstimatedSavings": "$30.00/month", "CheckCategory": "Rightsizing Opportunities"},
    ]
    advanced = [
        {"InstanceId": "i-CCC", "EstimatedSavings": "$40.00/month", "CheckCategory": "Cron Job Instances"},
        {"InstanceId": "i-DDD", "EstimatedSavings": "$20.00/month", "CheckCategory": "Batch Job Instances"},
    ]

    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: co)
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": enhanced})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", lambda *a, **k: {"recommendations": advanced})
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 4)

    ctx = SimpleNamespace(cost_hub_splits={"ec2": coh}, pricing_multiplier=1.0, fast_mode=False)
    findings = EC2Module().scan(ctx)

    # CoH 100 + CO i-BBB 50 + heuristic i-CCC max(30,40)=40 + i-DDD 20 = 210
    # (Naive sum without dedup would be 100+80+50+90+30+40+20 = 410.)
    assert findings.total_monthly_savings == pytest.approx(210.0)
    # i-AAA(coh) + i-BBB(co) + i-CCC + i-DDD = 4 unique opportunities
    assert findings.total_recommendations == 4
    # i-CCC kept as the advanced (cron, $40) finding, not the enhanced ($30) one
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.sources["advanced_ec2_checks"].count == 2


def test_compute_optimizer_optin_placeholder_not_counted(monkeypatch):
    """The $0 'enable Compute Optimizer' placeholder is dropped, not counted as a rec."""
    placeholder = {
        "ResourceId": "compute-optimizer-service",
        "Recommendation": "Enable AWS Compute Optimizer for EC2 rightsizing recommendations",
        "estimatedMonthlySavings": 0.0,
        "Service": "Compute Optimizer",
    }
    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: [placeholder])
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 0)

    warns = []
    ctx = SimpleNamespace(
        cost_hub_splits={"ec2": []},
        pricing_multiplier=1.0,
        fast_mode=False,
        warn=lambda message, service="": warns.append((service, message)),
    )
    findings = EC2Module().scan(ctx)

    assert findings.sources["compute_optimizer"].count == 0
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert any(svc == "ec2" and "Compute Optimizer is not enabled" in msg for svc, msg in warns)


# --------------------------------------------------------------------------- #
# DynamoDB Cost Optimization Hub wiring (previously dropped opportunities)
# --------------------------------------------------------------------------- #
def test_dynamodb_consumes_cost_hub_recs(monkeypatch):
    """DynamoDBTable CoH recs are now captured (deduped against per-table checks)."""
    import services.adapters.dynamodb as ddb_adapter

    monkeypatch.setattr(
        ddb_adapter, "get_dynamodb_table_analysis",
        lambda ctx: {"optimization_opportunities": [], "total_tables": 3},
    )
    monkeypatch.setattr(ddb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": []})
    monkeypatch.setattr(ddb_adapter, "get_dynamodb_optimization_descriptions", lambda: {})

    coh = [
        {"resourceId": "arn:aws:dynamodb:eu-west-1:1:table/Orders", "estimatedMonthlySavings": 42.0,
         "actionType": "Rightsize"},
        {"resourceId": "arn:aws:dynamodb:eu-west-1:1:table/Sessions", "estimatedMonthlySavings": 8.0,
         "actionType": "Rightsize"},
    ]
    ctx = SimpleNamespace(cost_hub_splits={"dynamodb": coh}, pricing_multiplier=1.0)
    findings = ddb_adapter.DynamoDbModule().scan(ctx)

    assert findings.sources["cost_optimization_hub"].count == 2
    assert findings.total_monthly_savings == pytest.approx(50.0)
    assert findings.total_recommendations == 2


def test_dynamodb_cost_hub_dedupes_against_own_checks(monkeypatch):
    """A table covered by the per-table analysis is not double-counted from CoH."""
    import services.adapters.dynamodb as ddb_adapter

    monkeypatch.setattr(
        ddb_adapter, "get_dynamodb_table_analysis",
        lambda ctx: {"optimization_opportunities": [{"TableName": "Orders", "ReadCapacityUnits": 0,
                                                      "WriteCapacityUnits": 0, "EstimatedMonthlyCost": 0}],
                     "total_tables": 1},
    )
    monkeypatch.setattr(ddb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": []})
    monkeypatch.setattr(ddb_adapter, "get_dynamodb_optimization_descriptions", lambda: {})

    coh = [{"resourceId": "arn:aws:dynamodb:eu-west-1:1:table/Orders", "estimatedMonthlySavings": 42.0,
            "actionType": "Rightsize"}]
    ctx = SimpleNamespace(cost_hub_splits={"dynamodb": coh}, pricing_multiplier=1.0)
    findings = ddb_adapter.DynamoDbModule().scan(ctx)

    # Orders already covered by table_analysis -> CoH rec dropped, not double counted.
    assert findings.sources["cost_optimization_hub"].count == 0


# --------------------------------------------------------------------------- #
# Trend-analysis permission failures are recorded (not silently printed)
# --------------------------------------------------------------------------- #
def test_trend_access_denied_is_recorded():
    from botocore.exceptions import ClientError

    from core.trend_analysis import analyze_spend_trends

    recorded = {}

    class _Ctx:
        def client(self, name, region=None):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetCostAndUsage"
            )

        def permission_issue(self, message, service, action=None):
            recorded["perm"] = (service, action)

        def warn(self, message, service=""):
            recorded["warn"] = service

    result = analyze_spend_trends(_Ctx())
    assert recorded.get("perm") == ("trend_analysis", "ce:GetCostAndUsage")
    assert result.total_spend == 0.0  # empty trend returned, scan continues

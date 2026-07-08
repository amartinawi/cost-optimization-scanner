"""Tests for active-commitment (Savings Plan / Reserved Instance) coverage.

Covers the coverage model, family matching, the pure demotion split, the CoH
resource-type extractor, the live fetch (with a fake ctx), and the end-to-end
adapter wiring that demotes commitment-covered rightsizing recs to advisory.

The scenario mirrors the live alyasra/eu-central-1 audit: EC2-Instance Savings
Plans locked to families {m4, m5, r5}, so CoH rightsizing/Graviton recs on those
families must demote to advisory while t2 (no SP) stays counted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.commitment_coverage import (
    CommitmentCoverage,
    coh_resource_type,
    demote_coh_by_commitment,
    demote_recs_in_place,
    fetch_commitment_coverage,
    instance_family,
    normalize_type,
    rec_gross,
    split_by_commitment,
)


# --------------------------------------------------------------------------- #
# instance_family
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("m5.xlarge", "m5"),
        ("m4.2xlarge", "m4"),
        ("t2.small", "t2"),
        ("m7i-flex.large", "m7i-flex"),
        ("db.r5.large", "r5"),
        ("cache.r6g.large", "r6g"),
        ("r6g.large.search", "r6g"),
        ("ra3.xlplus", "ra3"),
        ("", ""),
        ("  M5.XLARGE ", "m5"),
    ],
)
def test_instance_family(raw: str, expected: str) -> None:
    assert instance_family(raw) == expected


# --------------------------------------------------------------------------- #
# coverage predicates
# --------------------------------------------------------------------------- #
def _ec2_cov() -> CommitmentCoverage:
    return CommitmentCoverage(
        region="eu-central-1", ec2_sp_families=frozenset({"m4", "m5", "r5"})
    )


def test_covers_ec2_family_locked() -> None:
    cov = _ec2_cov()
    assert cov.covers_ec2("m4.2xlarge") is True
    assert cov.covers_ec2("r5.xlarge") is True
    assert cov.covers_ec2("t2.small") is False
    assert cov.covers_ec2("r6g.large") is False  # migration target, uncovered


def test_compute_sp_covers_every_family_and_lambda() -> None:
    cov = CommitmentCoverage(region="eu-central-1", has_compute_sp=True)
    assert cov.covers_ec2("r6g.large") is True
    assert cov.covers_ec2("t2.small") is True
    assert cov.covers_lambda() is True


def test_ec2_instance_sp_does_not_cover_lambda() -> None:
    assert _ec2_cov().covers_lambda() is False


def test_ri_predicates_by_family() -> None:
    cov = CommitmentCoverage(
        region="eu-central-1",
        rds_ri_families=frozenset({"r5"}),
        elasticache_ri_families=frozenset({"r6g"}),
        redshift_ri_types=frozenset({"ra3.xlplus"}),  # exact — not size-flexible
        opensearch_ri_types=frozenset({"r6g.large"}),  # exact — not size-flexible
    )
    assert cov.covers_rds("db.r5.4xlarge") is True  # size-flexible within family
    assert cov.covers_rds("db.m6g.large") is False
    assert cov.covers_elasticache("cache.r6g.large") is True
    # Redshift/OpenSearch are EXACT-type: the reserved type matches, a different
    # size in the same family does NOT.
    assert cov.covers_redshift("ra3.xlplus") is True
    assert cov.covers_redshift("ra3.4xlarge") is False
    assert cov.covers_opensearch("r6g.large.search") is True
    assert cov.covers_opensearch("r6g.xlarge.search") is False
    assert cov.covers("rds", "db.r5.large") is True
    assert cov.covers("unknown", "x") is False


def test_has_any_commitment() -> None:
    assert CommitmentCoverage().has_any_commitment is False
    assert _ec2_cov().has_any_commitment is True


# --------------------------------------------------------------------------- #
# coh_resource_type — EC2 and RDS nested shapes
# --------------------------------------------------------------------------- #
def test_coh_resource_type_ec2() -> None:
    rec = {"currentResourceDetails": {"ec2Instance": {"configuration": {"instance": {"type": "m4.2xlarge"}}}}}
    assert coh_resource_type(rec) == "m4.2xlarge"


def test_coh_resource_type_rds() -> None:
    rec = {"currentResourceDetails": {"rdsDbInstance": {"configuration": {"instance": {"dbInstanceClass": "db.r5.4xlarge"}}}}}
    assert coh_resource_type(rec) == "db.r5.4xlarge"


def test_coh_resource_type_missing() -> None:
    assert coh_resource_type({}) == ""
    assert coh_resource_type({"currentResourceDetails": {}}) == ""


# --------------------------------------------------------------------------- #
# split_by_commitment — the pure demotion split
# --------------------------------------------------------------------------- #
def test_split_by_commitment_partitions_and_annotates() -> None:
    recs = [
        {"id": "a", "gross": 100.0, "fam": "m5"},
        {"id": "b", "gross": 5.0, "fam": "t2"},
    ]
    counted, advisory = split_by_commitment(
        recs,
        is_covered=lambda r: r["fam"] == "m5",
        gross_of=lambda r: r["gross"],
        note_of=lambda r, g: f"covered {g}",
    )
    assert [r["id"] for r in counted] == ["b"]
    assert [r["id"] for r in advisory] == ["a"]
    adv = advisory[0]
    assert adv["Counted"] is False
    assert adv["AdvisoryEstimate"] == 100.0
    assert adv["CommitmentCoverageNote"] == "covered 100.0"


def test_split_by_commitment_is_immutable() -> None:
    original = {"id": "a", "gross": 100.0}
    split_by_commitment(
        [original],
        is_covered=lambda r: True,
        gross_of=lambda r: r["gross"],
        note_of=lambda r, g: "x",
    )
    assert "Counted" not in original  # original untouched


def test_split_by_commitment_empty_when_nothing_covered() -> None:
    recs = [{"id": "a"}]
    counted, advisory = split_by_commitment(
        recs, is_covered=lambda r: False, gross_of=lambda r: 0.0, note_of=lambda r, g: ""
    )
    assert counted == recs and advisory == []


# --------------------------------------------------------------------------- #
# demote_coh_by_commitment — data-store convenience wrapper
# --------------------------------------------------------------------------- #
def _rds_coh(cls: str, gross: float) -> dict:
    return {
        "currentResourceDetails": {"rdsDbInstance": {"configuration": {"instance": {"dbInstanceClass": cls}}}},
        "estimatedMonthlySavings": gross,
    }


def test_demote_coh_no_coverage_is_noop() -> None:
    recs = [_rds_coh("db.r5.large", 50.0)]
    counted, advisory = demote_coh_by_commitment(recs, None, "rds", lambda r: r["estimatedMonthlySavings"])
    assert counted == recs and advisory == []
    # Empty coverage object behaves the same.
    counted2, advisory2 = demote_coh_by_commitment(recs, CommitmentCoverage(), "rds", lambda r: 50.0)
    assert counted2 == recs and advisory2 == []


def test_demote_coh_splits_on_ri_family() -> None:
    recs = [_rds_coh("db.r5.4xlarge", 940.0), _rds_coh("db.m6g.large", 71.0)]
    cov = CommitmentCoverage(region="ap-south-1", rds_ri_families=frozenset({"r5"}))
    counted, advisory = demote_coh_by_commitment(recs, cov, "rds", lambda r: r["estimatedMonthlySavings"])
    assert len(counted) == 1 and counted[0]["estimatedMonthlySavings"] == 71.0
    assert len(advisory) == 1 and advisory[0]["Counted"] is False
    assert advisory[0]["AdvisoryEstimate"] == 940.0


# --------------------------------------------------------------------------- #
# fetch_commitment_coverage — live fetch with a fake ctx
# --------------------------------------------------------------------------- #
def _fetch_ctx(savings_plans: list[dict], region: str = "eu-central-1", *, ri: dict | None = None):
    sp_client = MagicMock()
    sp_client.describe_savings_plans.return_value = {"savingsPlans": savings_plans}
    ce_client = MagicMock()
    ce_client.get_savings_plans_utilization.return_value = {
        "Total": {"Utilization": {"UtilizationPercentage": "92.1", "UnusedCommitment": "158.8"}}
    }
    clients = {"savingsplans": sp_client, "ce": ce_client}
    ri = ri or {}
    warn = MagicMock()
    perm = MagicMock()
    return SimpleNamespace(
        region=region,
        client=lambda name, region=None: clients.get(name, MagicMock()),
        warn=warn,
        permission_issue=perm,
    )


def test_fetch_matches_region_locked_ec2_sp_families() -> None:
    ctx = _fetch_ctx(
        [
            {"savingsPlanType": "EC2Instance", "ec2InstanceFamily": "m4", "region": "eu-central-1"},
            {"savingsPlanType": "EC2Instance", "ec2InstanceFamily": "r5", "region": "eu-central-1"},
            # Different region → excluded.
            {"savingsPlanType": "EC2Instance", "ec2InstanceFamily": "c5", "region": "us-east-1"},
        ]
    )
    cov = fetch_commitment_coverage(ctx, {"ec2"})
    assert cov.ec2_sp_families == frozenset({"m4", "r5"})
    assert cov.has_compute_sp is False
    assert cov.sp_utilization_pct == pytest.approx(92.1)
    assert cov.sp_unused_monthly == pytest.approx(158.8)


def test_fetch_detects_compute_sp() -> None:
    ctx = _fetch_ctx([{"savingsPlanType": "Compute", "region": None}])
    cov = fetch_commitment_coverage(ctx, {"ec2", "lambda"})
    assert cov.has_compute_sp is True


def test_fetch_skips_sp_when_no_compute_service_selected() -> None:
    ctx = _fetch_ctx([{"savingsPlanType": "EC2Instance", "ec2InstanceFamily": "m4", "region": "eu-central-1"}])
    cov = fetch_commitment_coverage(ctx, {"s3"})
    assert cov.ec2_sp_families == frozenset()


def test_fetch_access_denied_is_permission_issue_not_crash() -> None:
    from botocore.exceptions import ClientError

    sp_client = MagicMock()
    sp_client.describe_savings_plans.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "DescribeSavingsPlans"
    )
    perm = MagicMock()
    ctx = SimpleNamespace(
        region="eu-central-1",
        client=lambda name, region=None: sp_client,
        warn=MagicMock(),
        permission_issue=perm,
    )
    cov = fetch_commitment_coverage(ctx, {"ec2"})
    assert cov.ec2_sp_families == frozenset()  # fail-safe: nothing covered
    perm.assert_called_once()


def test_fetch_rds_reserved_instances() -> None:
    rds_client = MagicMock()
    rds_client.get_paginator.return_value.paginate.return_value = [
        {"ReservedDBInstances": [{"State": "active", "DBInstanceClass": "db.r5.large"}]}
    ]
    ctx = SimpleNamespace(
        region="ap-south-1",
        client=lambda name, region=None: rds_client,
        warn=MagicMock(),
        permission_issue=MagicMock(),
    )
    cov = fetch_commitment_coverage(ctx, {"rds"})
    assert cov.rds_ri_families == frozenset({"r5"})


# --------------------------------------------------------------------------- #
# EC2 adapter integration — the live alyasra scenario in miniature
# --------------------------------------------------------------------------- #
def _ec2_coh(iid: str, itype: str, gross: float) -> dict:
    return {
        "resourceId": iid,
        "actionType": "MigrateToGraviton",
        "estimatedMonthlySavings": gross,
        "currentResourceDetails": {"ec2Instance": {"configuration": {"instance": {"type": itype}}}},
    }


def test_ec2_adapter_demotes_sp_covered_families(monkeypatch) -> None:
    import services.adapters.ec2 as ec2_adapter
    from services.adapters.ec2 import EC2Module

    # m4/m5/r5 covered by EC2-Instance SPs; t2 uncovered (mirrors alyasra).
    coh = [
        _ec2_coh("i-m4", "m4.2xlarge", 324.70),
        _ec2_coh("i-m5", "m5.xlarge", 211.99),
        _ec2_coh("i-r5", "r5.xlarge", 33.29),
        _ec2_coh("i-t2a", "t2.small", 5.55),
        _ec2_coh("i-t2b", "t2.micro", 2.77),
    ]
    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 5)

    ctx = SimpleNamespace(
        cost_hub_splits={"ec2": coh},
        commitment_coverage=_ec2_cov(),
        pricing_multiplier=1.0,
        fast_mode=False,
        client=lambda name, region=None: None,
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )
    findings = EC2Module().scan(ctx)

    # Only the two t2 recs (no SP family) stay counted.
    assert findings.total_monthly_savings == pytest.approx(5.55 + 2.77)
    assert findings.total_recommendations == 2

    recs = findings.sources["cost_optimization_hub"].recommendations
    counted = [r for r in recs if r.get("Counted") is not False]
    advisory = [r for r in recs if r.get("Counted") is False]
    assert {r["resourceId"] for r in counted} == {"i-t2a", "i-t2b"}
    assert {r["resourceId"] for r in advisory} == {"i-m4", "i-m5", "i-r5"}
    # Demoted recs keep an indicative gross + explanatory note.
    m4 = next(r for r in advisory if r["resourceId"] == "i-m4")
    assert m4["AdvisoryEstimate"] == pytest.approx(324.70)
    assert "Savings Plan" in m4["CommitmentCoverageNote"]


def test_ec2_adapter_no_coverage_counts_all(monkeypatch) -> None:
    import services.adapters.ec2 as ec2_adapter
    from services.adapters.ec2 import EC2Module

    coh = [_ec2_coh("i-m4", "m4.2xlarge", 324.70), _ec2_coh("i-t2", "t2.small", 5.55)]
    monkeypatch.setattr(ec2_adapter, "get_ec2_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_asg_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(ec2_adapter, "get_enhanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_advanced_ec2_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(ec2_adapter, "get_ec2_instance_count", lambda ctx: 2)

    ctx = SimpleNamespace(
        cost_hub_splits={"ec2": coh},
        commitment_coverage=None,  # no prefetch → count as before
        pricing_multiplier=1.0,
        fast_mode=False,
        client=lambda name, region=None: None,
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )
    findings = EC2Module().scan(ctx)
    assert findings.total_monthly_savings == pytest.approx(324.70 + 5.55)
    assert findings.total_recommendations == 2


# --------------------------------------------------------------------------- #
# Redshift adapter integration — RI demotion keeps heuristic suppression
# --------------------------------------------------------------------------- #
def test_redshift_adapter_demotes_ri_covered_cluster(monkeypatch) -> None:
    import services.adapters.redshift as rs_adapter
    from services.adapters.redshift import RedshiftModule

    coh = [
        {
            "ClusterIdentifier": "cl-ra3",
            "actionType": "Rightsize",
            "estimatedMonthlySavings": 500.0,
            "currentResourceType": "RedshiftCluster",
            "currentResourceDetails": {"redshiftCluster": {"configuration": {"instance": {"nodeType": "ra3.xlplus"}}}},
        }
    ]
    monkeypatch.setattr(rs_adapter, "get_enhanced_redshift_checks", lambda ctx: {"recommendations": []})

    ctx = SimpleNamespace(
        cost_hub_splits={"redshift": coh},
        commitment_coverage=CommitmentCoverage(region="eu-central-1", redshift_ri_types=frozenset({"ra3.xlplus"})),
        region="eu-central-1",
        pricing_multiplier=1.0,
        fast_mode=False,
        client=lambda name, region=None: None,
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )
    findings = RedshiftModule().scan(ctx)
    assert findings.total_monthly_savings == pytest.approx(0.0)  # demoted
    recs = findings.sources["cost_optimization_hub"].recommendations
    assert all(r.get("Counted") is False for r in recs)
    assert recs[0]["AdvisoryEstimate"] == pytest.approx(500.0)


# --------------------------------------------------------------------------- #
# normalize_type / exact-type matching
# --------------------------------------------------------------------------- #
def test_normalize_type() -> None:
    assert normalize_type("db.r5.large") == "r5.large"
    assert normalize_type("cache.r6g.large") == "r6g.large"
    assert normalize_type("r6g.large.search") == "r6g.large"
    assert normalize_type("ra3.xlplus") == "ra3.xlplus"


# --------------------------------------------------------------------------- #
# EC2 classic Reserved Instances (regional family + zonal exact type)
# --------------------------------------------------------------------------- #
def test_covers_ec2_classic_ri() -> None:
    cov = CommitmentCoverage(
        region="eu-central-1",
        ec2_ri_families=frozenset({"c5"}),       # regional RI -> family
        ec2_ri_types=frozenset({"m5.xlarge"}),   # zonal RI -> exact type
    )
    assert cov.covers_ec2("c5.9xlarge") is True   # family-flex
    assert cov.covers_ec2("m5.xlarge") is True     # exact zonal
    assert cov.covers_ec2("m5.2xlarge") is False   # different size, zonal is exact
    assert cov.covers_ec2("t3.large") is False


# --------------------------------------------------------------------------- #
# SageMaker SP / DynamoDB reserved / Compute-SP-for-containers predicates
# --------------------------------------------------------------------------- #
def test_sagemaker_and_dynamodb_and_containers_predicates() -> None:
    sm = CommitmentCoverage(region="x", has_sagemaker_sp=True)
    assert sm.covers_sagemaker() is True
    assert sm.covers_containers() is False   # SageMaker SP != Compute SP
    compute = CommitmentCoverage(region="x", has_compute_sp=True)
    assert compute.covers_containers() is True
    assert compute.covers_lambda() is True
    assert compute.covers_sagemaker() is False   # Compute SP does NOT cover SageMaker
    ddb = CommitmentCoverage(region="x", dynamodb_reserved=True)
    assert ddb.covers_dynamodb() is True
    assert ddb.has_any_commitment is True


# --------------------------------------------------------------------------- #
# CE headroom cap — realizable savings survive up to uncovered on-demand
# --------------------------------------------------------------------------- #
def test_headroom_cap_counts_up_to_uncovered_then_demotes() -> None:
    # Family r5 has $100/mo uncovered on-demand; three r5 recs total $160.
    recs = [{"g": 80, "fam": "r5"}, {"g": 50, "fam": "r5"}, {"g": 30, "fam": "r5"}]
    counted, advisory = split_by_commitment(
        recs,
        is_covered=lambda r: True,
        gross_of=lambda r: r["g"],
        note_of=lambda r, g: "x",
        ceiling_of=lambda r: 100.0,
        key_of=lambda r: r["fam"],
    )
    # Greedy highest-first: 80 fits (budget->20), 50 & 30 exceed remaining -> demoted.
    assert sum(r["g"] for r in counted) <= 100.0
    assert {r["g"] for r in counted} == {80}
    assert len(advisory) == 2


def test_headroom_cap_independent_budgets_per_family() -> None:
    # Two families each with an equal $100 ceiling must NOT share a budget.
    recs = [{"g": 90, "fam": "r5"}, {"g": 90, "fam": "m5"}]
    counted, advisory = split_by_commitment(
        recs,
        is_covered=lambda r: True,
        gross_of=lambda r: r["g"],
        note_of=lambda r, g: "x",
        ceiling_of=lambda r: 100.0,
        key_of=lambda r: r["fam"],
    )
    assert len(counted) == 2 and not advisory  # each family's 90 fits its own 100


def test_headroom_cap_zero_ceiling_demotes_all() -> None:
    recs = [{"g": 10, "fam": "r5"}]
    counted, advisory = split_by_commitment(
        recs, is_covered=lambda r: True, gross_of=lambda r: r["g"],
        note_of=lambda r, g: "x", ceiling_of=lambda r: 0.0, key_of=lambda r: r["fam"],
    )
    assert not counted and len(advisory) == 1


# --------------------------------------------------------------------------- #
# rec_gross + demote_recs_in_place (whole-service gate)
# --------------------------------------------------------------------------- #
def test_rec_gross_variants() -> None:
    assert rec_gross({"EstimatedMonthlySavings": 12.5}) == 12.5
    assert rec_gross({"estimatedMonthlySavings": 8.0}) == 8.0
    assert rec_gross({"EstimatedSavings": "$3.40/month"}) == pytest.approx(3.40)
    assert rec_gross({}) == 0.0


def test_demote_recs_in_place_respects_only_filter() -> None:
    recs = [
        {"EstimatedMonthlySavings": 100.0, "LaunchType": "FARGATE", "Counted": True},
        {"EstimatedMonthlySavings": 40.0, "CheckCategory": "ECR Storage", "Counted": True},
    ]
    removed = demote_recs_in_place(
        recs, lambda g: "compute-covered", only=lambda r: str(r.get("LaunchType", "")).upper() == "FARGATE"
    )
    assert removed == pytest.approx(100.0)
    assert recs[0]["Counted"] is False and recs[0]["AdvisoryEstimate"] == 100.0
    assert recs[1]["Counted"] is True  # ECR storage NOT demoted (not Compute-SP-covered)


# --------------------------------------------------------------------------- #
# Fetch: SageMaker SP, EC2 classic RI, exact-type OS/Redshift, uncovered cap
# --------------------------------------------------------------------------- #
def test_fetch_sagemaker_sp_and_ec2_ri(monkeypatch) -> None:
    sp = MagicMock()
    sp.describe_savings_plans.return_value = {
        "savingsPlans": [{"savingsPlanType": "SageMaker", "region": None}]
    }
    ec2 = MagicMock()
    ec2.describe_reserved_instances.return_value = {
        "ReservedInstances": [
            {"InstanceType": "c5.9xlarge", "Scope": "Region", "State": "active"},
            {"InstanceType": "m5.large", "Scope": "Availability Zone", "State": "active"},
        ]
    }
    ce = MagicMock()
    ce.get_savings_plans_utilization.return_value = {"Total": {"Utilization": {}}}
    ce.get_savings_plans_coverage.return_value = {"SavingsPlansCoverages": []}
    ce.get_reservation_coverage.return_value = {"CoveragesByTime": [{"Groups": []}]}
    clients = {"savingsplans": sp, "ec2": ec2, "ce": ce}
    ctx = SimpleNamespace(
        region="eu-central-1", client=lambda n, region=None: clients.get(n, MagicMock()),
        warn=MagicMock(), permission_issue=MagicMock(),
    )
    cov = fetch_commitment_coverage(ctx, {"ec2", "sagemaker"})
    assert cov.has_sagemaker_sp is True
    assert cov.ec2_ri_families == frozenset({"c5"})
    assert cov.ec2_ri_types == frozenset({"m5.large"})


def test_fetch_reservation_coverage_ceiling(monkeypatch) -> None:
    rds = MagicMock()
    rds.get_paginator.return_value.paginate.return_value = [
        {"ReservedDBInstances": [{"State": "active", "DBInstanceClass": "db.r5.large"}]}
    ]
    ce = MagicMock()
    ce.get_reservation_coverage.return_value = {
        "CoveragesByTime": [
            {"Groups": [{"Attributes": {"instanceTypeFamily": "r5"},
                         "Coverage": {"CoverageCost": {"OnDemandCost": "123.45"}}}]}
        ]
    }
    clients = {"rds": rds, "ce": ce}
    ctx = SimpleNamespace(
        region="ap-south-1", client=lambda n, region=None: clients.get(n, MagicMock()),
        warn=MagicMock(), permission_issue=MagicMock(),
    )
    cov = fetch_commitment_coverage(ctx, {"rds"})
    assert cov.rds_ri_families == frozenset({"r5"})
    assert cov.realizable_ceiling("rds", "db.r5.large") == pytest.approx(123.45)


# --------------------------------------------------------------------------- #
# Adapter integration: DynamoDB reserved capacity demotes counted recs
# --------------------------------------------------------------------------- #
def test_dynamodb_reserved_gate_demotes_counted_recs() -> None:
    # Mirrors the dynamodb adapter's whole-service gate: under reserved capacity,
    # every counted table/CoH saving is demoted (the reservation bills regardless).
    cov = CommitmentCoverage(region="x", dynamodb_reserved=True)
    opt = [{"TableName": "t1", "EstimatedMonthlySavings": 50.0, "Counted": True}]
    coh = [{"TableName": "t2", "estimatedMonthlySavings": 30.0, "Counted": True}]
    removed = 0.0
    if cov.covers_dynamodb():
        removed = demote_recs_in_place(opt + coh, lambda g: cov.plan_note("DynamoDB reserved capacity", g))
    assert removed == pytest.approx(80.0)
    assert all(r["Counted"] is False for r in opt + coh)
    assert "DynamoDB reserved capacity" in opt[0]["CommitmentCoverageNote"]

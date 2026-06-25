"""Tests for the report-audit remediation fixes across multiple adapters."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services._savings import mark_zero_savings_advisory


# --------------------------------------------------------------------------- #
# Shared helper
# --------------------------------------------------------------------------- #
def test_mark_zero_savings_advisory():
    recs = [{"x": 1, "s": 5.0}, {"x": 2, "s": 0.0}, {"x": 3, "s": 0.0, "Counted": False}]
    mark_zero_savings_advisory(recs, lambda r: r.get("s", 0))
    assert recs[0].get("Counted", True) is True
    assert recs[1]["Counted"] is False
    assert recs[2]["Counted"] is False


# --------------------------------------------------------------------------- #
# Lambda CO $0 filter (advisor)
# --------------------------------------------------------------------------- #
def test_lambda_co_helper_filters_zero():
    import services.advisor as advisor

    raw = {"lambdaFunctionRecommendations": [
        {"functionArn": "arn:aws:lambda:::function:keep",
         "memorySizeRecommendationOptions": [{"memorySize": 512, "savingsOpportunity": {"estimatedMonthlySavings": {"value": 9.0}}}]},
        {"functionArn": "arn:aws:lambda:::function:optimized",
         "memorySizeRecommendationOptions": [{"memorySize": 1024, "savingsOpportunity": {"estimatedMonthlySavings": {"value": 0.0}}}]},
    ]}
    co = MagicMock()
    co.get_lambda_function_recommendations.return_value = raw
    ctx = SimpleNamespace(pricing_multiplier=1.0)
    ctx.client = lambda n, region=None: co if n == "compute-optimizer" else None
    out = advisor.get_lambda_compute_optimizer_recommendations(ctx)
    assert [r["resource_name"] for r in out] == ["keep"]


# --------------------------------------------------------------------------- #
# ElastiCache double-count + Reserved advisory
# --------------------------------------------------------------------------- #
def test_elasticache_one_counted_lever_per_cluster():
    import services.adapters.elasticache as ec_mod
    from services.adapters.elasticache import ElasticacheModule

    recs = [
        {"ClusterId": "c1", "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Valkey Migration", "EstimatedSavings": "Valkey..."},
        {"ClusterId": "c1", "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Underutilized Cluster", "EstimatedSavings": "30-50%"},
        {"ClusterId": "c1", "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Reserved Nodes Opportunity", "EstimatedSavings": "30-60%"},
    ]
    pe = MagicMock(); pe.get_instance_monthly_price.return_value = 267.47
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ec_mod.get_enhanced_elasticache_checks = lambda c: {"recommendations": recs}
    f = ElasticacheModule().scan(ctx)
    counted = [r for r in f.sources["enhanced_checks"].recommendations if r.get("Counted") is True]
    # Only ONE lever counted on the single cluster (the best non-commitment one).
    assert len(counted) == 1
    # Reserved is advisory; total = one lever (Underutilized 0.30 > Valkey 0.20).
    assert counted[0]["CheckCategory"] == "Underutilized Cluster"
    assert f.total_monthly_savings == round(267.47 * 0.30, 2)
    reserved = [r for r in f.sources["enhanced_checks"].recommendations if "Reserved" in r["CheckCategory"]][0]
    assert reserved["Counted"] is False


# --------------------------------------------------------------------------- #
# OpenSearch Reserved advisory + storage separate axis
# --------------------------------------------------------------------------- #
def test_opensearch_reserved_advisory_storage_counted():
    import services.adapters.opensearch as os_mod
    from services.adapters.opensearch import OpensearchModule

    recs = [
        {"DomainName": "d1", "InstanceType": "r6g.large.search", "InstanceCount": 2, "CheckCategory": "Graviton Migration", "EstimatedSavings": "Graviton"},
        {"DomainName": "d1", "InstanceType": None, "CheckCategory": "Underutilized Domain", "EstimatedSavings": "30-50%"},
        {"DomainName": "d1", "EBSVolumeSize": 150, "CheckCategory": "Storage Optimization", "EstimatedSavings": "20%"},
        {"DomainName": "d1", "InstanceType": "r6g.large.search", "InstanceCount": 2, "CheckCategory": "Reserved Instances Opportunity", "EstimatedSavings": "30-60%"},
    ]
    pe = MagicMock(); pe.get_instance_monthly_price.return_value = 100.0
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    os_mod.get_enhanced_opensearch_checks = lambda c: {"recommendations": recs}
    f = OpensearchModule().scan(ctx)
    rs = {r["CheckCategory"]: r for r in f.sources["enhanced_checks"].recommendations}
    assert rs["Reserved Instances Opportunity"]["Counted"] is False  # commitment lever
    assert rs["Underutilized Domain"]["Counted"] is False            # $0 (no InstanceType)
    assert rs["Graviton Migration"]["Counted"] is True               # instance lever
    assert rs["Storage Optimization"]["Counted"] is True             # separate axis
    # total = graviton (100*2*0.25=50) + storage (150*0.11*0.20=3.30) = 53.30
    assert f.total_monthly_savings == round(100.0 * 2 * 0.25 + 150 * 0.11 * 0.20, 2)

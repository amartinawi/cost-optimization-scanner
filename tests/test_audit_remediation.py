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
        {"ClusterId": "c1", "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Underutilized Cluster", "EstimatedSavings": "30-50%",
         # Downsizing is gated on memory headroom as well as CPU; this test covers
         # the one-counted-lever-per-cluster dedup, so grant the headroom.
         "MemoryHeadroomOk": True, "PeakMemoryUsagePct": 5.0, "Evictions": 0.0},
        {"ClusterId": "c1", "NodeType": "cache.r6g.xlarge", "NumNodes": 1, "CheckCategory": "Reserved Nodes Opportunity", "EstimatedSavings": "30-60%"},
    ]
    # Underutilized (H3) is the live current->one-size-down delta:
    # cache.r6g.xlarge $200 -> cache.r6g.large $100 = $100; Valkey is $200*0.20=$40.
    prices = {"cache.r6g.xlarge": 200.0, "cache.r6g.large": 100.0}
    pe = MagicMock()
    pe.get_instance_monthly_price.side_effect = lambda svc, itype, *, engine=None: prices.get(itype, 0.0)
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    ec_mod.get_enhanced_elasticache_checks = lambda c: {"recommendations": recs}
    f = ElasticacheModule().scan(ctx)
    counted = [r for r in f.sources["enhanced_checks"].recommendations if r.get("Counted") is True]
    # Only ONE lever counted on the single cluster (the best non-commitment one).
    assert len(counted) == 1
    # Reserved is advisory; total = one lever (Underutilized $100 > Valkey $40).
    assert counted[0]["CheckCategory"] == "Underutilized Cluster"
    assert f.total_monthly_savings == round(200.0 - 100.0, 2)
    reserved = [r for r in f.sources["enhanced_checks"].recommendations if "Reserved" in r["CheckCategory"]][0]
    assert reserved["Counted"] is False


# --------------------------------------------------------------------------- #
# OpenSearch Reserved advisory + storage separate axis
# --------------------------------------------------------------------------- #
def test_opensearch_reserved_advisory_storage_counted():
    import services.adapters.opensearch as os_mod
    from services.adapters.opensearch import OpensearchModule

    recs = [
        {"DomainName": "d1", "InstanceType": "r5.large.search", "InstanceCount": 2, "CheckCategory": "Graviton Migration", "EstimatedSavings": "Graviton"},
        {"DomainName": "d1", "InstanceType": None, "CheckCategory": "Underutilized Domain", "EstimatedSavings": "30-50%"},
        {"DomainName": "d1", "EBSVolumeSize": 150, "CheckCategory": "Storage Optimization", "EstimatedSavings": "20%"},
        {"DomainName": "d1", "InstanceType": "r5.large.search", "InstanceCount": 2, "CheckCategory": "Reserved Instances Opportunity", "EstimatedSavings": "30-60%"},
    ]
    # Graviton (H4) is the live x86->Graviton node delta:
    # r5.large.search $100 -> r6g.large.search $90 = $10/node x 2 = $20.
    prices = {"r5.large.search": 100.0, "r6g.large.search": 90.0}
    pe = MagicMock()
    pe.get_instance_monthly_price.side_effect = lambda svc, itype, *, engine=None: prices.get(itype, 0.0)
    ctx = SimpleNamespace(pricing_engine=pe, pricing_multiplier=1.0)
    os_mod.get_enhanced_opensearch_checks = lambda c: {"recommendations": recs}
    f = OpensearchModule().scan(ctx)
    rs = {r["CheckCategory"]: r for r in f.sources["enhanced_checks"].recommendations}
    assert rs["Reserved Instances Opportunity"]["Counted"] is False  # commitment lever
    assert rs["Underutilized Domain"]["Counted"] is False            # $0 (no InstanceType)
    assert rs["Graviton Migration"]["Counted"] is True               # instance lever
    assert rs["Storage Optimization"]["Counted"] is True             # separate axis
    # total = graviton node delta ((100-90)*2=20) + storage gp2->gp3 delta
    # (150 * (0.135-0.122) = 1.95) = 21.95. Graviton now uses the real x86->Graviton
    # node-price delta, not the old flat 0.25 proxy (live-audit H4).
    assert f.total_monthly_savings == round((100.0 - 90.0) * 2 + 150 * (0.135 - 0.122), 2)

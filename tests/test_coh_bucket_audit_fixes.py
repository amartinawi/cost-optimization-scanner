"""Tests for SR-3 — orphaned Cost Optimization Hub buckets now consumed.

The orchestrator buckets CoH recommendations into ``ctx.cost_hub_splits[<svc>]``
for ElastiCache / OpenSearch / Redshift, but no adapter consumed them — the
highest-authority, account-specific AWS savings dropped silently. These tests
drive each adapter's ``scan()`` with a fake ctx + monkeypatched enhanced-checks
helpers and assert: a CoH rec for a resource suppresses that resource's
heuristic lever (counted once, not double-counted), RI/SP purchase recs are
filtered out, and an empty bucket changes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.elasticache as elasticache_adapter
import services.adapters.opensearch as opensearch_adapter
import services.adapters.redshift as redshift_adapter


class _FakePricing:
    """Returns a fixed monthly node price; records the engine kwarg (ElastiCache)."""

    def __init__(self, monthly: float = 100.0):
        self._monthly = monthly

    def get_instance_monthly_price(self, service_code, instance_type, *, engine=None):
        return self._monthly


def _ctx(svc: str, coh_recs, pricing_monthly: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        pricing_engine=_FakePricing(pricing_monthly),
        pricing_multiplier=1.0,
        region="us-east-1",
        account_id="123456789012",
        fast_mode=False,
        cost_hub_splits={svc: coh_recs},
        warnings=[],
        permission_issues=[],
        client=lambda name, region=None: None,
        warn=lambda message, service=None: None,
        permission_issue=lambda message, service=None, action=None: None,
    )


def _coh_rec(resource_id: str, savings: float, *, action="Rightsize", rtype="cluster") -> dict:
    return {
        "resourceId": resource_id,
        "resourceArn": f"arn:aws:fake:us-east-1:1:{rtype}:{resource_id}",
        "currentResourceType": "FakeCluster",
        "actionType": action,
        "estimatedMonthlySavings": savings,
        "recommendationId": f"coh-{resource_id}",
    }


# --------------------------------------------------------------------------- #
# ElastiCache (SR-3 / ElastiCache C1)
# --------------------------------------------------------------------------- #
def test_elasticache_coh_consumed_and_heuristic_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CoH rec for cluster 'prod' suppresses the heuristic Graviton lever on
    the same cluster; CoH dollars are counted once, not stacked."""
    heuristic_recs = [
        {
            "ClusterId": "prod",
            "Engine": "Redis",
            "NodeType": "cache.r6g.large",
            "NumNodes": 2,
            "CheckCategory": "Graviton Migration",
            "EstimatedSavings": "20-40%",
        }
    ]
    monkeypatch.setattr(
        elasticache_adapter,
        "get_enhanced_elasticache_checks",
        lambda ctx: {"recommendations": list(heuristic_recs)},
    )
    coh = [_coh_rec("prod", 75.0)]
    findings = elasticache_adapter.ElasticacheModule().scan(_ctx("elasticache", coh))

    # CoH source emitted with the one rec.
    assert "cost_optimization_hub" in findings.sources
    assert findings.sources["cost_optimization_hub"].count == 1
    # Heuristic lever demoted (Counted=False) — not double-counted.
    heur = findings.sources["enhanced_checks"].recommendations[0]
    assert heur["Counted"] is False
    # Total = CoH only (75.0), not 75.0 + heuristic.
    assert findings.total_monthly_savings == pytest.approx(75.0)
    assert findings.total_recommendations == 2  # 1 heuristic + 1 CoH


def test_elasticache_coh_ri_rec_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """RI purchase recs are routed to commitment_analysis; they must not render
    or count in the ElastiCache tab."""
    monkeypatch.setattr(
        elasticache_adapter,
        "get_enhanced_elasticache_checks",
        lambda ctx: {"recommendations": []},
    )
    coh = [_coh_rec("prod", 50.0, action="PurchaseReservedInstances")]
    findings = elasticache_adapter.ElasticacheModule().scan(_ctx("elasticache", coh))

    assert "cost_optimization_hub" not in findings.sources
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# OpenSearch (SR-3 / OpenSearch C1)
# --------------------------------------------------------------------------- #
def test_opensearch_coh_consumed_and_heuristic_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CoH rec for domain 'logs' suppresses the heuristic Graviton lever on
    the same domain; CoH dollars counted once."""
    heuristic_recs = [
        {
            "DomainName": "logs",
            "InstanceType": "r6g.large.search",
            "InstanceCount": 2,
            "CheckCategory": "Graviton Migration",
        }
    ]
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: {"recommendations": list(heuristic_recs)},
    )
    coh = [_coh_rec("logs", 60.0, rtype="domain")]
    findings = opensearch_adapter.OpensearchModule().scan(_ctx("opensearch", coh))

    assert "cost_optimization_hub" in findings.sources
    heur = findings.sources["enhanced_checks"].recommendations[0]
    assert heur["Counted"] is False
    assert findings.total_monthly_savings == pytest.approx(60.0)


def test_opensearch_empty_bucket_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CoH recs → no cost_optimization_hub source; heuristic path unchanged."""
    monkeypatch.setattr(
        opensearch_adapter,
        "get_enhanced_opensearch_checks",
        lambda ctx: {"recommendations": []},
    )
    findings = opensearch_adapter.OpensearchModule().scan(_ctx("opensearch", []))
    assert "cost_optimization_hub" not in findings.sources
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# Redshift (SR-3 / Redshift C1)
# --------------------------------------------------------------------------- #
def test_redshift_coh_consumed_and_heuristic_demoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CoH rec for cluster 'warehouse' suppresses the heuristic rightsizing
    lever on the same cluster; CoH dollars counted once."""
    heuristic_recs = [
        {
            "ClusterIdentifier": "warehouse",
            "NodeType": "ra3.xlplus",
            "NumberOfNodes": 4,
            "CheckCategory": "Cluster Rightsizing",
        }
    ]
    monkeypatch.setattr(
        redshift_adapter,
        "get_enhanced_redshift_checks",
        lambda ctx: {"recommendations": list(heuristic_recs)},
    )
    coh = [_coh_rec("warehouse", 200.0)]
    findings = redshift_adapter.RedshiftModule().scan(_ctx("redshift", coh))

    assert "cost_optimization_hub" in findings.sources
    heur = findings.sources["enhanced_checks"].recommendations[0]
    assert heur["Counted"] is False
    # Total = CoH only (200.0), the heuristic lever was skipped.
    assert findings.total_monthly_savings == pytest.approx(200.0)


def test_redshift_coh_na_resource_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """CoH recs with resourceId 'N/A' carry no concrete cluster and are dropped."""
    monkeypatch.setattr(
        redshift_adapter,
        "get_enhanced_redshift_checks",
        lambda ctx: {"recommendations": []},
    )
    coh = [
        _coh_rec("N/A", 999.0),
        _coh_rec("real-cluster", 50.0),
    ]
    findings = redshift_adapter.RedshiftModule().scan(_ctx("redshift", coh))
    # Only the real cluster counted.
    assert findings.sources["cost_optimization_hub"].count == 1
    assert findings.total_monthly_savings == pytest.approx(50.0)

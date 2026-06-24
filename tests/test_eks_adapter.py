"""Tests for the EKS cost adapter audit-remediation fixes.

Covers Extended Support surcharge pricing (evidence-gated), idle/empty cluster
control-plane savings, node-group and Fargate findings being advisory
(Counted=False, EC2-domain), pricing-API-driven rates, and Cost-Hub bucket
consumption under the corrected 'eks_cost' key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.eks import EksCostModule


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Paginator:
    def __init__(self, key, items):
        self._key, self._items = key, items

    def paginate(self, **kwargs):
        return [{self._key: self._items}]


class _FakeEks:
    """Minimal EKS client driven by a per-cluster config dict."""

    def __init__(self, clusters):
        self._clusters = clusters  # name -> {cluster:..., nodegroups:[], fargate:[], addons:[]}

    def get_paginator(self, op):
        if op == "list_clusters":
            return _Paginator("clusters", list(self._clusters))
        if op == "list_nodegroups":
            return _NgPaginator(self._clusters)
        if op == "list_fargate_profiles":
            return _FpPaginator(self._clusters)
        raise AssertionError(op)

    def describe_cluster(self, name):
        return {"cluster": self._clusters[name]["cluster"]}

    def describe_nodegroup(self, clusterName, nodegroupName):
        return {"nodegroup": self._clusters[clusterName]["nodegroups"][nodegroupName]}

    def list_addons(self, clusterName):
        return {"addons": self._clusters[clusterName].get("addons", [])}


class _NgPaginator:
    def __init__(self, clusters):
        self._clusters = clusters

    def paginate(self, clusterName):
        return [{"nodegroups": list(self._clusters[clusterName].get("nodegroups", {}))}]


class _FpPaginator:
    def __init__(self, clusters):
        self._clusters = clusters

    def paginate(self, clusterName):
        return [{"fargateProfileNames": list(self._clusters[clusterName].get("fargate", []))}]


def _ctx(eks, **kw):
    pe = MagicMock()
    pe.get_eks_control_plane_hourly.return_value = 0.10
    pe.get_eks_extended_support_hourly.return_value = 0.50
    base = dict(
        cost_hub_splits={"eks_cost": []},
        pricing_multiplier=1.0,
        fast_mode=False,
        pricing_engine=pe,
    )
    base.update(kw)
    ns = SimpleNamespace(**base)
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    ns.client = lambda name, region=None: eks if name == "eks" else MagicMock()
    return ns


def _recs(findings, source):
    return list(findings.sources[source].recommendations)


# --------------------------------------------------------------------------- #
# Extended Support
# --------------------------------------------------------------------------- #
def test_extended_support_charges_surcharge_when_on_extended():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.27",
                        "upgradePolicy": {"supportType": "EXTENDED"}},
            "nodegroups": {"ng1": {"instanceTypes": ["m6g.large"], "capacityType": "ON_DEMAND"}},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    cluster_recs = _recs(findings, "cluster_costs")
    ext = [r for r in cluster_recs if r["check_type"] == "extended_support"]
    assert len(ext) == 1
    assert ext[0]["monthly_savings"] == round(0.50 * 730, 2)  # 365.00
    assert findings.total_monthly_savings >= 365.0


def test_no_extended_support_charge_on_standard_version():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31",
                        "upgradePolicy": {"supportType": "STANDARD"}},
            "nodegroups": {"ng1": {"instanceTypes": ["m6g.large"], "capacityType": "ON_DEMAND"}},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert ext == []


# --------------------------------------------------------------------------- #
# Idle / empty cluster
# --------------------------------------------------------------------------- #
def test_idle_empty_cluster_counts_control_plane():
    eks = _FakeEks({
        "empty": {"cluster": {"status": "ACTIVE", "version": "1.31"},
                  "nodegroups": {}, "fargate": []}
    })
    findings = EksCostModule().scan(_ctx(eks))
    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert len(idle) == 1
    assert idle[0]["monthly_savings"] == round(0.10 * 730, 2)  # 73.00
    assert findings.total_monthly_savings == round(0.10 * 730, 2)


def test_cluster_with_nodegroups_not_idle():
    eks = _FakeEks({
        "busy": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {"instanceTypes": ["m6g.large"], "capacityType": "SPOT"}},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle == []


# --------------------------------------------------------------------------- #
# Node groups & Fargate are advisory (EC2 domain / no fabrication)
# --------------------------------------------------------------------------- #
def test_node_group_findings_are_advisory_not_counted():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {"instanceTypes": ["m4.large"], "capacityType": "ON_DEMAND"}},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    ng = _recs(findings, "node_group_optimization")
    assert ng, "expected node-group advisory findings"
    assert all(r["monthly_savings"] == 0.0 for r in ng)
    assert all(r["Counted"] is False for r in ng)


def test_fargate_profile_advisory_no_fabricated_savings():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {"instanceTypes": ["m6g.large"], "capacityType": "SPOT"}},
            "fargate": ["fp-default"],
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    fg = _recs(findings, "fargate_analysis")
    assert len(fg) == 1
    assert fg[0]["monthly_savings"] == 0.0
    assert fg[0]["Counted"] is False


# --------------------------------------------------------------------------- #
# Cost Hub bucket consumption (corrected key)
# --------------------------------------------------------------------------- #
def test_cost_hub_consumed_from_eks_cost_bucket():
    eks = _FakeEks({})
    coh = [{"recommendationId": "r1", "recommendationSummary": "x", "estimatedMonthlySavings": 12.0}]
    findings = EksCostModule().scan(_ctx(eks, cost_hub_splits={"eks_cost": coh}))
    assert findings.sources["cost_hub_recommendations"].count == 1
    assert findings.total_monthly_savings == 12.0


def test_pricing_lookup_failure_warns_and_zeroes():
    eks = _FakeEks({
        "empty": {"cluster": {"status": "ACTIVE", "version": "1.31"}, "nodegroups": {}, "fargate": []}
    })
    ctx = _ctx(eks)
    ctx.pricing_engine.get_eks_control_plane_hourly.side_effect = RuntimeError("api down")
    findings = EksCostModule().scan(ctx)
    ctx.warn.assert_called()
    assert findings.total_monthly_savings == 0.0

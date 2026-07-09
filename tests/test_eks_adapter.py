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

    def describe_cluster_versions(self, **_kw):
        """Authoritative support status per version (eks:DescribeClusterVersions).

        Only 1.27 is genuinely past end-of-standard-support and therefore
        surcharged; 1.31/1.33 remain in standard support.
        """
        return {
            "clusterVersions": [
                {"clusterVersion": "1.27", "versionStatus": "EXTENDED_SUPPORT",
                 "endOfStandardSupportDate": "2024-07-24T00:00:00Z"},
                {"clusterVersion": "1.31", "versionStatus": "STANDARD_SUPPORT",
                 "endOfStandardSupportDate": "2026-11-26T00:00:00Z"},
                {"clusterVersion": "1.33", "versionStatus": "STANDARD_SUPPORT",
                 "endOfStandardSupportDate": "2026-07-29T00:00:00Z"},
            ]
        }

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
    pe.get_ec2_hourly_price.return_value = 0.10  # node-group instance $/hr
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
def test_node_group_estimate_advisory_not_counted():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {
                "instanceTypes": ["m5.large"], "capacityType": "ON_DEMAND",
                "scalingConfig": {"desiredSize": 2},
            }},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))  # ec2 price 0.10/hr -> $146/mo for 2 nodes
    ng = _recs(findings, "node_group_optimization")
    # ON_DEMAND + x86 -> ONE collapsed advisory naming BOTH mutually exclusive
    # levers (Spot ~70% and Graviton ~20%), carrying only the larger Spot saving.
    assert len(ng) == 1
    rec = ng[0]
    assert "Spot" in rec["recommended_value"]
    assert "Graviton" in rec["recommended_value"]
    assert rec["monthly_savings"] == round(146.0 * 0.70, 2)   # ~102.20 (larger lever only)
    # Advisory — never counted in the EKS tab total (EC2 domain). Collapsing to one
    # rec means even a future Counted=True promotion can never sum 0.70x + 0.20x.
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0


def test_spot_x86_node_group_gets_graviton_only_advisory():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {
                "instanceTypes": ["m5.large"], "capacityType": "SPOT",
                "scalingConfig": {"desiredSize": 2},
            }},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))  # ec2 price 0.10/hr -> $146/mo for 2 nodes
    ng = _recs(findings, "node_group_optimization")
    # SPOT but still x86: Spot lever already taken -> ONE Graviton-only advisory.
    assert len(ng) == 1
    rec = ng[0]
    assert "Graviton" in rec["recommended_value"]
    assert "Spot" not in rec["recommended_value"]
    assert rec["monthly_savings"] == round(146.0 * 0.20, 2)   # ~29.20
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0


def test_graviton_node_group_gets_no_graviton_estimate():
    eks = _FakeEks({
        "prod": {
            "cluster": {"status": "ACTIVE", "version": "1.31"},
            "nodegroups": {"ng1": {
                "instanceTypes": ["m6g.large"], "capacityType": "SPOT",
                "scalingConfig": {"desiredSize": 3},
            }},
        }
    })
    findings = EksCostModule().scan(_ctx(eks))
    ng = _recs(findings, "node_group_optimization")
    # SPOT + already-Graviton → neither a Spot nor a Graviton line.
    assert ng == []


def test_is_graviton_detection():
    from services.adapters.eks import _is_graviton
    assert _is_graviton("m6g.large") and _is_graviton("c7g.xlarge") and _is_graviton("t4g.medium")
    assert _is_graviton("m6gd.large") and _is_graviton("x2gd.metal") and _is_graviton("a1.large")
    assert not _is_graviton("m5.large") and not _is_graviton("c6i.large") and not _is_graviton("t3.micro")


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


# --------------------------------------------------------------------------- #
# bnc live regression (2026-07-09): supportType is a POLICY, not a billing state
# --------------------------------------------------------------------------- #
def test_policy_extended_on_standard_version_is_advisory_not_counted():
    # bnc: two clusters on Kubernetes 1.33 (STANDARD_SUPPORT until 2026-07-29)
    # carried upgradePolicy.supportType == EXTENDED. The old check counted
    # $365/mo each ($730 phantom) while AWS billed only the $0.10/hr base rate.
    eks = _FakeEks({
        "c1": {"cluster": {"status": "ACTIVE", "version": "1.33",
                           "upgradePolicy": {"supportType": "EXTENDED"}},
               "nodegroups": {}, "fargate": []},
    })
    findings = EksCostModule().scan(_ctx(eks))
    recs = _recs(findings, "cluster_costs")
    counted = [r for r in recs if r["check_type"] == "extended_support"]
    pending = [r for r in recs if r["check_type"] == "extended_support_pending"]
    assert counted == [], "must not count a surcharge AWS is not billing"
    assert len(pending) == 1
    assert pending[0]["monthly_savings"] == 0.0
    assert pending[0]["Counted"] is False
    assert pending[0]["AdvisoryEstimate"] == 365.0
    assert "2026-07-29" in pending[0]["current_value"]
    # The $0 advisory must not reach the headline.
    assert all(r["check_type"] != "extended_support" for r in recs)


def test_extended_support_counted_when_version_actually_extended():
    eks = _FakeEks({
        "c1": {"cluster": {"status": "ACTIVE", "version": "1.27",
                           "upgradePolicy": {"supportType": "EXTENDED"}},
               "nodegroups": {}, "fargate": []},
    })
    recs = _recs(EksCostModule().scan(_ctx(eks)), "cluster_costs")
    ext = [r for r in recs if r["check_type"] == "extended_support"]
    assert len(ext) == 1 and ext[0]["monthly_savings"] == 365.0
    assert "versionStatus == EXTENDED_SUPPORT" in ext[0]["audit_basis"]["evidence"]


def test_version_support_lookup_failure_counts_nothing():
    # Fail closed: if DescribeClusterVersions is denied we cannot substantiate a
    # surcharge, so none is counted (never invent a charge).
    class _NoVersions(_FakeEks):
        def describe_cluster_versions(self, **_kw):
            raise RuntimeError("AccessDeniedException")

    eks = _NoVersions({
        "c1": {"cluster": {"status": "ACTIVE", "version": "1.27",
                           "upgradePolicy": {"supportType": "EXTENDED"}},
               "nodegroups": {}, "fargate": []},
    })
    ctx = _ctx(eks)
    recs = _recs(EksCostModule().scan(ctx), "cluster_costs")
    assert [r for r in recs if r["check_type"] == "extended_support"] == []
    assert ctx.warn.called

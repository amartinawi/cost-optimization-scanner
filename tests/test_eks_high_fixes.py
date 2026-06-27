"""HIGH cost-correctness fixes for the EKS adapter (eks_cost H1, H2).

Drives the pure ``scan()`` path with a SimpleNamespace ctx + fake boto3 clients,
proving each counted dollar (or advisory $0) with an explicit assertion.

  - H1  No CoH-vs-cluster dedup: a cluster flagged by a heuristic (idle /
        extended support / failed) AND returned by Cost Optimization Hub
        (EksCluster) is counted twice. The overlapping heuristic cluster_costs
        rec is demoted (Counted=False) — authority CoH > heuristic — before
        summing, keyed on the normalized cluster name.
  - H2  Fail-safe idle: a Karpenter / self-managed cluster with 0 managed node
        groups + 0 Fargate profiles is NOT counted idle (~$73/mo control plane,
        a delete rec) unless EC2 instances tagged kubernetes.io/cluster/<name>
        =owned corroborate zero capacity. Live nodes -> advisory; an
        unavailable/ambiguous read (no ec2 client / API error) -> advisory.

Live-validated rates (AWS Pricing API, us-east-1, 2026-06-27):
  - EKS control plane  USE1-AmazonEKS-Hours:perCluster       = $0.10/cluster-hr
    -> idle counted dollar 0.10 x 730 = $73.00/mo
  - EKS extended support USE1-AmazonEKS-Hours:extendedSupport = $0.50/cluster-hr
    -> $365.00/mo
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.eks import HOURS_PER_MONTH, EksCostModule

CP_RATE = 0.10
EXT_RATE = 0.50
IDLE_MONTHLY = round(CP_RATE * HOURS_PER_MONTH, 2)  # 73.00
EXT_MONTHLY = round(EXT_RATE * HOURS_PER_MONTH, 2)   # 365.00


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _Pager:
    def __init__(self, key: str, items: list[Any]) -> None:
        self._key, self._items = key, items

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{self._key: self._items}]


class _NgPager:
    def __init__(self, clusters: dict[str, Any]) -> None:
        self._c = clusters

    def paginate(self, clusterName: str) -> list[dict[str, Any]]:  # noqa: N803
        return [{"nodegroups": list(self._c[clusterName].get("nodegroups", {}))}]


class _FpPager:
    def __init__(self, clusters: dict[str, Any]) -> None:
        self._c = clusters

    def paginate(self, clusterName: str) -> list[dict[str, Any]]:  # noqa: N803
        return [{"fargateProfileNames": list(self._c[clusterName].get("fargate", []))}]


class _FakeEks:
    """Minimal EKS client driven by a per-cluster config dict."""

    def __init__(self, clusters: dict[str, Any]) -> None:
        self._clusters = clusters

    def get_paginator(self, op: str) -> Any:
        if op == "list_clusters":
            return _Pager("clusters", list(self._clusters))
        if op == "list_nodegroups":
            return _NgPager(self._clusters)
        if op == "list_fargate_profiles":
            return _FpPager(self._clusters)
        raise AssertionError(op)

    def describe_cluster(self, name: str) -> dict[str, Any]:
        return {"cluster": self._clusters[name]["cluster"]}

    def describe_nodegroup(self, clusterName: str, nodegroupName: str) -> dict[str, Any]:  # noqa: N803
        return {"nodegroup": self._clusters[clusterName]["nodegroups"][nodegroupName]}

    def list_addons(self, clusterName: str) -> dict[str, Any]:  # noqa: N803
        return {"addons": self._clusters[clusterName].get("addons", [])}


class _Ec2Pager:
    def __init__(self, reservations: list[dict[str, Any]], error: Exception | None) -> None:
        self._res = reservations
        self._error = error

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return [{"Reservations": self._res}]


class _FakeEc2:
    """Fake EC2 client returning a fixed count of owned instances (or an error)."""

    def __init__(self, owned: int = 0, error: Exception | None = None) -> None:
        instances = [{"InstanceId": f"i-{i}"} for i in range(owned)]
        self._res = [{"Instances": instances}] if owned else []
        self._error = error

    def get_paginator(self, op: str) -> _Ec2Pager:
        assert op == "describe_instances"
        return _Ec2Pager(self._res, self._error)


class _PE:
    """Pricing engine stub with live-validated EKS rates."""

    def get_eks_control_plane_hourly(self) -> float:
        return CP_RATE

    def get_eks_extended_support_hourly(self) -> float:
        return EXT_RATE

    def get_ec2_hourly_price(self, _t: str, quiet: bool = False) -> float:
        return 0.10


def _ctx(eks: Any, ec2: Any = None, *, cost_hub: list | None = None, **kw: Any) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=_PE(),
        pricing_multiplier=1.0,
        fast_mode=False,
        cost_hub_splits={"eks_cost": list(cost_hub or [])},
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(
        (service, action, msg)
    )
    clients = {"eks": eks, "ec2": ec2, "cloudwatch": None}
    ctx.client = lambda name, region=None: clients.get(name)
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _recs(findings: Any, source: str) -> list[dict[str, Any]]:
    return list(findings.sources[source].recommendations)


def _empty_cluster(version: str = "1.31") -> dict[str, Any]:
    return {"cluster": {"status": "ACTIVE", "version": version}, "nodegroups": {}, "fargate": []}


def _ext_cluster_with_ng() -> dict[str, Any]:
    return {
        "cluster": {
            "status": "ACTIVE",
            "version": "1.27",
            "upgradePolicy": {"supportType": "EXTENDED"},
        },
        "nodegroups": {
            "ng1": {
                "instanceTypes": ["m5.large"],
                "capacityType": "ON_DEMAND",
                "scalingConfig": {"desiredSize": 2},
            }
        },
    }


# --------------------------------------------------------------------------- #
# H1 — CoH > heuristic dedup
# --------------------------------------------------------------------------- #
def test_h1_extended_support_deduped_against_coh_no_double_count():
    """Cluster on Extended Support AND in CoH -> heuristic demoted, only CoH counts."""
    eks = _FakeEks({"prod": _ext_cluster_with_ng()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceArn": "arn:aws:eks:us-east-1:123456789012:cluster/prod",
            "recommendationSummary": "Rightsize prod control plane",
            "estimatedMonthlySavings": 50.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert len(ext) == 1
    # Heuristic ext-support rec demoted (authority CoH > heuristic).
    assert ext[0]["Counted"] is False
    assert ext[0]["dedup_basis"] == "CoH > heuristic (EksCluster)"
    # CoH rec is the single counted lever for this cluster.
    assert findings.sources["cost_hub_recommendations"].count == 1
    # Counted == rendered: only the $50 CoH dollar, NOT $50 + $365.
    assert findings.total_monthly_savings == 50.0


def test_h1_no_overlap_keeps_heuristic_counted():
    """CoH covering a DIFFERENT cluster must not demote this cluster's heuristic."""
    eks = _FakeEks({"prod": _ext_cluster_with_ng()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceArn": "arn:aws:eks:us-east-1:1:cluster/staging",
            "recommendationSummary": "other cluster",
            "estimatedMonthlySavings": 50.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert ext[0].get("Counted", True) is True
    # Heuristic ($365) + non-overlapping CoH ($50) both count.
    assert findings.total_monthly_savings == EXT_MONTHLY + 50.0


def test_h1_dedup_matches_bare_resource_id():
    """CoH resourceId given as a bare cluster name still matches the heuristic key."""
    eks = _FakeEks({"prod": _ext_cluster_with_ng()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceId": "prod",  # bare name, no ARN
            "recommendationSummary": "rightsize",
            "estimatedMonthlySavings": 12.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert ext[0]["Counted"] is False
    assert findings.total_monthly_savings == 12.0


def test_h1_empty_coh_bucket_leaves_heuristic_counted():
    """Empty CoH bucket (no resource keys) -> no dedup, heuristic counted."""
    eks = _FakeEks({"prod": _ext_cluster_with_ng()})
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=[]))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert ext[0].get("Counted", True) is True
    assert findings.total_monthly_savings == EXT_MONTHLY


# --------------------------------------------------------------------------- #
# H2 — fail-safe idle: EC2-evidence corroboration
# --------------------------------------------------------------------------- #
def test_h2_idle_counted_when_zero_owned_ec2_nodes():
    """0 managed NG/FP AND 0 owned EC2 nodes -> idle is a real counted $73/mo."""
    eks = _FakeEks({"empty": _empty_cluster()})
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0)))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert len(idle) == 1
    assert idle[0].get("Counted", True) is True
    assert idle[0]["monthly_savings"] == IDLE_MONTHLY  # 73.00
    assert "owned" in idle[0]["audit_basis"]["evidence"]
    assert findings.total_monthly_savings == IDLE_MONTHLY


def test_h2_idle_demoted_when_owned_ec2_nodes_present():
    """Karpenter/self-managed nodes (owned EC2) present -> NOT counted (fail-safe)."""
    eks = _FakeEks({"karpenter": _empty_cluster()})
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=3)))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert len(idle) == 1
    assert idle[0]["Counted"] is False
    assert idle[0]["monthly_savings"] == 0.0
    assert "self-managed/Karpenter" in idle[0]["reason"]
    # No fabricated control-plane dollar.
    assert findings.total_monthly_savings == 0.0


def test_h2_idle_demoted_when_ec2_read_is_access_denied():
    """Ambiguous read (AccessDenied) -> abstain (advisory) AND a permission issue."""
    eks = _FakeEks({"empty": _empty_cluster()})
    denied = Exception("AccessDeniedException: not authorized to DescribeInstances")
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(error=denied)))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle[0]["Counted"] is False
    assert idle[0]["monthly_savings"] == 0.0
    assert "unavailable/ambiguous" in idle[0]["reason"]
    assert findings.total_monthly_savings == 0.0


def test_h2_ec2_access_denied_recorded_as_permission_issue():
    eks = _FakeEks({"empty": _empty_cluster()})
    denied = Exception("AccessDeniedException: not authorized")
    ctx = _ctx(eks, _FakeEc2(error=denied))
    EksCostModule().scan(ctx)

    assert ctx.permissions, "AccessDenied on DescribeInstances must be a permission_issue"
    svc, _action, _msg = ctx.permissions[0]
    assert svc == "eks_cost"
    assert not ctx.warnings  # classified as permission, not a generic warn


def test_h2_ec2_transient_error_recorded_as_warning():
    eks = _FakeEks({"empty": _empty_cluster()})
    transient = Exception("ThrottlingException: rate exceeded")
    ctx = _ctx(eks, _FakeEc2(error=transient))
    findings = EksCostModule().scan(ctx)

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle[0]["Counted"] is False
    assert findings.total_monthly_savings == 0.0
    # Non-permission error surfaced as a warn (never swallowed).
    assert any(svc == "eks_cost" for svc, _ in ctx.warnings)


def test_h2_idle_demoted_when_no_ec2_client():
    """No ec2 client -> corroboration unavailable -> abstain (advisory)."""
    eks = _FakeEks({"empty": _empty_cluster()})
    findings = EksCostModule().scan(_ctx(eks, ec2=None))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle[0]["Counted"] is False
    assert idle[0]["monthly_savings"] == 0.0
    assert findings.total_monthly_savings == 0.0


def test_h2_cluster_with_managed_nodegroup_skips_corroboration_and_is_not_idle():
    """A cluster with a managed node group is not a candidate idle: no idle rec."""
    eks = _FakeEks(
        {
            "busy": {
                "cluster": {"status": "ACTIVE", "version": "1.31"},
                "nodegroups": {
                    "ng1": {
                        "instanceTypes": ["m6g.large"],
                        "capacityType": "SPOT",
                        "scalingConfig": {"desiredSize": 3},
                    }
                },
            }
        }
    )
    # ec2 client that would RAISE if queried — proves corroboration is skipped.
    boom = _FakeEc2(error=AssertionError("ec2 must not be queried for a non-candidate cluster"))
    findings = EksCostModule().scan(_ctx(eks, boom))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

"""HIGH cost-correctness fixes for the EKS adapter (eks_cost H1, H2).

Drives the pure ``scan()`` path with a SimpleNamespace ctx + fake boto3 clients,
proving each counted dollar (or advisory $0) with an explicit assertion.

  - H1  No CoH-vs-cluster dedup: a cluster flagged by a heuristic (idle /
        failed control plane) AND returned by Cost Optimization Hub (EksCluster)
        is counted twice. The overlapping heuristic cluster_costs rec is demoted
        (Counted=False) — authority CoH > heuristic — before summing, keyed on
        the normalized cluster name. Demotion is check_type-aware: the Extended
        Support surcharge is an independent cost dimension (removed only by a
        k8s version upgrade, which CoH never recommends) and is NEVER demoted,
        else a ~$365/mo additive saving is silently dropped.
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

    def describe_cluster_versions(self, **_kw: Any) -> dict[str, Any]:
        """Authoritative per-version support status (mirrors eks:DescribeClusterVersions).

        1.27 is genuinely past end-of-standard-support (surcharged); newer
        versions are still in standard support, so a cluster on one of those is
        NOT billed the surcharge even if upgradePolicy.supportType == EXTENDED.
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


def _idle_ext_cluster() -> dict[str, Any]:
    """Idle cluster (no node groups / Fargate) that is ALSO on Extended Support.

    Yields BOTH a control-plane ``idle_cluster`` rec (the cluster's base cost,
    superseded by a CoH cluster rec) and an independent ``extended_support``
    surcharge rec (removed only by a k8s version upgrade, NOT covered by CoH).
    """
    return {
        "cluster": {
            "status": "ACTIVE",
            "version": "1.27",
            "upgradePolicy": {"supportType": "EXTENDED"},
        },
        "nodegroups": {},
        "fargate": [],
    }


# --------------------------------------------------------------------------- #
# H1 — CoH > heuristic dedup
# --------------------------------------------------------------------------- #
def test_h1_extended_support_not_demoted_when_coh_covers_compute():
    """Cluster on Extended Support AND in CoH -> surcharge stays counted.

    The Extended Support surcharge is removed only by a Kubernetes version
    upgrade — not a CoH action type (Rightsize/Stop/Graviton/Delete/commitment),
    so a CoH EksCluster compute rec does NOT include it. Demoting it would
    silently drop a ~$365/mo additive saving (eks dedup check_type awareness).
    """
    eks = _FakeEks({"prod": _ext_cluster_with_ng()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceArn": "arn:aws:eks:us-east-1:123456789012:cluster/prod",
            "recommendationSummary": "Rightsize prod node compute",
            "estimatedMonthlySavings": 50.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    assert len(ext) == 1
    # The surcharge is an independent cost dimension -> NOT demoted.
    assert ext[0].get("Counted", True) is True
    assert "dedup_basis" not in ext[0]
    assert findings.sources["cost_hub_recommendations"].count == 1
    # Both additive: the $365 surcharge AND the $50 CoH compute saving count.
    assert findings.total_monthly_savings == EXT_MONTHLY + 50.0


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
    """CoH resourceId given as a bare cluster name still matches the heuristic key.

    Probes a demotable check_type (idle control plane) since the Extended
    Support surcharge is now exempt from demotion.
    """
    eks = _FakeEks({"prod": _empty_cluster()})  # idle, standard support
    coh = [
        {
            "recommendationId": "r1",
            "resourceId": "prod",  # bare name, no ARN
            "recommendationSummary": "rightsize",
            "estimatedMonthlySavings": 12.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    assert idle and idle[0]["Counted"] is False  # base control-plane cost superseded by CoH
    assert findings.total_monthly_savings == 12.0


def test_h1_extended_support_demoted_when_coh_eliminates_full_cluster():
    """A CoH Stop/Delete EksCluster rec prices the WHOLE cluster (control plane +
    surcharge), so the Extended Support surcharge IS demoted under it — exempting
    it then would double-count. Guards the actionType-aware narrowing (eks B1)."""
    eks = _FakeEks({"prod": _idle_ext_cluster()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceArn": "arn:aws:eks:us-east-1:1:cluster/prod",
            "actionType": "Stop",  # whole-cluster elimination
            "recommendationSummary": "Stop idle prod cluster",
            "estimatedMonthlySavings": EXT_MONTHLY + IDLE_MONTHLY,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    # Under a Stop/Delete, the surcharge is included in the CoH total -> demoted.
    assert ext and ext[0]["Counted"] is False
    assert idle and idle[0]["Counted"] is False
    # Counted == rendered: only the single CoH elimination dollar, no $365 on top.
    assert findings.total_monthly_savings == EXT_MONTHLY + IDLE_MONTHLY


def test_h1_check_type_aware_idle_demoted_but_extended_support_kept():
    """An idle cluster on Extended Support, covered by CoH: the base control-plane
    (idle) cost is superseded (demoted), but the independent Extended Support
    surcharge stays counted — additive to the CoH compute saving."""
    eks = _FakeEks({"prod": _idle_ext_cluster()})
    coh = [
        {
            "recommendationId": "r1",
            "resourceArn": "arn:aws:eks:us-east-1:1:cluster/prod",
            "recommendationSummary": "Rightsize prod node compute",
            "estimatedMonthlySavings": 40.0,
        }
    ]
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0), cost_hub=coh))

    idle = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "idle_cluster"]
    ext = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "extended_support"]
    # Idle base cost demoted (CoH > heuristic, same control-plane dimension).
    assert idle and idle[0]["Counted"] is False
    # Extended Support surcharge independent -> stays counted.
    assert ext and ext[0].get("Counted", True) is True
    # Total = surcharge ($365) + CoH ($40); the idle $73 is NOT double-counted.
    assert findings.total_monthly_savings == EXT_MONTHLY + 40.0


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


# --------------------------------------------------------------------------- #
# L1 — counted failed-cluster rec carries audit_basis (parity with the other
#      two counted cluster recs: extended_support and idle_cluster)
# --------------------------------------------------------------------------- #
def _failed_cluster() -> dict[str, Any]:
    return {"cluster": {"status": "FAILED", "version": "1.31"}, "nodegroups": {}, "fargate": []}


def test_l1_failed_cluster_rec_carries_audit_basis():
    """A counted failed-cluster rec must carry audit_basis (rate/unit/formula/evidence)."""
    eks = _FakeEks({"broken": _failed_cluster()})
    findings = EksCostModule().scan(_ctx(eks, _FakeEc2(owned=0)))

    failed = [r for r in _recs(findings, "cluster_costs") if r["check_type"] == "failed_cluster"]
    assert len(failed) == 1
    basis = failed[0]["audit_basis"]
    assert basis["rate"] == CP_RATE
    assert basis["unit"] == "USD/cluster-hour"
    assert basis["formula"] == f"{CP_RATE} x {HOURS_PER_MONTH} hr"
    assert basis["evidence"] == "cluster.status == FAILED"
    # The rec is counted (no Counted=False): its dollar feeds the headline.
    assert failed[0]["monthly_savings"] == IDLE_MONTHLY  # 0.10 x 730 = 73.00
    assert findings.total_monthly_savings == IDLE_MONTHLY


# --------------------------------------------------------------------------- #
# L2 — node-group pricing failure must warn (never silently swallow)
# --------------------------------------------------------------------------- #
def test_l2_node_group_pricing_failure_warns():
    """A pricing-API failure in node-group costing emits a ctx.warn (never silent)."""
    ctx = _ctx(_FakeEks({}))

    def _boom(_t: str, quiet: bool = False) -> float:
        raise RuntimeError("pricing api down")

    ctx.pricing_engine.get_ec2_hourly_price = _boom

    cost = EksCostModule()._node_group_monthly_cost(ctx, ["m5.large"], 2)

    assert cost == 0.0
    assert any(svc == "eks_cost" and "m5.large" in msg for svc, msg in ctx.warnings)


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

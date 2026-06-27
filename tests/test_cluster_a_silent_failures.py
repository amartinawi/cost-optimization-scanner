"""Cluster A (HIGH) — silent failures must be classified, never swallowed.

Each adapter/shim that previously dropped an AWS exception with ``except: pass``
/ ``logger.warning`` now routes it through ``services/_aws_errors.record_aws_error``
so an account-wide ``AccessDenied`` surfaces as a ``ctx.permission_issue`` (not a
clean-looking empty tab) and emits no fabricated counted dollar. These tests drive
each path with a fake boto3 client that raises ``AccessDenied`` and assert the
permission gap is recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.advisor as advisor
import services.api_gateway as api_gateway
import services.batch_svc as batch_svc
import services.cloudfront as cloudfront
import services.monitoring as monitoring
import services.quicksight as quicksight
import services.workspaces as workspaces
import services.adapters.aurora as aurora_adapter
import services.adapters.ec2 as ec2_adapter
from services.adapters.network_cost import NetworkCostModule


def _access_denied(op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, op)


class _Ctx:
    """Minimal ScanContext double capturing warn / permission_issue calls."""

    def __init__(self, clients: dict[str, Any], *, fast_mode: bool = False, pricing_engine: Any = None):
        self._clients = clients
        self.fast_mode = fast_mode
        self.pricing_engine = pricing_engine
        self.pricing_multiplier = 1.0
        self.region = "us-east-1"
        self.account_id = "123456789012"
        self.cost_hub_splits: dict[str, Any] = {}
        self.warnings: list[tuple] = []
        self.permission_issues: list[tuple] = []

    def client(self, name: str, region: str | None = None) -> Any:
        return self._clients.get(name)

    def warn(self, message: str, service: str | None = None) -> None:
        self.warnings.append((service, message))

    def permission_issue(self, message: str, service: str | None = None, action: str | None = None) -> None:
        self.permission_issues.append((service, message, action))


class _Boom:
    """A client whose every attribute, when called, raises ``exc``."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def __getattr__(self, _name: str):
        def _raise(*_a: Any, **_k: Any):
            raise self._exc

        return _raise


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]):
        self._pages = pages

    def paginate(self, **_kwargs: Any):
        return iter(self._pages)


class _RaisingPaginator:
    def __init__(self, exc: BaseException):
        self._exc = exc

    def paginate(self, **_kwargs: Any):
        raise self._exc


def _perm_services(ctx: _Ctx) -> list[str]:
    return [svc for svc, *_ in ctx.permission_issues]


# --------------------------------------------------------------------------- #
# api_gateway H1 — outer GetRestApis failure classified
# --------------------------------------------------------------------------- #
def test_api_gateway_outer_failure_classified() -> None:
    ctx = _Ctx({"apigateway": _Boom(_access_denied("GetRestApis"))})
    api_gateway.get_enhanced_api_gateway_checks(ctx)
    assert "api_gateway" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# api_gateway H2 — a failed CloudWatch read marks the rec advisory, not $0-genuine
# --------------------------------------------------------------------------- #
class _ApiGwOK:
    def get_paginator(self, _name: str):
        return _FakePaginator([{"items": [{"id": "a1", "name": "api-one"}]}])

    def get_resources(self, restApiId: str):
        return {"items": [{"id": "r1"}]}  # 1 resource ≤ 10 → check fires


def test_api_gateway_metric_read_failure_marks_advisory() -> None:
    ctx = _Ctx({"apigateway": _ApiGwOK(), "cloudwatch": _Boom(_access_denied("GetMetricStatistics"))})
    result = api_gateway.get_enhanced_api_gateway_checks(ctx)
    recs = result["recommendations"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec.get("MetricReadFailed") is True
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "api_gateway" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# batch H3 — DescribeComputeEnvironments failure classified
# --------------------------------------------------------------------------- #
def test_batch_describe_compute_environments_classified() -> None:
    ctx = _Ctx({"batch": _Boom(_access_denied("DescribeComputeEnvironments"))})
    batch_svc.get_enhanced_batch_checks(ctx)
    assert "batch" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# aurora H4 — DescribeDBClusters failure surfaces (whole tab no longer empties silently)
# --------------------------------------------------------------------------- #
def test_aurora_describe_clusters_classified() -> None:
    ctx = _Ctx({})
    rds = _Boom(_access_denied("DescribeDBClusters"))
    clusters = aurora_adapter._describe_aurora_clusters(rds, ctx)
    assert clusters == []
    assert "aurora" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# ec2 H1 — ASG-member dedup degradation is visible + partial set returned
# --------------------------------------------------------------------------- #
def test_ec2_asg_member_failure_classified() -> None:
    ctx = _Ctx({"autoscaling": _Boom(_access_denied("DescribeAutoScalingGroups"))})
    ids = ec2_adapter._asg_member_instance_ids(ctx)
    assert ids == set()
    assert "ec2" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# containers H2 — ECS Compute Optimizer non-opt-in failure classified
# --------------------------------------------------------------------------- #
def test_containers_ecs_co_failure_classified() -> None:
    ctx = _Ctx({"compute-optimizer": _Boom(_access_denied("GetECSServiceRecommendations"))})
    recs = advisor.get_ecs_compute_optimizer_recommendations(ctx)
    assert recs == []
    assert "containers" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# network_cost H3 — CE + both EC2 describes classified (no silent zeroing)
# --------------------------------------------------------------------------- #
def test_network_topology_describe_failures_classified() -> None:
    ctx = _Ctx({})
    ec2 = _Boom(_access_denied("Describe"))
    peering, tgw = NetworkCostModule()._fetch_network_topology(ec2, ctx)
    assert (peering, tgw) == (0, 0)
    # Both DescribeVpcPeeringConnections and DescribeTransitGateways recorded.
    assert _perm_services(ctx).count("network_cost") == 2


def test_network_transfer_spend_failure_classified() -> None:
    ctx = _Ctx({})
    ce = _Boom(_access_denied("GetCostAndUsage"))
    total, _breakdown = NetworkCostModule()._fetch_transfer_spend(ce, {"Start": "2026-05-01", "End": "2026-06-01"}, ctx)
    assert total == 0.0
    assert "network_cost" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# quicksight H1 + H2 — ListUsers + DescribeSpiceCapacity classified; enum gap
# does not silently zero the SPICE check
# --------------------------------------------------------------------------- #
class _FakeQuickSight:
    def describe_account_subscription(self, AwsAccountId: str):
        return {"AccountInfo": {"AccountSubscriptionStatus": "ACCOUNT_CREATED", "Edition": "ENTERPRISE"}}

    def get_paginator(self, name: str):
        if name == "list_namespaces":
            return _FakePaginator([{"Namespaces": [{"Name": "default"}]}])
        if name == "list_users":
            return _RaisingPaginator(_access_denied("ListUsers"))
        raise ValueError(name)

    def describe_spice_capacity(self, AwsAccountId: str):
        raise _access_denied("DescribeSpiceCapacity")


def test_quicksight_list_users_and_spice_failures_classified() -> None:
    ctx = _Ctx({"quicksight": _FakeQuickSight()})
    quicksight.get_enhanced_quicksight_checks(ctx)
    svcs = _perm_services(ctx)
    # H2 (ListUsers) AND H1 (DescribeSpiceCapacity) both surfaced — the enum gap
    # did not short-circuit before the SPICE read.
    assert svcs.count("quicksight") == 2


# --------------------------------------------------------------------------- #
# workspaces C1 — DescribeWorkspaces failure classified
# --------------------------------------------------------------------------- #
def test_workspaces_describe_failure_classified() -> None:
    ctx = _Ctx({"workspaces": _Boom(_access_denied("DescribeWorkspaces"))})
    workspaces.get_enhanced_workspaces_checks(ctx)
    assert "workspaces" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# cloudfront H1 — Requests / CacheHitRate read failures classified
# --------------------------------------------------------------------------- #
class _FakeCloudFront:
    def get_paginator(self, _name: str):
        return _FakePaginator(
            [
                {
                    "DistributionList": {
                        "Items": [
                            {
                                "Id": "d1",
                                "DomainName": "x.cloudfront.net",
                                "PriceClass": "PriceClass_All",
                                "Status": "Deployed",
                                "Enabled": True,
                            }
                        ]
                    }
                }
            ]
        )

    def get_distribution_config(self, Id: str):
        return {"DistributionConfig": {"Origins": {"Items": []}}}


def test_cloudfront_metric_read_failure_classified() -> None:
    ctx = _Ctx({"cloudfront": _FakeCloudFront(), "cloudwatch": _Boom(_access_denied("GetMetricStatistics"))})
    cloudfront.get_enhanced_cloudfront_checks(ctx)
    assert "cloudfront" in _perm_services(ctx)


# --------------------------------------------------------------------------- #
# monitoring H1 — CloudWatch checks failures classified (not logger-only)
# --------------------------------------------------------------------------- #
def test_monitoring_cloudwatch_failures_classified() -> None:
    ctx = _Ctx({"cloudwatch": _Boom(_access_denied("ListMetrics")), "logs": _Boom(_access_denied("DescribeLogGroups"))})
    monitoring.get_cloudwatch_checks(ctx, 1.0)
    assert "monitoring" in _perm_services(ctx)

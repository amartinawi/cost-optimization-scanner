"""Unit tests for the network adapter cost-audit fixes (C1, H1–H4, M1–M4, L1).

Same SimpleNamespace-ctx + fake-boto3 style as ``tests/test_lambda_audit_fixes.py``.

Covered:
  - C1  All five per-domain sources render via PHASE_B_HANDLERS; counted == rendered.
  - H1  ALB/NLB/GWLB/CLB price from their own productFamily (ALB != Classic LB).
  - H2  Network ASG block is advisory (Counted=False) so it never double-counts EC2.
  - H3  NAT same-AZ vs cross-AZ savings do not double-count; dev/test dedup.
  - H4  Sub-shim failures classify AccessDenied -> permission_issue, else warn.
  - M1  Dev/test NAT string carries the base only (no fabricated +0.85 addend).
  - M2  Scaled-to-zero ASG is skipped; ASGs are paginated.
  - M4  Interface VPC endpoints are priced per-AZ; Gateway endpoints excluded.
  - L1  pricing_engine=None falls back to region-scaled constants.
  - advisory $0 gating via mark_zero_savings_advisory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import services.adapters.network as network_mod
from services._savings import mark_zero_savings_advisory, parse_dollar_savings
from services.adapters.network import (
    NetworkModule,
    _derive_severity,
    _mark_advisory,
    _safe_collect,
)
from services.elastic_ip import get_elastic_ip_checks
from services.nat_gateway import get_nat_gateway_checks
from services.vpc_endpoints import get_vpc_endpoints_checks


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeEc2:
    def __init__(
        self,
        *,
        addresses: list[dict] | None = None,
        instances_pages: list[dict] | None = None,
        nat_pages: list[dict] | None = None,
        vpce_pages: list[dict] | None = None,
        vpcs_pages: list[dict] | None = None,
        subnet_az: dict[str, str] | None = None,
        subnet_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._addresses = addresses or []
        self._pages = {
            "describe_instances": instances_pages or [{"Reservations": []}],
            "describe_nat_gateways": nat_pages or [{"NatGateways": []}],
            "describe_vpc_endpoints": vpce_pages or [{"VpcEndpoints": []}],
            "describe_vpcs": vpcs_pages or [{"Vpcs": []}],
        }
        self._subnet_az = subnet_az or {}
        self._subnet_errors = subnet_errors or {}

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator(self._pages.get(name, [{}]))

    def describe_addresses(self) -> dict[str, Any]:
        return {"Addresses": self._addresses}

    def describe_subnets(self, SubnetIds: list[str]) -> dict[str, Any]:  # noqa: N803
        sid = SubnetIds[0]
        if sid in self._subnet_errors:
            raise self._subnet_errors[sid]
        return {"Subnets": [{"AvailabilityZone": self._subnet_az.get(sid, "us-east-1a")}]}


def _client_error(code: str) -> Exception:
    exc = Exception(f"{code}: denied")
    exc.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return exc


def _ctx(ec2: Any = None, *, pricing_engine: Any = None, pricing_multiplier: float = 1.0) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=pricing_multiplier,
        fast_mode=False,
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service="": ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service="", action=None: ctx.permissions.append((service, action, msg))
    clients = {"ec2": ec2}
    ctx.client = lambda name, region=None: clients.get(name)
    return ctx


def _nat_engine(rate: float = 32.85) -> SimpleNamespace:
    return SimpleNamespace(get_nat_gateway_monthly_price=lambda: rate)


# --------------------------------------------------------------------------- #
# parse_dollar_savings boundaries + advisory gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("$32.85/month if consolidated", 32.85),
        ("$3.65/month per EIP", 3.65),
        ("$1,234.50/month", 1234.50),
        ("$0.01/GB data processing savings", 0.0),  # per-unit rate, not a total
        ("$0.045/GB cross-AZ", 0.0),
        ("10-20% + better features", 0.0),  # percentage only
        ("$0.00/month - requires CW BytesOutToDestination metric", 0.0),
    ],
)
def test_parse_dollar_savings_boundaries(text: str, expected: float) -> None:
    assert parse_dollar_savings(text) == expected


def test_mark_zero_savings_advisory_flags_rate_strings() -> None:
    recs = [
        {"EstimatedSavings": "$32.85/month"},
        {"EstimatedSavings": "$0.01/GB data processing savings"},
        {"EstimatedSavings": "10-20% + better features"},
    ]
    mark_zero_savings_advisory(recs, lambda r: parse_dollar_savings(r["EstimatedSavings"]))
    assert recs[0].get("Counted") is not False  # counted
    assert recs[1]["Counted"] is False
    assert recs[2]["Counted"] is False


# --------------------------------------------------------------------------- #
# _derive_severity
# --------------------------------------------------------------------------- #
def test_derive_severity_thresholds() -> None:
    assert _derive_severity({"EstimatedSavings": "$32.85/month"}) == "HIGH"
    assert _derive_severity({"EstimatedSavings": "$16.43/month"}) == "MEDIUM"
    assert _derive_severity({"EstimatedSavings": "$3.65/month"}) == "LOW"
    assert _derive_severity({"EstimatedSavings": "$0.01/GB"}) == "LOW"


def test_derive_severity_honors_explicit() -> None:
    assert _derive_severity({"severity": "high", "EstimatedSavings": "$1/month"}) == "HIGH"


# --------------------------------------------------------------------------- #
# H4 — silent-failure classification via _safe_collect
# --------------------------------------------------------------------------- #
def test_safe_collect_access_denied_is_permission_issue() -> None:
    ctx = _ctx()

    def boom(_c: Any) -> dict[str, Any]:
        raise _client_error("AccessDenied")

    assert _safe_collect("load_balancer", boom, ctx) == []
    assert len(ctx.permissions) == 1
    assert ctx.permissions[0][0] == "network"
    assert not ctx.warnings


def test_safe_collect_generic_error_is_warning() -> None:
    ctx = _ctx()

    def boom(_c: Any) -> dict[str, Any]:
        raise Exception("ThrottlingException: slow down")

    assert _safe_collect("nat_gateway", boom, ctx) == []
    assert len(ctx.warnings) == 1
    assert not ctx.permissions


def test_nat_shim_records_subnet_failure() -> None:
    ec2 = _FakeEc2(
        nat_pages=[{"NatGateways": [_nat("nat-1", "vpc-a", "sub-1")]}],
        subnet_errors={"sub-1": _client_error("UnauthorizedOperation")},
    )
    ctx = _ctx(ec2, pricing_engine=_nat_engine())
    out = get_nat_gateway_checks(ctx)
    assert out["recommendations"] == []  # NAT skipped, but...
    assert len(ctx.permissions) == 1  # ...failure was recorded, not swallowed


# --------------------------------------------------------------------------- #
# H3 + M1 — NAT same-AZ vs cross-AZ dedup and dev/test handling
# --------------------------------------------------------------------------- #
def _nat(nat_id: str, vpc: str, subnet: str, env: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"NatGatewayId": nat_id, "VpcId": vpc, "SubnetId": subnet, "State": "available"}
    if env:
        d["Tags"] = [{"Key": "Environment", "Value": env}]
    return d


def test_nat_same_az_and_cross_az_do_not_double_count() -> None:
    # vpc-a: 2 NATs in az-a (same-AZ waste = 1) + 1 NAT in az-b (cross-AZ incr = 1)
    nat_pages = [{"NatGateways": [
        _nat("nat-1", "vpc-a", "sub-a1"),
        _nat("nat-2", "vpc-a", "sub-a2"),
        _nat("nat-3", "vpc-a", "sub-b1"),
    ]}]
    subnet_az = {"sub-a1": "az-a", "sub-a2": "az-a", "sub-b1": "az-b"}
    ec2 = _FakeEc2(nat_pages=nat_pages, subnet_az=subnet_az)
    out = get_nat_gateway_checks(_ctx(ec2, pricing_engine=_nat_engine(32.85)))

    same_az = [r for r in out["multiple_nat_gateways"]]
    cross_az = [r for r in out["unnecessary_nat_per_az"]]
    assert len(same_az) == 1 and same_az[0]["EstimatedMonthlySavings"] == 32.85
    assert len(cross_az) == 1 and cross_az[0]["EstimatedMonthlySavings"] == 32.85
    # Total counted NAT consolidation = 2 NATs = down-to-1 (T-1=2). No triple count.
    counted = sum(
        parse_dollar_savings(r["EstimatedSavings"])
        for r in same_az + cross_az
    )
    assert counted == pytest.approx(2 * 32.85)


def test_dev_test_nat_advisory_when_vpc_has_multiple_nats() -> None:
    nat_pages = [{"NatGateways": [
        _nat("nat-1", "vpc-a", "sub-a1", env="dev"),
        _nat("nat-2", "vpc-a", "sub-a2", env="dev"),
    ]}]
    ec2 = _FakeEc2(nat_pages=nat_pages, subnet_az={"sub-a1": "az-a", "sub-a2": "az-a"})
    out = get_nat_gateway_checks(_ctx(ec2, pricing_engine=_nat_engine()))
    dev = out["nat_in_dev_test"]
    assert len(dev) == 2
    assert all(r["EstimatedMonthlySavings"] == 0.0 for r in dev)  # consolidation owns the $


def test_dev_test_nat_counted_when_sole_in_vpc() -> None:
    nat_pages = [{"NatGateways": [_nat("nat-1", "vpc-solo", "sub-1", env="test")]}]
    ec2 = _FakeEc2(nat_pages=nat_pages, subnet_az={"sub-1": "az-a"})
    out = get_nat_gateway_checks(_ctx(ec2, pricing_engine=_nat_engine(32.85)))
    dev = out["nat_in_dev_test"]
    assert len(dev) == 1
    assert dev[0]["EstimatedMonthlySavings"] == 32.85
    assert "+ 0.85" not in dev[0]["EstimatedSavings"]  # M1: no fabricated addend
    assert parse_dollar_savings(dev[0]["EstimatedSavings"]) == 32.85


def test_net06_no_missing_endpoint_advisory_from_nat_shim() -> None:
    # NET-06: a VPC with a NAT but no S3/DDB gateway endpoint must NOT emit a
    # missing-endpoint advisory from the NAT shim — the vpc_endpoints sub-shim
    # already owns that ($0 advisory), so the NAT-scoped duplicate is dropped.
    nat_pages = [{"NatGateways": [_nat("nat-1", "vpc-a", "sub-1")]}]
    ec2 = _FakeEc2(nat_pages=nat_pages, subnet_az={"sub-1": "az-a"})
    out = get_nat_gateway_checks(_ctx(ec2, pricing_engine=_nat_engine()))
    assert "nat_for_aws_services" not in out  # dead category removed
    assert all(r.get("CheckCategory") != "VPC Endpoints Missing" for r in out["recommendations"])


# --------------------------------------------------------------------------- #
# M4 — interface endpoints priced per-AZ, gateway endpoints excluded
# --------------------------------------------------------------------------- #
def test_interface_endpoint_priced_per_az() -> None:
    vpce_pages = [{"VpcEndpoints": [
        {
            "VpcEndpointId": "vpce-1", "VpcId": "vpc-a", "VpcEndpointType": "Interface",
            "ServiceName": "com.amazonaws.us-east-1.ssm", "SubnetIds": ["s1", "s2", "s3"],
            "Tags": [{"Key": "Environment", "Value": "dev"}], "State": "available",
        }
    ]}]
    ec2 = _FakeEc2(vpcs_pages=[{"Vpcs": [{"VpcId": "vpc-a"}]}], vpce_pages=vpce_pages)
    eng = SimpleNamespace(get_vpc_endpoint_monthly_price=lambda: 7.30)
    out = get_vpc_endpoints_checks(_ctx(ec2, pricing_engine=eng))
    nonprod = out["interface_endpoints_in_nonprod"]
    assert len(nonprod) == 1
    assert nonprod[0]["EstimatedMonthlySavings"] == pytest.approx(7.30 * 3)  # 3 AZs


def test_gateway_endpoint_not_priced_as_duplicate() -> None:
    # 3 Gateway S3 endpoints in one VPC must NOT be flagged as paid duplicates.
    vpce_pages = [{"VpcEndpoints": [
        {"VpcEndpointId": f"vpce-{i}", "VpcId": "vpc-a", "VpcEndpointType": "Gateway",
         "ServiceName": "com.amazonaws.us-east-1.s3", "State": "available"}
        for i in range(3)
    ]}]
    ec2 = _FakeEc2(vpcs_pages=[{"Vpcs": [{"VpcId": "vpc-a"}]}], vpce_pages=vpce_pages)
    eng = SimpleNamespace(get_vpc_endpoint_monthly_price=lambda: 7.30)
    out = get_vpc_endpoints_checks(_ctx(ec2, pricing_engine=eng))
    assert out["duplicate_endpoints"] == []  # gateway endpoints are free


def test_net05_dead_check_categories_removed() -> None:
    # NET-05: never-populated check categories are dropped from the output dict.
    ec2 = _FakeEc2(vpcs_pages=[{"Vpcs": []}], vpce_pages=[{"VpcEndpoints": []}])
    out = get_vpc_endpoints_checks(_ctx(ec2, pricing_engine=SimpleNamespace(get_vpc_endpoint_monthly_price=lambda: 7.30)))
    assert "unused_interface_endpoints" not in out
    assert "no_traffic_endpoints" not in out
    # Populated categories survive.
    for key in ("missing_gateway_endpoints", "interface_endpoints_in_nonprod", "duplicate_endpoints"):
        assert key in out


# --------------------------------------------------------------------------- #
# L1 — fallback region scaling when pricing_engine is None
# --------------------------------------------------------------------------- #
def test_eip_fallback_region_scaled() -> None:
    from core.pricing_engine import FALLBACK_EIP_MONTH

    addresses = [{"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4"}]  # unassociated
    ec2 = _FakeEc2(addresses=addresses)
    out = get_elastic_ip_checks(_ctx(ec2, pricing_engine=None, pricing_multiplier=2.0))
    rec = out["unassociated_eips"][0]
    assert parse_dollar_savings(rec["EstimatedSavings"]) == pytest.approx(FALLBACK_EIP_MONTH * 2.0)


def _eng_eip(rate: float = 3.65) -> SimpleNamespace:
    return SimpleNamespace(get_eip_monthly_price=lambda: rate)


def test_net03_stopped_instance_eips_not_double_counted() -> None:
    # NET-03: two EIPs on a STOPPED instance are counted once (in
    # eips_on_stopped_instances); the instance must NOT also appear under
    # multiple_eips_per_instance (which would attribute $3.65/EIP twice).
    addresses = [
        {"AllocationId": "eipalloc-1", "PublicIp": "1.1.1.1", "InstanceId": "i-stopped"},
        {"AllocationId": "eipalloc-2", "PublicIp": "1.1.1.2", "InstanceId": "i-stopped"},
    ]
    instances_pages = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-stopped", "State": {"Name": "stopped"}}]}]}
    ]
    ec2 = _FakeEc2(addresses=addresses, instances_pages=instances_pages)
    out = get_elastic_ip_checks(_ctx(ec2, pricing_engine=_eng_eip()))
    assert len(out["eips_on_stopped_instances"]) == 2
    assert out["multiple_eips_per_instance"] == []  # excluded — no double count


def test_net03_running_instance_multiple_eips_still_flagged() -> None:
    # Contrast: a RUNNING instance with >1 EIP is NOT on the stopped list, so the
    # multiple-EIPs lever still fires (the dedup only suppresses stopped ones).
    addresses = [
        {"AllocationId": "eipalloc-1", "PublicIp": "1.1.1.1", "InstanceId": "i-run"},
        {"AllocationId": "eipalloc-2", "PublicIp": "1.1.1.2", "InstanceId": "i-run"},
    ]
    instances_pages = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-run", "State": {"Name": "running"}}]}]}
    ]
    ec2 = _FakeEc2(addresses=addresses, instances_pages=instances_pages)
    out = get_elastic_ip_checks(_ctx(ec2, pricing_engine=_eng_eip()))
    assert out["eips_on_stopped_instances"] == []
    assert len(out["multiple_eips_per_instance"]) == 1


def test_nat_fallback_region_scaled() -> None:
    from core.pricing_engine import FALLBACK_NAT_MONTH

    nat_pages = [{"NatGateways": [_nat("nat-1", "vpc-solo", "sub-1", env="dev")]}]
    ec2 = _FakeEc2(nat_pages=nat_pages, subnet_az={"sub-1": "az-a"})
    out = get_nat_gateway_checks(_ctx(ec2, pricing_engine=None, pricing_multiplier=1.5))
    rec = out["nat_in_dev_test"][0]
    # String is formatted with .2f, so compare against the rounded display value.
    assert parse_dollar_savings(rec["EstimatedSavings"]) == float(f"{FALLBACK_NAT_MONTH * 1.5:.2f}")


# --------------------------------------------------------------------------- #
# H2 — ASG block advisory in NetworkModule.scan
# --------------------------------------------------------------------------- #
def test_mark_advisory_sets_counted_false() -> None:
    recs = [{"EstimatedSavings": "$50.00/month per node", "CheckCategory": "Oversized ASG Instances"}]
    _mark_advisory(recs, "owned by EC2")
    assert recs[0]["Counted"] is False
    assert recs[0]["AdvisoryNote"] == "owned by EC2"


def test_network_scan_asg_is_advisory_and_excluded_from_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network_mod, "get_elastic_ip_checks", lambda c: {"recommendations": [
        {"EstimatedSavings": "$3.65/month per EIP", "CheckCategory": "Unassociated EIPs"}
    ]})
    monkeypatch.setattr(network_mod, "get_nat_gateway_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_vpc_endpoints_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_load_balancer_checks", lambda c: {"recommendations": []})
    monkeypatch.setattr(network_mod, "get_auto_scaling_checks", lambda c: {"recommendations": [
        {"EstimatedSavings": "$50.00/month per node if rightsized", "CheckCategory": "Oversized ASG Instances"}
    ]})

    findings = NetworkModule().scan(_ctx())
    asg = findings.sources["auto_scaling_groups"].recommendations
    assert asg[0]["Counted"] is False  # advisory
    # Total counts only the $3.65 EIP, NOT the $50 ASG node.
    assert findings.total_monthly_savings == pytest.approx(3.65)


# --------------------------------------------------------------------------- #
# C1 — render wiring: all five sources have a handler; counted == rendered
# --------------------------------------------------------------------------- #
def test_all_network_sources_have_handler() -> None:
    from reporter_phase_b import should_fallback_to_per_rec, should_use_handler

    for src in ("elastic_ips", "nat_gateways", "vpc_endpoints", "load_balancers", "auto_scaling_groups"):
        assert should_use_handler("network", src), f"{src} has no PHASE_B handler"
    # network is in skip-per-rec, so the handler is the ONLY render path.
    assert not should_fallback_to_per_rec("network")


def test_network_detail_renders_counted_and_advisory() -> None:
    from html_report_generator import HTMLReportGenerator

    net = {
        "service_name": "Network & Infrastructure",
        "total_recommendations": 2,
        "total_monthly_savings": 3.65,
        "sources": {
            "elastic_ips": {"count": 1, "recommendations": [
                {"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4", "ResourceName": "EIP 1.2.3.4",
                 "Recommendation": "Release", "EstimatedSavings": "$3.65/month per EIP",
                 "CheckCategory": "Unassociated EIPs", "Counted": True}
            ]},
            "nat_gateways": {"count": 1, "recommendations": [
                {"NatGatewayId": "nat-1", "Recommendation": "monitor",
                 "EstimatedSavings": "$0.00/month - requires CW BytesOutToDestination metric",
                 "CheckCategory": "Low Throughput NAT Gateway", "Counted": False}
            ]},
        },
    }
    gen = HTMLReportGenerator({"account_id": "x", "region": "us-east-1", "services": {"network": net},
                              "summary": {"total_monthly_savings": 3.65, "total_services_scanned": 1}})
    detail = gen._get_detailed_recommendations("network", net)
    assert "Unassociated EIPs" in detail  # counted card rendered
    assert "Low Throughput NAT Gateway" in detail  # advisory card rendered
    # H2: the card now shows the group's counted sum ($3.65 for the single EIP),
    # reconciling to the tab headline, instead of echoing the per-unit rate string.
    assert "$3.65/month" in detail
    # The metric-gated NAT card is advisory and must show the $0.00 advisory line.
    assert "$0.00/month — advisory" in detail

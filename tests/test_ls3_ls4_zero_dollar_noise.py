"""LS-3 / LS-4 — cards that carry no realizable dollar.

Both were raised by the first live scan (2026-08-10, account 597637668689),
which produced 52 advisory recs and $0.00 counted.

**LS-3** — `missing_gateway_endpoints` emits "Create S3/DynamoDB Gateway
endpoint **to reduce NAT Gateway costs**" for any VPC lacking the endpoint, with
no NAT gate. Verified live: 0 NAT gateways in the account, only the default VPC,
both cards still emitted. With no NAT there is no data-processing charge to
avoid, so the stated mechanism does not exist. Every AWS account has a default
VPC with no gateway endpoints, so this was 2 permanent noise cards on every scan
of every account in every region.

**LS-4** — two `$0` best-practice nudges outside the strictly-cost scope:

* *Never-Expiring Log Groups.* The filed finding claimed `stored_GB x rate` was
  the missing dollar. **That claim is wrong**, and the code already refutes it
  (monitoring H2): setting a retention policy deletes only data OLDER than the
  chosen window, and `describe_log_groups` exposes no age distribution, so
  charging 100% of `storedBytes` fabricates a saving.

  There IS a provable subset, which is what these tests pin: if the ingestion
  read SUCCEEDED and reported no bytes at all over the window, then every stored
  byte is already older than that window, so setting retention to it deletes all
  of them and 100% of the storage cost is genuinely realizable. Live-verified
  polarity (2026-08-10): `AWS/Logs IncomingBytes` publishes only when data
  arrives, and `GetMetricData` returns `StatusCode=Complete` with an EMPTY
  `Values` list for a silent group — so "proven silent" is distinguishable from
  "not measured", which is the whole basis of the counted dollar.

* *Versioning Optimization.* "Monitor versioning growth" delegates the analysis
  back to the user. Noncurrent-version bytes are not exposed by any free API
  (`BucketSizeBytes` folds them into the storage-class totals; only S3 Storage
  Lens advanced metrics break them out), and the card fired on versioning being
  ENABLED rather than on any measured noncurrent bytes. Deleted, along with its
  per-bucket API call — the same call already made six lines below for the
  cross-region-replication and access-logging nudges.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services.monitoring import CW_LOGS_GB_MONTH, get_cloudwatch_checks
from services.vpc_endpoints import get_vpc_endpoints_checks

_GB = 1024**3


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kw: Any) -> list[dict[str, Any]]:
        return self._pages


class _FakeEc2:
    def __init__(self, vpcs: list[dict[str, Any]], endpoints: list[dict[str, Any]]) -> None:
        self._vpcs = vpcs
        self._endpoints = endpoints

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_vpcs":
            return _FakePaginator([{"Vpcs": self._vpcs}])
        if name == "describe_vpc_endpoints":
            return _FakePaginator([{"VpcEndpoints": self._endpoints}])
        raise KeyError(name)


def _ctx(ec2: Any = None, clients: dict[str, Any] | None = None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(get_vpc_endpoint_monthly_price=lambda: 7.30),
        pricing_multiplier=1.0,
        fast_mode=False,
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service="", **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service="", action=None, **k: ctx.permissions.append(msg)
    pool = dict(clients or {})
    if ec2 is not None:
        pool["ec2"] = ec2
    ctx.client = lambda name, region=None: pool.get(name)
    return ctx


def _missing(out: dict[str, Any]) -> list[dict[str, Any]]:
    return out.get("missing_gateway_endpoints", [])


# --------------------------------------------------------------------------- #
# LS-3 — the NAT gate
# --------------------------------------------------------------------------- #
def test_no_nat_in_the_vpc_emits_no_gateway_endpoint_card() -> None:
    """The live default-VPC case: no NAT anywhere, so no data-processing charge
    exists to avoid and the recommendation's own stated mechanism is absent."""
    ec2 = _FakeEc2([{"VpcId": "vpc-default"}], [])
    out = get_vpc_endpoints_checks(_ctx(ec2), nat_vpc_ids=set())
    assert _missing(out) == []


def test_a_nat_bearing_vpc_still_gets_both_cards() -> None:
    ec2 = _FakeEc2([{"VpcId": "vpc-a"}], [])
    out = get_vpc_endpoints_checks(_ctx(ec2), nat_vpc_ids={"vpc-a"})
    assert sorted(r["MissingService"] for r in _missing(out)) == ["DynamoDB", "S3"]


def test_the_gate_is_per_vpc_not_per_account() -> None:
    """A NAT in one VPC says nothing about a different VPC's egress path."""
    ec2 = _FakeEc2([{"VpcId": "vpc-with-nat"}, {"VpcId": "vpc-without"}], [])
    out = get_vpc_endpoints_checks(_ctx(ec2), nat_vpc_ids={"vpc-with-nat"})
    assert {r["VpcId"] for r in _missing(out)} == {"vpc-with-nat"}


def test_unknown_nat_topology_does_not_suppress() -> None:
    """``None`` means the NAT enumeration FAILED, which is not evidence of
    absence. Fail open: suppressing on a failed read would silently delete a
    finding, the harder failure to notice."""
    ec2 = _FakeEc2([{"VpcId": "vpc-a"}], [])
    out = get_vpc_endpoints_checks(_ctx(ec2), nat_vpc_ids=None)
    assert len(_missing(out)) == 2


def test_an_existing_gateway_endpoint_still_suppresses_its_own_card() -> None:
    """Regression guard: the NAT gate is additional to the pre-existing one."""
    ec2 = _FakeEc2(
        [{"VpcId": "vpc-a"}],
        [{"VpcEndpointId": "vpce-1", "VpcId": "vpc-a", "VpcEndpointType": "Gateway",
          "ServiceName": "com.amazonaws.us-east-1.s3", "State": "available"}],
    )
    out = get_vpc_endpoints_checks(_ctx(ec2), nat_vpc_ids={"vpc-a"})
    assert [r["MissingService"] for r in _missing(out)] == ["DynamoDB"]


def test_network_adapter_passes_the_nat_topology_through() -> None:
    """End-to-end: the adapter already resolves the NAT->VPC map for CoH
    attribution, so the gate costs no extra API call."""
    import services.adapters.network as net

    seen: dict[str, Any] = {}

    def _spy(ctx: Any, nat_vpc_ids: Any = None) -> dict[str, Any]:
        seen["ids"] = nat_vpc_ids
        return {"recommendations": []}

    net_ctx = _ctx(clients={})
    net_ctx.cost_hub_splits = {}
    net_ctx.region = "us-east-1"

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(net, "get_vpc_endpoints_checks", _spy)
        mp.setattr(net, "get_elastic_ip_checks", lambda c: {"recommendations": []})
        mp.setattr(net, "get_load_balancer_checks", lambda c: {"recommendations": []})
        mp.setattr(net, "get_auto_scaling_checks", lambda c: {"recommendations": []})
        mp.setattr(
            net, "get_nat_gateway_checks",
            lambda c, exclude_nat_ids=None: {
                "recommendations": [],
                "nat_vpc_map": {"nat-1": "vpc-a", "nat-2": "vpc-a", "nat-3": "vpc-b"},
            },
        )
        net.NetworkModule().scan(net_ctx)
    finally:
        mp.undo()

    assert seen["ids"] == {"vpc-a", "vpc-b"}


def test_network_adapter_passes_none_when_nat_enumeration_fails() -> None:
    import services.adapters.network as net

    seen: dict[str, Any] = {}

    def _spy(ctx: Any, nat_vpc_ids: Any = None) -> dict[str, Any]:
        seen["ids"] = nat_vpc_ids
        return {"recommendations": []}

    def _boom(c: Any, exclude_nat_ids: Any = None) -> dict[str, Any]:
        raise Exception("AccessDeniedException")

    net_ctx = _ctx(clients={})
    net_ctx.cost_hub_splits = {}
    net_ctx.region = "us-east-1"

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(net, "get_vpc_endpoints_checks", _spy)
        mp.setattr(net, "get_elastic_ip_checks", lambda c: {"recommendations": []})
        mp.setattr(net, "get_load_balancer_checks", lambda c: {"recommendations": []})
        mp.setattr(net, "get_auto_scaling_checks", lambda c: {"recommendations": []})
        mp.setattr(net, "get_nat_gateway_checks", _boom)
        net.NetworkModule().scan(net_ctx)
    finally:
        mp.undo()

    assert seen["ids"] is None


# --------------------------------------------------------------------------- #
# LS-4a — never-expiring log groups
# --------------------------------------------------------------------------- #
class _Logs:
    def __init__(self, groups: list[dict[str, Any]]) -> None:
        self._groups = groups

    def describe_log_groups(self, **_kw: Any) -> dict[str, Any]:
        return {"logGroups": self._groups}


class _Cw:
    """CloudWatch stub speaking the live GetMetricData shape.

    ``silent`` names groups CloudWatch answered for with an EMPTY Values list —
    verified live as StatusCode=Complete, i.e. the read worked and nothing was
    ingested. A name in neither map gets no result row at all: not measured.
    """

    def __init__(self, ingested: dict[str, float] | None = None,
                 silent: set[str] | None = None) -> None:
        self._ingested = ingested or {}
        self._silent = silent or set()

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator([{"Metrics": []}] if name == "list_metrics" else [{"MetricAlarms": []}])

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        results = []
        for q in kwargs.get("MetricDataQueries", []):
            name = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            if name in self._ingested:
                results.append({"Id": q["Id"], "StatusCode": "Complete",
                                "Values": [self._ingested[name]]})
            elif name in self._silent:
                results.append({"Id": q["Id"], "StatusCode": "Complete", "Values": []})
        return {"MetricDataResults": results}


def _logs_ctx(groups: list[dict[str, Any]], cw: _Cw) -> SimpleNamespace:
    return _ctx(clients={"logs": _Logs(groups), "cloudwatch": cw})


def _never_expiring(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("never_expiring_logs", [])


def test_a_provably_silent_group_counts_its_whole_storage_cost() -> None:
    """No bytes ingested in the window => every stored byte predates it, so
    setting retention to the window deletes all of them. 100% IS realizable
    here — which is exactly why monitoring H2 forbade assuming it elsewhere."""
    groups = [{"logGroupName": "/app/dead", "retentionInDays": None, "storedBytes": 40 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw(silent={"/app/dead"})),
                                   pricing_multiplier=1.0)
    recs = _never_expiring(result)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is True
    assert rec["EstimatedMonthlySavings"] == pytest.approx(40 * CW_LOGS_GB_MONTH)
    assert "AuditBasis" in rec


def test_an_active_group_is_never_counted() -> None:
    """Bytes are still arriving, so an unknown share of storedBytes is younger
    than any retention window — the H2 fabrication this must not re-open."""
    groups = [{"logGroupName": "/app/live", "retentionInDays": None, "storedBytes": 500 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw(ingested={"/app/live": 5 * _GB})),
                                   pricing_multiplier=1.0)
    recs = _never_expiring(result)
    assert len(recs) == 1
    assert recs[0]["Counted"] is False
    assert recs[0]["EstimatedMonthlySavings"] == 0.0


def test_an_unmeasured_group_is_never_counted() -> None:
    """No result row = the read did not answer for this group. Absence of
    evidence must not become evidence of silence (the LS-8 pager lesson in
    another costume)."""
    groups = [{"logGroupName": "/app/unknown", "retentionInDays": None, "storedBytes": 500 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw()), pricing_multiplier=1.0)
    recs = _never_expiring(result)
    assert len(recs) == 1
    assert recs[0]["Counted"] is False


def test_the_advisory_carries_the_measured_ceiling() -> None:
    """LS-4's real complaint: '$0.00, no figure' is a nudge. The storage cost IS
    measurable and bounds the saving, so it renders as an explicit ceiling."""
    groups = [{"logGroupName": "/app/live", "retentionInDays": None, "storedBytes": 500 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw(ingested={"/app/live": 5 * _GB})),
                                   pricing_multiplier=1.0)
    rec = _never_expiring(result)[0]
    assert rec["PotentialMonthlySavings"] == pytest.approx(500 * CW_LOGS_GB_MONTH)
    assert "15.00" in rec["EstimatedSavings"]


def test_a_group_whose_figure_rounds_to_zero_emits_nothing() -> None:
    """A card whose own dollar renders as $0.00 refutes itself (LS-5). The live
    account's 21 never-expiring groups were all Lambda logs of a few KB —
    ~$0.0000006/mo each, which is noise however it is labelled."""
    groups = [
        {"logGroupName": "/aws/lambda/tiny-dead", "retentionInDays": None, "storedBytes": 20_387},
        {"logGroupName": "/aws/lambda/tiny-live", "retentionInDays": None, "storedBytes": 14_105},
    ]
    cw = _Cw(ingested={"/aws/lambda/tiny-live": 1024.0}, silent={"/aws/lambda/tiny-dead"})
    result = get_cloudwatch_checks(_logs_ctx(groups, cw), pricing_multiplier=1.0)
    assert _never_expiring(result) == []


def test_a_group_with_a_retention_policy_is_untouched() -> None:
    groups = [{"logGroupName": "/app/ok", "retentionInDays": 30, "storedBytes": 80 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw(silent={"/app/ok"})),
                                   pricing_multiplier=1.0)
    assert _never_expiring(result) == []


def test_fast_mode_never_counts() -> None:
    """--fast skips the ingestion probe, so nothing is proven silent."""
    groups = [{"logGroupName": "/app/dead", "retentionInDays": None, "storedBytes": 40 * _GB}]
    ctx = _logs_ctx(groups, _Cw(silent={"/app/dead"}))
    ctx.fast_mode = True
    recs = _never_expiring(get_cloudwatch_checks(ctx, pricing_multiplier=1.0))
    assert len(recs) == 1 and recs[0]["Counted"] is False


def test_regional_rate_scaling_applies_to_the_counted_dollar() -> None:
    groups = [{"logGroupName": "/app/dead", "retentionInDays": None, "storedBytes": 40 * _GB}]
    result = get_cloudwatch_checks(_logs_ctx(groups, _Cw(silent={"/app/dead"})),
                                   pricing_multiplier=1.08)
    assert _never_expiring(result)[0]["EstimatedMonthlySavings"] == pytest.approx(
        round(40 * CW_LOGS_GB_MONTH * 1.08, 2)
    )


# --------------------------------------------------------------------------- #
# LS-4b — the versioning nudge is gone
# --------------------------------------------------------------------------- #
class _S3:
    """S3 stub whose versioning call fails the test if it is ever made."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def list_buckets(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {"Buckets": [{"Name": "b1", "CreationDate": datetime(2020, 1, 1, tzinfo=UTC)}]}

    def get_bucket_location(self, **_kw: Any) -> dict[str, Any]:
        return {"LocationConstraint": None}

    def get_bucket_versioning(self, **_kw: Any) -> dict[str, Any]:
        self._calls.append("get_bucket_versioning")
        return {"Status": "Enabled"}

    def __getattr__(self, name: str) -> Any:
        def _stub(**_kw: Any) -> dict[str, Any]:
            self._calls.append(name)
            return {}

        return _stub


def test_versioning_nudge_and_its_api_call_are_both_gone() -> None:
    """The card fired on versioning being ENABLED, never on measured noncurrent
    bytes, so it could not tell a bucket wasting money on old versions from one
    with none — and the per-bucket call bought nothing."""
    from services.s3 import get_enhanced_s3_checks

    calls: list[str] = []
    ctx = _ctx(clients={"s3": _S3(calls)})
    ctx.region = "us-east-1"
    out = get_enhanced_s3_checks(ctx, pricing_multiplier=1.0)

    assert "versioning_growth" not in out
    assert not any(r.get("CheckCategory") == "Versioning Optimization"
                   for r in out.get("recommendations", []))
    assert "get_bucket_versioning" not in calls


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))

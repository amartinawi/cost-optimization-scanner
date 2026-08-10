"""Unit tests for the Monitoring/Route53 HIGH cost-audit fixes (H2, H3, H4).

Drives the pure shim logic (``get_cloudwatch_checks`` / ``get_route53_checks``)
with a ``SimpleNamespace`` ctx + fake boto3 clients/paginators, plus the
``MonitoringModule.scan()`` path with monkeypatched helpers, so every counted
dollar (or advisory $0) is proven by an explicit assertion rather than inferred
from a golden fixture.

Covered findings:

  - monitoring H2  never_expiring_logs charges 100% of storedBytes with no age
    evidence → demoted to a $0 advisory (Counted=False), S3-style.
  - monitoring H3  unused_custom_metrics drove its removable quantity from a
    fabricated count//2 → now driven by a measured staleness signal
    (GetMetricData: metrics with no datapoints over N days). No evidence (fast
    mode / API failure) → $0 advisory; never count//2.
  - monitoring H4  a low-record private zone that is also a duplicate was
    counted in BOTH unused_hosted_zones and duplicate_private_zones → deduped by
    normalized HostedZoneId so each zone's monthly $ is summed exactly once.

Load-bearing rates re-verified live against the AWS Pricing API (2026-06-27):
  - CloudWatch custom metrics  SKU KG586CTNGQ4VRZKZ  $0.30/$0.10/$0.05 @10k/250k/1M
  - Route 53 hosted zones      SKU KVPGEJE88UW8S779  $0.50 first 25, $0.10 extra
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.monitoring as monitoring_adapter
from services.monitoring import (
    CW_CUSTOM_METRIC_TIER_1,
    _cw_custom_metrics_monthly_cost,
    get_cloudtrail_checks,
    get_cloudwatch_checks,
)
from services.route53 import _normalize_zone_id, get_route53_checks


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs: Any):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeRecordPaginator:
    """Paginator for list_resource_record_sets (keyed by HostedZoneId)."""

    def __init__(self, records_by_zone: dict[str, list[dict[str, Any]]]) -> None:
        self._records_by_zone = records_by_zone

    def paginate(self, HostedZoneId: str | None = None, **_kwargs: Any):  # noqa: N803,ANN201
        return iter([{"ResourceRecordSets": self._records_by_zone.get(HostedZoneId, [])}])


class _FakeLogsClient:
    def __init__(self, log_groups: list[dict[str, Any]]) -> None:
        self._log_groups = log_groups

    def describe_log_groups(self, **_kwargs: Any) -> dict[str, Any]:
        return {"logGroups": self._log_groups}


class _FakeCloudWatchClient:
    def __init__(
        self,
        metrics: list[dict[str, Any]] | None = None,
        alarms: list[dict[str, Any]] | None = None,
        metric_data_fn: Any = None,
    ) -> None:
        self._metrics = metrics or []
        self._alarms = alarms or []
        self._metric_data_fn = metric_data_fn
        self.get_metric_data_calls = 0
        self.list_metrics_paginate_calls = 0
        self.describe_alarms_paginate_calls = 0

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_alarms":
            self.describe_alarms_paginate_calls += 1
            return _FakePaginator([{"MetricAlarms": self._alarms}])
        if name == "list_metrics":
            self.list_metrics_paginate_calls += 1
            return _FakePaginator([{"Metrics": self._metrics}])
        raise KeyError(name)

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.get_metric_data_calls += 1
        if self._metric_data_fn is None:
            return {"MetricDataResults": []}
        return self._metric_data_fn(**kwargs)


class _FakeRoute53Client:
    def __init__(
        self,
        hosted_zones: list[dict[str, Any]],
        records_by_zone: dict[str, list[dict[str, Any]]] | None = None,
        health_checks: list[dict[str, Any]] | None = None,
    ) -> None:
        self._hosted_zones = hosted_zones
        self._records_by_zone = records_by_zone or {}
        self._health_checks = health_checks or []

    def get_paginator(self, name: str):  # noqa: ANN201 - boto3 shape
        if name == "list_hosted_zones":
            return _FakePaginator([{"HostedZones": self._hosted_zones}])
        if name == "list_health_checks":
            return _FakePaginator([{"HealthChecks": self._health_checks}])
        if name == "list_resource_record_sets":
            return _FakeRecordPaginator(self._records_by_zone)
        raise KeyError(name)


class _FakeCloudTrailClient:
    """Records whether any CloudTrail API was invoked (it must not be)."""

    def __init__(self) -> None:
        self.describe_trails_calls = 0
        self.get_event_selectors_calls = 0

    def describe_trails(self, **_kwargs: Any) -> dict[str, Any]:
        self.describe_trails_calls += 1
        return {"trailList": [{"Name": "t1", "TrailARN": "arn", "IsMultiRegionTrail": True}]}

    def get_event_selectors(self, **_kwargs: Any) -> dict[str, Any]:
        self.get_event_selectors_calls += 1
        return {"EventSelectors": []}


def _shim_ctx(clients: dict[str, Any], *, fast_mode: bool = False) -> SimpleNamespace:
    ctx = SimpleNamespace(fast_mode=fast_mode, warnings=[], permissions=[])
    ctx.warn = lambda msg, service=None, **_k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **_k: ctx.permissions.append(
        (service, action, msg)
    )
    ctx.client = lambda name, region=None: clients[name]
    return ctx


def _counted_sum(recs: list[dict[str, Any]]) -> float:
    """Mirror MonitoringModule.scan(): sum only recs that aren't Counted=False."""
    return sum(
        float(r.get("EstimatedMonthlySavings", 0.0) or 0.0)
        for r in recs
        if r.get("Counted", True)
    )


def _metric_data_fn(active_names: set[str]) -> Any:
    """Return a get_metric_data stub: a metric whose MetricName is in
    `active_names` reports a datapoint, otherwise empty Values (=stale)."""

    def fn(MetricDataQueries: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:  # noqa: N803
        results = []
        for q in MetricDataQueries:
            name = q["MetricStat"]["Metric"]["MetricName"]
            values = [42.0] if name in active_names else []
            results.append({"Id": q["Id"], "Values": values})
        return {"MetricDataResults": results}

    return fn


def _custom_metrics(namespace: str, n: int) -> list[dict[str, Any]]:
    return [
        {"Namespace": namespace, "MetricName": f"m{i}", "Dimensions": [{"Name": "d", "Value": str(i)}]}
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# monitoring H2 — never_expiring_logs is a $0 advisory (no age evidence)
# --------------------------------------------------------------------------- #
def test_never_expiring_logs_is_zero_advisory() -> None:
    logs = _FakeLogsClient(
        [{"logGroupName": "/app/huge", "retentionInDays": None, "storedBytes": 500 * 1024**3}]
    )
    cw = _FakeCloudWatchClient()
    ctx = _shim_ctx({"logs": logs, "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    recs = result["never_expiring_logs"]
    assert len(recs) == 1
    rec = recs[0]
    # 500 GB at $0.03/GB would have fabricated $15/mo; now $0 advisory.
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    assert "AuditBasis" in rec
    assert _counted_sum(result["recommendations"]) == 0.0


# --------------------------------------------------------------------------- #
# monitoring H3 — removable quantity is measured staleness, never count//2
# --------------------------------------------------------------------------- #
def test_custom_metric_cost_applies_fourth_tier_above_1m() -> None:
    # monitoring L2: the 4th tier ($0.02 above 1M) caps the marginal rate; the
    # old 3-tier code charged everything above 250k at $0.05, overstating cost.
    assert _cw_custom_metrics_monthly_cost(5_000) == pytest.approx(1_500.0)
    assert _cw_custom_metrics_monthly_cost(250_000) == pytest.approx(27_000.0)
    # 3k + 24k + 750k*0.05 = 64,500 at exactly 1M.
    assert _cw_custom_metrics_monthly_cost(1_000_000) == pytest.approx(64_500.0)
    # 64,500 + 1,000,000*0.02 = 84,500 at 2M (NOT the old 114,500).
    assert _cw_custom_metrics_monthly_cost(2_000_000) == pytest.approx(84_500.0)
    old_flat_tier3 = 27_000.0 + (2_000_000 - 250_000) * 0.05  # 114,500
    assert _cw_custom_metrics_monthly_cost(2_000_000) < old_flat_tier3


def test_custom_metrics_advisory_reports_measured_spend_never_counts() -> None:
    """MON-1: CloudWatch bills per PutMetricData-ACTIVE metric-month — an idle
    metric is already free and there is no DeleteMetric API, so a counted
    "stale metrics removable" dollar was a phantom (and ListMetrics' ~2-week
    horizon meant the stale set was empty on healthy accounts, non-empty
    exactly when the GetMetricData probe broke — MON-2). The lever is now a
    measured-SPEND advisory: $0 counted, the billed figure in AuditBasis."""
    metrics = _custom_metrics("MyApp", 120)
    cw = _FakeCloudWatchClient(metrics=metrics)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    recs = result["unused_custom_metrics"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    # Measured spend (120 x $0.30 = $36.00) surfaces as billed estimate.
    assert rec["AuditBasis"]["billed_monthly_estimate"] == pytest.approx(36.0)
    assert _counted_sum(result["recommendations"]) == 0.0
    # MON-2: the fragile GetMetricData staleness probe is gone entirely.
    assert cw.get_metric_data_calls == 0


def test_custom_metrics_spend_is_proportional_and_order_independent() -> None:
    # AWS tiers custom metrics account-wide per region ($0.30 first 10k, then
    # $0.10): cost(12000) = $3200, NOT 12000*0.30 = $3600. Each namespace gets
    # its PROPORTIONAL share — a marginal off-the-top walk priced identical
    # namespaces differently depending on sort position (review suggestion 3).
    metrics = _custom_metrics("nsA", 8000) + _custom_metrics("nsB", 4000)
    cw = _FakeCloudWatchClient(metrics=metrics)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    recs = {r["Namespace"]: r for r in result["unused_custom_metrics"]}
    assert recs["nsA"]["AuditBasis"]["billed_monthly_estimate"] == pytest.approx(3200.0 * 8 / 12, abs=0.01)
    assert recs["nsB"]["AuditBasis"]["billed_monthly_estimate"] == pytest.approx(3200.0 * 4 / 12, abs=0.01)
    total_billed = sum(r["AuditBasis"]["billed_monthly_estimate"] for r in recs.values())
    assert total_billed == pytest.approx(3200.0, abs=0.02)
    # Nothing counted, ever.
    assert _counted_sum(result["recommendations"]) == 0.0


def test_custom_metrics_advisory_gated_on_spend_floor_not_cardinality() -> None:
    # Review suggestion 4: the old >100-METRIC gate hid a 99-metric namespace
    # (~$29.70/mo) while surfacing trivial ones on huge accounts. The gate is
    # now a spend floor.
    metrics = _custom_metrics("worth-a-look", 99) + _custom_metrics("tiny", 10)
    cw = _FakeCloudWatchClient(metrics=metrics)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    names = [r["Namespace"] for r in result["unused_custom_metrics"]]
    assert "worth-a-look" in names  # ~$29.70/mo >= $10 floor
    assert "tiny" not in names      # ~$3/mo < floor


def test_custom_metrics_spend_estimate_region_scaled() -> None:
    metrics = _custom_metrics("MyApp", 120)
    cw = _FakeCloudWatchClient(metrics=metrics)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.25)
    rec = result["unused_custom_metrics"][0]
    assert rec["AuditBasis"]["billed_monthly_estimate"] == pytest.approx(36.0 * 1.25)
    assert rec["EstimatedMonthlySavings"] == 0.0


def test_fast_mode_skips_cloudwatch_describe_paginators() -> None:
    # L3 — MonitoringModule declares reads_fast_mode=True, so under --fast the
    # shim must skip the list_metrics + describe_alarms paginators (the full
    # CloudWatch describes), not just the GetMetricData staleness probe. The
    # custom-metrics branch therefore emits no recs and no metric reads occur.
    metrics = _custom_metrics("MyApp", 200)
    alarms = [{"AlarmName": "a", "StateReason": "x"}]
    cw = _FakeCloudWatchClient(metrics=metrics, alarms=alarms, metric_data_fn=_metric_data_fn(set()))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw}, fast_mode=True)

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    assert result["unused_custom_metrics"] == []
    # No expensive CloudWatch API calls under fast mode.
    assert cw.get_metric_data_calls == 0
    assert cw.list_metrics_paginate_calls == 0
    assert cw.describe_alarms_paginate_calls == 0


def test_no_get_metric_data_probe_remains() -> None:
    """MON-2: the staleness probe (whose broken reads manufactured the stale
    set) is deleted — the custom-metric block must not call GetMetricData."""
    metrics = _custom_metrics("MyApp", 200)

    def boom(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("GetMetricData must not be called")

    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=boom)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    rec = result["unused_custom_metrics"][0]
    assert rec["Counted"] is False
    assert cw.get_metric_data_calls == 0


# --------------------------------------------------------------------------- #
# MON-7 — the Route 53 tier ladder must descend as zones are claimed.
#
# Route 53 bills the first 25 hosted zones at $0.50 and the rest at $0.10.
# Pricing every removable zone against the SAME starting count made the ladder
# stand still: with 26 zones and 3 removable, all three priced at the $0.10
# tier when only the first one sits there.
# --------------------------------------------------------------------------- #
def _r53_ctx(zones):
    from types import SimpleNamespace

    class _R53:
        def get_paginator(self, name):
            return SimpleNamespace(paginate=lambda **kw: [{"HostedZones": zones}])

        def list_resource_record_sets(self, **kw):
            return {"ResourceRecordSets": []}

        def list_health_checks(self, **kw):
            return {"HealthChecks": []}

    ctx = SimpleNamespace(region="us-east-1", fast_mode=False, warnings=[])
    ctx.client = lambda name, region=None: _R53()
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None
    return ctx


def _zone(i, records=1):
    return {
        "Id": f"/hostedzone/Z{i}",
        "Name": f"z{i}.example.com.",
        "Config": {"PrivateZone": False},
        "ResourceRecordSetCount": records,
    }


def test_zone_ladder_descends_as_zones_are_claimed() -> None:
    """26 zones, 3 removable: the first sits in the $0.10 tier, the next two
    fall back into the $0.50 tier as the count descends. $1.10, not $0.30."""
    from services.route53 import get_route53_checks

    zones = [_zone(i, records=1) for i in range(3)] + [_zone(i, records=9) for i in range(3, 26)]
    out = get_route53_checks(_r53_ctx(zones), 1.0)
    unused = out["unused_hosted_zones"]
    assert len(unused) == 3
    assert sum(r["EstimatedMonthlySavings"] for r in unused) == pytest.approx(1.10, abs=0.01)


def test_zone_ladder_matches_the_batch_calculation() -> None:
    """Walking one zone at a time must equal pricing the batch in one call."""
    from services.route53 import _route53_zone_monthly_cost

    walked, base = 0.0, 26
    for _ in range(3):
        walked += _route53_zone_monthly_cost(1, base_zones_in_account=base)
        base -= 1
    assert walked == pytest.approx(_route53_zone_monthly_cost(3, base_zones_in_account=26))


def test_small_account_prices_every_zone_at_tier_one() -> None:
    """Below the 25-zone limit nothing changes: every removable zone is $0.50."""
    from services.route53 import get_route53_checks

    zones = [_zone(i, records=1) for i in range(3)]
    out = get_route53_checks(_r53_ctx(zones), 1.0)
    assert sum(r["EstimatedMonthlySavings"] for r in out["unused_hosted_zones"]) == pytest.approx(1.50)


# --------------------------------------------------------------------------- #
# MON-8 — the duplicate-private-zone lever counted a dollar while its own
# recommendation text asked the reader to "check VPC associations".
#
# Two same-named private zones attached to DIFFERENT VPCs are split-horizon
# DNS: a correct, common design that cannot be consolidated at all.
# --------------------------------------------------------------------------- #
def _dup_ctx(zone_vpcs, *, get_error=None):
    """zone_vpcs: {zone id -> [vpc ids]}; two zones share the name dup.example.com."""
    from types import SimpleNamespace

    zones = [
        {
            "Id": f"/hostedzone/{zid}",
            "Name": "dup.example.com.",
            "Config": {"PrivateZone": True},
            "ResourceRecordSetCount": 40,
        }
        for zid in zone_vpcs
    ]

    class _R53:
        def get_paginator(self, name):
            return SimpleNamespace(paginate=lambda **kw: [{"HostedZones": zones}])

        def list_resource_record_sets(self, **kw):
            return {"ResourceRecordSets": []}

        def list_health_checks(self, **kw):
            return {"HealthChecks": []}

        def get_hosted_zone(self, Id):
            if get_error is not None:
                raise get_error
            return {"VPCs": [{"VPCRegion": "us-east-1", "VPCId": v} for v in zone_vpcs[Id]]}

    ctx = SimpleNamespace(region="us-east-1", fast_mode=False, warnings=[])
    ctx.client = lambda name, region=None: _R53()
    ctx.warn = lambda msg, service=None: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None: None
    return ctx


def _dup_rec(ctx):
    from services.route53 import get_route53_checks

    out = get_route53_checks(ctx, 1.0)
    return out["duplicate_private_zones"][0]


def test_zones_sharing_a_vpc_are_counted() -> None:
    """Genuinely redundant: both zones answer for the same VPC."""
    rec = _dup_rec(_dup_ctx({"Z1": ["vpc-a"], "Z2": ["vpc-a"]}))
    assert rec["Counted"] is True
    assert rec["EstimatedMonthlySavings"] > 0
    assert rec["AuditBasis"]["zones_share_a_vpc"] is True


def test_zones_on_different_vpcs_are_advisory() -> None:
    """Split-horizon DNS is not a duplicate. The old lever counted it anyway."""
    rec = _dup_rec(_dup_ctx({"Z1": ["vpc-a"], "Z2": ["vpc-b"]}))
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "split-horizon" in rec["EstimatedSavings"]
    # The figure still reaches the reader.
    assert rec["PotentialMonthlySavings"] > 0


def test_unreadable_vpc_associations_abstain() -> None:
    ctx = _dup_ctx({"Z1": ["vpc-a"], "Z2": ["vpc-a"]}, get_error=Exception("AccessDenied"))
    rec = _dup_rec(ctx)
    assert rec["Counted"] is False
    assert "could not be read" in rec["EstimatedSavings"]
    assert ctx.warnings


def test_private_zone_with_no_reported_vpcs_abstains() -> None:
    rec = _dup_rec(_dup_ctx({"Z1": [], "Z2": ["vpc-a"]}))
    assert rec["Counted"] is False

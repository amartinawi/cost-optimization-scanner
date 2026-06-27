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
    _stale_custom_metric_counts,
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

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_alarms":
            return _FakePaginator([{"MetricAlarms": self._alarms}])
        if name == "list_metrics":
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


def test_unused_custom_metrics_counts_only_stale_metrics() -> None:
    metrics = _custom_metrics("MyApp", 120)
    # 40 active (m0..m39) → 80 stale.
    active = {f"m{i}" for i in range(40)}
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(active))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    recs = result["unused_custom_metrics"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["StaleMetricCount"] == 80
    # cost(120)-cost(40) = (120-40)*$0.30 = $24.00 (NOT count//2 → $18.00).
    expected = (_cw_custom_metrics_monthly_cost(120) - _cw_custom_metrics_monthly_cost(40))
    assert expected == pytest.approx(24.0)
    assert rec["EstimatedMonthlySavings"] == pytest.approx(24.0)
    old_count_half = _cw_custom_metrics_monthly_cost(120) - _cw_custom_metrics_monthly_cost(60)
    assert rec["EstimatedMonthlySavings"] != pytest.approx(old_count_half)  # not $18.00
    assert rec.get("Counted", True) is True
    assert _counted_sum(result["recommendations"]) == pytest.approx(24.0)


def test_custom_metrics_priced_at_account_wide_marginal_tier() -> None:
    # AWS tiers custom metrics account-wide per region ($0.30 first 10k, then
    # $0.10). Two high-volume namespaces (8000 + 4000 = 12000 metrics, all stale)
    # must be priced at the marginal tier, not as if each started at $0.30.
    metrics = _custom_metrics("nsA", 8000) + _custom_metrics("nsB", 4000)
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(active_names=set()))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    total = _counted_sum(result["recommendations"])

    # Account-wide marginal: cost(12000) - cost(0) = 10000*0.30 + 2000*0.10 = $3200.
    expected = _cw_custom_metrics_monthly_cost(12000) - _cw_custom_metrics_monthly_cost(0)
    assert expected == pytest.approx(3200.0)
    assert total == pytest.approx(3200.0)
    # The old per-namespace pricing (each starting at tier-1 $0.30) overstated by
    # $400: 8000*0.30 + 4000*0.30 = $3600.
    old_per_namespace = 8000 * 0.30 + 4000 * 0.30
    assert old_per_namespace == pytest.approx(3600.0)
    assert total != pytest.approx(old_per_namespace)


def test_unused_custom_metrics_region_scaled() -> None:
    metrics = _custom_metrics("MyApp", 120)
    active = {f"m{i}" for i in range(40)}  # 80 stale
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(active))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.25)
    rec = result["unused_custom_metrics"][0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(24.0 * 1.25)  # $30.00


def test_unused_custom_metrics_no_stale_is_advisory() -> None:
    metrics = _custom_metrics("MyApp", 150)
    active = {f"m{i}" for i in range(150)}  # all active → 0 stale
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(active))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    rec = result["unused_custom_metrics"][0]
    assert rec["StaleMetricCount"] == 0
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False
    assert _counted_sum(result["recommendations"]) == 0.0


def test_unused_custom_metrics_fast_mode_advisory_no_metric_reads() -> None:
    metrics = _custom_metrics("MyApp", 200)
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(set()))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw}, fast_mode=True)

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    rec = result["unused_custom_metrics"][0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False
    assert rec["StaleMetricCount"] is None
    # Fast mode must not make any GetMetricData call.
    assert cw.get_metric_data_calls == 0


def test_unused_custom_metrics_get_metric_data_denied_is_permission_and_advisory() -> None:
    metrics = _custom_metrics("MyApp", 200)

    def denied(**_kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no GetMetricData"}}, "GetMetricData"
        )

    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=denied)
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    rec = result["unused_custom_metrics"][0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["Counted"] is False
    # Permission gap classified, not swallowed.
    assert any("GetMetricData" in msg for _svc, _action, msg in ctx.permissions)
    assert ctx.warnings == []


def test_low_volume_namespace_not_probed() -> None:
    # 50 custom metrics (<=100) → no rec, no GetMetricData probe.
    metrics = _custom_metrics("Small", 50)
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(set()))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    assert result["unused_custom_metrics"] == []
    assert cw.get_metric_data_calls == 0


def test_aws_namespace_metrics_excluded() -> None:
    # 200 AWS/ metrics must not be treated as billable custom metrics.
    metrics = [
        {"Namespace": "AWS/EC2", "MetricName": f"m{i}", "Dimensions": []} for i in range(200)
    ]
    cw = _FakeCloudWatchClient(metrics=metrics, metric_data_fn=_metric_data_fn(set()))
    ctx = _shim_ctx({"logs": _FakeLogsClient([]), "cloudwatch": cw})

    result = get_cloudwatch_checks(ctx, pricing_multiplier=1.0)
    assert result["unused_custom_metrics"] == []
    assert cw.get_metric_data_calls == 0


# --------------------------------------------------------------------------- #
# monitoring H3 — staleness helper: batching + empty-Values detection
# --------------------------------------------------------------------------- #
def test_stale_helper_detects_empty_values_per_namespace() -> None:
    metrics = (
        _custom_metrics("nsA", 3)  # names m0,m1,m2
        + [{"Namespace": "nsB", "MetricName": f"b{i}", "Dimensions": []} for i in range(2)]
    )
    # Active: m0 (nsA) and b0 (nsB). Stale: m1, m2 (nsA) and b1 (nsB).
    cw = _FakeCloudWatchClient(metric_data_fn=_metric_data_fn({"m0", "b0"}))
    stale = _stale_custom_metric_counts(cw, metrics, lookback_days=30)
    assert stale == {"nsA": 2, "nsB": 1}


def test_stale_helper_batches_in_chunks_of_500() -> None:
    metrics = _custom_metrics("Big", 1001)  # 1001 → 3 GetMetricData calls
    cw = _FakeCloudWatchClient(metric_data_fn=_metric_data_fn(set()))  # all stale
    stale = _stale_custom_metric_counts(cw, metrics, lookback_days=30)
    assert stale == {"Big": 1001}
    assert cw.get_metric_data_calls == 3


def test_stale_helper_handles_get_metric_data_pagination() -> None:
    metrics = _custom_metrics("Pag", 2)

    state = {"calls": 0}

    def paged(MetricDataQueries: list[dict[str, Any]], NextToken: str | None = None, **_kw: Any):  # noqa: N803
        state["calls"] += 1
        if NextToken is None:
            # First page returns only m0 active; signal more via NextToken.
            return {
                "MetricDataResults": [{"Id": MetricDataQueries[0]["Id"], "Values": [1.0]}],
                "NextToken": "more",
            }
        # Second page returns m1 with no datapoints (stale).
        return {"MetricDataResults": [{"Id": MetricDataQueries[1]["Id"], "Values": []}]}

    cw = _FakeCloudWatchClient(metric_data_fn=paged)
    stale = _stale_custom_metric_counts(cw, metrics, lookback_days=30)
    assert stale == {"Pag": 1}  # m1 stale, m0 active
    assert state["calls"] == 2


# --------------------------------------------------------------------------- #
# monitoring H4 — Route 53 zone counted once across the two checks
# --------------------------------------------------------------------------- #
def _zone(zid: str, name: str, *, private: bool, records: int) -> dict[str, Any]:
    return {
        "Id": f"/hostedzone/{zid}",
        "Name": name,
        "Config": {"PrivateZone": private},
        "ResourceRecordSetCount": records,
    }


def test_route53_normalize_zone_id() -> None:
    assert _normalize_zone_id("/hostedzone/Z123") == "Z123"
    assert _normalize_zone_id("Z123") == "Z123"
    assert _normalize_zone_id("") == ""


def test_route53_low_record_duplicate_counted_once() -> None:
    # Two same-name private zones, BOTH nearly empty → both flagged unused.
    # The duplicate check must add $0 (every removable zone already counted).
    zones = [
        _zone("Z1", "dup.internal.", private=True, records=2),
        _zone("Z2", "dup.internal.", private=True, records=2),
    ]
    ctx = _shim_ctx({"route53": _FakeRoute53Client(zones)})
    result = get_route53_checks(ctx, pricing_multiplier=1.0)

    unused = result["unused_hosted_zones"]
    dup = result["duplicate_private_zones"]
    assert len(unused) == 2  # both zones, $0.50 each
    assert len(dup) == 1
    assert dup[0]["EstimatedMonthlySavings"] == 0.0
    assert dup[0]["Counted"] is False
    assert dup[0]["AuditBasis"]["already_counted_as_unused"] == 2
    assert dup[0]["AuditBasis"]["removable_zones"] == 0
    # Old code: 2*$0.50 (unused) + $0.50 (dup) = $1.50 (Z2 double-counted).
    # Deduped: only the two unused zones → $1.00.
    assert _counted_sum(result["recommendations"]) == pytest.approx(1.0)


def test_route53_high_record_duplicates_counted_in_duplicate_check() -> None:
    # Three same-name private zones, none nearly-empty → duplicate check owns
    # the (N-1)=2 removable zones; no overlap with unused.
    zones = [
        _zone("Z3", "dup2.internal.", private=True, records=10),
        _zone("Z4", "dup2.internal.", private=True, records=10),
        _zone("Z5", "dup2.internal.", private=True, records=10),
    ]
    ctx = _shim_ctx({"route53": _FakeRoute53Client(zones)})
    result = get_route53_checks(ctx, pricing_multiplier=1.0)

    assert result["unused_hosted_zones"] == []
    dup = result["duplicate_private_zones"]
    assert len(dup) == 1
    # 2 removable zones × $0.50 (base 3 zones < 25) = $1.00.
    assert dup[0]["EstimatedMonthlySavings"] == pytest.approx(1.0)
    assert dup[0]["AuditBasis"]["removable_zones"] == 2
    assert _counted_sum(result["recommendations"]) == pytest.approx(1.0)


def test_route53_partial_overlap_no_double_count() -> None:
    # Three same-name private zones; ONE is nearly empty (counted unused).
    # Consolidation removes 2 zones total; the unused one is one of them, so
    # the duplicate check may only add the remaining 1.
    zones = [
        _zone("Z6", "dup3.internal.", private=True, records=2),   # unused
        _zone("Z7", "dup3.internal.", private=True, records=10),
        _zone("Z8", "dup3.internal.", private=True, records=10),
    ]
    ctx = _shim_ctx({"route53": _FakeRoute53Client(zones)})
    result = get_route53_checks(ctx, pricing_multiplier=1.0)

    unused = result["unused_hosted_zones"]
    dup = result["duplicate_private_zones"]
    assert len(unused) == 1
    assert unused[0]["EstimatedMonthlySavings"] == pytest.approx(0.5)
    assert dup[0]["AuditBasis"]["already_counted_as_unused"] == 1
    assert dup[0]["AuditBasis"]["removable_zones"] == 1
    assert dup[0]["EstimatedMonthlySavings"] == pytest.approx(0.5)
    # Total: $0.50 (unused) + $0.50 (dup) = $1.00 — NOT $1.50 (old double count).
    assert _counted_sum(result["recommendations"]) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# scan() path — adapter sums only counted recs (advisory $0 excluded)
# --------------------------------------------------------------------------- #
def test_scan_excludes_advisory_dollars_from_counted_total(monkeypatch: pytest.MonkeyPatch) -> None:
    cw_recs = [
        {"EstimatedMonthlySavings": 24.0, "CheckCategory": "Excessive Custom Metrics"},  # counted
        {
            "EstimatedMonthlySavings": 0.0,
            "Counted": False,
            "CheckCategory": "Never-Expiring Log Groups",
        },  # H2 advisory
    ]
    r53_recs = [
        {"EstimatedMonthlySavings": 1.0, "CheckCategory": "Unused Hosted Zones"},  # counted
        {
            "EstimatedMonthlySavings": 0.0,
            "Counted": False,
            "CheckCategory": "Duplicate Private Zones",
        },  # H4 deduped advisory
    ]
    monkeypatch.setattr(
        monitoring_adapter,
        "get_cloudwatch_checks",
        lambda ctx, mult: {"recommendations": [dict(r) for r in cw_recs]},
    )
    monkeypatch.setattr(
        monitoring_adapter, "get_cloudtrail_checks", lambda ctx: {"recommendations": []}
    )
    monkeypatch.setattr(
        monitoring_adapter, "get_backup_checks", lambda ctx: {"recommendations": []}
    )
    monkeypatch.setattr(
        monitoring_adapter,
        "get_route53_checks",
        lambda ctx, mult: {"recommendations": [dict(r) for r in r53_recs]},
    )

    findings = monitoring_adapter.MonitoringModule().scan(SimpleNamespace(pricing_multiplier=1.0))
    # Only the two counted recs: $24.00 + $1.00 = $25.00; advisory $0 excluded.
    assert findings.total_monthly_savings == pytest.approx(25.0)


def test_tier_constant_matches_live_pricing() -> None:
    # Live AWS Pricing API (2026-06-27) tier-1 custom metric rate = $0.30.
    assert CW_CUSTOM_METRIC_TIER_1 == 0.30

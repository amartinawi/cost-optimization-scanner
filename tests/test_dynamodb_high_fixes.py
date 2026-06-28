"""HIGH cost-correctness fixes for the DynamoDB service.

Proves the three DynamoDB HIGH remediations with explicit dollar assertions,
driving both the pure logic and the ``scan()`` path with a ``SimpleNamespace``
ctx + monkeypatched enhanced-checks helpers + fake boto3 clients:

* H1 — over-provisioned savings are gated on measured low utilization and
  computed as an exact ``current - rightsized`` delta (target =
  ``ceil(avg_consumed x buffer)``); no CloudWatch evidence -> $0 advisory.
* H2 — Reserved Capacity (a commitment lever) is a $0 advisory, never summed
  into the rightsizing headline.
* H3 — GSI provisioned throughput is folded into table cost and the rightsizing
  delta; per-GSI over-provisioning is surfaced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.dynamodb as dynamodb_adapter
import services.dynamodb as ddb_shim
from services.adapters.dynamodb import (
    _DYNAMODB_RCU_HOURLY,
    _DYNAMODB_WCU_HOURLY,
    _HOURS_PER_MONTH,
    _over_provisioned_savings,
)
from services.dynamodb import (
    DYNAMODB_ADVISORY_CATEGORIES,
    _rightsize_dimension,
    _sum_gsi_throughput,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kwargs):
        yield from self._pages


class _FakeDynamoDB:
    def __init__(self, tables):
        # tables: list of Table dicts (each as returned under describe_table["Table"])
        self._tables = {t["TableName"]: t for t in tables}

    def get_paginator(self, _name):
        return _FakePaginator([{"TableNames": list(self._tables.keys())}])

    def describe_table(self, TableName):
        return {"Table": self._tables[TableName]}


class _FakeCloudWatch:
    """Returns datapoints keyed by (MetricName, TableName, GSIName-or-None).

    A missing key returns an empty Datapoints list (the genuine "no data" case);
    a registered key returns one datapoint whose Average is the configured value.
    """

    def __init__(self, points, raise_exc=None):
        self._points = points
        self._raise_exc = raise_exc

    def get_metric_statistics(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        dims = {d["Name"]: d["Value"] for d in kwargs["Dimensions"]}
        key = (kwargs["MetricName"], dims.get("TableName"), dims.get("GlobalSecondaryIndexName"))
        value = self._points.get(key)
        if value is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": value, "Maximum": value}]}


def _ctx(*, dynamodb=None, cloudwatch=None, pricing_multiplier=1.0, cost_hub_splits=None):
    warnings: list[str] = []
    permission_issues: list[str] = []
    clients = {"dynamodb": dynamodb, "cloudwatch": cloudwatch}
    return SimpleNamespace(
        pricing_multiplier=pricing_multiplier,
        region="us-east-1",
        fast_mode=False,
        cost_hub_splits=cost_hub_splits or {},
        warnings=warnings,
        permission_issues=permission_issues,
        client=lambda name, region=None: clients.get(name),
        warn=lambda message, service=None: warnings.append(message),
        permission_issue=lambda message, service=None, action=None: permission_issues.append(message),
    )


def _expected_delta(cur_r, cur_w, tgt_r, tgt_w, mult=1.0):
    current = (cur_r * _DYNAMODB_RCU_HOURLY + cur_w * _DYNAMODB_WCU_HOURLY) * _HOURS_PER_MONTH * mult
    target = (tgt_r * _DYNAMODB_RCU_HOURLY + tgt_w * _DYNAMODB_WCU_HOURLY) * _HOURS_PER_MONTH * mult
    return current - target


# --------------------------------------------------------------------------- #
# Pure logic — H3 GSI summation
# --------------------------------------------------------------------------- #
def test_sum_gsi_throughput_adds_base_plus_indexes():
    table = {
        "ProvisionedThroughput": {"ReadCapacityUnits": 200, "WriteCapacityUnits": 200},
        "GlobalSecondaryIndexes": [
            {"IndexName": "g1", "ProvisionedThroughput": {"ReadCapacityUnits": 300, "WriteCapacityUnits": 100}},
            {"IndexName": "g2", "ProvisionedThroughput": {"ReadCapacityUnits": 50, "WriteCapacityUnits": 25}},
        ],
    }
    gsi_read, gsi_write, per_gsi = _sum_gsi_throughput(table)
    assert gsi_read == 350
    assert gsi_write == 125
    assert {g["IndexName"] for g in per_gsi} == {"g1", "g2"}


def test_sum_gsi_throughput_no_indexes():
    assert _sum_gsi_throughput({"ProvisionedThroughput": {}}) == (0, 0, [])


# --------------------------------------------------------------------------- #
# Pure logic — H1 rightsizing
# --------------------------------------------------------------------------- #
def test_rightsize_dimension_low_utilization_targets_ceil_buffer():
    # capacity 200, avg 10 -> util 5% (< 20%) -> target = ceil(10 * 1.2) = 12
    target, util, is_low = _rightsize_dimension(200, 10.0)
    assert target == 12
    assert util == pytest.approx(5.0)
    assert is_low is True


def test_rightsize_dimension_acceptable_utilization_keeps_capacity():
    # capacity 100, avg 50 -> util 50% (>= 20%) -> no reduction
    target, util, is_low = _rightsize_dimension(100, 50.0)
    assert target == 100
    assert util == pytest.approx(50.0)
    assert is_low is False


def test_rightsize_dimension_no_metric_abstains():
    target, util, is_low = _rightsize_dimension(100, None)
    assert (target, util, is_low) == (100, None, False)


# --------------------------------------------------------------------------- #
# Pure logic — H1 adapter savings helper (counted only with metric + low util)
# --------------------------------------------------------------------------- #
def test_over_provisioned_savings_counts_exact_delta():
    rec = {
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "RightsizedReadCapacity": 36,
        "RightsizedWriteCapacity": 9,
        "MetricsAvailable": True,
        "LowUtilization": True,
    }
    savings, current, counted = _over_provisioned_savings(rec, 1.0)
    assert counted is True
    assert savings == pytest.approx(_expected_delta(500, 300, 36, 9), abs=1e-6)
    assert current == pytest.approx(_expected_delta(500, 300, 0, 0), abs=1e-6)


def test_over_provisioned_savings_no_metric_is_zero_advisory():
    rec = {"ReadCapacityUnits": 500, "WriteCapacityUnits": 300, "MetricsAvailable": False, "LowUtilization": False}
    savings, _current, counted = _over_provisioned_savings(rec, 1.0)
    assert counted is False


def test_over_provisioned_savings_acceptable_util_not_counted():
    # Metrics present but utilization acceptable: target == current -> delta 0.
    rec = {
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "RightsizedReadCapacity": 500,
        "RightsizedWriteCapacity": 300,
        "MetricsAvailable": True,
        "LowUtilization": False,
    }
    savings, _current, counted = _over_provisioned_savings(rec, 1.0)
    assert counted is False
    assert savings == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# scan() path — H1 counted delta
# --------------------------------------------------------------------------- #
def test_scan_counts_metric_gated_over_provisioned_delta(monkeypatch):
    over_rec = {
        "TableName": "t1",
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "RightsizedReadCapacity": 36,
        "RightsizedWriteCapacity": 9,
        "MetricsAvailable": True,
        "LowUtilization": True,
        "MetricWindowDays": 7,
        "Buffer": 1.2,
        "CheckCategory": "DynamoDB Over-Provisioned Capacity",
    }
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(
        dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": [dict(over_rec)]}
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx())
    expected = _expected_delta(500, 300, 36, 9)
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.01)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is True
    assert rec["EstimatedMonthlySavings"] == pytest.approx(round(expected, 2), abs=0.01)
    assert rec["EstimatedSavings"].startswith("$")
    assert rec["AuditBasis"]["rcu_rate_per_hr"] == _DYNAMODB_RCU_HOURLY
    assert findings.total_recommendations == 1


def test_scan_over_provisioned_without_metrics_is_advisory(monkeypatch):
    over_rec = {
        "TableName": "t1",
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "MetricsAvailable": False,
        "LowUtilization": False,
        "CheckCategory": "DynamoDB Over-Provisioned Capacity",
    }
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(
        dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": [dict(over_rec)]}
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx())
    assert findings.total_monthly_savings == 0.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "advisory" in rec["EstimatedSavings"]
    assert findings.total_recommendations == 0  # count hygiene: $0 advisory excluded


# --------------------------------------------------------------------------- #
# scan() path — H2 Reserved Capacity is advisory, never double-counted
# --------------------------------------------------------------------------- #
def test_scan_reserved_capacity_is_advisory_and_not_summed(monkeypatch):
    enhanced = [
        {
            "TableName": "t1",
            "ReadCapacityUnits": 500,
            "WriteCapacityUnits": 300,
            "CheckCategory": "DynamoDB Reserved Capacity",
        },
        {
            "TableName": "t1",
            "ReadCapacityUnits": 500,
            "WriteCapacityUnits": 300,
            "RightsizedReadCapacity": 36,
            "RightsizedWriteCapacity": 9,
            "MetricsAvailable": True,
            "LowUtilization": True,
            "CheckCategory": "DynamoDB Over-Provisioned Capacity",
        },
    ]
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(
        dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": [dict(r) for r in enhanced]}
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx())
    recs = findings.sources["enhanced_checks"].recommendations
    reserved = next(r for r in recs if r["CheckCategory"] == "DynamoDB Reserved Capacity")
    over = next(r for r in recs if r["CheckCategory"] == "DynamoDB Over-Provisioned Capacity")
    assert reserved["Counted"] is False
    assert reserved["EstimatedMonthlySavings"] == 0.0
    assert "Commitment Analysis" in reserved["EstimatedSavings"]
    assert over["Counted"] is True
    # Only the over-provisioned delta is counted; reserved adds nothing.
    assert findings.total_monthly_savings == pytest.approx(_expected_delta(500, 300, 36, 9), abs=0.01)


def test_reserved_capacity_in_advisory_categories():
    assert "DynamoDB Reserved Capacity" in DYNAMODB_ADVISORY_CATEGORIES


# --------------------------------------------------------------------------- #
# scan() path — table_analysis rows are advisory (H1: no blanket factor)
# --------------------------------------------------------------------------- #
def test_scan_table_analysis_rows_are_zero_advisory(monkeypatch):
    opt_opps = [{"TableName": "t1", "ReadCapacityUnits": 80, "WriteCapacityUnits": 40, "EstimatedMonthlyCost": 26.0}]
    monkeypatch.setattr(
        dynamodb_adapter,
        "get_dynamodb_table_analysis",
        lambda ctx: {"optimization_opportunities": [dict(r) for r in opt_opps], "total_tables": 1},
    )
    monkeypatch.setattr(dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": []})
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx())
    assert findings.total_monthly_savings == 0.0
    row = findings.sources["dynamodb_table_analysis"].recommendations[0]
    assert row["Counted"] is False
    assert row["EstimatedMonthlySavings"] == 0.0
    assert findings.total_recommendations == 0


# --------------------------------------------------------------------------- #
# Shim end-to-end — H3 GSI cost visibility + H1 metric gating + per-GSI surfacing
# --------------------------------------------------------------------------- #
_TABLE_WITH_GSI = {
    "TableName": "t1",
    "TableStatus": "ACTIVE",
    "ItemCount": 1000,
    "TableSizeBytes": 1024,
    "ProvisionedThroughput": {"ReadCapacityUnits": 200, "WriteCapacityUnits": 200},
    "GlobalSecondaryIndexes": [
        {"IndexName": "gsi1", "ProvisionedThroughput": {"ReadCapacityUnits": 300, "WriteCapacityUnits": 100}},
    ],
}


def test_table_analysis_includes_gsi_in_cost():
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(_TABLE_WITH_GSI)]))
    analysis = ddb_shim.get_dynamodb_table_analysis(ctx)
    row = analysis["optimization_opportunities"][0]
    # Total = base(200/200) + gsi(300/100) = 500 RCU / 300 WCU.
    assert row["ReadCapacityUnits"] == 500
    assert row["WriteCapacityUnits"] == 300
    assert row["BaseReadCapacityUnits"] == 200
    expected_cost = round(500 * (0.00013 * 730) + 300 * (0.00065 * 730), 2)
    assert row["EstimatedMonthlyCost"] == pytest.approx(expected_cost, abs=0.01)
    assert {g["IndexName"] for g in row["GsiThroughput"]} == {"gsi1"}


def test_enhanced_over_provisioned_low_util_builds_counted_rec_with_gsi():
    cw = _FakeCloudWatch(
        {
            ("ConsumedReadCapacityUnits", "t1", None): 10.0,   # 10/200 = 5% low
            ("ConsumedWriteCapacityUnits", "t1", None): 5.0,    # 5/200 = 2.5% low
            ("ConsumedReadCapacityUnits", "t1", "gsi1"): 20.0,  # 20/300 = 6.7% low
            ("ConsumedWriteCapacityUnits", "t1", "gsi1"): 2.0,  # 2/100 = 2% low
        }
    )
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(_TABLE_WITH_GSI)]), cloudwatch=cw)
    result = ddb_shim.get_enhanced_dynamodb_checks(ctx)
    over = next(r for r in result["recommendations"] if r["CheckCategory"] == "DynamoDB Over-Provisioned Capacity")
    assert over["ReadCapacityUnits"] == 500
    assert over["WriteCapacityUnits"] == 300
    assert over["MetricsAvailable"] is True
    assert over["LowUtilization"] is True
    # targets: base read ceil(10*1.2)=12, base write ceil(5*1.2)=6,
    # gsi read ceil(20*1.2)=24, gsi write ceil(2*1.2)=3 -> totals 36/9
    assert over["RightsizedReadCapacity"] == 36
    assert over["RightsizedWriteCapacity"] == 9
    gsi = next(g for g in over["GsiOverProvisioned"] if g["IndexName"] == "gsi1")
    assert gsi["OverProvisioned"] is True

    # The adapter then counts the exact delta over base + GSI.
    findings = _scan_with_real_shim(ctx)
    assert findings.total_monthly_savings == pytest.approx(_expected_delta(500, 300, 36, 9), abs=0.01)


def test_enhanced_over_provisioned_no_datapoints_is_advisory():
    cw = _FakeCloudWatch({})  # every metric returns empty Datapoints
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(_TABLE_WITH_GSI)]), cloudwatch=cw)
    result = ddb_shim.get_enhanced_dynamodb_checks(ctx)
    over = next(r for r in result["recommendations"] if r["CheckCategory"] == "DynamoDB Over-Provisioned Capacity")
    assert over["MetricsAvailable"] is False
    findings = _scan_with_real_shim(ctx)
    assert findings.total_monthly_savings == 0.0


def test_enhanced_over_provisioned_cw_error_classified_and_abstains():
    err = RuntimeError("ThrottlingException: rate exceeded")
    cw = _FakeCloudWatch({}, raise_exc=err)
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(_TABLE_WITH_GSI)]), cloudwatch=cw)
    result = ddb_shim.get_enhanced_dynamodb_checks(ctx)
    over = next(r for r in result["recommendations"] if r["CheckCategory"] == "DynamoDB Over-Provisioned Capacity")
    assert over["MetricsAvailable"] is False
    # Error classified onto ctx (not swallowed); non-permission -> warn.
    assert any("ThrottlingException" in w for w in ctx.warnings)
    findings = _scan_with_real_shim(ctx)
    assert findings.total_monthly_savings == 0.0


def test_enhanced_over_provisioned_access_denied_is_permission_issue():
    class _Denied(Exception):
        def __init__(self):
            super().__init__("AccessDenied")
            self.response = {"Error": {"Code": "AccessDenied"}}

    cw = _FakeCloudWatch({}, raise_exc=_Denied())
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(_TABLE_WITH_GSI)]), cloudwatch=cw)
    ddb_shim.get_enhanced_dynamodb_checks(ctx)
    assert ctx.permission_issues  # routed to permission_issue, not warn


def _scan_with_real_shim(ctx):
    """Run the adapter against the real shims using the fake-client ctx."""
    return dynamodb_adapter.DynamoDbModule().scan(ctx)


# --------------------------------------------------------------------------- #
# scan() path — L3 Cost Hub index-ARN dedup
# --------------------------------------------------------------------------- #
def test_scan_coh_index_arn_dedupes_against_covered_table(monkeypatch):
    """A CoH rec on a GSI of an already-covered table must not double-count.

    The resourceId is an index ARN (...:table/MyTable/index/MyGSI). The table
    "MyTable" is already covered by the enhanced over-provisioned rec, so the
    naive split("/")[-1] would yield "MyGSI" and fail to dedupe; the fix extracts
    "MyTable" and drops the CoH saving rather than summing it (DynamoDB L3).
    """
    over_rec = {
        "TableName": "MyTable",
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "RightsizedReadCapacity": 36,
        "RightsizedWriteCapacity": 9,
        "MetricsAvailable": True,
        "LowUtilization": True,
        "CheckCategory": "DynamoDB Over-Provisioned Capacity",
    }
    coh = [
        {
            "resourceId": "arn:aws:dynamodb:us-east-1:123456789012:table/MyTable/index/MyGSI",
            "estimatedMonthlySavings": 99.0,
        }
    ]
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(
        dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": [dict(over_rec)]}
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx(cost_hub_splits={"dynamodb": coh}))
    # CoH index-ARN rec is deduped against MyTable: dropped, not summed.
    assert findings.sources["cost_optimization_hub"].count == 0
    # Only the over-provisioned delta remains in the headline.
    assert findings.total_monthly_savings == pytest.approx(_expected_delta(500, 300, 36, 9), abs=0.01)


def test_scan_coh_index_arn_uncovered_table_is_kept(monkeypatch):
    """A CoH index-ARN rec on a table not otherwise covered is kept and summed."""
    coh = [
        {
            "resourceId": "arn:aws:dynamodb:us-east-1:123456789012:table/OtherTable/index/OtherGSI",
            "estimatedMonthlySavings": 42.0,
        }
    ]
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": []})
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx(cost_hub_splits={"dynamodb": coh}))
    assert findings.sources["cost_optimization_hub"].count == 1
    assert findings.total_monthly_savings == pytest.approx(42.0, abs=0.01)


def test_scan_coh_plain_table_arn_dedupes_against_covered_table(monkeypatch):
    """A CoH rec with a plain table ARN still dedupes against the covered table."""
    over_rec = {
        "TableName": "MyTable",
        "ReadCapacityUnits": 500,
        "WriteCapacityUnits": 300,
        "RightsizedReadCapacity": 36,
        "RightsizedWriteCapacity": 9,
        "MetricsAvailable": True,
        "LowUtilization": True,
        "CheckCategory": "DynamoDB Over-Provisioned Capacity",
    }
    coh = [
        {
            "resourceId": "arn:aws:dynamodb:us-east-1:123456789012:table/MyTable",
            "estimatedMonthlySavings": 77.0,
        }
    ]
    monkeypatch.setattr(dynamodb_adapter, "get_dynamodb_table_analysis", lambda ctx: {"optimization_opportunities": []})
    monkeypatch.setattr(
        dynamodb_adapter, "get_enhanced_dynamodb_checks", lambda ctx: {"recommendations": [dict(over_rec)]}
    )
    findings = dynamodb_adapter.DynamoDbModule().scan(_ctx(cost_hub_splits={"dynamodb": coh}))
    assert findings.sources["cost_optimization_hub"].count == 0
    assert findings.total_monthly_savings == pytest.approx(_expected_delta(500, 300, 36, 9), abs=0.01)


# --------------------------------------------------------------------------- #
# Shim — L4 ACTIVE gate in get_dynamodb_table_analysis
# --------------------------------------------------------------------------- #
def test_table_analysis_skips_non_active_tables():
    """CREATING/DELETING/UPDATING tables produce no optimization opportunities."""
    creating = {
        "TableName": "pending",
        "TableStatus": "CREATING",
        "ItemCount": 0,
        "TableSizeBytes": 0,
        "ProvisionedThroughput": {"ReadCapacityUnits": 100, "WriteCapacityUnits": 100},
    }
    ctx = _ctx(dynamodb=_FakeDynamoDB([dict(creating), dict(_TABLE_WITH_GSI)]))
    analysis = ddb_shim.get_dynamodb_table_analysis(ctx)
    surfaced = {row["TableName"] for row in analysis["optimization_opportunities"]}
    assert "pending" not in surfaced
    assert "t1" in surfaced

"""MON-3 — CloudWatch Logs ingestion, the dominant CloudWatch cost.

Standard-class ingestion is $0.50/GB and Infrequent Access is $0.25/GB
(validated against the live Pricing API: AmazonCloudWatch us-east-1,
USE1-DataProcessing-Bytes vs USE1-DataProcessingIA-Bytes, operation
PutLogEvents). Nothing measured it before.

The lever is deliberately ADVISORY, unlike the other new levers in this
tranche. AWS documents that ``logGroupClass`` "can't be changed after a log
group is created", so realizing this means creating a new log group and
repointing every producer - not a config toggle - and IA drops EMF, Live Tail,
anomaly detection and console viewing, none of which this scan can rule out.
Same call as the FSx SSD->HDD demotion: an exact figure, rendered but not
counted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.monitoring import (
    CW_LOGS_INGEST_IA_GB,
    CW_LOGS_INGEST_STANDARD_GB,
    get_cloudwatch_checks,
)

_CATEGORY = "CloudWatch Logs Class Migration"
_GB = 1024**3


class _FakeLogs:
    def __init__(self, groups: list[dict[str, Any]]) -> None:
        self._groups = groups

    def describe_log_groups(self, **kwargs: Any) -> dict[str, Any]:
        return {"logGroups": self._groups}


class _FakeCloudWatch:
    """`ingestion` maps log group name -> GB ingested (absent = no data)."""

    def __init__(self, ingestion: dict[str, float], *, error: Exception | None = None) -> None:
        self._ingestion = ingestion
        self._error = error
        self.data_calls: list[dict[str, Any]] = []

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.data_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        results = []
        for query in kwargs["MetricDataQueries"]:
            name = query["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            gb = self._ingestion.get(name)
            results.append(
                {"Id": query["Id"], "Values": [] if gb is None else [gb * _GB]}
            )
        return {"MetricDataResults": results}

    # The unrelated custom-metric / alarm legs of this check.
    def list_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return {"Metrics": []}

    def get_paginator(self, name: str) -> Any:
        return SimpleNamespace(paginate=lambda **kw: [{"Metrics": [], "MetricAlarms": []}])

    def describe_alarms(self, **kwargs: Any) -> dict[str, Any]:
        return {"MetricAlarms": []}


def _group(name: str, *, klass: str | None = "STANDARD", stored: int = 1000) -> dict[str, Any]:
    group: dict[str, Any] = {"logGroupName": name, "storedBytes": stored, "retentionInDays": 7}
    if klass is not None:
        group["logGroupClass"] = klass
    return group


def _ctx(
    groups: list[dict[str, Any]],
    ingestion: dict[str, float] | None = None,
    *,
    error: Exception | None = None,
    fast: bool = False,
) -> SimpleNamespace:
    cw = _FakeCloudWatch(ingestion or {}, error=error)
    ctx = SimpleNamespace(fast_mode=fast, region="us-east-1", warnings=[], permissions=[])
    clients = {"logs": _FakeLogs(groups), "cloudwatch": cw}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda msg, service=None: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None: ctx.permissions.append(msg)
    ctx._cw = cw
    return ctx


def _recs(ctx: SimpleNamespace, multiplier: float = 1.0) -> list[dict[str, Any]]:
    out = get_cloudwatch_checks(ctx, multiplier)
    return [r for r in out.get("log_class_migration", [])]


# --------------------------------------------------------------------------- #
# The figure
# --------------------------------------------------------------------------- #
def test_ingestion_is_priced_at_the_standard_minus_ia_delta() -> None:
    recs = _recs(_ctx([_group("/aws/lambda/app")], {"/aws/lambda/app": 400.0}))
    assert len(recs) == 1
    expected = 400.0 * (CW_LOGS_INGEST_STANDARD_GB - CW_LOGS_INGEST_IA_GB)
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(expected, abs=0.01)
    assert recs[0]["MonthlyIngestedGB"] == pytest.approx(400.0, abs=0.01)


def test_region_multiplier_scales_the_figure() -> None:
    recs = _recs(_ctx([_group("g")], {"g": 100.0}), multiplier=1.08)
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(100.0 * 0.25 * 1.08, abs=0.01)


# --------------------------------------------------------------------------- #
# Advisory, not counted
# --------------------------------------------------------------------------- #
def test_the_lever_is_advisory_and_says_why() -> None:
    rec = _recs(_ctx([_group("g")], {"g": 400.0}))[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["PotentialMonthlySavings"] > 0
    assert "cannot be changed in place" in rec["EstimatedSavings"]
    assert "cannot be changed after creation" in rec["AuditBasis"]["reason"]


# --------------------------------------------------------------------------- #
# Scope and abstention
# --------------------------------------------------------------------------- #
def test_groups_already_on_ia_are_skipped() -> None:
    ctx = _ctx([_group("g", klass="INFREQUENT_ACCESS")], {"g": 900.0})
    assert _recs(ctx) == []
    assert ctx._cw.data_calls == [], "an IA group should not even be probed"


def test_missing_class_defaults_to_standard() -> None:
    """describe_log_groups omits logGroupClass on older groups; AWS documents
    the default as STANDARD."""
    assert len(_recs(_ctx([_group("g", klass=None)], {"g": 10.0}))) == 1


def test_group_with_no_metric_data_emits_nothing() -> None:
    """An absent series is unknown volume, not zero."""
    assert _recs(_ctx([_group("g")], {})) == []


def test_zero_ingestion_emits_nothing() -> None:
    assert _recs(_ctx([_group("g")], {"g": 0.0})) == []


def test_denied_metric_read_is_classified_and_emits_nothing() -> None:
    ctx = _ctx([_group("g")], {"g": 400.0}, error=Exception("AccessDeniedException"))
    assert _recs(ctx) == []
    assert ctx.permissions


def test_fast_mode_makes_no_metric_call() -> None:
    ctx = _ctx([_group("g")], {"g": 400.0}, fast=True)
    assert _recs(ctx) == []
    assert ctx._cw.data_calls == []


# --------------------------------------------------------------------------- #
# Batching and the truncation disclosure
# --------------------------------------------------------------------------- #
def test_all_groups_are_probed_in_one_call() -> None:
    groups = [_group(f"g{i}", stored=i) for i in range(50)]
    ctx = _ctx(groups, {f"g{i}": 1.0 for i in range(50)})
    assert len(_recs(ctx)) == 50
    assert len(ctx._cw.data_calls) == 1
    assert len(ctx._cw.data_calls[0]["MetricDataQueries"]) == 50


def test_truncation_is_disclosed_and_takes_the_largest_groups() -> None:
    """No silent caps: a bounded probe must say what it left out."""
    groups = [_group(f"g{i}", stored=i) for i in range(600)]
    ctx = _ctx(groups, {f"g{i}": 1.0 for i in range(600)})
    recs = _recs(ctx)

    assert len(recs) == 500
    assert any("were not measured" in w for w in ctx.warnings)
    # Sorted by storedBytes descending, so g599 is in and g0 is out.
    names = {r["LogGroupName"] for r in recs}
    assert "g599" in names and "g0" not in names

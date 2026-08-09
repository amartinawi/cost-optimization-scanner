"""DMS-4 and DMS-1 — an unpriced rec that still counted, and a config lever
that could only be reached through a CloudWatch metric.

DMS-4: when neither the terminate nor the downsize branch could price a rec,
the fall-through appended it UNCHANGED. It carried no ``Counted`` flag (so the
headline counted it), no ``EstimatedMonthlySavings`` (so it contributed $0 -
count and dollar disagreeing, D4), and the shim's prose survived intact:
"Rightsize for ~35% savings on instance cost", a percentage nothing computed
(B3).

DMS-1: the Multi-AZ -> Single-AZ lever is a pure config finding, but the
adapter iterated the recs produced by the CPU path, so a dev Multi-AZ instance
at normal utilization produced no rec at all and its per-AZ delta (~$204/mo on
an r5.large) was never seen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest




# --------------------------------------------------------------------------- #
# DMS-4 / DMS-1 — the unpriced fall-through, and a config lever gated on a metric
# --------------------------------------------------------------------------- #
def _instance(iid: str, *, klass: str = "dms.r5.large", multi_az: bool = True, status: str = "available") -> dict:
    return {
        "ReplicationInstanceIdentifier": iid,
        "ReplicationInstanceClass": klass,
        "ReplicationInstanceStatus": status,
        "MultiAZ": multi_az,
    }


class _BusyCw:
    """Healthy CPU: no rightsizing and no unused finding, so the CPU path
    produces no rec at all."""

    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": [{"Average": 55.0}]}


class _DmsPaginator:
    def __init__(self, instances):
        self._instances = instances

    def paginate(self):
        return [{"ReplicationInstances": self._instances}]


class _FakeDms:
    def __init__(self, instances):
        self._instances = instances

    def get_paginator(self, _name):
        return _DmsPaginator(self._instances)

    def describe_replication_tasks(self, **kwargs):
        return {"ReplicationTasks": []}


class _DmsPricing:
    """Multi-AZ is exactly 2x Single-AZ, as validated for dms.t3.medium."""

    def get_dms_instance_monthly_price(self, instance_class, multi_az=False):
        base = {"dms.r5.large": 204.0, "dms.t3.medium": 54.39}.get(instance_class, 0.0)
        return base * (2 if multi_az else 1)


def _dms_ctx(instances, *, pricing=None):
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        pricing_engine=pricing if pricing is not None else _DmsPricing(),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=False,
        warnings=[],
        permissions=[],
    )
    clients = {"dms": _FakeDms(instances), "cloudwatch": _BusyCw()}
    ctx.client = lambda name, region=None, **kw: clients.get(name)
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    return ctx


def test_multi_az_lever_fires_without_a_cpu_finding() -> None:
    """DMS-1: the lever iterated recs produced by the CloudWatch CPU path, so a
    dev Multi-AZ instance at normal utilization never reached it."""
    from services.adapters.dms import DmsModule

    findings = DmsModule().scan(_dms_ctx([_instance("dev-migrator")]))

    # r5.large: $408 Multi-AZ - $204 Single-AZ = $204/mo per-AZ delta.
    assert findings.total_monthly_savings == pytest.approx(204.0, abs=0.01)
    rec = findings.sources["multi_az_review"].recommendations[0]
    assert rec["Counted"] is True
    assert rec["InstanceId"] == "dev-migrator"


def test_prod_multi_az_instance_is_not_flagged() -> None:
    from services.adapters.dms import DmsModule

    findings = DmsModule().scan(_dms_ctx([_instance("prod-migrator")]))
    assert findings.total_monthly_savings == 0.0
    assert "multi_az_review" not in findings.sources


def test_single_az_instance_is_not_flagged() -> None:
    from services.adapters.dms import DmsModule

    findings = DmsModule().scan(_dms_ctx([_instance("dev-migrator", multi_az=False)]))
    assert findings.total_monthly_savings == 0.0


def test_unpriceable_multi_az_instance_is_advisory() -> None:
    from services.adapters.dms import DmsModule

    findings = DmsModule().scan(_dms_ctx([_instance("dev-migrator", klass="dms.unknown.type")]))
    rec = findings.sources["multi_az_review"].recommendations[0]
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0


def test_unpriced_rightsizing_rec_is_demoted_not_left_with_the_35_percent_prose() -> None:
    """DMS-4: the fall-through appended the rec unchanged - no Counted flag (so
    the headline counted it), no dollar (so it contributed $0), and the shim's
    '~35%' prose intact."""
    from services.adapters.dms import DmsModule

    class _LowCpu:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [{"Average": 12.0}]}

    ctx = _dms_ctx([_instance("solo-migrator", klass="dms.unknown.type", multi_az=False)])
    clients = {"dms": _FakeDms([_instance("solo-migrator", klass="dms.unknown.type", multi_az=False)]),
               "cloudwatch": _LowCpu()}
    ctx.client = lambda name, region=None, **kw: clients.get(name)

    findings = DmsModule().scan(ctx)
    rec = findings.sources["instance_rightsizing"].recommendations[0]

    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "35%" not in rec["EstimatedSavings"]
    assert rec["EstimatedSavings"].startswith("$0.00")
    # Count and dollar now agree: an uncounted rec is out of both.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0

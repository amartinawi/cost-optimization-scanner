"""MSK-1 — idle provisioned clusters, and the MSK-5 broker-count default.

The adapter already priced the full recoverable figure into
``AuditBasis.current_monthly_cost`` and then reported $0, because the
rightsizing question ("how much smaller could this be?") needs a target broker
size MSK does not expose. The idle question does not: a cluster nobody has
connected to in 30 days is wasting its entire broker + storage spend.

The gate reads ``ConnectionCount``, a free DEFAULT-level metric, and its
polarity is the OPPOSITE of Transfer Family's BytesIn/BytesOut:

* ConnectionCount publishes continuously once a cluster is ACTIVE, so an EMPTY
  series means the read found nothing -> abstain. Only present-and-zero proves
  idleness.
* BytesIn/BytesOut publish only while a connection exists, so THERE an empty
  series is the proof.

Its dimensions are ``Cluster Name`` **and** ``Broker ID``, so the read must be
per broker: a cluster-name-only read matches no dimension set, returns nothing,
and would look exactly like idleness (the SM-1 trap).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.adapters.msk import MskModule
from services.msk import get_enhanced_msk_checks

_CATEGORY = "Idle MSK Cluster"
_BROKER_HOURLY = 0.21  # kafka.m5.large us-east-1


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self) -> list[dict[str, Any]]:
        return self._pages


class _FakeKafka:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "list_clusters":
            return _FakePaginator([{"ClusterInfoList": self._clusters}])
        return _FakePaginator([{"ClusterInfoList": []}])


class _FakeCloudWatch:
    """`per_broker` maps broker id -> list of Maximum values (None = no series)."""

    def __init__(
        self, per_broker: dict[int, list[float] | None], *, error: Exception | None = None
    ) -> None:
        self._per_broker = per_broker
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        broker = int(next(d["Value"] for d in kwargs["Dimensions"] if d["Name"] == "Broker ID"))
        values = self._per_broker.get(broker)
        if values is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Maximum": v} for v in values]}


class _FakePricing:
    def get_msk_broker_hourly_price(self, instance_type: str) -> float:
        return _BROKER_HOURLY


def _cluster(
    *,
    name: str = "c1",
    state: str = "ACTIVE",
    instance_type: str | None = "kafka.t3.small",
    brokers: int | None = 2,
    volume_size: int | None = 100,
) -> dict[str, Any]:
    bng: dict[str, Any] = {}
    if instance_type is not None:
        bng["InstanceType"] = instance_type
    if volume_size is not None:
        bng["StorageInfo"] = {"EBSStorageInfo": {"VolumeSize": volume_size}}
    cluster: dict[str, Any] = {"ClusterName": name, "State": state, "BrokerNodeGroupInfo": bng}
    if brokers is not None:
        cluster["NumberOfBrokerNodes"] = brokers
    return cluster


def _ctx(
    clusters: list[dict[str, Any]],
    *,
    per_broker: dict[int, list[float] | None] | None = None,
    cw_error: Exception | None = None,
    fast: bool = False,
) -> SimpleNamespace:
    cw = _FakeCloudWatch(per_broker if per_broker is not None else {}, error=cw_error)
    ctx = SimpleNamespace(
        pricing_engine=_FakePricing(),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast,
        warnings=[],
        permissions=[],
    )
    clients = {"kafka": _FakeKafka(clusters), "cloudwatch": cw}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda msg, service=None: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None: ctx.permissions.append(msg)
    ctx._cw = cw
    return ctx


def _idle(ctx: SimpleNamespace) -> list[dict[str, Any]]:
    out = get_enhanced_msk_checks(ctx)
    return [r for r in out["recommendations"] if r["CheckCategory"] == _CATEGORY]


# --------------------------------------------------------------------------- #
# Metric polarity
# --------------------------------------------------------------------------- #
def test_all_brokers_reporting_zero_is_idle() -> None:
    recs = _idle(_ctx([_cluster()], per_broker={1: [0.0, 0.0], 2: [0.0]}))
    assert len(recs) == 1 and recs[0]["IdleEvidence"] is True


def test_any_broker_with_a_connection_is_not_idle() -> None:
    assert _idle(_ctx([_cluster()], per_broker={1: [0.0, 0.0], 2: [0.0, 3.0]})) == []


def test_a_broker_with_no_series_abstains() -> None:
    """ConnectionCount publishes continuously once ACTIVE, so an empty series
    means the read found nothing - not that nobody connected."""
    assert _idle(_ctx([_cluster()], per_broker={1: [0.0], 2: None})) == []


def test_denied_metric_read_abstains_and_is_classified() -> None:
    ctx = _ctx([_cluster()], cw_error=Exception("AccessDeniedException"))
    assert _idle(ctx) == []
    assert ctx.permissions


def test_fast_mode_abstains_and_makes_no_metric_call() -> None:
    ctx = _ctx([_cluster()], per_broker={1: [0.0], 2: [0.0]}, fast=True)
    assert _idle(ctx) == []
    assert ctx._cw.calls == []


def test_read_is_per_broker_with_the_full_dimension_set() -> None:
    """A Cluster-Name-only read matches no dimension set, returns nothing, and
    would read as idle (SM-1)."""
    ctx = _ctx([_cluster(brokers=3)], per_broker={1: [0.0], 2: [0.0], 3: [0.0]})
    _idle(ctx)
    assert len(ctx._cw.calls) == 3
    assert [d["Name"] for d in ctx._cw.calls[0]["Dimensions"]] == ["Cluster Name", "Broker ID"]
    assert {c["Dimensions"][1]["Value"] for c in ctx._cw.calls} == {"1", "2", "3"}


def test_inactive_cluster_is_not_a_candidate() -> None:
    assert _idle(_ctx([_cluster(state="CREATING")], per_broker={1: [0.0], 2: [0.0]})) == []


def test_small_instance_types_are_not_excluded() -> None:
    """MSK-2: the rightsizing lever's `"large" in instance_type` gate drops
    t3.small clusters. A small idle cluster is just as wasted."""
    recs = _idle(_ctx([_cluster(instance_type="kafka.t3.small")], per_broker={1: [0.0], 2: [0.0]}))
    assert len(recs) == 1


# --------------------------------------------------------------------------- #
# MSK-5 — the fabricated broker count
# --------------------------------------------------------------------------- #
def test_missing_broker_count_abstains_instead_of_assuming_three() -> None:
    """The old `cluster.get("NumberOfBrokerNodes", 3)` was a wrong displayed
    figure while everything was advisory; with a counted lever it would be a
    fabricated 3x multiplier on a real dollar."""
    ctx = _ctx([_cluster(brokers=None)], per_broker={1: [0.0], 2: [0.0], 3: [0.0]})
    assert _idle(ctx) == []
    assert ctx._cw.calls == [], "no broker count means nothing to read"


def test_broker_count_drives_the_metric_reads_and_the_price() -> None:
    ctx = _ctx([_cluster(brokers=2)], per_broker={1: [0.0], 2: [0.0]})
    findings = MskModule().scan(ctx)
    rec = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    )
    assert rec["AuditBasis"]["broker_count"] == 2


# --------------------------------------------------------------------------- #
# Pricing through the adapter
# --------------------------------------------------------------------------- #
def test_idle_cluster_counts_its_whole_spend() -> None:
    ctx = _ctx([_cluster(brokers=2, volume_size=100)], per_broker={1: [0.0], 2: [0.0]})
    findings = MskModule().scan(ctx)

    brokers = _BROKER_HOURLY * 730 * 2
    storage = 100 * 0.10 * 2
    assert findings.total_monthly_savings == pytest.approx(brokers + storage, abs=0.01)
    rec = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    )
    assert rec["Counted"] is True
    assert rec["AuditBasis"]["realizable_monthly_savings"] == pytest.approx(brokers + storage, abs=0.01)


def test_every_other_msk_lever_stays_advisory() -> None:
    ctx = _ctx([_cluster(instance_type="kafka.m5.large", volume_size=1500)], per_broker={1: [0.0], 2: [3.0]})
    findings = MskModule().scan(ctx)
    recs = findings.sources["enhanced_checks"].recommendations
    assert recs, "expected the rightsizing / storage advisories"
    assert all(r["Counted"] is False for r in recs)
    assert findings.total_monthly_savings == 0.0


def test_unpriceable_idle_cluster_falls_back_to_advisory() -> None:
    """Evidence of idleness without a defensible rate must not count."""
    ctx = _ctx([_cluster(brokers=2, volume_size=None)], per_broker={1: [0.0], 2: [0.0]})
    ctx.pricing_engine = None
    findings = MskModule().scan(ctx)
    rec = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    )
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0

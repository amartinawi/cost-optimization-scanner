"""FS-7 — idle FSx file systems, the largest unscanned cost in the tail.

An unused FSx file system wastes BOTH provisioned legs: storage capacity and
throughput capacity bill 24/7 whether or not anything mounts it. Nothing
measured this before.

The gate's polarity comes from the AWS docs, not from convenience. FSx
publishes ``DataReadBytes`` / ``DataWriteBytes`` into ``AWS/FSx`` **for all
file systems** at 1-minute periods, so the series exists whenever the file
system does:

* present-and-zero -> nothing read or written -> idle
* empty            -> the read found nothing -> ABSTAIN

AWS also documents that metrics "might not be published ... during file system
maintenance or infrastructure component replacement", and for Multi-AZ during
failover — exactly when an empty read would otherwise be mistaken for an
unused file system.

Throughput rates validated against the live Pricing API 2026-08-10 (AmazonFSx,
us-east-1, productFamily "Provisioned Throughput", unit MiBps-Mo): Windows
Single-AZ $2.20, Multi-AZ $4.50; ONTAP $0.72 / $1.20; OpenZFS $0.26 / $0.87.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.efs_fsx import get_fsx_findings

_CATEGORY = "Idle FSx File System"


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **kw: Any) -> list[dict[str, Any]]:
        return self._pages


class _FakeFsx:
    def __init__(self, file_systems: list[dict[str, Any]]) -> None:
        self._fs = file_systems

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_file_systems":
            return _FakePaginator([{"FileSystems": self._fs}])
        return _FakePaginator([{}])

    def describe_file_caches(self, **kw: Any) -> dict[str, Any]:
        return {"FileCaches": []}


class _FakeCw:
    """`totals` maps metric name -> list of Sum values (None = no datapoints)."""

    def __init__(self, totals: Mapping[str, list[float] | None], *, error: Exception | None = None) -> None:
        self._totals = totals
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        values = self._totals.get(kwargs["MetricName"])
        if values is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Sum": v} for v in values]}


class _FakePricing:
    def __init__(self, storage: float = 0.130, throughput: float = 2.20) -> None:
        self._storage = storage
        self._throughput = throughput

    def get_fsx_storage_price_per_gb(self, fs_type, storage_type, deployment_option="Single-AZ"):
        return self._storage

    def get_fsx_throughput_price_per_mbps(self, fs_type, deployment_option="Single-AZ"):
        return self._throughput


def _fs(
    *,
    fs_id: str = "fs-1",
    fs_type: str = "WINDOWS",
    capacity: int = 1000,
    throughput: int | None = 32,
    deployment: str = "SINGLE_AZ_1",
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"DeploymentType": deployment}
    if throughput is not None:
        cfg["ThroughputCapacity"] = throughput
    return {
        "FileSystemId": fs_id,
        "FileSystemType": fs_type,
        "StorageCapacity": capacity,
        "StorageType": "SSD",
        "Lifecycle": "AVAILABLE",
        "WindowsConfiguration": cfg,
    }


def _ctx(
    file_systems: list[dict[str, Any]],
    totals: Mapping[str, list[float] | None] | None = None,
    *,
    cw_error: Exception | None = None,
    fast: bool = False,
    pricing: Any = "default",
) -> SimpleNamespace:
    cw = _FakeCw(totals if totals is not None else {}, error=cw_error)
    ctx = SimpleNamespace(
        pricing_engine=_FakePricing() if pricing == "default" else pricing,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast,
        warnings=[],
        permissions=[],
    )
    clients = {"fsx": _FakeFsx(file_systems), "cloudwatch": cw}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append(msg)
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(msg)
    ctx._cw = cw
    return ctx


def _idle(ctx: SimpleNamespace) -> list[dict[str, Any]]:
    out = get_fsx_findings(ctx, 1.0)
    return [r for r in out["counted"] if r["CheckCategory"] == _CATEGORY]


_ZERO = {"DataReadBytes": [0.0, 0.0], "DataWriteBytes": [0.0]}


# --------------------------------------------------------------------------- #
# Metric polarity
# --------------------------------------------------------------------------- #
def test_present_and_zero_series_is_idle() -> None:
    recs = _idle(_ctx([_fs()], _ZERO))
    assert len(recs) == 1
    # 1000 GB x $0.130 + 32 MBps x $2.20 = $130.00 + $70.40
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(200.40, abs=0.01)
    assert recs[0]["AuditBasis"]["throughput_monthly"] == pytest.approx(70.40, abs=0.01)


def test_empty_series_abstains() -> None:
    """The read found nothing - or the file system was in maintenance. AWS
    publishes these for ALL file systems, so absence is not idleness."""
    assert _idle(_ctx([_fs()], {"DataReadBytes": None, "DataWriteBytes": None})) == []


def test_any_traffic_is_not_idle() -> None:
    assert _idle(_ctx([_fs()], {"DataReadBytes": [0.0], "DataWriteBytes": [4096.0]})) == []


def test_partial_series_still_counts_when_zero() -> None:
    """One metric reporting and the other silent is still a present series."""
    recs = _idle(_ctx([_fs()], {"DataReadBytes": [0.0, 0.0], "DataWriteBytes": None}))
    assert len(recs) == 1


# --------------------------------------------------------------------------- #
# Fail-closed paths
# --------------------------------------------------------------------------- #
def test_denied_metric_read_abstains_and_is_classified() -> None:
    ctx = _ctx([_fs()], _ZERO, cw_error=Exception("AccessDeniedException"))
    assert _idle(ctx) == []
    assert ctx.permissions or ctx.warnings


def test_fast_mode_abstains_and_makes_no_metric_call() -> None:
    ctx = _ctx([_fs()], _ZERO, fast=True)
    assert _idle(ctx) == []
    assert ctx._cw.calls == []


def test_file_system_still_creating_is_skipped() -> None:
    fs = _fs()
    fs["Lifecycle"] = "CREATING"
    assert _idle(_ctx([fs], _ZERO)) == []


def test_no_storage_capacity_abstains() -> None:
    assert _idle(_ctx([_fs(capacity=0)], _ZERO)) == []


# --------------------------------------------------------------------------- #
# Pricing legs
# --------------------------------------------------------------------------- #
def test_storage_only_when_throughput_has_no_rate() -> None:
    """Lustre persistent throughput is sold in per-TiB-of-storage tiers, so
    there is no flat MBps rate - the leg is omitted, never guessed."""
    pricing = _FakePricing(storage=0.145, throughput=0.0)
    recs = _idle(_ctx([_fs(fs_type="LUSTRE")], _ZERO, pricing=pricing))
    assert len(recs) == 1
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(145.0, abs=0.01)
    assert "omitted" in recs[0]["AuditBasis"]["throughput_leg"]


def test_missing_throughput_capacity_prices_storage_alone() -> None:
    recs = _idle(_ctx([_fs(throughput=None)], _ZERO))
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(130.0, abs=0.01)
    assert "throughput_monthly" not in recs[0]["AuditBasis"]


def test_multi_az_uses_its_own_throughput_rate() -> None:
    pricing = _FakePricing(storage=0.230, throughput=4.50)
    recs = _idle(_ctx([_fs(deployment="MULTI_AZ_1")], _ZERO, pricing=pricing))
    # 1000 x 0.230 + 32 x 4.50 = 230 + 144
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(374.0, abs=0.01)


def test_idle_file_system_does_not_also_emit_the_hdd_advisory() -> None:
    """Deleting the file system and migrating it to HDD are different
    remediations on the same resource; only one may render against it."""
    out = get_fsx_findings(_ctx([_fs(capacity=2000)], _ZERO), 1.0)
    advisory_ids = [r.get("FileSystemId") for r in out["advisory"]]
    assert "fs-1" not in advisory_ids


def test_a_busy_file_system_still_gets_the_hdd_advisory() -> None:
    out = get_fsx_findings(
        _ctx([_fs(capacity=2000)], {"DataReadBytes": [9999.0], "DataWriteBytes": [0.0]}), 1.0
    )
    assert any(r.get("FileSystemId") == "fs-1" for r in out["advisory"])

"""Unit tests for the EFS/FSx (file_systems) adapter — evidence-based savings,
storage-class-aware pricing, counted-vs-advisory separation, and per-file-system
dedup. All logic is exercised without AWS via pure functions + fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.adapters.file_systems as fs_adapter
from services.adapters.file_systems import FileSystemsModule
from services.efs_fsx import get_efs_findings, get_fsx_findings
from services.file_systems_logic import (
    dedupe_counted,
    efs_idle_savings,
    efs_lifecycle_savings,
    efs_one_zone_savings,
    fs_id,
    fsx_ssd_to_hdd_savings,
)
from core.pricing_engine import PricingEngine


GB = 1024**3


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
class TestFileSystemsLogic:
    def test_efs_lifecycle_savings(self):
        # 100 GB Standard, $0.30→$0.025 delta, 0.5 fraction = 100×0.275×0.5
        assert efs_lifecycle_savings(100, 0.30, 0.025) == pytest.approx(13.75)

    def test_efs_lifecycle_never_negative(self):
        assert efs_lifecycle_savings(100, 0.02, 0.30) == 0.0

    def test_efs_idle_savings(self):
        assert efs_idle_savings(200, 0.30) == pytest.approx(60.0)

    def test_efs_one_zone_savings(self):
        assert efs_one_zone_savings(100, 0.30, 0.16) == pytest.approx(14.0)

    def test_fsx_ssd_to_hdd_savings(self):
        # Windows 4000 GB, $0.130→$0.013
        assert fsx_ssd_to_hdd_savings(4000, 0.130, 0.013) == pytest.approx(468.0)

    def test_dedupe_counted_keeps_highest_per_fs(self):
        recs = [
            {"FileSystemId": "fs-1", "_savings": 10.0},
            {"FileSystemId": "fs-1", "_savings": 25.0},
            {"FileSystemId": "fs-2", "_savings": 5.0},
        ]
        kept = dedupe_counted(recs)
        assert {fs_id(r): r["_savings"] for r in kept} == {"fs-1": 25.0, "fs-2": 5.0}


# --------------------------------------------------------------------------- #
# PricingEngine fallback rates (storage-class / FSx-type aware)
# --------------------------------------------------------------------------- #
class TestPricingFallbacks:
    def _engine(self):
        client = MagicMock()
        client.get_products.side_effect = Exception("no api")  # force fallback
        return PricingEngine("us-east-1", client, fallback_multiplier=1.0)

    @pytest.mark.parametrize(
        "storage_class,expected",
        [("Standard", 0.30), ("IA", 0.025), ("One Zone", 0.16), ("One Zone-IA", 0.0133)],
    )
    def test_efs_class_rates(self, storage_class, expected):
        assert self._engine().get_efs_monthly_price_per_gb(storage_class) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "fs_type,storage,expected",
        [("Windows", "SSD", 0.130), ("Windows", "HDD", 0.013), ("Lustre", "HDD", 0.025)],
    )
    def test_fsx_rates(self, fs_type, storage, expected):
        assert self._engine().get_fsx_storage_price_per_gb(fs_type, storage) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Shim findings (EFS / FSx) driven with fake boto3 clients
# --------------------------------------------------------------------------- #
class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return self._pages


class _FakeEfs:
    def __init__(self, file_systems, lifecycle=None):
        self._fs = file_systems
        self._lifecycle = lifecycle or {}

    def get_paginator(self, name):
        return _Paginator([{"FileSystems": self._fs}])

    def describe_lifecycle_configuration(self, FileSystemId):
        return {"LifecyclePolicies": self._lifecycle.get(FileSystemId, [])}


class _FakeFsx:
    def __init__(self, file_systems, caches=None):
        self._fs = file_systems
        self._caches = caches or []

    def get_paginator(self, name):
        return _Paginator([{"FileSystems": self._fs}])

    def describe_file_caches(self):
        return {"FileCaches": self._caches}


class _PE:
    """Region-correct rate stand-in matching the validated us-east-1 rates."""

    _EFS = {"Standard": 0.30, "IA": 0.025, "One Zone": 0.16, "One Zone-IA": 0.0133}
    _FSX = {("WINDOWS", "SSD"): 0.130, ("WINDOWS", "HDD"): 0.013, ("LUSTRE", "SSD"): 0.145, ("LUSTRE", "HDD"): 0.025}

    def get_efs_monthly_price_per_gb(self, storage_class="Standard"):
        return self._EFS[storage_class]

    def get_fsx_storage_price_per_gb(self, fs_type, storage_type, deployment_option="Single-AZ"):
        return self._FSX[(fs_type.upper(), storage_type.upper())]


def _ctx(efs=None, fsx=None):
    warns: list = []
    clients = {"efs": efs, "fsx": fsx}
    ns = SimpleNamespace(
        region="us-east-1",
        pricing_multiplier=1.0,
        pricing_engine=_PE(),
        client=lambda name, region=None: clients.get(name),
        warn=lambda message, service="": warns.append((service, message)),
    )
    ns._warns = warns
    return ns


def _efs_fs(fs_id, total_gb, standard_gb, mount_targets, one_zone=False, throughput="bursting"):
    d = {
        "FileSystemId": fs_id,
        "Name": fs_id,
        "SizeInBytes": {"Value": int(total_gb * GB), "ValueInStandard": int(standard_gb * GB)},
        "NumberOfMountTargets": mount_targets,
        "ThroughputMode": throughput,
    }
    if one_zone:
        d["AvailabilityZoneName"] = "us-east-1a"
    return d


class TestEfsFindings:
    def test_idle_delete_counted_100pct(self):
        fs = _efs_fs("fs-idle", total_gb=200, standard_gb=200, mount_targets=0)
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs])), 1.0)
        counted = out["counted"]
        assert len(counted) == 1
        assert counted[0]["CheckCategory"] == "Idle EFS File System"
        assert counted[0]["_savings"] == pytest.approx(60.0)  # 200 × 0.30
        assert "AuditBasis" in counted[0]

    def test_lifecycle_counted_when_no_ia_policy(self):
        fs = _efs_fs("fs-life", total_gb=100, standard_gb=100, mount_targets=2)
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs])), 1.0)
        counted = out["counted"]
        assert len(counted) == 1
        assert counted[0]["CheckCategory"] == "EFS No Lifecycle"
        assert counted[0]["_savings"] == pytest.approx(13.75)  # 100 × 0.275 × 0.5
        # One Zone shows up as advisory, not counted.
        assert any(a["CheckCategory"] == "EFS One Zone Migration" for a in out["advisory"])

    def test_existing_ia_policy_not_counted(self):
        fs = _efs_fs("fs-tiered", total_gb=100, standard_gb=100, mount_targets=2)
        lifecycle = {"fs-tiered": [{"TransitionToIA": "AFTER_30_DAYS"}]}
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs], lifecycle)), 1.0)
        assert all(c["CheckCategory"] != "EFS No Lifecycle" for c in out["counted"])

    def test_provisioned_throughput_is_advisory(self):
        fs = _efs_fs("fs-pt", total_gb=5, standard_gb=5, mount_targets=2, throughput="provisioned")
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs])), 1.0)
        assert any(a["CheckCategory"] == "EFS Throughput Optimization" for a in out["advisory"])


class TestFsxFindings:
    def test_ssd_to_hdd_counted(self):
        fs = {
            "FileSystemId": "fs-w", "FileSystemType": "WINDOWS", "StorageCapacity": 4000,
            "StorageType": "SSD", "Lifecycle": "AVAILABLE",
            "WindowsConfiguration": {"DeploymentType": "SINGLE_AZ_2"},
        }
        out = get_fsx_findings(_ctx(fsx=_FakeFsx([fs])), 1.0)
        counted = out["counted"]
        assert len(counted) == 1
        assert counted[0]["_savings"] == pytest.approx(468.0)  # 4000 × (0.130 − 0.013)
        assert counted[0]["AuditBasis"]["deployment"] == "Single-AZ"

    def test_small_ssd_not_counted(self):
        fs = {
            "FileSystemId": "fs-s", "FileSystemType": "WINDOWS", "StorageCapacity": 100,
            "StorageType": "SSD", "Lifecycle": "AVAILABLE", "WindowsConfiguration": {},
        }
        out = get_fsx_findings(_ctx(fsx=_FakeFsx([fs])), 1.0)
        assert out["counted"] == []
        # Still gets advisory (dedup nudge).
        assert any(a["CheckCategory"] == "FSx Data Deduplication" for a in out["advisory"])


# --------------------------------------------------------------------------- #
# Adapter integration
# --------------------------------------------------------------------------- #
class TestAdapter:
    def test_counted_vs_advisory_and_totals(self, monkeypatch):
        efs = {
            "counted": [
                {"FileSystemId": "fs-1", "CheckCategory": "Idle EFS File System", "EstimatedSavings": "$60.00/month", "_savings": 60.0},
            ],
            "advisory": [{"FileSystemId": "fs-1", "CheckCategory": "EFS One Zone Migration", "Counted": False}],
        }
        fsx = {
            "counted": [
                {"FileSystemId": "fs-w", "CheckCategory": "FSx Storage Type Optimization", "EstimatedSavings": "$468.00/month", "_savings": 468.0},
            ],
            "advisory": [{"FileSystemId": "fs-w", "CheckCategory": "FSx Data Deduplication", "Counted": False}],
        }
        monkeypatch.setattr(fs_adapter, "get_efs_findings", lambda *a, **k: efs)
        monkeypatch.setattr(fs_adapter, "get_fsx_findings", lambda *a, **k: fsx)
        monkeypatch.setattr(fs_adapter, "get_efs_file_system_count", lambda ctx: {})
        monkeypatch.setattr(fs_adapter, "get_fsx_file_system_count", lambda ctx: {})
        ctx = SimpleNamespace(pricing_multiplier=1.0)

        findings = FileSystemsModule().scan(ctx)

        # Only counted findings drive savings + total; advisory excluded.
        assert findings.total_monthly_savings == pytest.approx(528.0)
        assert findings.total_recommendations == 2
        assert findings.sources["efs_lifecycle_analysis"].count == 1
        assert findings.sources["fsx_optimization_analysis"].count == 1
        assert findings.sources["advisory"].count == 2

    def test_dedup_within_adapter(self, monkeypatch):
        efs = {
            "counted": [
                {"FileSystemId": "fs-1", "CheckCategory": "EFS No Lifecycle", "EstimatedSavings": "$10.00/month", "_savings": 10.0},
                {"FileSystemId": "fs-1", "CheckCategory": "Idle EFS File System", "EstimatedSavings": "$60.00/month", "_savings": 60.0},
            ],
            "advisory": [],
        }
        monkeypatch.setattr(fs_adapter, "get_efs_findings", lambda *a, **k: efs)
        monkeypatch.setattr(fs_adapter, "get_fsx_findings", lambda *a, **k: {"counted": [], "advisory": []})
        monkeypatch.setattr(fs_adapter, "get_efs_file_system_count", lambda ctx: {})
        monkeypatch.setattr(fs_adapter, "get_fsx_file_system_count", lambda ctx: {})

        findings = FileSystemsModule().scan(SimpleNamespace(pricing_multiplier=1.0))

        # fs-1 counted once at the higher saving.
        assert findings.total_recommendations == 1
        assert findings.total_monthly_savings == pytest.approx(60.0)

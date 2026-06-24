"""Unit tests for the EFS/FSx (file_systems) adapter — evidence-based savings,
storage-class-aware pricing, counted-vs-advisory separation, and per-file-system
dedup. All logic is exercised without AWS via pure functions + fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import services.adapters.file_systems as fs_adapter
from services.adapters.file_systems import FileSystemsModule
from services.efs_fsx import get_efs_findings, get_fsx_findings
from services.file_systems_logic import (
    dedupe_counted,
    efs_idle_savings,
    efs_lifecycle_net_savings,
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

    def test_efs_lifecycle_net_cold(self):
        # 100 GB Standard, 1 GB accessed -> cold 99, net = 99*0.275 - 1*0.01
        est = efs_lifecycle_net_savings(100, 1, 0.30, 0.025, 0.01)
        assert est.cold_gb == pytest.approx(99.0)
        assert est.gross_savings == pytest.approx(99 * 0.275)
        assert est.access_charge == pytest.approx(0.01)
        assert est.net_savings == pytest.approx(99 * 0.275 - 0.01)

    def test_efs_lifecycle_net_hot_is_negative(self):
        # Access exceeds storage -> cold 0, net negative (drops the finding).
        est = efs_lifecycle_net_savings(100, 5000, 0.30, 0.025, 0.01)
        assert est.cold_gb == 0.0
        assert est.net_savings < 0

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
        [("Windows", "SSD", 0.130), ("Windows", "HDD", 0.013), ("Lustre", "HDD", 0.025),
         ("ONTAP", "SSD", 0.125), ("OpenZFS", "SSD", 0.09)],
    )
    def test_fsx_rates(self, fs_type, storage, expected):
        assert self._engine().get_fsx_storage_price_per_gb(fs_type, storage) == pytest.approx(expected)

    def test_fsx_multi_az_fallback_is_distinct_not_double(self):
        # L4: Multi-AZ uses its own fallback constant, not a flat 2x of Single-AZ.
        eng = self._engine()
        assert eng.get_fsx_storage_price_per_gb("Windows", "SSD", "Multi-AZ") == pytest.approx(0.230)
        assert eng.get_fsx_storage_price_per_gb("ONTAP", "SSD", "Multi-AZ") == pytest.approx(0.250)

    def test_efs_archive_fallback_current(self):
        # L2: Archive fallback refreshed to the live $0.008.
        assert self._engine().get_efs_monthly_price_per_gb("Archive") == pytest.approx(0.008)

    def test_efs_ia_access_fallback(self):
        # B: IA per-GB access rate fallback ($0.01) used when the API is down.
        assert self._engine().get_efs_ia_access_price_per_gb() == pytest.approx(0.01)


class TestLivePricingFilters:
    """Exercise the live _fetch_* filter paths the _PE/exception stubs bypass."""

    def _engine_capturing(self, price_list=None):
        captured: list = []
        client = MagicMock()

        def gp(**kw):
            captured.append(kw)
            return {"PriceList": price_list or []}

        client.get_products.side_effect = gp
        return PricingEngine("us-east-1", client, fallback_multiplier=1.0), captured

    @pytest.mark.parametrize(
        "fs_type,label",
        [("ONTAP", "ONTAP"), ("OPENZFS", "OpenZFS"), ("WINDOWS", "Windows"), ("LUSTRE", "Lustre")],
    )
    def test_fsx_type_label_pinned(self, fs_type, label):
        # H2: ONTAP/OpenZFS must use exact casing, not str.capitalize().
        eng, captured = self._engine_capturing()
        eng.get_fsx_storage_price_per_gb(fs_type, "SSD", "Single-AZ")
        value = [f for f in captured[0]["Filters"] if f["Field"] == "fileSystemType"][0]["Value"]
        assert value == label


def _efs_item(usagetype: str, usd: str) -> str:
    return json.dumps(
        {
            "product": {"attributes": {"usagetype": usagetype}},
            "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": usd}}}}}},
        }
    )


class TestEfsStorageRateSelection:
    """M1: storageClass alone matches storage + read + write SKUs; pin storage."""

    def _engine(self, price_list):
        client = MagicMock()
        client.get_products.return_value = {"PriceList": price_list}
        return PricingEngine("us-east-1", client, fallback_multiplier=1.0)

    def test_picks_timed_storage_over_data_access(self):
        eng = self._engine([
            _efs_item("USE1-IADataAccess-Bytes", "0.01"),     # access — must be skipped
            _efs_item("USE1-IATimedStorage-ByteHrs", "0.025"),  # storage — must win
        ])
        assert eng.get_efs_monthly_price_per_gb("IA") == pytest.approx(0.025)

    def test_skips_smallfiles_overhead(self):
        eng = self._engine([
            _efs_item("USE1-IATimedStorage-SmallFiles", "0.025"),  # overhead — skip
            _efs_item("USE1-IATimedStorage-ByteHrs", "0.0249"),    # storage — win
        ])
        assert eng.get_efs_monthly_price_per_gb("IA") == pytest.approx(0.0249)


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

    def get_efs_ia_access_price_per_gb(self):
        return 0.01

    def get_fsx_storage_price_per_gb(self, fs_type, storage_type, deployment_option="Single-AZ"):
        return self._FSX[(fs_type.upper(), storage_type.upper())]


class _FakeCw:
    """Fake CloudWatch: maps MetricName -> datapoints, or raises a fixed error."""

    def __init__(self, datapoints_by_metric=None, error=None):
        self._dp = datapoints_by_metric or {}
        self._error = error

    def get_metric_statistics(self, **kw):
        if self._error:
            raise self._error
        return {"Datapoints": self._dp.get(kw["MetricName"], [])}


def _ctx(efs=None, fsx=None, cloudwatch=None):
    warns: list = []
    perms: list = []
    clients = {"efs": efs, "fsx": fsx, "cloudwatch": cloudwatch}
    ns = SimpleNamespace(
        region="us-east-1",
        pricing_multiplier=1.0,
        pricing_engine=_PE(),
        client=lambda name, region=None: clients.get(name),
        warn=lambda message, service="": warns.append((service, message)),
        permission_issue=lambda message, service="", action=None: perms.append((service, action, message)),
    )
    ns._warns = warns
    ns._perms = perms
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

    def test_lifecycle_advisory_when_no_cloudwatch(self):
        # No CloudWatch client -> no usage evidence -> advisory indicative gross,
        # never counted.
        fs = _efs_fs("fs-life", total_gb=100, standard_gb=100, mount_targets=2)
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs])), 1.0)
        assert out["counted"] == []
        life = [a for a in out["advisory"] if a["CheckCategory"] == "EFS No Lifecycle"]
        assert len(life) == 1
        assert life[0]["Counted"] is False
        assert "gross" in life[0]["EstimatedSavings"]
        assert "_savings" not in life[0]  # never feeds counted totals
        # One Zone migration also advisory.
        assert any(a["CheckCategory"] == "EFS One Zone Migration" for a in out["advisory"])

    def test_lifecycle_counted_when_metrics_prove_cold(self):
        # B: CloudWatch shows little access -> cold set -> net-positive COUNTED.
        fs = _efs_fs("fs-cold", total_gb=100, standard_gb=100, mount_targets=2)
        cw = _FakeCw({"DataReadIOBytes": [{"Sum": float(1 * GB)}]})  # 1 GB read, no writes
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs]), cloudwatch=cw), 1.0)
        counted = [c for c in out["counted"] if c["CheckCategory"] == "EFS No Lifecycle"]
        assert len(counted) == 1
        assert counted[0]["Counted"] is True
        # cold_gb=99, net = 99*(0.30-0.025) - 1*0.01 = 27.225 - 0.01
        assert counted[0]["_savings"] == pytest.approx(99 * 0.275 - 0.01)
        ab = counted[0]["AuditBasis"]
        assert ab["cold_gb"] == pytest.approx(99.0)
        assert ab["monthly_access_gb"] == pytest.approx(1.0)
        assert ab["ia_access_charge"] == pytest.approx(0.01)

    def test_lifecycle_advisory_when_metrics_show_hot(self):
        # B: heavy access -> cold_gb 0 / net <= 0 -> advisory "not cost-effective".
        fs = _efs_fs("fs-hot", total_gb=100, standard_gb=100, mount_targets=2)
        cw = _FakeCw({"DataReadIOBytes": [{"Sum": float(500 * GB)}]})
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs]), cloudwatch=cw), 1.0)
        assert all(c["CheckCategory"] != "EFS No Lifecycle" for c in out["counted"])
        adv = [a for a in out["advisory"] if a["CheckCategory"] == "EFS No Lifecycle"]
        assert len(adv) == 1
        assert "not cost-effective" in adv[0]["EstimatedSavings"]

    def test_lifecycle_fast_mode_skips_metrics_and_warns(self):
        # B: fast_mode -> no metric read -> advisory + one fast-mode warning.
        fs = _efs_fs("fs-fast", total_gb=100, standard_gb=100, mount_targets=2)
        cw = _FakeCw({"DataReadIOBytes": [{"Sum": float(1 * GB)}]})
        ctx = _ctx(efs=_FakeEfs([fs]), cloudwatch=cw)
        out = get_efs_findings(ctx, 1.0, fast_mode=True)
        assert all(c["CheckCategory"] != "EFS No Lifecycle" for c in out["counted"])
        adv = [a for a in out["advisory"] if a["CheckCategory"] == "EFS No Lifecycle"]
        assert len(adv) == 1 and "gross" in adv[0]["EstimatedSavings"]
        assert any("fast mode" in m for _s, m in ctx._warns)

    def test_lifecycle_metric_access_denied_to_permission_issue(self):
        # M2/B: a denied GetMetricStatistics -> permission_issue, then advisory.
        fs = _efs_fs("fs-deny", total_gb=100, standard_gb=100, mount_targets=2)
        err = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetMetricStatistics")
        ctx = _ctx(efs=_FakeEfs([fs]), cloudwatch=_FakeCw(error=err))
        out = get_efs_findings(ctx, 1.0)
        assert all(c["CheckCategory"] != "EFS No Lifecycle" for c in out["counted"])
        assert any(action == "cloudwatch:GetMetricStatistics" for _svc, action, _msg in ctx._perms)
        assert any(a["CheckCategory"] == "EFS No Lifecycle" for a in out["advisory"])

    def test_lifecycle_no_datapoints_warns_and_advisory(self):
        # B: CloudWatch present but no datapoints -> warn + advisory, never $.
        fs = _efs_fs("fs-nodp", total_gb=100, standard_gb=100, mount_targets=2)
        ctx = _ctx(efs=_FakeEfs([fs]), cloudwatch=_FakeCw({}))
        out = get_efs_findings(ctx, 1.0)
        assert all(c["CheckCategory"] != "EFS No Lifecycle" for c in out["counted"])
        assert any("No EFS access metrics" in m for _s, m in ctx._warns)
        assert any(a["CheckCategory"] == "EFS No Lifecycle" for a in out["advisory"])

    def test_creating_efs_not_flagged_idle(self):
        # L3: a transient CREATING file system (0 mount targets) must be skipped.
        fs = _efs_fs("fs-new", total_gb=200, standard_gb=200, mount_targets=0)
        fs["LifeCycleState"] = "creating"
        out = get_efs_findings(_ctx(efs=_FakeEfs([fs])), 1.0)
        assert out["counted"] == []
        assert out["advisory"] == []

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

    def test_lustre_ssd_advisory_not_counted(self):
        # C1: Lustre SSD->HDD is not like-for-like (Persistent-only HDD, different
        # throughput tier) — advisory, never counted.
        fs = {
            "FileSystemId": "fs-l", "FileSystemType": "LUSTRE", "StorageCapacity": 4000,
            "StorageType": "SSD", "Lifecycle": "AVAILABLE",
        }
        out = get_fsx_findings(_ctx(fsx=_FakeFsx([fs])), 1.0)
        assert out["counted"] == []
        assert any(a["CheckCategory"] == "FSx Lustre Storage Optimization" for a in out["advisory"])

    def test_ontap_ssd_not_counted(self):
        # H3: ONTAP has no HDD storage type — no SSD->HDD counted finding.
        fs = {
            "FileSystemId": "fs-o", "FileSystemType": "ONTAP", "StorageCapacity": 4000,
            "StorageType": "SSD", "Lifecycle": "AVAILABLE",
            "OntapConfiguration": {"DeploymentType": "SINGLE_AZ_1"},
        }
        out = get_fsx_findings(_ctx(fsx=_FakeFsx([fs])), 1.0)
        assert out["counted"] == []
        assert any(a["CheckCategory"] == "FSx ONTAP Data Efficiency" for a in out["advisory"])


class TestErrorClassification:
    def test_efs_access_denied_records_permission_issue(self):
        # M2: AccessDenied is routed to permission_issue, not a generic warn,
        # and yields no fabricated findings.
        class _DenyEfs:
            def get_paginator(self, name):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeFileSystems")

        ctx = _ctx(efs=_DenyEfs())
        out = get_efs_findings(ctx, 1.0)
        assert out["counted"] == [] and out["advisory"] == []
        assert any(svc == "efs" for svc, _action, _msg in ctx._perms)
        assert ctx._warns == []

    def test_fsx_throttle_records_warn(self):
        class _ThrottleFsx:
            def get_paginator(self, name):
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "DescribeFileSystems")

        ctx = _ctx(fsx=_ThrottleFsx())
        out = get_fsx_findings(ctx, 1.0)
        assert out["counted"] == []
        assert any(svc == "fsx" for svc, _msg in ctx._warns)
        assert ctx._perms == []


# --------------------------------------------------------------------------- #
# Adapter integration
# --------------------------------------------------------------------------- #
class TestReporterCountedAdvisorySplit:
    """Part C: the tab header distinguishes counted from advisory recs."""

    def test_split_counts_counted_vs_advisory(self):
        from html_report_generator import _counted_advisory_counts

        sources = {
            "efs_lifecycle_analysis": {"recommendations": [{"Counted": True}]},
            "fsx_optimization_analysis": {"recommendations": []},
            "advisory": {"recommendations": [{"Counted": False}, {"Counted": False}]},
        }
        assert _counted_advisory_counts(sources) == (1, 2)

    def test_split_treats_missing_counted_flag_as_counted(self):
        from html_report_generator import _counted_advisory_counts

        # Other adapters whose recs omit the flag are treated as counted.
        sources = {"s": {"recommendations": [{"foo": 1}, {"Counted": False}]}}
        assert _counted_advisory_counts(sources) == (1, 1)


class TestModuleIdentity:
    @pytest.mark.parametrize("token", ["efs", "fsx", "file_systems", "FSx", "EFS"])
    def test_aliases_resolve_to_file_systems(self, token):
        from core.filtering import resolve_cli_keys

        keys = resolve_cli_keys([FileSystemsModule()], scan_only={token}, skip=None)
        assert keys == {"file_systems"}

    def test_unknown_token_resolves_to_nothing(self):
        from core.filtering import resolve_cli_keys

        assert resolve_cli_keys([FileSystemsModule()], scan_only={"nope"}, skip=None) == set()


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

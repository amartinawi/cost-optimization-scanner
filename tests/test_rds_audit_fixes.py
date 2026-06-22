"""Tests for the RDS audit-remediation fixes.

Covers, slice by slice:
  * advisor: at-source filtering of non-actionable Compute Optimizer recs,
    and permission/opt-in/other-error classification on ``ctx``;
  * adapter: the Compute-Optimizer opt-in placeholder is converted to a
    warning and dropped from the counted recommendations.

The adapter is driven with a ``SimpleNamespace``-style fake ctx and
monkeypatched source helpers, mirroring ``tests/test_ec2_audit_fixes.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.rds as rds_adapter
from services.advisor import get_rds_compute_optimizer_recommendations


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeCtx:
    """Minimal ScanContext stand-in recording warnings and permission issues."""

    def __init__(self, *, co_client=None, pricing_engine=None):
        self._co_client = co_client
        self.pricing_engine = pricing_engine
        self.pricing_multiplier = 1.0
        self.old_snapshot_days = 90
        self.region = "us-east-1"
        self.account_id = "123456789012"
        self.fast_mode = False
        self.cost_hub_splits: dict = {}
        self.warnings: list[str] = []
        self.permission_issues: list[dict] = []

    def client(self, name, region=None):
        if name == "compute-optimizer":
            return self._co_client
        return None

    def warn(self, message, service=None):
        self.warnings.append(message)

    def permission_issue(self, message, service=None, action=None):
        self.permission_issues.append({"message": message, "service": service, "action": action})


class _FakeCoClient:
    """Fake compute-optimizer client: returns a payload or raises an exception."""

    def __init__(self, *, payload=None, error=None):
        self._payload = payload or {"rdsDBRecommendations": []}
        self._error = error

    def get_rds_database_recommendations(self, nextToken=None):
        if self._error:
            raise self._error
        return self._payload


def _co_rec(arn: str, value: float) -> dict:
    """Compute-Optimizer-shaped rec with one rank-1 instance option savings."""
    return {
        "resourceArn": arn,
        "instanceFinding": "Overprovisioned" if value > 0 else "Optimized",
        "instanceRecommendationOptions": [
            {"rank": 1, "savingsOpportunity": {"estimatedMonthlySavings": {"currency": "USD", "value": value}}},
        ],
    }


# --------------------------------------------------------------------------- #
# H5 — at-source filtering of non-actionable CO recs
# --------------------------------------------------------------------------- #
def test_co_drops_optimized_and_zero_savings():
    payload = {
        "rdsDBRecommendations": [
            _co_rec("arn:aws:rds:us-east-1:1:db:keep", 30.0),
            _co_rec("arn:aws:rds:us-east-1:1:db:drop-optimized", 0.0),
        ]
    }
    ctx = _FakeCtx(co_client=_FakeCoClient(payload=payload))
    recs = get_rds_compute_optimizer_recommendations(ctx)
    arns = [r["resourceArn"] for r in recs]
    assert arns == ["arn:aws:rds:us-east-1:1:db:keep"]


def test_co_keeps_overprovisioned_with_savings():
    payload = {"rdsDBRecommendations": [_co_rec("arn:aws:rds:us-east-1:1:db:over", 12.5)]}
    ctx = _FakeCtx(co_client=_FakeCoClient(payload=payload))
    recs = get_rds_compute_optimizer_recommendations(ctx)
    assert len(recs) == 1


# --------------------------------------------------------------------------- #
# H4 — permission / opt-in / other-error classification
# --------------------------------------------------------------------------- #
def test_co_optin_returns_placeholder_not_permission_issue():
    err = Exception("OptInRequiredException: account not registered")
    ctx = _FakeCtx(co_client=_FakeCoClient(error=err))
    recs = get_rds_compute_optimizer_recommendations(ctx)
    assert len(recs) == 1
    assert recs[0]["ResourceId"] == "compute-optimizer-service"
    assert ctx.permission_issues == []


def test_co_accessdenied_records_permission_issue():
    err = Exception("AccessDeniedException: not authorized")
    ctx = _FakeCtx(co_client=_FakeCoClient(error=err))
    recs = get_rds_compute_optimizer_recommendations(ctx)
    assert recs == []
    assert len(ctx.permission_issues) == 1
    assert ctx.permission_issues[0]["action"] == "compute-optimizer:GetRDSDatabaseRecommendations"


def test_co_other_error_records_warning():
    err = Exception("ThrottlingException: slow down")
    ctx = _FakeCtx(co_client=_FakeCoClient(error=err))
    recs = get_rds_compute_optimizer_recommendations(ctx)
    assert recs == []
    assert ctx.permission_issues == []
    assert any("unavailable" in w for w in ctx.warnings)


# --------------------------------------------------------------------------- #
# H2 — adapter converts the opt-in placeholder to a warning, drops from count
# --------------------------------------------------------------------------- #
def test_adapter_placeholder_becomes_warning_not_rec(monkeypatch):
    placeholder = {"ResourceId": "compute-optimizer-service", "estimatedMonthlySavings": 0.0}
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [placeholder])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, mult, days, fast=False: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 0})

    ctx = _FakeCtx()
    findings = rds_adapter.RdsModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.sources["compute_optimizer"].count == 0
    assert findings.total_monthly_savings == 0.0
    assert any("Compute Optimizer is not enabled" in w for w in ctx.warnings)


def test_adapter_real_co_rec_is_counted(monkeypatch):
    rec = _co_rec("arn:aws:rds:us-east-1:1:db:real", 40.0)
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [rec])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, mult, days, fast=False: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    ctx = _FakeCtx()
    findings = rds_adapter.RdsModule().scan(ctx)

    assert findings.total_recommendations == 1
    assert findings.sources["compute_optimizer"].count == 1
    assert findings.total_monthly_savings == pytest.approx(40.0)


# --------------------------------------------------------------------------- #
# Slice 2 — RDS pricing filters (M3 storage volumeType, M4 Multi-AZ deployment,
# M2 deterministic backup engine pin)
# --------------------------------------------------------------------------- #
from core.pricing_engine import PricingEngine  # noqa: E402


class _CapturingPricingClient:
    """Fake boto3 pricing client recording the Filters of the last get_products call."""

    def __init__(self):
        self.last_filters: list[dict] = []

    def get_products(self, ServiceCode, Filters, MaxResults=1):  # noqa: N803 (boto3 casing)
        self.last_filters = Filters
        return {"PriceList": []}  # empty -> engine falls back; we only inspect filters


def _filter_value(filters: list[dict], field: str):
    return next((f["Value"] for f in filters if f["Field"] == field), None)


def _engine(client):
    return PricingEngine("us-east-1", client, fallback_multiplier=1.0)


def test_storage_filter_maps_gp3_volume_type():
    client = _CapturingPricingClient()
    _engine(client).get_rds_monthly_storage_price_per_gb("gp3")
    assert _filter_value(client.last_filters, "volumeType") == "General Purpose-GP3"


def test_storage_filter_maps_gp2_volume_type():
    client = _CapturingPricingClient()
    _engine(client).get_rds_monthly_storage_price_per_gb("gp2")
    assert _filter_value(client.last_filters, "volumeType") == "General Purpose"


def test_instance_filter_sqlserver_multiaz_uses_mirror_deployment():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("sqlserver-se", "db.m5.large", multi_az=True)
    assert _filter_value(client.last_filters, "deploymentOption") == "Multi-AZ (SQL Server Mirror)"


def test_instance_filter_mysql_multiaz_uses_plain_multiaz():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("mysql", "db.t3.medium", multi_az=True)
    assert _filter_value(client.last_filters, "deploymentOption") == "Multi-AZ"


def test_instance_filter_single_az():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("mysql", "db.t3.medium", multi_az=False)
    assert _filter_value(client.last_filters, "deploymentOption") == "Single-AZ"


def test_backup_filter_pins_engine_for_determinism():
    client = _CapturingPricingClient()
    _engine(client).get_rds_backup_storage_price_per_gb()
    assert _filter_value(client.last_filters, "databaseEngine") == "MySQL"
    assert _filter_value(client.last_filters, "productFamily") == "Storage Snapshot"


# --------------------------------------------------------------------------- #
# Enhanced-checks fakes (shared by slice 3 / slice 4)
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return list(self._pages)


class _FakeRdsClient:
    def __init__(self, *, instances=None, snapshots=None, clusters=None, cluster_snapshots=None, tags=None):
        self._instances = instances or []
        self._snapshots = snapshots or []
        self._clusters = clusters or []
        self._cluster_snapshots = cluster_snapshots or []
        self._tags = tags or {}

    def get_paginator(self, op):
        pages = {
            "describe_db_instances": [{"DBInstances": self._instances}],
            "describe_db_snapshots": [{"DBSnapshots": self._snapshots}],
            "describe_db_clusters": [{"DBClusters": self._clusters}],
            "describe_db_cluster_snapshots": [{"DBClusterSnapshots": self._cluster_snapshots}],
        }[op]
        return _FakePaginator(pages)

    def list_tags_for_resource(self, ResourceName):  # noqa: N803
        return {"TagList": self._tags.get(ResourceName, [])}


class _FakeRdsPricingEngine:
    """Deterministic RDS pricing: Multi-AZ = 2× Single-AZ; storage/backup flat.

    Records the license_model it was last called with so tests can assert the
    adapter threads the instance's LicenseModel through to pricing.
    """

    def __init__(self):
        self.last_license_model = "UNSET"

    def get_rds_instance_monthly_price(self, engine, instance_class, *, multi_az=False, license_model=None):
        self.last_license_model = license_model
        return 200.0 if multi_az else 100.0

    def get_rds_monthly_storage_price_per_gb(self, storage_type, *, multi_az=False):
        return 0.115

    def get_rds_backup_storage_price_per_gb(self):
        return 0.095


class _FakeCloudWatch:
    """Fake CloudWatch returning one DatabaseConnections datapoint (or empty)."""

    def __init__(self, avg=0.5, mx=2.0, empty=False, error=None):
        self._avg = avg
        self._mx = mx
        self._empty = empty
        self._error = error

    def get_metric_statistics(self, **kwargs):
        if self._error:
            raise self._error
        if self._empty:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": self._avg, "Maximum": self._mx}]}


class _EnhancedCtx(_FakeCtx):
    def __init__(self, rds_client, pricing_engine=None, cloudwatch=None):
        super().__init__(pricing_engine=pricing_engine or _FakeRdsPricingEngine())
        self._rds_client = rds_client
        # Default to a low-usage signal so evidence-gated checks fire in tests.
        self._cloudwatch = cloudwatch if cloudwatch is not None else _FakeCloudWatch()

    def client(self, name, region=None):
        if name == "rds":
            return self._rds_client
        if name == "cloudwatch":
            return self._cloudwatch
        return None


def _instance(**over):
    base = {
        "DBInstanceIdentifier": "prod-db",
        "DBInstanceClass": "db.t3.medium",
        "Engine": "mysql",
        "DBInstanceStatus": "available",
        "MultiAZ": False,
        "BackupRetentionPeriod": 7,
        "AllocatedStorage": 100,
        "StorageType": "gp2",
        "EngineVersion": "8.0",
    }
    base.update(over)
    return base


def _recs(ctx):
    from services.rds import get_enhanced_rds_checks

    return get_enhanced_rds_checks(ctx, 1.0, 90)["recommendations"]


# --------------------------------------------------------------------------- #
# Slice 3 — C1-a: no phantom gp2->gp3 storage savings
# --------------------------------------------------------------------------- #
def test_no_gp2_gp3_storage_recommendation():
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[_instance(StorageType="gp2")]))
    cats = [r.get("CheckCategory", "") for r in _recs(ctx)]
    assert not any("Storage" in c for c in cats)


# --------------------------------------------------------------------------- #
# Slice 4 — H3 source-level dedup + M1-b RI demotion (adapter level)
# --------------------------------------------------------------------------- #
def _enh(arn, savings_str, category):
    return {"resourceArn": arn, "EstimatedSavings": savings_str, "CheckCategory": category}


def test_adapter_dedups_multiremediation_and_excludes_ri(monkeypatch):
    arn = "arn:aws:rds:us-east-1:1:db:prod"
    enhanced = [
        _enh(arn, "$53.00/month with single-AZ deployment", "Multi-AZ Optimization"),
        _enh(arn, "$34.00/month with nights/weekends shutdown", "Non-Production Scheduling"),
        _enh(arn, "up to $40.00/month (1yr All Upfront)", "Reserved Instance Opportunities"),
    ]
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, m, d, fast=False: {"recommendations": enhanced})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    findings = rds_adapter.RdsModule().scan(_FakeCtx())

    # Concrete checks dedup to the single max ($53); RI is excluded from savings.
    assert findings.total_monthly_savings == pytest.approx(53.0)
    # Rendered enhanced recs = winning concrete ($53) + RI advisory = 2; CO = 0.
    enhanced_recs = findings.sources["enhanced_checks"].recommendations
    assert len(enhanced_recs) == 2
    assert findings.sources["compute_optimizer"].count == 0
    # counted == rendered
    assert findings.total_recommendations == len(enhanced_recs)


def test_adapter_co_beats_heuristic_same_db(monkeypatch):
    arn = "arn:aws:rds:us-east-1:1:db:prod"
    co = [_co_rec(arn, 80.0)]
    enhanced = [_enh(arn, "$53.00/month with single-AZ deployment", "Multi-AZ Optimization")]
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: co)
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, m, d, fast=False: {"recommendations": enhanced})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    findings = rds_adapter.RdsModule().scan(_FakeCtx())

    # CO ($80) wins; heuristic Multi-AZ dropped from the enhanced source.
    assert findings.total_monthly_savings == pytest.approx(80.0)
    assert findings.sources["compute_optimizer"].count == 1
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_recommendations == 1


# --------------------------------------------------------------------------- #
# Slice 5 — H1: consume the Cost Optimization Hub rds bucket
# --------------------------------------------------------------------------- #
from services.adapters.rds import _coh_is_renderable  # noqa: E402
from services.rds_logic import normalize_rds_arn  # noqa: E402


def _coh_rec(resource_id, savings, action_type="Rightsize"):
    return {
        "resourceId": resource_id,
        "estimatedMonthlySavings": savings,
        "actionType": action_type,
        "currentResourceType": "RdsDbInstance",
    }


def test_normalize_converges_arn_and_bare_id():
    # CoH bare resourceId and CO/heuristic ARN must produce the same dedup key.
    assert normalize_rds_arn("arn:aws:rds:us-east-1:1:db:prod") == "prod"
    assert normalize_rds_arn("prod") == "prod"
    # Snapshots keep their namespace prefix so they never collide with instances.
    assert normalize_rds_arn("arn:aws:rds:us-east-1:1:snapshot:s1") == "snapshot:s1"


def test_coh_is_renderable_filters_purchase_and_na():
    assert _coh_is_renderable({"resourceId": "prod", "actionType": "Rightsize"}) is True
    assert _coh_is_renderable({"actionType": "PurchaseReservedInstances"}) is False
    assert _coh_is_renderable({"actionType": "PurchaseSavingsPlans"}) is False
    assert _coh_is_renderable({"resourceId": "N/A", "actionType": "Rightsize"}) is False


def test_adapter_consumes_coh_split(monkeypatch):
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, m, d, fast=False: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    ctx = _FakeCtx()
    ctx.cost_hub_splits = {"rds": [_coh_rec("prod", 60.0)]}
    findings = rds_adapter.RdsModule().scan(ctx)

    assert "cost_optimization_hub" in findings.sources
    assert findings.sources["cost_optimization_hub"].count == 1
    assert findings.total_monthly_savings == pytest.approx(60.0)
    assert findings.total_recommendations == 1


def test_coh_suppresses_co_and_heuristic_for_same_db(monkeypatch):
    arn = "arn:aws:rds:us-east-1:1:db:prod"
    co = [_co_rec(arn, 80.0)]
    enhanced = [_enh(arn, "$53.00/month with single-AZ deployment", "Multi-AZ Optimization")]
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: co)
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, m, d, fast=False: {"recommendations": enhanced})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    ctx = _FakeCtx()
    ctx.cost_hub_splits = {"rds": [_coh_rec("prod", 70.0)]}
    findings = rds_adapter.RdsModule().scan(ctx)

    # CoH ($70) is authoritative: CO ($80) and heuristic ($53) for the same DB
    # are suppressed, so only the CoH saving is counted (no double-count).
    assert findings.total_monthly_savings == pytest.approx(70.0)
    assert findings.sources["compute_optimizer"].count == 0
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.sources["cost_optimization_hub"].count == 1
    assert findings.total_recommendations == 1


def test_coh_ri_purchase_excluded_from_rds_tab(monkeypatch):
    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, m, d, fast=False: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    ctx = _FakeCtx()
    ctx.cost_hub_splits = {"rds": [_coh_rec("prod", 200.0, action_type="PurchaseReservedInstances")]}
    findings = rds_adapter.RdsModule().scan(ctx)

    # RI purchase recs belong to commitment_analysis, not the RDS tab.
    assert "cost_optimization_hub" not in findings.sources
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# Slice 6 — L1 engine-unknown warning, L2 AuditBasis on findings
# --------------------------------------------------------------------------- #
def test_unknown_engine_records_pricing_warning():
    client = _CapturingPricingClient()
    eng = _engine(client)
    eng.get_rds_instance_monthly_price("frobdb", "db.t3.medium")
    assert any("Unknown RDS engine" in w for w in eng.warnings)


def test_multiaz_finding_carries_audit_basis():
    instance = _instance(
        DBInstanceIdentifier="dev-db", MultiAZ=True, BackupRetentionPeriod=7, StorageType="gp3"
    )
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[instance]))
    multi_az = next(r for r in _recs(ctx) if r["CheckCategory"] == "Multi-AZ Optimization")
    basis = multi_az["AuditBasis"]
    assert basis["region"] == "us-east-1"
    assert basis["engine"] == "mysql"
    assert "Multi-AZ" in basis["formula"] and "Single-AZ" in basis["formula"]


def test_ri_finding_audit_basis_marks_advisory():
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[_instance(DBInstanceIdentifier="prod-db")]))
    ri = next(r for r in _recs(ctx) if r["CheckCategory"] == "Reserved Instance Opportunities")
    assert "commitment_analysis" in ri["AuditBasis"]["metric_window"]


# --------------------------------------------------------------------------- #
# Slice A — N-H1 databaseEdition pinning, N-M1 license-model threading
# --------------------------------------------------------------------------- #
from core.pricing_engine import _normalize_rds_license_model  # noqa: E402


def test_sqlserver_pricing_pins_edition():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("sqlserver-se", "db.m5.large")
    assert _filter_value(client.last_filters, "databaseEdition") == "Standard"


def test_oracle_ee_defaults_to_byol_and_enterprise_edition():
    # Oracle EE has no "No license required" row; the old engine-static default
    # missed and silently fell back. It must default to BYOL + Enterprise.
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("oracle-ee", "db.m5.large")
    assert _filter_value(client.last_filters, "licenseModel") == "Bring your own license"
    assert _filter_value(client.last_filters, "databaseEdition") == "Enterprise"


def test_instance_license_model_threaded_through():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price(
        "oracle-se2", "db.m5.large", license_model="bring-your-own-license"
    )
    assert _filter_value(client.last_filters, "licenseModel") == "Bring your own license"
    assert _filter_value(client.last_filters, "databaseEdition") == "Standard Two"


def test_mysql_has_no_edition_filter_and_no_license_charge():
    client = _CapturingPricingClient()
    _engine(client).get_rds_instance_monthly_price("mysql", "db.t3.medium")
    assert _filter_value(client.last_filters, "databaseEdition") is None
    assert _filter_value(client.last_filters, "licenseModel") == "No license required"


def test_normalize_rds_license_model():
    assert _normalize_rds_license_model("license-included", "oracle-ee") == "License included"
    assert _normalize_rds_license_model("bring-your-own-license", "oracle-se2") == "Bring your own license"
    assert _normalize_rds_license_model("general-public-license", "mysql") == "No license required"
    # Engine-appropriate defaults when the instance value is absent.
    assert _normalize_rds_license_model(None, "oracle-ee") == "Bring your own license"
    assert _normalize_rds_license_model(None, "oracle-se2") == "License included"
    assert _normalize_rds_license_model(None, "sqlserver-se") == "License included"
    assert _normalize_rds_license_model(None, "mysql") == "No license required"


def test_adapter_threads_license_model_from_instance():
    # The instance's LicenseModel must reach the pricing engine (N-M1).
    pe = _FakeRdsPricingEngine()
    instance = _instance(DBInstanceIdentifier="prod-ora", Engine="oracle-ee",
                         LicenseModel="bring-your-own-license")
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[instance]), pricing_engine=pe)
    _recs(ctx)
    assert pe.last_license_model == "bring-your-own-license"


# --------------------------------------------------------------------------- #
# Slice B — N-M3 CloudWatch evidence gating, N-M4 broadened scheduling engines
# --------------------------------------------------------------------------- #
def _recs_fast(ctx):
    from services.rds import get_enhanced_rds_checks

    return get_enhanced_rds_checks(ctx, 1.0, 90, fast_mode=True)["recommendations"]


def test_scheduling_emitted_with_idle_evidence():
    # dev DB with avg connections <= 1.0 -> schedulable.
    ctx = _EnhancedCtx(
        _FakeRdsClient(instances=[_instance(DBInstanceIdentifier="dev-db")]),
        cloudwatch=_FakeCloudWatch(avg=0.2, mx=1.0),
    )
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Non-Production Scheduling" in cats


def test_scheduling_suppressed_when_busy():
    # dev DB but sustained connections -> not idle -> no scheduling finding.
    ctx = _EnhancedCtx(
        _FakeRdsClient(instances=[_instance(DBInstanceIdentifier="dev-db")]),
        cloudwatch=_FakeCloudWatch(avg=42.0, mx=80.0),
    )
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Non-Production Scheduling" not in cats


def test_scheduling_covers_non_aurora_engines():
    # N-M4: SQL Server (previously excluded) is schedulable when idle.
    ctx = _EnhancedCtx(
        _FakeRdsClient(instances=[_instance(DBInstanceIdentifier="test-mssql", Engine="sqlserver-se")]),
        cloudwatch=_FakeCloudWatch(avg=0.0, mx=0.0),
    )
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Non-Production Scheduling" in cats


def test_aurora_engine_not_scheduled():
    ctx = _EnhancedCtx(
        _FakeRdsClient(instances=[_instance(DBInstanceIdentifier="dev-aurora", Engine="aurora-mysql")]),
        cloudwatch=_FakeCloudWatch(avg=0.0, mx=0.0),
    )
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Non-Production Scheduling" not in cats


def test_multiaz_suppressed_when_busy():
    inst = _instance(DBInstanceIdentifier="dev-db", MultiAZ=True)
    ctx = _EnhancedCtx(
        _FakeRdsClient(instances=[inst]),
        cloudwatch=_FakeCloudWatch(avg=50.0, mx=120.0),
    )
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Multi-AZ Optimization" not in cats


def test_fast_mode_skips_metric_gated_checks_with_warning():
    inst = _instance(DBInstanceIdentifier="dev-db", MultiAZ=True)
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[inst]))
    cats = [r["CheckCategory"] for r in _recs_fast(ctx)]
    assert "Multi-AZ Optimization" not in cats
    assert "Non-Production Scheduling" not in cats
    assert any("fast mode" in w for w in ctx.warnings)


def test_no_metric_data_skips_and_warns():
    inst = _instance(DBInstanceIdentifier="dev-db", MultiAZ=True)
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[inst]), cloudwatch=_FakeCloudWatch(empty=True))
    cats = [r["CheckCategory"] for r in _recs(ctx)]
    assert "Multi-AZ Optimization" not in cats
    assert "Non-Production Scheduling" not in cats
    assert any("no DatabaseConnections data" in w for w in ctx.warnings)


def test_cloudwatch_accessdenied_records_permission_issue():
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetMetricStatistics")
    inst = _instance(DBInstanceIdentifier="dev-db", MultiAZ=True)
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[inst]), cloudwatch=_FakeCloudWatch(error=err))
    _recs(ctx)
    assert any(p["action"] == "cloudwatch:GetMetricStatistics" for p in ctx.permission_issues)


# --------------------------------------------------------------------------- #
# Slice C — N-M2: backup retention is advisory (no fabricated $), excluded from total
# --------------------------------------------------------------------------- #
def test_backup_retention_is_advisory_no_dollar():
    inst = _instance(DBInstanceIdentifier="prod-db", BackupRetentionPeriod=30, AllocatedStorage=500)
    ctx = _EnhancedCtx(_FakeRdsClient(instances=[inst]))
    backup = next(r for r in _recs(ctx) if r["CheckCategory"] == "Backup Retention Optimization")
    # No fabricated "$X/month" figure (advisory only).
    assert "/month" not in backup["EstimatedSavings"]
    assert "advisory" in backup["EstimatedSavings"].lower()


def test_backup_retention_excluded_from_savings_but_rendered():
    arn = "arn:aws:rds:us-east-1:1:db:prod"
    enhanced = [
        {"resourceArn": arn, "CheckCategory": "Backup Retention Optimization",
         "EstimatedSavings": "advisory — see Cost Explorer"},
    ]
    from services.rds_logic import resolve_rds_findings
    _coh, _co, kept_enh, savings, count = resolve_rds_findings([], enhanced)
    assert savings == 0.0          # not summed into the headline
    assert len(kept_enh) == 1      # but still rendered
    assert count == 1

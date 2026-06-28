"""Unit tests for the DMS adapter cost-audit HIGH fixes (dms H1/H2).

Drives both the pure enhanced-checks logic (``get_enhanced_dms_checks``) and the
``DmsModule.scan()`` pricing path with a ``SimpleNamespace`` ctx + fake boto3
paginators + a fake PricingEngine, proving:

  - H1  Both the rightsizing/unused lever and the Multi-AZ lever price the
        instance through the deterministic, AZ-pinned
        ``get_dms_instance_monthly_price`` (Single-AZ ``InstanceUsg`` vs Multi-AZ
        ``Multi-AZUsg`` SKU) — never the non-deterministic generic
        ``get_instance_monthly_price`` (the fake raises if that is called).
  - H2  The Multi-AZ -> Single-AZ lever counts the real per-AZ price delta
        (``multi_az_monthly - single_az_monthly``), not 50% of an ambiguous
        lookup that may have returned the Single-AZ SKU (which halved the lever).
  - The shim propagates ``MultiAZ`` onto each instance rec.
  - No double count: a Multi-AZ non-prod instance is owned by the Multi-AZ lever
        and excluded from the 35% rightsizing count and heuristic sources.
  - Pricing unavailable -> $0 advisory (``Counted=False``), excluded from
        ``total_recommendations`` (count hygiene).

Live-validated rates (us-east-1, AWS Pricing API AWSDatabaseMigrationSvc /
Replication Server, 2026-06): ``InstanceUsg:dms.t3.medium`` = $0.0745/hr
($54.385/mo); ``Multi-AZUsg:dms.t3.medium`` = $0.149/hr ($108.77/mo); per-AZ
delta = $54.385/mo (Multi-AZ is exactly 2x Single-AZ).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.dms as dms_adapter
from services.adapters.dms import DmsModule
from services.dms import get_enhanced_dms_checks

# Live-validated monthly prices (×730) for dms.t3.medium, us-east-1.
SINGLE_AZ_MONTHLY = 54.385  # $0.0745/hr
MULTI_AZ_MONTHLY = 108.77  # $0.149/hr
PER_AZ_DELTA = round(MULTI_AZ_MONTHLY - SINGLE_AZ_MONTHLY, 2)  # 54.39


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _ClientError(Exception):
    """botocore-shaped error carrying an AWS error code."""

    def __init__(self, code: str) -> None:
        super().__init__(f"{code}: denied")
        self.response = {"Error": {"Code": code}}


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeDmsClient:
    """Minimal boto3 DMS client driving the enhanced-checks shim."""

    def __init__(self, instances: list[dict[str, Any]], serverless: list[dict[str, Any]] | None = None) -> None:
        self._instances = instances
        self._serverless = serverless or []

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_replication_instances":
            return _FakePaginator([{"ReplicationInstances": self._instances}])
        if name == "describe_replication_configs":
            return _FakePaginator([{"ReplicationConfigs": self._serverless}])
        return _FakePaginator([{}])


class _RaisingPaginator:
    """Paginator whose ``paginate`` raises — drives the shim error handlers."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def paginate(self):  # noqa: ANN201 - boto3 shape
        raise self._error


class _ConfigurableDmsClient:
    """DMS client whose paginators can be wired to raise (error-path tests)."""

    def __init__(
        self,
        instances: list[dict[str, Any]] | None = None,
        *,
        instances_error: Exception | None = None,
        configs_error: Exception | None = None,
    ) -> None:
        self._instances = instances or []
        self._instances_error = instances_error
        self._configs_error = configs_error

    def get_paginator(self, name: str):  # noqa: ANN201 - boto3 shape
        if name == "describe_replication_instances":
            if self._instances_error:
                return _RaisingPaginator(self._instances_error)
            return _FakePaginator([{"ReplicationInstances": self._instances}])
        if name == "describe_replication_configs":
            if self._configs_error:
                return _RaisingPaginator(self._configs_error)
            return _FakePaginator([{"ReplicationConfigs": []}])
        return _FakePaginator([{}])


class _FakeCloudWatch:
    """Returns a canned average CPU per instance id, or raises a canned error."""

    def __init__(self, avg_by_id: dict[str, float] | None = None, error: Exception | None = None) -> None:
        self._avg = avg_by_id or {}
        self._error = error

    def get_metric_statistics(self, **kw: Any) -> dict[str, Any]:
        if self._error:
            raise self._error
        iid = kw["Dimensions"][0]["Value"]
        avg = self._avg.get(iid)
        if avg is None:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": avg}]}


class _FakeDmsPricing:
    """Deterministic AZ-aware DMS price source; records each call.

    Calling the non-deterministic generic ``get_instance_monthly_price`` is a
    hard failure — that proves the adapter consumes the new method (H1).
    """

    def __init__(self, single: float = SINGLE_AZ_MONTHLY, multi: float = MULTI_AZ_MONTHLY, *, zero: bool = False) -> None:
        self.single = single
        self.multi = multi
        self.zero = zero
        self.calls: list[tuple[str, bool]] = []

    def get_dms_instance_monthly_price(self, instance_class: str, *, multi_az: bool = False) -> float:
        self.calls.append((instance_class, multi_az))
        if self.zero:
            return 0.0
        return self.multi if multi_az else self.single

    def get_instance_monthly_price(self, *a: Any, **k: Any) -> float:  # pragma: no cover - guard
        raise AssertionError("adapter must use get_dms_instance_monthly_price, not get_instance_monthly_price")


def _ctx(
    pricing_engine: Any,
    *,
    region: str = "us-east-1",
    dms_client: Any = None,
    cw_client: Any = None,
    fast_mode: bool = False,
) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=pricing_engine,
        pricing_multiplier=1.0,
        region=region,
        fast_mode=fast_mode,
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append((service, action, msg))
    clients = {"dms": dms_client, "cloudwatch": cw_client}
    ctx.client = lambda name, region=None: clients.get(name)
    return ctx


def _instance(iid: str, *, multi_az: bool, klass: str = "dms.t3.medium", status: str = "available") -> dict[str, Any]:
    return {
        "ReplicationInstanceIdentifier": iid,
        "ReplicationInstanceClass": klass,
        "ReplicationInstanceStatus": status,
        "MultiAZ": multi_az,
    }


def _rec(iid: str, *, multi_az: bool, klass: str = "dms.t3.medium") -> dict[str, Any]:
    return {
        "InstanceId": iid,
        "InstanceClass": klass,
        "MultiAZ": multi_az,
        "AvgCPU": "15.0%",
        "Recommendation": "Low CPU utilization",
        "EstimatedSavings": "Rightsize for ~35% savings on instance cost",
        "CheckCategory": "Instance Optimization",
    }


def _unused_rec(iid: str, *, multi_az: bool, klass: str = "dms.t3.medium") -> dict[str, Any]:
    return {
        "InstanceId": iid,
        "InstanceClass": klass,
        "MultiAZ": multi_az,
        "AvgCPU": "2.0%",
        "Recommendation": "Very low CPU utilization - consider stopping if unused",
        "EstimatedSavings": "Full instance cost if terminated",
        "CheckCategory": "Unused DMS Instances",
    }


def _patch_shim(monkeypatch: pytest.MonkeyPatch, recs: list[dict[str, Any]], checks: dict[str, list]) -> None:
    monkeypatch.setattr(
        dms_adapter,
        "get_enhanced_dms_checks",
        lambda ctx: {"recommendations": recs, "checks": checks},
    )


# --------------------------------------------------------------------------- #
# Pure logic — get_enhanced_dms_checks propagates MultiAZ and abstains safely
# --------------------------------------------------------------------------- #
def test_shim_propagates_multi_az_on_rightsizing_rec() -> None:
    cw = _FakeCloudWatch(avg_by_id={"dms-dev-1": 20.0})
    ctx = _ctx(None, dms_client=_FakeDmsClient([_instance("dms-dev-1", multi_az=True)]), cw_client=cw)

    result = get_enhanced_dms_checks(ctx)

    rightsizing = result["checks"]["instance_rightsizing"]
    assert len(rightsizing) == 1
    assert rightsizing[0]["MultiAZ"] is True
    assert rightsizing[0]["InstanceClass"] == "dms.t3.medium"
    assert result["checks"]["unused_instances"] == []


def test_shim_propagates_multi_az_on_unused_rec() -> None:
    cw = _FakeCloudWatch(avg_by_id={"dms-x": 2.0})
    ctx = _ctx(None, dms_client=_FakeDmsClient([_instance("dms-x", multi_az=False)]), cw_client=cw)

    result = get_enhanced_dms_checks(ctx)

    unused = result["checks"]["unused_instances"]
    assert len(unused) == 1
    assert unused[0]["MultiAZ"] is False
    assert result["checks"]["instance_rightsizing"] == []


def test_shim_abstains_when_no_datapoints() -> None:
    cw = _FakeCloudWatch(avg_by_id={})  # -> empty Datapoints
    ctx = _ctx(None, dms_client=_FakeDmsClient([_instance("dms-new", multi_az=True)]), cw_client=cw)

    result = get_enhanced_dms_checks(ctx)

    assert result["recommendations"] == []
    assert ctx.permissions == []


def test_shim_classifies_cloudwatch_accessdenied() -> None:
    cw = _FakeCloudWatch(error=_ClientError("AccessDenied"))
    ctx = _ctx(None, dms_client=_FakeDmsClient([_instance("dms-1", multi_az=True)]), cw_client=cw)

    result = get_enhanced_dms_checks(ctx)

    assert result["recommendations"] == []
    assert ctx.permissions and ctx.permissions[0][0] == "dms"  # permission_issue recorded, no fabricated rec


# --------------------------------------------------------------------------- #
# scan() — H1: deterministic AZ-pinned SKU consumed for the rightsizing lever
# --------------------------------------------------------------------------- #
def test_scan_single_az_rightsizing_uses_single_az_sku(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _rec("dms-prod-1", multi_az=False)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    # 35% of the Single-AZ monthly price (deterministic InstanceUsg SKU).
    assert findings.total_monthly_savings == pytest.approx(SINGLE_AZ_MONTHLY * 0.35)
    assert ("dms.t3.medium", False) in pricing.calls
    assert ("dms.t3.medium", True) not in pricing.calls
    assert findings.total_recommendations == 1


def test_scan_multi_az_prod_rightsizing_uses_multi_az_sku(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Multi-AZ but production (no non-prod keyword) instance is NOT eligible
    # for the Multi-AZ lever; it must still price the rightsizing 35% on the
    # Multi-AZ SKU (H1: the AZ flag flows into the deterministic lookup).
    rec = _rec("dms-prod-2", multi_az=True)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    assert "multi_az_review" not in findings.sources
    assert findings.total_monthly_savings == pytest.approx(MULTI_AZ_MONTHLY * 0.35)
    assert ("dms.t3.medium", True) in pricing.calls
    assert findings.total_recommendations == 1


# --------------------------------------------------------------------------- #
# scan() — H2: Multi-AZ -> Single-AZ lever counts the real per-AZ delta
# --------------------------------------------------------------------------- #
def test_scan_multi_az_nonprod_counts_per_az_delta_no_double_count(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _rec("dms-dev-1", multi_az=True)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    block = findings.sources["multi_az_review"]
    assert block.count == 1
    az_rec = block.recommendations[0]
    # Real per-AZ delta, NOT 50% of an ambiguous lookup (== $27.19 here).
    assert az_rec["EstimatedMonthlySavings"] == pytest.approx(PER_AZ_DELTA)
    assert az_rec["EstimatedMonthlySavings"] != pytest.approx(SINGLE_AZ_MONTHLY * 0.5)
    assert az_rec["Counted"] is True
    assert az_rec["AuditBasis"]["formula"] == "Multi-AZ monthly - Single-AZ monthly (real per-AZ delta)"
    assert az_rec["AuditBasis"]["single_az_monthly"] == pytest.approx(round(SINGLE_AZ_MONTHLY, 2))
    assert az_rec["AuditBasis"]["multi_az_monthly"] == pytest.approx(round(MULTI_AZ_MONTHLY, 2))

    # No double count: the per-AZ delta is the ONLY counted dollar; the same
    # instance is excluded from the 35% rightsizing sum and from its source.
    assert findings.total_monthly_savings == pytest.approx(PER_AZ_DELTA)
    assert "instance_rightsizing" not in findings.sources
    assert findings.total_recommendations == 1
    # Both AZ SKUs were priced (delta), proving the deterministic method.
    assert ("dms.t3.medium", True) in pricing.calls
    assert ("dms.t3.medium", False) in pricing.calls


def test_scan_multi_az_pricing_unavailable_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _rec("dms-dev-2", multi_az=True)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing(zero=True)
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    block = findings.sources["multi_az_review"]
    az_rec = block.recommendations[0]
    assert az_rec["EstimatedMonthlySavings"] == 0.0
    assert az_rec["Counted"] is False
    assert "advisory" in az_rec["EstimatedSavings"].lower()
    assert findings.total_monthly_savings == 0.0
    # Count hygiene: a $0 advisory rec renders but is excluded from the count.
    assert findings.total_recommendations == 0


def test_scan_no_pricing_engine_emits_no_counted_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _rec("dms-prod-9", multi_az=False)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    ctx = _ctx(None)

    findings = DmsModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# scan() — end-to-end through the real shim (fake boto3) proves MultiAZ flow
# --------------------------------------------------------------------------- #
def test_scan_end_to_end_multi_az_dev_instance() -> None:
    cw = _FakeCloudWatch(avg_by_id={"dms-dev-e2e": 15.0})
    dms_client = _FakeDmsClient([_instance("dms-dev-e2e", multi_az=True)])
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing, dms_client=dms_client, cw_client=cw)

    findings = DmsModule().scan(ctx)

    assert findings.service_name == "DMS"
    assert "multi_az_review" in findings.sources
    assert "instance_rightsizing" not in findings.sources
    assert findings.total_monthly_savings == pytest.approx(PER_AZ_DELTA)
    assert findings.total_recommendations == 1


# --------------------------------------------------------------------------- #
# scan() — L1: unused vs rightsizing factor split (1.0 vs 0.35)
# --------------------------------------------------------------------------- #
def test_scan_unused_counts_full_instance_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    # An *unused* instance recovers its FULL cost on termination (factor 1.0),
    # NOT 35% — the rightsizing factor must not bleed onto the unused lever.
    rec = _unused_rec("dms-idle-1", multi_az=False)
    _patch_shim(monkeypatch, [rec], {"unused_instances": [rec], "instance_rightsizing": [], "serverless_migration": []})
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    assert findings.total_monthly_savings == pytest.approx(SINGLE_AZ_MONTHLY * 1.0)
    assert findings.total_monthly_savings != pytest.approx(SINGLE_AZ_MONTHLY * 0.35)
    assert ("dms.t3.medium", False) in pricing.calls
    assert findings.total_recommendations == 1

    # L5: the enriched rec carries the counted dollar so the card renders the
    # same figure the headline sums.
    enriched = findings.sources["unused_instances"].recommendations[0]
    assert enriched["EstimatedMonthlySavings"] == pytest.approx(SINGLE_AZ_MONTHLY * 1.0)
    assert enriched["Counted"] is True
    assert enriched["EstimatedSavings"] == f"${SINGLE_AZ_MONTHLY * 1.0:.2f}/month"
    assert enriched["AuditBasis"]["factor"] == 1.0
    assert enriched["AuditBasis"]["formula"] == "full instance monthly (terminate)"


# --------------------------------------------------------------------------- #
# scan() — L5: rightsizing rec also carries a counted dollar (render parity)
# --------------------------------------------------------------------------- #
def test_scan_rightsizing_rec_renders_counted_dollar(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _rec("dms-prod-7", multi_az=False)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing()
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    enriched = findings.sources["instance_rightsizing"].recommendations[0]
    assert enriched["EstimatedMonthlySavings"] == pytest.approx(SINGLE_AZ_MONTHLY * 0.35)
    assert enriched["Counted"] is True
    assert enriched["EstimatedSavings"] == f"${SINGLE_AZ_MONTHLY * 0.35:.2f}/month"
    assert enriched["AuditBasis"]["factor"] == 0.35


def test_scan_pricing_miss_renders_prose_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pricing returns 0 -> no counted dollar; the rec still renders (prose) but
    # carries no EstimatedMonthlySavings and is excluded from the headline.
    rec = _rec("dms-prod-8", multi_az=False)
    _patch_shim(monkeypatch, [rec], {"instance_rightsizing": [rec], "unused_instances": [], "serverless_migration": []})
    pricing = _FakeDmsPricing(zero=True)
    ctx = _ctx(pricing)

    findings = DmsModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0
    enriched = findings.sources["instance_rightsizing"].recommendations[0]
    assert "EstimatedMonthlySavings" not in enriched


# --------------------------------------------------------------------------- #
# shim — L4: billable non-`available` states are in scope; pre-live/terminal out
# --------------------------------------------------------------------------- #
def test_shim_includes_billable_modifying_state() -> None:
    cw = _FakeCloudWatch(avg_by_id={"dms-mod": 2.0})
    ctx = _ctx(
        None,
        dms_client=_FakeDmsClient([_instance("dms-mod", multi_az=False, status="modifying")]),
        cw_client=cw,
    )

    result = get_enhanced_dms_checks(ctx)

    unused = result["checks"]["unused_instances"]
    assert len(unused) == 1
    assert unused[0]["InstanceId"] == "dms-mod"


def test_shim_excludes_non_billable_creating_state() -> None:
    cw = _FakeCloudWatch(avg_by_id={"dms-new": 2.0})
    ctx = _ctx(
        None,
        dms_client=_FakeDmsClient([_instance("dms-new", multi_az=False, status="creating")]),
        cw_client=cw,
    )

    result = get_enhanced_dms_checks(ctx)

    assert result["recommendations"] == []


# --------------------------------------------------------------------------- #
# shim — L2: serverless-configs error is classified, not silently swallowed
# --------------------------------------------------------------------------- #
def test_shim_classifies_serverless_configs_accessdenied() -> None:
    client = _ConfigurableDmsClient(instances=[], configs_error=_ClientError("AccessDenied"))
    ctx = _ctx(None, dms_client=client, cw_client=_FakeCloudWatch())

    result = get_enhanced_dms_checks(ctx)

    assert ctx.permissions and ctx.permissions[0][0] == "dms"
    assert "describe_replication_configs" in ctx.permissions[0][2]
    assert result["recommendations"] == []


# --------------------------------------------------------------------------- #
# shim — L3: outer error promotes AccessDenied to permission_issue, else warn
# --------------------------------------------------------------------------- #
def test_shim_outer_accessdenied_routes_to_permission_issue() -> None:
    client = _ConfigurableDmsClient(instances_error=_ClientError("AccessDenied"))
    ctx = _ctx(None, dms_client=client, cw_client=_FakeCloudWatch())

    result = get_enhanced_dms_checks(ctx)

    assert ctx.permissions and ctx.permissions[0][0] == "dms"
    assert "Could not analyze DMS resources" in ctx.permissions[0][2]
    assert ctx.warnings == []
    assert result["recommendations"] == []


def test_shim_outer_non_permission_routes_to_warn() -> None:
    client = _ConfigurableDmsClient(instances_error=RuntimeError("boom"))
    ctx = _ctx(None, dms_client=client, cw_client=_FakeCloudWatch())

    result = get_enhanced_dms_checks(ctx)

    assert ctx.warnings and ctx.warnings[0][0] == "dms"
    assert ctx.permissions == []
    assert result["recommendations"] == []

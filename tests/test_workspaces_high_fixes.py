"""Unit tests for the WorkSpaces HIGH cost-correctness fixes (C2, C3, C5).

Mirrors tests/test_lambda_audit_fixes.py / tests/test_audit_fixes_counted_dollars.py:
a SimpleNamespace ctx + monkeypatched enhanced-checks helper drives the adapter's
pure pricing logic, and fake boto3 paginators + a fake CloudWatch client drive the
shim's metric-gating / propagation / error-classification paths.

  - C2  AlwaysOn->AutoStop saving is gated on measured session hours and computed
        as AlwaysOn - (AutoStop fee + hourly x hours); no metric, an unknown bundle,
        or a wrong-signed (heavy-usage) delta -> $0 advisory.
  - C3  billing_mode & unused recs carry the real ComputeType so scan() prices the
        actual bundle (VALUE $25 / GRAPHICSPRO $999), not the STANDARD $35 default.
  - C5  a STOPPED AUTO_STOP WorkSpace counts only the residual monthly fee; an
        ALWAYS_ON WorkSpace counts the full bundle cost; unknown running mode abstains.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.workspaces as adapter_mod
import services.workspaces as shim_mod
from services.adapters.workspaces import WorkspacesModule
from services.workspaces import (
    WORKSPACE_AUTOSTOP_PRICING,
    WORKSPACE_BUNDLE_MONTHLY,
    WORKSPACES_HOURS_PER_MONTH,
    WORKSPACES_SESSION_LOOKBACK_DAYS,
    get_enhanced_workspaces_checks,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
def _recording_ctx(**kw: Any) -> SimpleNamespace:
    """ctx that records warn / permission_issue calls."""
    ctx = SimpleNamespace(
        pricing_multiplier=kw.pop("pricing_multiplier", 1.0),
        pricing_engine=kw.pop("pricing_engine", object()),
        fast_mode=kw.pop("fast_mode", False),
        region="us-east-1",
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(
        (service, action, msg)
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeWorkspacesClient:
    def __init__(self, workspaces: list[dict[str, Any]]) -> None:
        self._workspaces = workspaces

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator([{"Workspaces": self._workspaces}])


class _FakeCloudWatch:
    """Returns canned UserConnected datapoints, or raises a canned error."""

    def __init__(self, datapoints: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._datapoints = datapoints
        self._error = error

    def get_metric_statistics(self, **_kw: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"Datapoints": self._datapoints if self._datapoints is not None else []}


def _client_factory(ws: Any, cw: Any):
    def _client(name: str, region: str | None = None):
        return {"workspaces": ws, "cloudwatch": cw}[name]

    return _client


def _ws(
    workspace_id: str,
    *,
    state: str = "AVAILABLE",
    running_mode: str = "ALWAYS_ON",
    compute: str = "STANDARD",
) -> dict[str, Any]:
    return {
        "WorkspaceId": workspace_id,
        "State": state,
        "WorkspaceProperties": {"RunningMode": running_mode, "ComputeTypeName": compute},
    }


def _expected_billing_saving(compute_type: str, hours: float, mult: float = 1.0) -> float:
    always_on = WORKSPACE_BUNDLE_MONTHLY[compute_type]
    fee, hourly = WORKSPACE_AUTOSTOP_PRICING[compute_type]
    return (always_on - (fee + hourly * hours)) * mult


def _patch_shim(monkeypatch: pytest.MonkeyPatch, recs: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        adapter_mod,
        "get_enhanced_workspaces_checks",
        lambda ctx: {"recommendations": [dict(r) for r in recs]},
    )


# --------------------------------------------------------------------------- #
# Adapter metadata / contract
# --------------------------------------------------------------------------- #
def test_adapter_declares_cloudwatch_contract() -> None:
    mod = WorkspacesModule()
    assert mod.requires_cloudwatch is True
    assert mod.reads_fast_mode is True
    assert "cloudwatch" in mod.required_clients()


# --------------------------------------------------------------------------- #
# C2 — AutoStop saving is metric-gated and AlwaysOn - (fee + hourly x hours)
# --------------------------------------------------------------------------- #
def test_c2_billing_mode_counted_from_measured_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {
        "WorkspaceId": "ws-1",
        "ComputeType": "STANDARD",
        "MeasuredMonthlyHours": 40.0,
        "CheckCategory": "Billing Mode Optimization",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    expected = _expected_billing_saving("STANDARD", 40.0)  # 35 - (9.75 + 0.30*40) = 13.25
    assert expected == pytest.approx(13.25, abs=0.001)
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.01)
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(13.25, abs=0.01)
    assert emitted.get("Counted") is not False
    assert emitted["EstimatedSavings"].startswith("$13.25")
    basis = emitted["AuditBasis"]
    assert basis["always_on_monthly"] == 35.0
    assert basis["autostop_fee_monthly"] == 9.75
    assert basis["autostop_hourly"] == 0.30
    assert basis["measured_monthly_hours"] == 40.0


def test_c2_billing_mode_wrong_signed_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    # 120 connected hrs/mo: AutoStop ($9.75 + 0.30*120 = $45.75) costs MORE than
    # AlwaysOn ($35). Saving must NOT be fabricated — it is a $0 advisory.
    rec = {
        "WorkspaceId": "ws-heavy",
        "ComputeType": "STANDARD",
        "MeasuredMonthlyHours": 120.0,
        "CheckCategory": "Billing Mode Optimization",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == 0.0
    assert emitted["Counted"] is False
    assert "not cheaper" in emitted["EstimatedSavings"]


def test_c2_billing_mode_no_metric_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {
        "WorkspaceId": "ws-nometric",
        "ComputeType": "STANDARD",
        "CheckCategory": "Billing Mode Optimization",
    }  # no MeasuredMonthlyHours (fast mode / no datapoints)
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["Counted"] is False
    assert "UserConnected" in emitted["EstimatedSavings"]


def test_c2_billing_mode_unknown_bundle_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    # GRAPHICS (legacy) has no validated AutoStop price -> advisory, never $0-priced
    # against a fabricated rate.
    rec = {
        "WorkspaceId": "ws-gfx",
        "ComputeType": "GRAPHICS",
        "MeasuredMonthlyHours": 10.0,
        "CheckCategory": "Billing Mode Optimization",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == 0.0
    assert findings.sources["enhanced_checks"].recommendations[0]["Counted"] is False


# --------------------------------------------------------------------------- #
# C3 — recs price the actual bundle, not the STANDARD default
# --------------------------------------------------------------------------- #
def test_c3_billing_mode_prices_actual_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # VALUE bundle: AlwaysOn $25 (not the old STANDARD $35 default). At 40 hrs:
    # 25 - (7.25 + 0.22*40) = 25 - 16.05 = $8.95.
    rec = {
        "WorkspaceId": "ws-value",
        "ComputeType": "VALUE",
        "MeasuredMonthlyHours": 40.0,
        "CheckCategory": "Billing Mode Optimization",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    expected = _expected_billing_saving("VALUE", 40.0)
    assert expected == pytest.approx(8.95, abs=0.001)
    assert findings.total_monthly_savings == pytest.approx(8.95, abs=0.01)
    # Had it defaulted to STANDARD ($35) the saving would have been 13.25, not 8.95.
    assert findings.total_monthly_savings != pytest.approx(13.25, abs=0.01)


def test_c3_compute_type_from_workspace_properties_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # A rec lacking ComputeType but carrying WorkspaceProperties still resolves.
    rec = {
        "WorkspaceId": "ws-props",
        "WorkspaceProperties": {"ComputeTypeName": "VALUE"},
        "MeasuredMonthlyHours": 40.0,
        "CheckCategory": "Billing Mode Optimization",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())
    assert findings.total_monthly_savings == pytest.approx(8.95, abs=0.01)


# --------------------------------------------------------------------------- #
# C5 — unused/stopped WorkSpaces priced by running mode
# --------------------------------------------------------------------------- #
def test_c5_unused_always_on_counts_full_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # ALWAYS_ON GRAPHICSPRO bills the full $999/mo even when not in use. This also
    # exercises C3 (the bundle is GRAPHICSPRO, not the STANDARD $35 default).
    rec = {
        "WorkspaceId": "ws-gpro",
        "State": "ERROR",
        "RunningMode": "ALWAYS_ON",
        "ComputeType": "GRAPHICSPRO",
        "CheckCategory": "Unused WorkSpaces",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == pytest.approx(999.0, abs=0.01)
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(999.0, abs=0.01)
    assert emitted.get("Counted") is not False
    assert emitted["AuditBasis"]["running_mode"] == "ALWAYS_ON"


def test_c5_unused_autostop_counts_only_residual_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    # STOPPED + AUTO_STOP STANDARD already bills only the $9.75/mo fixed fee — NOT
    # the full $35 AlwaysOn cost. Terminating saves only the residual fee.
    rec = {
        "WorkspaceId": "ws-stopped",
        "State": "STOPPED",
        "RunningMode": "AUTO_STOP",
        "ComputeType": "STANDARD",
        "CheckCategory": "Unused WorkSpaces",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == pytest.approx(9.75, abs=0.01)
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(9.75, abs=0.01)
    # NOT the full bundle cost.
    assert emitted["EstimatedMonthlySavings"] != pytest.approx(35.0, abs=0.01)


def test_c5_unused_unknown_running_mode_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail safe: a termination rec with no running mode must not assert a saving.
    rec = {
        "WorkspaceId": "ws-unknown",
        "State": "SUSPENDED",
        "RunningMode": None,
        "ComputeType": "STANDARD",
        "CheckCategory": "Unused WorkSpaces",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == 0.0
    assert findings.sources["enhanced_checks"].recommendations[0]["Counted"] is False


# --------------------------------------------------------------------------- #
# Region scaling + count hygiene + rightsizing pass-through
# --------------------------------------------------------------------------- #
def test_region_multiplier_applied_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {
        "WorkspaceId": "ws-eu",
        "State": "ERROR",
        "RunningMode": "ALWAYS_ON",
        "ComputeType": "STANDARD",
        "CheckCategory": "Unused WorkSpaces",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx(pricing_multiplier=1.5))
    assert findings.total_monthly_savings == pytest.approx(35.0 * 1.5, abs=0.01)


def test_count_hygiene_excludes_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {  # counted
            "WorkspaceId": "ws-a",
            "State": "ERROR",
            "RunningMode": "ALWAYS_ON",
            "ComputeType": "STANDARD",
            "CheckCategory": "Unused WorkSpaces",
        },
        {  # advisory (no metric)
            "WorkspaceId": "ws-b",
            "ComputeType": "STANDARD",
            "CheckCategory": "Billing Mode Optimization",
        },
    ]
    _patch_shim(monkeypatch, recs)
    findings = WorkspacesModule().scan(_recording_ctx())

    # Two recs rendered, only one counted into the headline.
    assert findings.sources["enhanced_checks"].count == 2
    assert findings.total_recommendations == 1
    assert findings.total_monthly_savings == pytest.approx(35.0, abs=0.01)


def test_bundle_rightsizing_is_advisory_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A high-tier bundle is not evidence of over-provisioning and WorkSpaces
    # publishes no default utilization metric, so the downsizing delta is a $0
    # advisory carrying the potential figure — never a counted dollar.
    rec = {
        "WorkspaceId": "ws-rs",
        "CurrentBundle": "POWERPRO",
        "RecommendedBundle": "STANDARD",
        "PotentialMonthlySavings": 105.0,
        "Counted": False,
        "CheckCategory": "Bundle Rightsizing",
    }
    _patch_shim(monkeypatch, [rec])
    findings = WorkspacesModule().scan(_recording_ctx())

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["Counted"] is False
    assert emitted["EstimatedMonthlySavings"] == 0.0
    assert emitted["EstimatedSavings"].startswith("$0.00/month")
    assert "105.00" in emitted["EstimatedSavings"]  # potential figure preserved
    # Advisory rec is excluded from the counted rec-count headline.
    assert findings.total_recommendations == 0


# --------------------------------------------------------------------------- #
# Shim — ComputeType propagation, CloudWatch gating, error classification
# --------------------------------------------------------------------------- #
def test_shim_propagates_compute_type_to_both_recs() -> None:
    ws = _FakeWorkspacesClient(
        [
            _ws("ws-on", state="AVAILABLE", running_mode="ALWAYS_ON", compute="POWERPRO"),
            _ws("ws-stopped", state="STOPPED", running_mode="AUTO_STOP", compute="VALUE"),
        ]
    )
    cw = _FakeCloudWatch(datapoints=[])  # no datapoints -> no MeasuredMonthlyHours
    ctx = _recording_ctx()
    ctx.client = _client_factory(ws, cw)

    recs = get_enhanced_workspaces_checks(ctx)["recommendations"]
    billing = next(r for r in recs if r["CheckCategory"] == "Billing Mode Optimization")
    unused = next(r for r in recs if r["CheckCategory"] == "Unused WorkSpaces")
    assert billing["ComputeType"] == "POWERPRO"
    assert unused["ComputeType"] == "VALUE"
    assert unused["RunningMode"] == "AUTO_STOP"
    # Fabricated free-text savings strings are gone (single-sourced in the adapter).
    assert "EstimatedSavings" not in billing
    assert "EstimatedSavings" not in unused


def test_shim_attaches_measured_hours_from_cloudwatch() -> None:
    # 72 hourly datapoints with a connection -> scaled to a 730h month.
    datapoints = [{"Maximum": 1} for _ in range(72)]
    ws = _FakeWorkspacesClient([_ws("ws-on", compute="STANDARD")])
    cw = _FakeCloudWatch(datapoints=datapoints)
    ctx = _recording_ctx()
    ctx.client = _client_factory(ws, cw)

    recs = get_enhanced_workspaces_checks(ctx)["recommendations"]
    billing = next(r for r in recs if r["CheckCategory"] == "Billing Mode Optimization")
    window_hours = WORKSPACES_SESSION_LOOKBACK_DAYS * 24
    expected = round(72 * (WORKSPACES_HOURS_PER_MONTH / window_hours), 1)
    assert billing["MeasuredMonthlyHours"] == pytest.approx(expected, abs=0.05)


def test_shim_zero_usage_window_is_zero_hours() -> None:
    # Datapoints present but never connected (Maximum 0) -> genuine 0.0 hours,
    # NOT None (a never-used AlwaysOn WorkSpace is the strongest AutoStop case).
    datapoints = [{"Maximum": 0} for _ in range(100)]
    ws = _FakeWorkspacesClient([_ws("ws-idle", compute="STANDARD")])
    cw = _FakeCloudWatch(datapoints=datapoints)
    ctx = _recording_ctx()
    ctx.client = _client_factory(ws, cw)

    billing = next(
        r
        for r in get_enhanced_workspaces_checks(ctx)["recommendations"]
        if r["CheckCategory"] == "Billing Mode Optimization"
    )
    assert billing["MeasuredMonthlyHours"] == 0.0


def test_shim_fast_mode_skips_cloudwatch_and_warns_once() -> None:
    ws = _FakeWorkspacesClient([_ws("ws-on", compute="STANDARD")])
    # A CloudWatch call in fast mode would raise this AssertionError.
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _recording_ctx(fast_mode=True)
    ctx.client = _client_factory(ws, cw)

    recs = get_enhanced_workspaces_checks(ctx)["recommendations"]
    billing = next(r for r in recs if r["CheckCategory"] == "Billing Mode Optimization")
    assert "MeasuredMonthlyHours" not in billing  # CW skipped -> advisory downstream
    assert sum("Fast mode" in msg for _svc, msg in ctx.warnings) == 1


def test_shim_cloudwatch_access_denied_is_permission_issue() -> None:
    ws = _FakeWorkspacesClient([_ws("ws-on", compute="STANDARD")])
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: no cloudwatch"))
    ctx = _recording_ctx()
    ctx.client = _client_factory(ws, cw)

    recs = get_enhanced_workspaces_checks(ctx)["recommendations"]
    billing = next(r for r in recs if r["CheckCategory"] == "Billing Mode Optimization")
    assert "MeasuredMonthlyHours" not in billing
    assert ctx.permissions, "CloudWatch AccessDenied must be a permission_issue"
    assert any(action == "cloudwatch:GetMetricStatistics" for _s, action, _m in ctx.permissions)


# --------------------------------------------------------------------------- #
# End-to-end: shim (with CloudWatch) feeds the adapter's counted dollar
# --------------------------------------------------------------------------- #
def test_scan_end_to_end_counts_autostop_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    datapoints = [{"Maximum": 1} for _ in range(48)]  # 48 connected hrs in window
    ws = _FakeWorkspacesClient([_ws("ws-on", compute="STANDARD")])
    cw = _FakeCloudWatch(datapoints=datapoints)
    ctx = _recording_ctx()
    ctx.client = _client_factory(ws, cw)

    findings = WorkspacesModule().scan(ctx)

    window_hours = WORKSPACES_SESSION_LOOKBACK_DAYS * 24
    hours = round(48 * (WORKSPACES_HOURS_PER_MONTH / window_hours), 1)
    expected = _expected_billing_saving("STANDARD", hours)
    assert expected > 0  # 48 hrs is below the ~84h break-even, so AutoStop is cheaper
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.05)
    assert findings.total_recommendations == 1

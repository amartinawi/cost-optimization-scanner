"""Unit tests for the Lightsail HIGH cost-audit fixes (H1–H4).

Drives the pure pricing logic and the ``scan()`` path with a SimpleNamespace
ctx + monkeypatched enhanced-checks helper + a fake boto3 Lightsail client,
mirroring ``tests/test_lambda_audit_fixes.py`` /
``tests/test_audit_fixes_counted_dollars.py``.

  - H1  Bundle costs are the live-validated AWS list prices ($5/7/12/24/44/84/164),
        not the old synthetic ×2 geometric series.
  - H2  Windows bundles are OS-aware priced (NOT the old $20 default).
  - H3  Unknown / missing bundle ids → $0 advisory (Counted=False) + ctx.warn,
        never a fabricated default dollar; the $20 default and the
        ``or "medium_2_0"`` fallback are gone.
  - H4  Unused static IPs are counted at $0.005/hr × 730, region-scaled, so the
        counted dollar equals the rendered card.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.lightsail as adapter_mod
import services.lightsail as shim_mod
from services.adapters.lightsail import LightsailModule
from services.lightsail import (
    HOURS_PER_MONTH,
    LIGHTSAIL_UNUSED_STATIC_IP_HOURLY,
    _parse_bundle_id,
    get_lightsail_bundle_cost,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
def _ctx(**kw: Any) -> SimpleNamespace:
    """ctx recording warn / permission_issue calls."""
    ctx = SimpleNamespace(
        pricing_multiplier=kw.pop("pricing_multiplier", 1.0),
        region=kw.pop("region", "us-east-1"),
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


def _result(checks: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a get_enhanced_lightsail_checks-shaped result from category lists.

    ``recommendations`` references the SAME dict objects as ``checks`` so the
    adapter's in-place mutation is observable from both views.
    """
    flat: list[dict[str, Any]] = []
    for recs in checks.values():
        flat.extend(recs)
    return {"recommendations": flat, "checks": checks}


def _idle(bundle_id: str, *, os_name: str = "Linux", name: str = "i1") -> dict[str, Any]:
    return {
        "InstanceName": name,
        "State": "stopped",
        "BundleId": bundle_id,
        "OperatingSystem": os_name,
        "CheckCategory": "Idle Resource Cleanup",
        "EstimatedSavings": "$0.00/month — pending pricing",
    }


def _static_ip(name: str = "ip1") -> dict[str, Any]:
    return {
        "StaticIpName": name,
        "IpAddress": "1.2.3.4",
        "CheckCategory": "Unused Resource Cleanup",
        "EstimatedSavings": "$0.00/month — pending pricing",
    }


def _oversized(bundle_id: str = "xlarge_2_0") -> dict[str, Any]:
    return {
        "InstanceName": "big",
        "BundleId": bundle_id,
        "State": "running",
        "CheckCategory": "Instance Rightsizing",
        "EstimatedSavings": "$0.00/month — advisory",
    }


# --------------------------------------------------------------------------- #
# H1 — pure logic: live-validated Linux bundle list prices, not the ×2 series
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bundle_id,expected",
    [
        ("nano_2_0", 5.00),
        ("micro_2_0", 7.00),
        ("small_2_0", 12.00),
        ("medium_2_0", 24.00),
        ("large_2_0", 44.00),
        ("xlarge_2_0", 84.00),
        ("2xlarge_2_0", 164.00),
        # gen-3 resolves to the same standard-bundle price as gen-2.
        ("medium_3_0", 24.00),
        ("2xlarge_3_0", 164.00),
    ],
)
def test_h1_linux_bundle_list_prices(bundle_id: str, expected: float) -> None:
    assert get_lightsail_bundle_cost(bundle_id) == expected


def test_h1_not_old_geometric_series() -> None:
    # The old synthetic values (3.50 / 6.86 / 13.72 / 27.45 / 54.90 / 80 / 160)
    # are gone — medium is the real $24, not the synthetic $27.45.
    assert get_lightsail_bundle_cost("medium_2_0") == 24.00
    assert get_lightsail_bundle_cost("medium_2_0") != 27.45


# --------------------------------------------------------------------------- #
# H2 — pure logic: OS-aware Windows prices (NOT the old $20 default)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bundle_id,expected",
    [
        ("nano_win_2_0", 9.50),
        ("micro_win_2_0", 14.00),
        ("small_win_2_0", 22.00),
        ("medium_win_2_0", 44.00),
        ("large_win_2_0", 74.00),
        ("xlarge_win_2_0", 124.00),
        ("2xlarge_win_2_0", 244.00),
        ("medium_win_3_0", 44.00),
    ],
)
def test_h2_windows_bundle_prices(bundle_id: str, expected: float) -> None:
    assert get_lightsail_bundle_cost(bundle_id) == expected


def test_h2_windows_not_priced_as_linux_or_default() -> None:
    # medium_win is $44 (Windows licensing premium), NOT $24 (Linux) and NOT
    # the old $20 fall-through default.
    assert get_lightsail_bundle_cost("medium_win_2_0") == 44.00
    assert get_lightsail_bundle_cost("medium_win_2_0") != 24.00
    assert get_lightsail_bundle_cost("medium_win_2_0") != 20.00


def test_h2_parse_detects_windows() -> None:
    assert _parse_bundle_id("medium_win_2_0") == ("medium", True)
    assert _parse_bundle_id("medium_2_0") == ("medium", False)


# --------------------------------------------------------------------------- #
# H3 — pure logic: unknown / malformed / missing bundle id → None (no default)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bundle_id", ["", "gpu_2_0", "weird", "memory_optimized_x", "  "])
def test_h3_unknown_bundle_returns_none(bundle_id: str) -> None:
    assert get_lightsail_bundle_cost(bundle_id) is None


def test_h3_no_twenty_dollar_default_constant() -> None:
    # The $20 default constant is removed from the module entirely.
    assert not hasattr(shim_mod, "_DEFAULT_BUNDLE_COST")
    assert not hasattr(shim_mod, "_BUNDLE_COSTS")


# --------------------------------------------------------------------------- #
# Scan path — H1: counted idle Linux instance == validated bundle price
# --------------------------------------------------------------------------- #
def test_scan_h1_idle_linux_counts_list_price(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _idle("medium_2_0")
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"idle_instances": [rec]}),
    )
    findings = LightsailModule().scan(_ctx())

    assert findings.total_monthly_savings == pytest.approx(24.00)
    assert findings.total_recommendations == 1
    emitted = findings.sources["idle_instances"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(24.00)
    assert emitted["EstimatedSavings"] == "$24.00/month"  # counted == rendered
    assert emitted.get("Counted") is not False
    assert emitted["AuditBasis"]["rate"] == 24.00
    assert emitted["AuditBasis"]["bundle_id"] == "medium_2_0"


# --------------------------------------------------------------------------- #
# Scan path — H2: Windows idle instance priced at Windows rate, not $20
# --------------------------------------------------------------------------- #
def test_scan_h2_idle_windows_counts_windows_price(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _idle("medium_win_2_0", os_name="Windows")
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"idle_instances": [rec]}),
    )
    findings = LightsailModule().scan(_ctx())

    # $44.00 (Windows), NOT $20 (old default) and NOT $24 (Linux medium).
    assert findings.total_monthly_savings == pytest.approx(44.00)
    emitted = findings.sources["idle_instances"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(44.00)
    assert emitted["AuditBasis"]["operating_system"] == "Windows"


# --------------------------------------------------------------------------- #
# Scan path — H3: unknown bundle id → $0 advisory + warn, not counted
# --------------------------------------------------------------------------- #
def test_scan_h3_unknown_bundle_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _idle("small_2_0", name="ok")
    bad = _idle("gpu_4_0", name="weird")
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"idle_instances": [good, bad]}),
    )
    ctx = _ctx()
    findings = LightsailModule().scan(ctx)

    # Only the recognized bundle counts; the unknown one is a $0 advisory.
    assert findings.total_monthly_savings == pytest.approx(12.00)
    assert findings.total_recommendations == 1  # advisory excluded from count
    assert bad["Counted"] is False
    assert bad["EstimatedMonthlySavings"] == 0.0
    assert bad["EstimatedSavings"].startswith("$0.00/month")
    assert any("gpu_4_0" in msg for _svc, msg in ctx.warnings)


# --------------------------------------------------------------------------- #
# Scan path — H4: unused static IP counted at $0.005/hr × 730, region-scaled
# --------------------------------------------------------------------------- #
def test_scan_h4_static_ip_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _static_ip()
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"unused_static_ips": [rec]}),
    )
    findings = LightsailModule().scan(_ctx())

    expected = round(LIGHTSAIL_UNUSED_STATIC_IP_HOURLY * HOURS_PER_MONTH, 2)
    assert expected == 3.65
    assert findings.total_monthly_savings == pytest.approx(3.65)
    emitted = findings.sources["unused_static_ips"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(3.65)
    assert emitted["EstimatedSavings"] == "$3.65/month"  # counted == displayed
    assert emitted["AuditBasis"]["rate"] == 0.005


def test_scan_h4_static_ip_region_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _static_ip()
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"unused_static_ips": [rec]}),
    )
    findings = LightsailModule().scan(_ctx(pricing_multiplier=1.2))

    # 3.65 × 1.2 = 4.38; the card string and the counted number both scale.
    assert findings.total_monthly_savings == pytest.approx(4.38)
    emitted = findings.sources["unused_static_ips"].recommendations[0]
    assert emitted["EstimatedSavings"] == "$4.38/month"


# --------------------------------------------------------------------------- #
# Scan path — region multiplier applied once to bundle dollars
# --------------------------------------------------------------------------- #
def test_scan_bundle_multiplier_applied_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _idle("medium_2_0")
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"idle_instances": [rec]}),
    )
    findings = LightsailModule().scan(_ctx(pricing_multiplier=1.5))
    assert findings.total_monthly_savings == pytest.approx(24.00 * 1.5)


# --------------------------------------------------------------------------- #
# Scan path — oversized rightsizing stays a $0 advisory (Cluster F desync)
# --------------------------------------------------------------------------- #
def test_scan_oversized_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _oversized("xlarge_2_0")
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({"oversized_instances": [rec]}),
    )
    findings = LightsailModule().scan(_ctx())

    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0  # advisory not counted
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0


# --------------------------------------------------------------------------- #
# Scan path — mixed population reconciles counted == sum of card dollars
# --------------------------------------------------------------------------- #
def test_scan_mixed_population_counted_equals_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    idle_lin = _idle("large_2_0", name="lin")          # $44
    idle_win = _idle("small_win_2_0", os_name="Windows", name="win")  # $22
    sip = _static_ip()                                  # $3.65
    over = _oversized("large_2_0")                      # $0 advisory
    unknown = _idle("tpu_9_0", name="unknown")          # $0 advisory
    monkeypatch.setattr(
        adapter_mod, "get_enhanced_lightsail_checks",
        lambda c: _result({
            "idle_instances": [idle_lin, idle_win, unknown],
            "unused_static_ips": [sip],
            "oversized_instances": [over],
        }),
    )
    ctx = _ctx()
    findings = LightsailModule().scan(ctx)

    assert findings.total_monthly_savings == pytest.approx(44.00 + 22.00 + 3.65)
    assert findings.total_recommendations == 3  # two idle + one static IP
    # counted == rendered: sum of every counted card's displayed dollar.
    counted_cards = [
        r for cat in findings.sources.values() for r in cat.recommendations
        if r.get("Counted") is not False
    ]
    rendered = sum(r["EstimatedMonthlySavings"] for r in counted_cards)
    assert rendered == pytest.approx(findings.total_monthly_savings)


# --------------------------------------------------------------------------- #
# Shim path — fake boto3: H3 fallback removed + Windows OS detected
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeLightsailClient:
    def __init__(self, instances: list[dict[str, Any]], static_ips: list[dict[str, Any]]) -> None:
        self._instances = instances
        self._static_ips = static_ips

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "get_instances"
        return _FakePaginator([{"instances": self._instances}])

    def get_static_ips(self) -> dict[str, Any]:
        return {"staticIps": self._static_ips}


def test_shim_no_medium_fallback_and_windows_detection() -> None:
    instances = [
        # No bundleId at all → must NOT be silently defaulted to medium_2_0.
        {"name": "no-bundle", "state": {"name": "stopped"}},
        # Windows bundle → OperatingSystem == "Windows".
        {"name": "win", "state": {"name": "stopped"}, "bundleId": "medium_win_2_0"},
    ]
    static_ips = [{"name": "free-ip", "ipAddress": "9.9.9.9", "attachedTo": None}]
    client = _FakeLightsailClient(instances, static_ips)
    ctx = _ctx()
    ctx.client = lambda name: client

    result = shim_mod.get_enhanced_lightsail_checks(ctx)
    idle = result["checks"]["idle_instances"]

    no_bundle = next(r for r in idle if r["InstanceName"] == "no-bundle")
    assert no_bundle["BundleId"] == ""  # no "medium_2_0" fallback (H3)

    win = next(r for r in idle if r["InstanceName"] == "win")
    assert win["OperatingSystem"] == "Windows"  # H2 OS detection

    assert len(result["checks"]["unused_static_ips"]) == 1


def test_shim_end_to_end_through_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Full shim → adapter path: the no-bundle stopped instance becomes a $0
    # advisory (H3), the static IP is counted at $3.65 (H4).
    instances = [{"name": "no-bundle", "state": {"name": "stopped"}}]
    static_ips = [{"name": "free-ip", "ipAddress": "9.9.9.9", "attachedTo": None}]
    client = _FakeLightsailClient(instances, static_ips)
    ctx = _ctx()
    ctx.client = lambda name: client

    findings = LightsailModule().scan(ctx)

    assert findings.total_monthly_savings == pytest.approx(3.65)  # static IP only
    # no-bundle stopped instance is a $0 advisory, not counted.
    assert findings.total_recommendations == 1
    assert ctx.warnings  # unknown/missing bundle surfaced

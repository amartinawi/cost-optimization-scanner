"""Unit tests for the QuickSight adapter HIGH fix (quicksight H3).

quicksight H3 — the SPICE ``EstimatedSavings`` card string used to be a
non-region-scaled, whole-dollar value (``quicksight_spice_rate`` WITHOUT
``pricing_multiplier``, formatted ``:.0f``) computed in the shim, while the
adapter counted a region-scaled ``:.2f`` number. In any non-us-east-1 region the
card string and the counted number disagreed, and the ``$0``-advisory branch
left a stale, non-zero string behind. The dollar is now single-sourced in the
adapter: a region-scaled value (``unused_gb × quicksight_spice_rate(edition) ×
pricing_multiplier``) is rounded once and written to BOTH the counted number
(``EstimatedMonthlySavings``) and the card string (``EstimatedSavings``); the
shim emits no competing dollar; the advisory branch's string matches its $0.

Live-validated rates (AWS Pricing API, us-east-1, 2026-06-27):
  Enterprise SPICE  $0.38/GB-Mo  SKU R8PKSKFCHES8YSKK  (USE1-QS-Enterprise-SPICE)
  Standard  SPICE   $0.25/GB-Mo  SKU T4GAEKP5WQQWCUD5  (USE1-QS-Provisioned-SPICE)

Style mirrors ``tests/test_audit_fixes_counted_dollars.py`` /
``tests/test_lambda_audit_fixes.py``: SimpleNamespace ctx + monkeypatched
enhanced-checks helper for the pure logic, and a fake boto3-paginator client for
the real ``scan()`` -> shim path.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

import services.adapters.quicksight as adapter_mod
import services.quicksight as shim_mod
from services.quicksight import quicksight_spice_rate

ENTERPRISE_RATE = 0.38
STANDARD_RATE = 0.25


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _rendered_dollar(text: str) -> float:
    """Parse the leading ``$<float>`` out of an EstimatedSavings card string."""
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    assert match is not None, f"no dollar in {text!r}"
    return float(match.group(1))


def _logic_ctx(*, pricing_multiplier: float = 1.0, region: str = "us-east-1") -> SimpleNamespace:
    """ctx for adapter-only tests (the shim is monkeypatched out)."""
    return SimpleNamespace(pricing_multiplier=pricing_multiplier, region=region)


def _patched_scan(
    monkeypatch: pytest.MonkeyPatch,
    recs: list[dict[str, Any]],
    *,
    pricing_multiplier: float = 1.0,
    region: str = "us-east-1",
):
    """Run the adapter with a monkeypatched shim returning ``recs``."""
    monkeypatch.setattr(
        adapter_mod,
        "get_enhanced_quicksight_checks",
        lambda ctx: {"recommendations": recs},
    )
    ctx = _logic_ctx(pricing_multiplier=pricing_multiplier, region=region)
    return adapter_mod.QuicksightModule().scan(ctx)


# --------------------------------------------------------------------------- #
# Fake boto3 client for the real shim -> adapter scan() path
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs: Any):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeQuickSight:
    """Minimal QuickSight client: enabled Enterprise account, underused SPICE."""

    def __init__(
        self,
        *,
        edition: str = "ENTERPRISE",
        used_gb: float = 30.0,
        total_gb: float = 100.0,
        status: str = "ACCOUNT_CREATED",
        users: int = 1,
    ) -> None:
        self._edition = edition
        self._used = used_gb
        self._total = total_gb
        self._status = status
        self._users = users

    def describe_account_subscription(self, AwsAccountId: str) -> dict[str, Any]:
        return {"AccountInfo": {"AccountSubscriptionStatus": self._status, "Edition": self._edition}}

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "list_namespaces":
            return _FakePaginator([{"Namespaces": [{"Name": "default"}]}])
        if name == "list_users":
            return _FakePaginator([{"UserList": [{"UserName": f"u{i}"} for i in range(self._users)]}])
        raise AssertionError(f"unexpected paginator: {name}")

    def describe_spice_capacity(self, AwsAccountId: str) -> dict[str, Any]:
        return {
            "SpiceCapacityConfiguration": {
                "UsedCapacityInBytes": int(self._used * 1024**3),
                "TotalCapacityInBytes": int(self._total * 1024**3),
            }
        }


def _scan_ctx(client: Any, *, pricing_multiplier: float = 1.0, region: str = "us-east-1") -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_multiplier=pricing_multiplier,
        region=region,
        account_id="123456789012",
        warnings=[],
        permission_issues=[],
    )
    ctx.client = lambda name, region=None: client
    ctx.warn = lambda message, service=None, **_k: ctx.warnings.append((service, message))
    ctx.permission_issue = lambda message, service=None, action=None, **_k: ctx.permission_issues.append(
        (service, action, message)
    )
    return ctx


# --------------------------------------------------------------------------- #
# H3 — pure logic: counted == rendered, region-scaled, in a non-us-east-1 region
# --------------------------------------------------------------------------- #
def test_h3_non_us_east_1_string_equals_counted_number(monkeypatch: pytest.MonkeyPatch) -> None:
    # Enterprise, 70 unused GB, region multiplier 1.15 (the desync repro region).
    recs = [
        {
            "Edition": "ENTERPRISE",
            "UnusedSpiceCapacityGB": 70,
            "UsedCapacityGB": 30,
            "TotalCapacityGB": 100,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    findings = _patched_scan(monkeypatch, recs, pricing_multiplier=1.15, region="eu-west-1")

    expected = round(70 * ENTERPRISE_RATE * 1.15, 2)  # 30.59
    rec = findings.sources["enhanced_checks"].recommendations[0]

    # counted number is region-scaled
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected, abs=0.001)
    # the card STRING parses to the same dollar (counted == rendered)
    assert _rendered_dollar(rec["EstimatedSavings"]) == pytest.approx(expected, abs=0.001)
    # headline == card
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    # the old bug (non-region-scaled flat value) would have shown 70*0.38 = 26.6
    assert rec["EstimatedMonthlySavings"] != pytest.approx(70 * ENTERPRISE_RATE, abs=0.001)


def test_h3_standard_edition_region_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "Edition": "STANDARD",
            "UnusedSpiceCapacityGB": 100,
            "UsedCapacityGB": 20,
            "TotalCapacityGB": 120,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    findings = _patched_scan(monkeypatch, recs, pricing_multiplier=1.2, region="ap-southeast-2")

    expected = round(100 * STANDARD_RATE * 1.2, 2)  # 30.0
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected, abs=0.001)
    assert _rendered_dollar(rec["EstimatedSavings"]) == pytest.approx(expected, abs=0.001)
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)


# --------------------------------------------------------------------------- #
# H3 — AuditBasis is structured (rate / region / edition / used-vs-total / formula)
# --------------------------------------------------------------------------- #
def test_h3_counted_rec_carries_audit_basis(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "Edition": "ENTERPRISE",
            "UnusedSpiceCapacityGB": 70,
            "UsedCapacityGB": 30,
            "TotalCapacityGB": 100,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    findings = _patched_scan(monkeypatch, recs, pricing_multiplier=1.15, region="eu-west-1")
    basis = findings.sources["enhanced_checks"].recommendations[0]["AuditBasis"]
    assert basis["edition"] == "ENTERPRISE"
    assert basis["rate_per_gb_month"] == ENTERPRISE_RATE
    assert basis["region"] == "eu-west-1"
    assert basis["used_gb"] == 30
    assert basis["total_gb"] == 100
    assert basis["unused_gb"] == 70
    assert basis["pricing_multiplier"] == 1.15
    assert "unused_gb" in basis["formula"] and "rate_per_gb_month" in basis["formula"]
    # the AuditBasis rate × gb × multiplier reconstructs the counted dollar
    recon = round(basis["unused_gb"] * basis["rate_per_gb_month"] * basis["pricing_multiplier"], 2)
    assert recon == pytest.approx(findings.total_monthly_savings, abs=0.001)


# --------------------------------------------------------------------------- #
# H3 — advisory branch: $0 number and a matching $0 string (no stale dollar)
# --------------------------------------------------------------------------- #
def test_h3_missing_edition_is_zero_advisory_with_matching_string(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "Edition": "",  # edition unresolved -> cannot price the rate
            "UnusedSpiceCapacityGB": 100,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    findings = _patched_scan(monkeypatch, recs, pricing_multiplier=1.5, region="eu-west-1")
    rec = findings.sources["enhanced_checks"].recommendations[0]

    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # string is honest $0 and matches the number — no leftover non-zero figure
    assert rec["EstimatedSavings"].startswith("$0.00/month")
    assert _rendered_dollar(rec["EstimatedSavings"]) == 0.0
    assert "PricingWarning" in rec
    # advisory $0 does NOT feed the headline
    assert findings.total_monthly_savings == 0.0


def test_h3_zero_unused_gb_is_zero_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        {
            "Edition": "ENTERPRISE",
            "UnusedSpiceCapacityGB": 0,
            "CheckCategory": "SPICE Optimization",
        }
    ]
    findings = _patched_scan(monkeypatch, recs, pricing_multiplier=1.0)
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert _rendered_dollar(rec["EstimatedSavings"]) == 0.0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# Immutability — adapter builds new rec dicts, never mutates shim output
# --------------------------------------------------------------------------- #
def test_adapter_does_not_mutate_shim_rec(monkeypatch: pytest.MonkeyPatch) -> None:
    original = {
        "Edition": "ENTERPRISE",
        "UnusedSpiceCapacityGB": 50,
        "UsedCapacityGB": 10,
        "TotalCapacityGB": 60,
        "CheckCategory": "SPICE Optimization",
    }
    findings = _patched_scan(monkeypatch, [original], pricing_multiplier=1.0)
    # the priced rec is a NEW object; the shim dict is untouched
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec is not original
    assert "EstimatedMonthlySavings" not in original
    assert "AuditBasis" not in original
    assert "EstimatedSavings" not in original
    assert rec["EstimatedMonthlySavings"] == pytest.approx(50 * ENTERPRISE_RATE, abs=0.001)


# --------------------------------------------------------------------------- #
# H3 — shim no longer emits a competing (non-region-scaled) dollar string
# --------------------------------------------------------------------------- #
def test_shim_emits_no_dollar_string() -> None:
    ctx = _scan_ctx(_FakeQuickSight(edition="ENTERPRISE", used_gb=30.0, total_gb=100.0))
    result = shim_mod.get_enhanced_quicksight_checks(ctx)
    recs = result["recommendations"]
    assert len(recs) == 1
    # single-source: the dollar string is set ONLY by the adapter, not the shim
    assert "EstimatedSavings" not in recs[0]
    assert recs[0]["Edition"] == "ENTERPRISE"
    assert recs[0]["UnusedSpiceCapacityGB"] == pytest.approx(70.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Full scan() path through the REAL shim with a fake paginator client
# --------------------------------------------------------------------------- #
def test_scan_path_counted_equals_rendered_region_scaled() -> None:
    client = _FakeQuickSight(edition="ENTERPRISE", used_gb=30.0, total_gb=100.0)
    ctx = _scan_ctx(client, pricing_multiplier=1.15, region="eu-west-1")
    findings = adapter_mod.QuicksightModule().scan(ctx)

    expected = round(70.0 * ENTERPRISE_RATE * 1.15, 2)  # 30.59
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert findings.service_name == "QuickSight"
    assert findings.total_monthly_savings == pytest.approx(expected, abs=0.001)
    assert rec["EstimatedMonthlySavings"] == pytest.approx(expected, abs=0.001)
    # the card dollar string equals both the counted number and the headline
    assert _rendered_dollar(rec["EstimatedSavings"]) == pytest.approx(expected, abs=0.001)
    assert _rendered_dollar(rec["EstimatedSavings"]) == pytest.approx(findings.total_monthly_savings, abs=0.001)
    assert rec["AuditBasis"]["region"] == "eu-west-1"


def test_scan_path_standard_edition_rate() -> None:
    client = _FakeQuickSight(edition="STANDARD", used_gb=30.0, total_gb=100.0)
    ctx = _scan_ctx(client, pricing_multiplier=1.0, region="us-east-1")
    findings = adapter_mod.QuicksightModule().scan(ctx)
    # 70 unused GB × $0.25 = $17.50 (Standard, not the $0.38-for-both bug)
    assert findings.total_monthly_savings == pytest.approx(70.0 * STANDARD_RATE, abs=0.01)


def test_rate_helper_matches_live_validated_skus() -> None:
    assert quicksight_spice_rate("ENTERPRISE") == ENTERPRISE_RATE
    assert quicksight_spice_rate("STANDARD") == STANDARD_RATE
    assert quicksight_spice_rate("enterprise") == ENTERPRISE_RATE

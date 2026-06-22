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
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, mult, days: {"recommendations": []})
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
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda ctx, mult, days: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {"total": 1})

    ctx = _FakeCtx()
    findings = rds_adapter.RdsModule().scan(ctx)

    assert findings.total_recommendations == 1
    assert findings.sources["compute_optimizer"].count == 1
    assert findings.total_monthly_savings == pytest.approx(40.0)

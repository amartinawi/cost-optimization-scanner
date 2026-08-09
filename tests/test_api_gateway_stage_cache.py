"""AG-3 — provisioned REST stage caches.

A stage cache bills 24/7 by size whether or not a single request hits it. The
shim previously found the rate and then deleted the lever, reasoning that
"whether net savings exist depends on backend pricing not measured here" — true
of the question "does caching pay for itself", but not of the question "is this
cache serving anything at all", which the stage request metric answers.

Rates validated against the live Pricing API 2026-08-09 (AmazonApiGateway,
us-east-1, productFamily "Amazon API Gateway Cache"): 0.5GB $0.020/hr ->
$14.60/mo ... 237GB $3.80/hr -> $2,774/mo.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.pricing_engine import FALLBACK_APIGW_CACHE_HOURLY
from services.adapters.api_gateway import ApiGatewayModule
from services.api_gateway import get_enhanced_api_gateway_checks

_CATEGORY = "API Gateway Stage Cache"


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self) -> list[dict[str, Any]]:
        return self._pages


class _FakeApiGateway:
    def __init__(self, stages: list[dict[str, Any]], *, stages_error: Exception | None = None) -> None:
        self._stages = stages
        self._stages_error = stages_error
        self.stage_calls: list[str] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        return _FakePaginator([{"items": [{"id": "api1", "name": "orders-api"}]}])

    def get_resources(self, restApiId: str) -> dict[str, Any]:  # noqa: N803
        # >10 resources keeps the unrelated REST->HTTP lever quiet.
        return {"items": [{"id": f"r{i}"} for i in range(20)]}

    def get_stages(self, restApiId: str) -> dict[str, Any]:  # noqa: N803
        self.stage_calls.append(restApiId)
        if self._stages_error is not None:
            raise self._stages_error
        return {"item": self._stages}


class _FakeCloudWatch:
    def __init__(self, total: float | None) -> None:
        self._total = total
        self.calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._total is None:
            raise Exception("AccessDenied")
        return {"Datapoints": ([{"Sum": self._total}] if self._total else [])}


def _stage(
    name: str = "prod",
    *,
    enabled: bool = True,
    size: str | None = "6.1",
    status: str = "AVAILABLE",
) -> dict[str, Any]:
    stage: dict[str, Any] = {"stageName": name, "cacheClusterEnabled": enabled, "cacheClusterStatus": status}
    if size is not None:
        stage["cacheClusterSize"] = size
    return stage


class _FakePricing:
    def get_apigateway_cache_monthly_price(self, cache_size: str) -> float:
        hourly = FALLBACK_APIGW_CACHE_HOURLY.get(cache_size)
        return hourly * 730 if hourly else 0.0


def _ctx(
    stages: list[dict[str, Any]],
    *,
    requests: float | None = 0.0,
    fast: bool = False,
    pricing: Any = "default",
    stages_error: Exception | None = None,
) -> SimpleNamespace:
    apigw = _FakeApiGateway(stages, stages_error=stages_error)
    cw = _FakeCloudWatch(requests)
    ctx = SimpleNamespace(
        pricing_engine=_FakePricing() if pricing == "default" else pricing,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=fast,
        warnings=[],
        permissions=[],
    )
    clients = {"apigateway": apigw, "cloudwatch": cw}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda message, service="": ctx.warnings.append((service, message))
    ctx.permission_issue = lambda message, service="", action=None: ctx.permissions.append(message)
    ctx._apigw = apigw
    ctx._cw = cw
    return ctx


def _recs(ctx: SimpleNamespace) -> list[dict[str, Any]]:
    out = get_enhanced_api_gateway_checks(ctx)
    return [r for r in out["recommendations"] if r["CheckCategory"] == _CATEGORY]


# --------------------------------------------------------------------------- #
# The counted case
# --------------------------------------------------------------------------- #
def test_zero_traffic_cache_is_counted_at_the_full_rate() -> None:
    recs = _recs(_ctx([_stage(size="6.1")], requests=0.0))
    assert len(recs) == 1
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(146.0)
    assert recs[0].get("Counted") is not False
    assert recs[0]["AuditBasis"]["cache_size_gb"] == "6.1"


@pytest.mark.parametrize(
    ("size", "monthly"),
    [("0.5", 14.60), ("1.6", 27.74), ("13.5", 182.50), ("237", 2774.00)],
)
def test_every_size_prices_from_its_own_rate(size: str, monthly: float) -> None:
    recs = _recs(_ctx([_stage(size=size)], requests=0.0))
    assert recs[0]["EstimatedMonthlySavings"] == pytest.approx(monthly, abs=0.01)


# --------------------------------------------------------------------------- #
# Everything that must NOT count
# --------------------------------------------------------------------------- #
def test_cache_with_traffic_is_advisory_but_keeps_its_figure() -> None:
    """A cache fronting live traffic may be load-bearing, and the backend cost
    it offsets is not measured here."""
    recs = _recs(_ctx([_stage()], requests=5_000_000.0))
    assert len(recs) == 1
    assert recs[0]["Counted"] is False
    assert recs[0]["EstimatedMonthlySavings"] == 0.0
    assert recs[0]["PotentialMonthlySavings"] == pytest.approx(146.0)
    assert "5,000,000 requests" in recs[0]["EstimatedSavings"]


def test_unreadable_metric_is_advisory_not_idle() -> None:
    """A denied CloudWatch read is not evidence of zero traffic (H2)."""
    ctx = _ctx([_stage()], requests=None)
    recs = _recs(ctx)
    assert len(recs) == 1 and recs[0]["Counted"] is False
    assert "idleness is unproven" in recs[0]["EstimatedSavings"]
    assert ctx.permissions, "the denied read must be classified, not swallowed"


def test_fast_mode_is_advisory_and_makes_no_metric_call() -> None:
    ctx = _ctx([_stage()], fast=True)
    recs = _recs(ctx)
    assert len(recs) == 1 and recs[0]["Counted"] is False
    assert ctx._cw.calls == []


def test_cache_disabled_emits_nothing() -> None:
    assert _recs(_ctx([_stage(enabled=False)], requests=0.0)) == []


@pytest.mark.parametrize("status", ["CREATE_IN_PROGRESS", "DELETE_IN_PROGRESS", "NOT_AVAILABLE"])
def test_non_billing_statuses_emit_nothing(status: str) -> None:
    assert _recs(_ctx([_stage(status=status)], requests=0.0)) == []


def test_unknown_cache_size_abstains_with_a_warning() -> None:
    """An unrecognized size has no defensible rate, so no dollar is invented."""
    ctx = _ctx([_stage(size="999")], requests=0.0)
    assert _recs(ctx) == []
    assert any("unpriceable size" in message for _svc, message in ctx.warnings)


def test_missing_pricing_engine_abstains() -> None:
    ctx = _ctx([_stage()], requests=0.0, pricing=None)
    assert _recs(ctx) == []


def test_get_stages_failure_is_classified_and_does_not_kill_the_scan() -> None:
    ctx = _ctx([], requests=0.0, stages_error=Exception("AccessDeniedException"))
    out = get_enhanced_api_gateway_checks(ctx)
    assert [r for r in out["recommendations"] if r["CheckCategory"] == _CATEGORY] == []
    assert ctx.permissions or ctx.warnings


# --------------------------------------------------------------------------- #
# Metric shape + adapter wiring
# --------------------------------------------------------------------------- #
def test_metric_uses_the_stage_scoped_dimension_pair() -> None:
    """(ApiName, Stage) is a standard dimension pair — unlike the 4-dimension
    form it needs no detailed metrics. An ApiName-only read would attribute the
    whole API's traffic to one stage."""
    ctx = _ctx([_stage("prod")], requests=0.0)
    _recs(ctx)
    call = next(c for c in ctx._cw.calls if c["MetricName"] == "Count")
    assert call["Dimensions"] == [
        {"Name": "ApiName", "Value": "orders-api"},
        {"Name": "Stage", "Value": "prod"},
    ]


def test_adapter_totals_only_the_counted_cache() -> None:
    idle = _stage("idle-stage", size="1.6")
    busy = _stage("busy-stage", size="237")
    ctx = _ctx([idle, busy], requests=0.0)
    findings = ApiGatewayModule().scan(ctx)
    cache_recs = [
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    ]
    assert len(cache_recs) == 2
    # Both stages read zero requests here, so both count: 27.74 + 2774.00.
    assert findings.total_monthly_savings == pytest.approx(2801.74, abs=0.01)


def test_adapter_never_sums_an_advisory_figure() -> None:
    ctx = _ctx([_stage(size="237")], requests=9_000_000.0)
    findings = ApiGatewayModule().scan(ctx)
    rec = next(
        r for r in findings.sources["enhanced_checks"].recommendations if r["CheckCategory"] == _CATEGORY
    )
    assert rec["PotentialMonthlySavings"] == pytest.approx(2774.0)
    assert findings.total_monthly_savings == 0.0

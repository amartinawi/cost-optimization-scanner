"""Unit tests for the CloudFront adapter HIGH cost-audit fixes.

Mirrors the SimpleNamespace-ctx + fake-boto3 style of
``tests/test_lambda_audit_fixes.py`` / ``tests/test_audit_fixes_counted_dollars.py``.

Covers:
  - H2  Fast mode short-circuits ``get_enhanced_cloudfront_checks``: no
        per-distribution CloudWatch reads, no ``get_distribution_config`` call,
        no recommendations emitted, and a single advisory warning surfaced.
  - Non-fast-mode control: with traffic > 1000 the price-class lever IS emitted
        (proving the guard does not break the normal path).
  - H1 regression (already fixed): a CloudWatch ``Requests`` AccessDenied is
        classified via ``record_aws_error`` (permission_issue), not swallowed.
  - Adapter dollar honesty: every emitted rec is priced at $0 advisory (no
        fabricated counted dollar; total_monthly_savings == 0.0).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import services.cloudfront as shim_mod
from services.adapters.cloudfront import CloudfrontModule


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
def _recording_ctx(**kw: Any) -> SimpleNamespace:
    """ctx that records warn / permission_issue calls."""
    ctx = SimpleNamespace(
        pricing_multiplier=kw.pop("pricing_multiplier", 1.0),
        fast_mode=kw.pop("fast_mode", False),
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


class _FakeCloudFront:
    """Minimal boto3 CloudFront client driving the enhanced-checks shim."""

    def __init__(
        self,
        distributions: list[dict[str, Any]],
        config_error: bool = False,
    ) -> None:
        self._distributions = distributions
        self._config_error = config_error
        self.config_calls = 0

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator([{"DistributionList": {"Items": self._distributions}}])

    def get_distribution_config(self, Id: str) -> dict[str, Any]:  # noqa: N803
        self.config_calls += 1
        if self._config_error:
            raise AssertionError("get_distribution_config must not be called in fast mode")
        return {"DistributionConfig": {"Origins": {"Items": []}}}


class _FakeCloudWatch:
    """Returns canned `Requests` datapoints, raises a canned error, or asserts."""

    def __init__(
        self,
        requests_sum: float = 0.0,
        error: Exception | None = None,
        forbid: bool = False,
    ) -> None:
        self._requests_sum = requests_sum
        self._error = error
        self._forbid = forbid
        self.calls = 0

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self._forbid:
            raise AssertionError("CloudWatch must not be called in fast mode")
        if self._error is not None:
            raise self._error
        metric = kwargs.get("MetricName")
        if metric == "Requests":
            return {"Datapoints": [{"Sum": self._requests_sum}]}
        return {"Datapoints": []}


def _client_factory(cf: Any, cw: Any):
    def _client(name: str, **_kw: Any):
        return {"cloudfront": cf, "cloudwatch": cw}[name]

    return _client


def _dist(dist_id: str, *, price_class: str = "PriceClass_All", enabled: bool = True) -> dict[str, Any]:
    return {
        "Id": dist_id,
        "DomainName": f"{dist_id}.cloudfront.net",
        "PriceClass": price_class,
        "Status": "Deployed",
        "Enabled": enabled,
    }


# --------------------------------------------------------------------------- #
# H2 — fast-mode short-circuit
# --------------------------------------------------------------------------- #
def test_fast_mode_skips_cloudwatch_and_config_and_warns_once() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1"), _dist("E2")], config_error=True)
    # Either CW or get_distribution_config being called in fast mode raises.
    cw = _FakeCloudWatch(forbid=True)
    ctx = _recording_ctx(fast_mode=True)
    ctx.client = _client_factory(cf, cw)

    result = shim_mod.get_enhanced_cloudfront_checks(ctx)

    # No per-distribution reads happened.
    assert cw.calls == 0, "fast mode must make no CloudWatch reads"
    assert cf.config_calls == 0, "fast mode must not call get_distribution_config"
    # No recommendations emitted (no traffic-gated evidence available).
    assert result["recommendations"] == []
    assert result["price_class_optimization"] == []
    # Exactly one fast-mode advisory warning surfaced.
    assert sum("Fast mode" in msg for _svc, msg in ctx.warnings) == 1
    # No permission issues fabricated.
    assert ctx.permissions == []


def test_fast_mode_adapter_path_emits_no_counted_dollars() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1")], config_error=True)
    cw = _FakeCloudWatch(forbid=True)
    ctx = _recording_ctx(fast_mode=True)
    ctx.client = _client_factory(cf, cw)

    findings = CloudfrontModule().scan(ctx)

    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert cw.calls == 0
    assert cf.config_calls == 0


# --------------------------------------------------------------------------- #
# Non-fast-mode control — guard does not break the normal path
# --------------------------------------------------------------------------- #
def test_normal_mode_emits_price_class_rec_when_traffic_high() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1")])
    cw = _FakeCloudWatch(requests_sum=5000.0)  # > 1000 weekly Requests
    ctx = _recording_ctx(fast_mode=False)
    ctx.client = _client_factory(cf, cw)

    result = shim_mod.get_enhanced_cloudfront_checks(ctx)

    pco = result["price_class_optimization"]
    assert len(pco) == 1, "high-traffic PriceClass_All distribution should surface the lever"
    assert pco[0]["DistributionId"] == "E1"
    # CloudWatch was actually consulted in normal mode.
    assert cw.calls >= 1
    # No fast-mode warning in the normal path.
    assert not any("Fast mode" in msg for _svc, msg in ctx.warnings)


def test_normal_mode_no_rec_when_traffic_low() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1")])
    cw = _FakeCloudWatch(requests_sum=10.0)  # <= 1000 → not flagged
    ctx = _recording_ctx(fast_mode=False)
    ctx.client = _client_factory(cf, cw)

    result = shim_mod.get_enhanced_cloudfront_checks(ctx)

    assert result["price_class_optimization"] == []


# --------------------------------------------------------------------------- #
# H1 regression — Requests read failure is classified, not swallowed
# --------------------------------------------------------------------------- #
def test_requests_access_denied_is_permission_issue() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1")])
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: no cloudwatch"))
    ctx = _recording_ctx(fast_mode=False)
    ctx.client = _client_factory(cf, cw)

    result = shim_mod.get_enhanced_cloudfront_checks(ctx)

    # The denied Requests read drops the price-class lever for this dist...
    assert result["price_class_optimization"] == []
    # ...but the error is classified as a permission issue (H1), not swallowed.
    assert ctx.permissions, "CloudWatch AccessDenied must be recorded via record_aws_error"
    assert any(svc == "cloudfront" for svc, _action, _msg in ctx.permissions)


# --------------------------------------------------------------------------- #
# Dollar honesty — normal-mode recs are $0 advisory (no fabricated dollar)
# --------------------------------------------------------------------------- #
def test_adapter_prices_recs_zero_advisory() -> None:
    cf = _FakeCloudFront(distributions=[_dist("E1")])
    cw = _FakeCloudWatch(requests_sum=5000.0)
    ctx = _recording_ctx(fast_mode=False)
    ctx.client = _client_factory(cf, cw)

    findings = CloudfrontModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 1
    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "PricingWarning" in rec

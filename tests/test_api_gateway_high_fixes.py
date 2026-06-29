"""Unit tests for the API Gateway HIGH cost-audit fix (H4).

H4 — REST-only coverage honesty. The module previously carried a REST/HTTP
docstring while scanning only v1 REST APIs (``apigateway:GetRestApis``); HTTP and
WebSocket (v2 ``apigatewayv2``) APIs were never scanned. The remediation states
the REST-only scope honestly rather than fabricating uncounted apigatewayv2
coverage (HTTP API is already the cheapest type — no cheaper migration target —
and a WebSocket saving needs per-API usage metrics that are not gathered here).

These tests prove, with the same SimpleNamespace-ctx + fake-boto3 style as
``tests/test_audit_fixes_counted_dollars.py`` / ``tests/test_lambda_audit_fixes.py``:

  - H4 docstrings honestly state REST-only scope and call out apigatewayv2 as
    out of scope.
  - The scan() path never touches the apigatewayv2 surface (only get_rest_apis).
  - The single REST→HTTP lever's COUNTED dollar equals
    ``(REST $3.50/M − HTTP $1.00/M) × measured monthly requests`` (rates validated
    live: AmazonApiGateway us-east-1, AWS Pricing API publication 2025-11-20), with
    region scaling applied exactly once and a structured AuditBasis attached.
  - A failed or fast-mode-skipped CloudWatch read yields a Counted=False $0
    advisory — never a fabricated counted dollar.
  - A genuine zero-traffic API (successful empty Datapoints) nets $0 but is not
    mislabeled as a failed metric read.
  - A GetResources AccessDenied is classified (permission_issue), not swallowed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.api_gateway as shim_mod
from services.adapters.api_gateway import ApiGatewayModule
from services.api_gateway import (
    HTTP_PER_M,
    REST_PER_M,
    SAVINGS_PER_M,
    get_enhanced_api_gateway_checks,
)


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


class _FakeApiGateway:
    """Minimal v1 apigateway client. Records which paginator names were asked
    for so a test can prove only the REST surface is touched."""

    def __init__(
        self,
        apis: list[dict[str, Any]],
        resource_counts: dict[str, int],
        resources_error: set[str] | None = None,
    ) -> None:
        self._apis = apis
        self._resource_counts = resource_counts
        self._resources_error = resources_error or set()
        self.paginators_requested: list[str] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        self.paginators_requested.append(name)
        return _FakePaginator([{"items": self._apis}])

    def get_resources(self, restApiId: str) -> dict[str, Any]:  # noqa: N803 - boto3 shape
        if restApiId in self._resources_error:
            raise Exception("AccessDeniedException: not authorized to GetResources")
        n = self._resource_counts.get(restApiId, 0)
        return {"items": [{"id": f"res-{i}"} for i in range(n)]}


class _FakeCloudWatch:
    """Returns canned Count datapoints, or raises a canned error."""

    def __init__(self, monthly_requests: float | None = 0.0, error: Exception | None = None) -> None:
        self._monthly_requests = monthly_requests
        self._error = error
        self.called = False

    def get_metric_statistics(self, **_kwargs: Any) -> dict[str, Any]:
        self.called = True
        if self._error is not None:
            raise self._error
        if self._monthly_requests is None:
            return {"Datapoints": []}  # genuine empty read
        return {"Datapoints": [{"Sum": self._monthly_requests}]}


def _client_factory(apigw: Any, cw: Any, *, forbid_v2: bool = True):
    def _client(name: str, **_kw: Any):
        if forbid_v2 and name == "apigatewayv2":
            raise AssertionError("apigatewayv2 must not be queried (REST-only scope, H4)")
        return {"apigateway": apigw, "cloudwatch": cw}[name]

    return _client


# --------------------------------------------------------------------------- #
# H4 — docstring honesty
# --------------------------------------------------------------------------- #
def test_module_docstring_states_rest_only_scope() -> None:
    doc = shim_mod.__doc__ or ""
    assert "REST APIs only" in doc
    # Honestly names the un-scanned v2 surface rather than implying coverage.
    assert "apigatewayv2" in doc
    assert "WebSocket" in doc and "HTTP" in doc
    assert "intentionally NOT scanned" in doc


def test_function_docstring_states_rest_only_scope() -> None:
    doc = get_enhanced_api_gateway_checks.__doc__ or ""
    assert "REST APIs" in doc
    assert "out of scope" in doc


# --------------------------------------------------------------------------- #
# H4 — scan() only touches the REST surface, never apigatewayv2
# --------------------------------------------------------------------------- #
def test_scan_only_touches_rest_surface() -> None:
    apigw = _FakeApiGateway(
        apis=[{"id": "a1", "name": "api-one"}],
        resource_counts={"a1": 3},
    )
    cw = _FakeCloudWatch(monthly_requests=0.0)
    ctx = _recording_ctx()
    ctx.client = _client_factory(apigw, cw)  # raises if apigatewayv2 requested

    # No AssertionError ⇒ the code never asked for the v2 client.
    findings = ApiGatewayModule().scan(ctx)

    assert apigw.paginators_requested == ["get_rest_apis"]
    assert findings.service_name == "API Gateway"


# --------------------------------------------------------------------------- #
# Counted dollar — REST→HTTP migration from measured traffic
# --------------------------------------------------------------------------- #
def test_counted_dollar_from_measured_requests() -> None:
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 5})
    cw = _FakeCloudWatch(monthly_requests=2_000_000.0)  # 2M requests/mo
    ctx = _recording_ctx(pricing_multiplier=1.0)
    ctx.client = _client_factory(apigw, cw)

    findings = ApiGatewayModule().scan(ctx)

    # (2,000,000 / 1e6) * (3.50 − 1.00) = 2 * 2.50 = $5.00
    expected = (2_000_000.0 / 1_000_000) * SAVINGS_PER_M
    assert expected == pytest.approx(5.00)
    assert findings.total_monthly_savings == pytest.approx(5.00)

    rec = findings.sources["enhanced_checks"].recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(5.00)
    assert rec.get("Counted") is not False  # counted, not advisory
    # AuditBasis defends the dollar from the report alone (rule 8).
    basis = rec["AuditBasis"]
    assert basis["rest_rate_per_million"] == REST_PER_M == 3.50
    assert basis["http_rate_per_million"] == HTTP_PER_M == 1.00
    assert basis["savings_rate_per_million"] == SAVINGS_PER_M == 2.50
    assert basis["metric_window_days"] == 30
    assert basis["monthly_requests"] == 2_000_000.0


def test_region_multiplier_not_applied_us_constant_floor() -> None:
    """API Gateway request pricing is regional but NOT proportional to the generic
    pricing_multiplier (eu-west-1 = us-east-1 $3.50/M; eu-central-1 = $3.70/M ≈
    +5.7%, not +12%). The savings keeps the us-east-1 constant floor and is NOT
    scaled — so the headline equals the rendered per-rec dollar (counted==rendered)
    and never overstates the real regional saving (api_gateway region fix)."""
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 5})
    cw = _FakeCloudWatch(monthly_requests=2_000_000.0)
    ctx = _recording_ctx(pricing_multiplier=1.5)
    ctx.client = _client_factory(apigw, cw)

    findings = ApiGatewayModule().scan(ctx)

    # 2.0M requests × $2.50/M delta = $5.00, NOT re-scaled by the 1.5 multiplier.
    assert findings.total_monthly_savings == pytest.approx(5.00)
    # counted == rendered: headline equals the sum of the rendered per-rec dollars.
    rendered = sum(
        r.get("EstimatedMonthlySavings", 0.0)
        for r in findings.sources["enhanced_checks"].recommendations
    )
    assert findings.total_monthly_savings == pytest.approx(rendered)


# --------------------------------------------------------------------------- #
# Advisory $0 — failed CloudWatch read is NOT evidence of zero traffic
# --------------------------------------------------------------------------- #
def test_metric_read_failure_is_advisory_zero() -> None:
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 4})
    cw = _FakeCloudWatch(error=Exception("ThrottlingException: rate exceeded"))
    ctx = _recording_ctx()
    ctx.client = _client_factory(apigw, cw)

    result = get_enhanced_api_gateway_checks(ctx)
    recs = result["recommendations"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert rec["MetricReadFailed"] is True
    assert "advisory" in rec["EstimatedSavings"]
    # A throttle is surfaced (warn), never swallowed.
    assert ctx.warnings, "metric read failure must be surfaced on ctx"

    # And through the adapter: rendered (counted in rec list) but $0 to the headline.
    ctx2 = _recording_ctx()
    ctx2.client = _client_factory(apigw, _FakeCloudWatch(error=Exception("ThrottlingException")))
    findings = ApiGatewayModule().scan(ctx2)
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 1
    assert findings.sources["enhanced_checks"].recommendations[0].get("Counted") is False


def test_cloudwatch_access_denied_is_permission_issue() -> None:
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 4})
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: no cloudwatch:GetMetricStatistics"))
    ctx = _recording_ctx()
    ctx.client = _client_factory(apigw, cw)

    get_enhanced_api_gateway_checks(ctx)

    assert ctx.permissions, "CloudWatch AccessDenied must be a permission_issue"
    svc = ctx.permissions[0][0]
    assert svc == "api_gateway"


# --------------------------------------------------------------------------- #
# Genuine zero traffic (successful empty Datapoints) → $0, not a failed read
# --------------------------------------------------------------------------- #
def test_genuine_zero_requests_nets_zero_without_failure_flag() -> None:
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 2})
    cw = _FakeCloudWatch(monthly_requests=None)  # successful, empty Datapoints
    ctx = _recording_ctx()
    ctx.client = _client_factory(apigw, cw)

    result = get_enhanced_api_gateway_checks(ctx)
    rec = result["recommendations"][0]
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "MetricReadFailed" not in rec  # genuine 0, not a failed read
    assert ctx.warnings == [] and ctx.permissions == []

    # Adapter still marks it advisory ($0 ⇒ Counted=False) so it never feeds the headline.
    findings = ApiGatewayModule().scan(ctx)
    assert findings.total_monthly_savings == 0.0
    assert findings.sources["enhanced_checks"].recommendations[0].get("Counted") is False


# --------------------------------------------------------------------------- #
# Fast mode skips the CloudWatch read entirely → advisory $0
# --------------------------------------------------------------------------- #
def test_fast_mode_skips_cloudwatch() -> None:
    apigw = _FakeApiGateway(apis=[{"id": "a1", "name": "api-one"}], resource_counts={"a1": 2})
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _recording_ctx(fast_mode=True)
    ctx.client = _client_factory(apigw, cw)

    result = get_enhanced_api_gateway_checks(ctx)
    rec = result["recommendations"][0]
    assert cw.called is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # No metric read attempted ⇒ not flagged as a failed read, just zero traffic evidence.
    assert "MetricReadFailed" not in rec

    findings = ApiGatewayModule().scan(ctx)
    assert findings.total_monthly_savings == 0.0
    assert findings.sources["enhanced_checks"].recommendations[0].get("Counted") is False


# --------------------------------------------------------------------------- #
# H1 (already applied) — GetResources AccessDenied classified, API not vanished
# --------------------------------------------------------------------------- #
def test_get_resources_access_denied_classified() -> None:
    apigw = _FakeApiGateway(
        apis=[{"id": "a1", "name": "api-one"}],
        resource_counts={"a1": 3},
        resources_error={"a1"},
    )
    cw = _FakeCloudWatch(monthly_requests=0.0)
    ctx = _recording_ctx()
    ctx.client = _client_factory(apigw, cw)

    result = get_enhanced_api_gateway_checks(ctx)

    # No rec emitted for the IAM-gapped API, but it surfaces as a permission gap.
    assert result["recommendations"] == []
    assert ctx.permissions, "GetResources AccessDenied must classify as permission_issue"
    assert ctx.permissions[0][0] == "api_gateway"

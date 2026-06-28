"""Unit tests for the SageMaker adapter HIGH cost-correctness fixes.

Mirrors ``tests/test_lambda_audit_fixes.py`` / ``tests/test_audit_fixes_counted_dollars.py``:
a ``SimpleNamespace`` ctx + fake boto3 clients drive the pure check helpers and
the full ``scan()`` path so every counted dollar (or advisory $0) is proven by
an explicit assertion.

Covers:
  - C2  $0 "Delete endpoint" placeholders abstain; advisory ($0) recs are
        excluded from BOTH total_monthly_savings and total_recommendations.
  - H1  spot_training is a one-time per-run figure -> advisory $0 (Counted=False),
        labelled "per-run, one-time", with an AuditBasis (rate/training_hours/0.70).
  - H2  an idle endpoint is excluded from the consolidation grouping population so
        the same compute is never counted twice (idle deletion is the single owner).

Validated rate (AWS Pricing API, live 2026-06-27): SageMaker real-time inference
Hosting ``ml.m5.xlarge`` us-east-1 = $0.23/hr -> $167.90/mo
(usagetype ``USE1-Host:ml.m5.xlarge``, SKU JDZQJFUVUR8SUWHS).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.sagemaker as sm_mod
from services.adapters.sagemaker import (
    CONSOLIDATION_SAVINGS_RATE,
    HOURS_PER_MONTH,
    SPOT_SAVINGS_RATE,
    SageMakerModule,
)

# Live-validated Hosting price for ml.m5.xlarge in us-east-1.
M5_XLARGE_MONTHLY = round(0.23 * 730, 2)  # $167.90/mo


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kw: Any):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeSageMaker:
    """Minimal boto3 SageMaker client driving the adapter helpers."""

    def __init__(
        self,
        *,
        endpoints: list[dict[str, Any]] | None = None,
        endpoint_configs: dict[str, dict[str, Any]] | None = None,
        notebooks: list[dict[str, Any]] | None = None,
        training_jobs: list[dict[str, Any]] | None = None,
        training_details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._endpoints = endpoints or []
        self._endpoint_configs = endpoint_configs or {}
        self._notebooks = notebooks or []
        self._training_jobs = training_jobs or []
        self._training_details = training_details or {}

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "list_endpoints":
            return _FakePaginator([{"Endpoints": list(self._endpoints)}])
        return _FakePaginator([{}])

    def describe_endpoint(self, EndpointName: str) -> dict[str, Any]:  # noqa: N803
        for ep in self._endpoints:
            if ep.get("EndpointName") == EndpointName:
                return {"EndpointConfigName": ep.get("EndpointConfigName", "")}
        return {}

    def describe_endpoint_config(self, EndpointConfigName: str) -> dict[str, Any]:  # noqa: N803
        return self._endpoint_configs.get(EndpointConfigName, {})

    def list_notebook_instances(self, **_kw: Any) -> dict[str, Any]:
        return {"NotebookInstances": list(self._notebooks)}

    def list_training_jobs(self, **_kw: Any) -> dict[str, Any]:
        return {"TrainingJobSummaries": list(self._training_jobs)}

    def describe_training_job(self, TrainingJobName: str) -> dict[str, Any]:  # noqa: N803
        return self._training_details.get(TrainingJobName, {})


class _FakeCloudWatch:
    """Returns canned per-endpoint invocation sums (or raises if configured)."""

    def __init__(
        self,
        invocations_by_name: dict[str, float] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._inv = invocations_by_name or {}
        self._error = error

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        dims = {d["Name"]: d["Value"] for d in kwargs.get("Dimensions", [])}
        name = dims.get("EndpointName")
        if name in self._inv:
            return {"Datapoints": [{"Sum": self._inv[name]}]}
        return {"Datapoints": []}


class _FakePricing:
    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self._prices = prices or {}

    def get_sagemaker_instance_monthly(self, instance_type: str) -> float:
        return self._prices.get(instance_type, 0.0)


def _ctx(
    sm: Any,
    cw: Any,
    *,
    prices: dict[str, float] | None = None,
    pricing_multiplier: float = 1.0,
    fast_mode: bool = False,
    region: str = "us-east-1",
) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=_FakePricing(prices),
        pricing_multiplier=pricing_multiplier,
        fast_mode=fast_mode,
        region=region,
        warnings=[],
        permissions=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(
        (service, action, msg)
    )
    ctx.client = lambda name, **_kw: {"sagemaker": sm, "cloudwatch": cw}[name]
    return ctx


def _endpoint(name: str, instance_type: str = "ml.m5.xlarge", status: str = "InService"):
    cfg = f"{name}-cfg"
    ep = {"EndpointName": name, "EndpointStatus": status, "EndpointConfigName": cfg}
    config = {cfg: {"ProductionVariants": [{"InstanceType": instance_type}]}}
    return ep, config


# --------------------------------------------------------------------------- #
# C2 — idle endpoint counted at full (live-validated) instance cost
# --------------------------------------------------------------------------- #
def test_idle_endpoint_counted_at_full_instance_cost() -> None:
    ep, cfg = _endpoint("idle-ep", "ml.m5.xlarge")
    sm = _FakeSageMaker(endpoints=[ep], endpoint_configs=cfg)
    cw = _FakeCloudWatch({"idle-ep": 0.0})  # zero invocations -> idle
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY})

    findings = SageMakerModule().scan(ctx)

    assert findings.total_monthly_savings == pytest.approx(M5_XLARGE_MONTHLY)  # $167.90
    assert findings.total_recommendations == 1
    rec = findings.sources["idle_endpoints"].recommendations[0]
    assert rec["Counted"] is True
    assert rec["EstimatedMonthlySavings"] == pytest.approx(M5_XLARGE_MONTHLY)
    # Counted == rendered: the formatted string equals the counted dollar.
    assert rec["EstimatedSavings"].startswith("$167.90")
    # Structured AuditBasis defends the counted dollar from the report alone.
    assert rec["AuditBasis"]["instance_monthly"] == pytest.approx(M5_XLARGE_MONTHLY)
    assert "delete idle endpoint" in rec["AuditBasis"]["formula"]


# --------------------------------------------------------------------------- #
# C2 — no price -> abstain (no $0 "Delete endpoint" placeholder, no count)
# --------------------------------------------------------------------------- #
def test_idle_endpoint_without_price_abstains() -> None:
    ep, cfg = _endpoint("idle-ep", "ml.unknown.type")
    sm = _FakeSageMaker(endpoints=[ep], endpoint_configs=cfg)
    cw = _FakeCloudWatch({"idle-ep": 0.0})
    ctx = _ctx(sm, cw, prices={})  # no price for the type -> 0.0

    findings = SageMakerModule().scan(ctx)

    # No fabricated $0 delete placeholder: zero recs, zero dollars.
    assert findings.sources["idle_endpoints"].count == 0
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# C2 — count hygiene: advisory notebook ($0) rendered but not counted
# --------------------------------------------------------------------------- #
def test_notebook_advisory_not_counted() -> None:
    nb = {"NotebookInstanceName": "nb-1", "InstanceType": "ml.t3.medium"}
    sm = _FakeSageMaker(notebooks=[nb])
    cw = _FakeCloudWatch()
    ctx = _ctx(sm, cw, prices={"ml.t3.medium": 36.5})

    findings = SageMakerModule().scan(ctx)

    # Rendered (visible in its source) ...
    assert findings.sources["idle_notebooks"].count == 1
    rec = findings.sources["idle_notebooks"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    # ... but excluded from BOTH the dollar total and the counted rec count.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# H1 — spot_training is a one-time per-run figure -> advisory $0
# --------------------------------------------------------------------------- #
def test_spot_training_is_advisory_per_run_one_time() -> None:
    job = {"TrainingJobName": "job-1", "TrainingTimeInSeconds": 36000}  # 10h
    detail = {
        "job-1": {
            "EnableManagedSpotTraining": False,
            "ResourceConfig": {"InstanceType": "ml.m5.xlarge"},
        }
    }
    sm = _FakeSageMaker(training_jobs=[job], training_details=detail)
    cw = _FakeCloudWatch()
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY})

    findings = SageMakerModule().scan(ctx)

    # The per-run figure must NOT inflate the recurring monthly headline.
    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    rec = findings.sources["spot_training"].recommendations[0]
    assert rec["Counted"] is False
    assert rec["EstimatedMonthlySavings"] == 0.0
    assert "per-run, one-time" in rec["EstimatedSavings"]

    # AuditBasis records rate / training_hours / 0.70 and a positive per-run $.
    hourly = M5_XLARGE_MONTHLY / HOURS_PER_MONTH
    expected_per_run = hourly * 10.0 * SPOT_SAVINGS_RATE
    basis = rec["AuditBasis"]
    assert basis["training_hours"] == pytest.approx(10.0)
    assert basis["per_run_savings"] == pytest.approx(round(expected_per_run, 2), abs=0.01)
    assert str(SPOT_SAVINGS_RATE) in basis["rate"]


# --------------------------------------------------------------------------- #
# H2 — idle endpoint excluded from consolidation grouping (no double-count)
# --------------------------------------------------------------------------- #
def test_idle_endpoint_excluded_from_consolidation_two_endpoints() -> None:
    # Two InService endpoints share ml.m5.xlarge; one is idle. With the idle one
    # removed from the grouping population, only ONE non-idle remains -> no
    # consolidation rec. The idle endpoint is counted exactly once (deletion).
    ep_a, cfg_a = _endpoint("idle-ep", "ml.m5.xlarge")
    ep_b, cfg_b = _endpoint("busy-ep", "ml.m5.xlarge")
    sm = _FakeSageMaker(endpoints=[ep_a, ep_b], endpoint_configs={**cfg_a, **cfg_b})
    cw = _FakeCloudWatch({"idle-ep": 0.0, "busy-ep": 5000.0})
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY})

    findings = SageMakerModule().scan(ctx)

    assert findings.sources["multi_model_consolidation"].count == 0
    assert findings.sources["idle_endpoints"].count == 1
    # Idle compute counted once, not once-as-idle + once-in-consolidation-group.
    assert findings.total_monthly_savings == pytest.approx(M5_XLARGE_MONTHLY)
    assert findings.total_recommendations == 1


def test_idle_endpoint_excluded_but_consolidation_still_fires_on_non_idle() -> None:
    # Three InService endpoints share ml.m5.xlarge; one idle, two busy. Idle is
    # counted for deletion; consolidation runs over the TWO non-idle endpoints
    # only (endpoint_count == 2, not 3).
    ep_a, cfg_a = _endpoint("idle-ep", "ml.m5.xlarge")
    ep_b, cfg_b = _endpoint("busy-1", "ml.m5.xlarge")
    ep_c, cfg_c = _endpoint("busy-2", "ml.m5.xlarge")
    sm = _FakeSageMaker(
        endpoints=[ep_a, ep_b, ep_c],
        endpoint_configs={**cfg_a, **cfg_b, **cfg_c},
    )
    cw = _FakeCloudWatch({"idle-ep": 0.0, "busy-1": 9000.0, "busy-2": 9000.0})
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY})

    findings = SageMakerModule().scan(ctx)

    cons = findings.sources["multi_model_consolidation"].recommendations
    assert len(cons) == 1
    # Group population excludes the idle endpoint -> 2 endpoints, not 3.
    assert cons[0]["endpoint_count"] == 2
    expected_cons = (2 - 1) * M5_XLARGE_MONTHLY * CONSOLIDATION_SAVINGS_RATE
    assert cons[0]["EstimatedMonthlySavings"] == pytest.approx(round(expected_cons, 2))
    assert cons[0]["Counted"] is True

    # Total = idle full cost + consolidation over the two non-idle endpoints.
    expected_total = round(M5_XLARGE_MONTHLY + expected_cons, 2)
    assert findings.total_monthly_savings == pytest.approx(expected_total, abs=0.01)
    assert findings.total_recommendations == 2


# --------------------------------------------------------------------------- #
# counted == rendered: the headline equals the sum of Counted!=False dollars
# --------------------------------------------------------------------------- #
def test_total_equals_sum_of_counted_recs() -> None:
    ep_a, cfg_a = _endpoint("idle-ep", "ml.m5.xlarge")
    ep_b, cfg_b = _endpoint("busy-1", "ml.m5.xlarge")
    ep_c, cfg_c = _endpoint("busy-2", "ml.m5.xlarge")
    nb = {"NotebookInstanceName": "nb-1", "InstanceType": "ml.t3.medium"}
    job = {"TrainingJobName": "job-1", "TrainingTimeInSeconds": 36000}
    detail = {"job-1": {"EnableManagedSpotTraining": False, "ResourceConfig": {"InstanceType": "ml.m5.xlarge"}}}
    sm = _FakeSageMaker(
        endpoints=[ep_a, ep_b, ep_c],
        endpoint_configs={**cfg_a, **cfg_b, **cfg_c},
        notebooks=[nb],
        training_jobs=[job],
        training_details=detail,
    )
    cw = _FakeCloudWatch({"idle-ep": 0.0, "busy-1": 9000.0, "busy-2": 9000.0})
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY, "ml.t3.medium": 36.5})

    findings = SageMakerModule().scan(ctx)

    all_recs = [
        r
        for src in findings.sources.values()
        for r in src.recommendations
    ]
    counted_sum = round(
        sum(r["EstimatedMonthlySavings"] for r in all_recs if r.get("Counted") is not False), 2
    )
    counted_n = sum(1 for r in all_recs if r.get("Counted") is not False)
    assert findings.total_monthly_savings == pytest.approx(counted_sum, abs=0.01)
    assert findings.total_recommendations == counted_n
    # advisory recs (notebook + spot) are present but excluded.
    assert any(r.get("Counted") is False for r in all_recs)


# --------------------------------------------------------------------------- #
# L2 — paginator-failure fallback walks every NextToken page (no 1-page cap)
# --------------------------------------------------------------------------- #
class _FakeSageMakerNoPaginator:
    """SageMaker client whose paginator is unavailable.

    ``get_paginator`` raises so the adapter takes the manual fallback; the
    fallback must then walk every ``NextToken`` page rather than capping at the
    first ~100 endpoints.
    """

    def __init__(self, pages_by_token: dict[Any, dict[str, Any]]) -> None:
        self._pages = pages_by_token
        self.calls: list[Any] = []

    def get_paginator(self, name: str) -> Any:  # noqa: ANN401 - boto3 shape
        raise RuntimeError("paginator unavailable")

    def list_endpoints(self, **kwargs: Any) -> dict[str, Any]:
        token = kwargs.get("NextToken")
        self.calls.append(token)
        return self._pages[token]


def test_list_endpoints_fallback_paginates_all_pages() -> None:
    pages = {
        None: {"Endpoints": [{"EndpointName": "ep-0"}], "NextToken": "t1"},
        "t1": {"Endpoints": [{"EndpointName": "ep-1"}], "NextToken": "t2"},
        "t2": {"Endpoints": [{"EndpointName": "ep-2"}]},  # no NextToken -> last
    }
    sm = _FakeSageMakerNoPaginator(pages)

    endpoints = sm_mod._list_endpoints(sm)

    # All three pages walked, not just the first (the old single-call fallback
    # would have returned only "ep-0").
    assert [ep["EndpointName"] for ep in endpoints] == ["ep-0", "ep-1", "ep-2"]
    # First call has no token; subsequent calls thread the prior NextToken.
    assert sm.calls == [None, "t1", "t2"]


def test_list_endpoints_fallback_single_page() -> None:
    pages = {None: {"Endpoints": [{"EndpointName": "only-ep"}]}}
    sm = _FakeSageMakerNoPaginator(pages)

    endpoints = sm_mod._list_endpoints(sm)

    assert [ep["EndpointName"] for ep in endpoints] == ["only-ep"]
    assert sm.calls == [None]  # one call, then break on absent NextToken


# --------------------------------------------------------------------------- #
# Fast mode: no CloudWatch reads for the idle check; no crash.
# --------------------------------------------------------------------------- #
def test_fast_mode_skips_idle_cloudwatch() -> None:
    ep_a, cfg_a = _endpoint("ep-1", "ml.m5.xlarge")
    sm = _FakeSageMaker(endpoints=[ep_a], endpoint_configs=cfg_a)
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _ctx(sm, cw, prices={"ml.m5.xlarge": M5_XLARGE_MONTHLY}, fast_mode=True)

    findings = SageMakerModule().scan(ctx)

    # Idle check is CloudWatch-gated; fast mode emits no idle recs.
    assert findings.sources["idle_endpoints"].count == 0
    assert findings.extras["active_endpoint_count"] == 1

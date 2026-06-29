"""Unit tests for the Lambda adapter cost-audit fixes (C1–C8).

Covers, with the same SimpleNamespace-ctx + fake-boto3 style as
``tests/test_lambda_dedupe_and_eks_zero.py``:

  - C1  CO opt-in placeholder is converted to a warning and dropped from counts.
  - C2  Lambda CO AccessDenied is classified as a permission issue (not silent).
  - C7  Cross-source dedup normalizes qualified ARNs to the bare function name.
  - C3  Provisioned Concurrency saving is metric-gated on utilization; without a
        utilization metric it is a $0 advisory.
  - C4  arm64 functions price PC at the (cheaper) arm64 PC rate.
  - C5  Fast mode skips the shim's CloudWatch reads and warns once.
  - C6  A PC-config read error does not skip the ARM check; CW AccessDenied is a
        permission issue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import services.adapters.lambda_svc as adapter_mod
import services.lambda_svc as shim_mod
from services.adapters.lambda_svc import (
    _LAMBDA_PC_PRICE_PER_GB_SEC,
    _LAMBDA_PC_PRICE_PER_GB_SEC_ARM,
    LambdaModule,
    _normalize_lambda_fn_name,
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
    ctx.cost_hub_splits = kw.pop("cost_hub_splits", {"lambda": []})
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakePCPaginator:
    """Paginator for list_provisioned_concurrency_configs.

    Emits one PC config per page so tests can prove the shim walks every page;
    raises for functions in ``pc_error`` to exercise the read-failure path.
    """

    def __init__(self, pc: dict[str, list[dict[str, Any]]], pc_error: set[str]) -> None:
        self._pc = pc
        self._pc_error = pc_error

    def paginate(self, FunctionName: str):  # noqa: ANN201, N803 - boto3 shape
        if FunctionName in self._pc_error:
            raise Exception("ServiceException: transient")
        configs = self._pc.get(FunctionName, [])
        pages = [{"ProvisionedConcurrencyConfigs": [c]} for c in configs]
        return iter(pages or [{"ProvisionedConcurrencyConfigs": []}])


class _FakeLambdaClient:
    """Minimal boto3 Lambda client driving the enhanced-checks shim."""

    def __init__(
        self,
        functions: list[dict[str, Any]],
        pc: dict[str, list[dict[str, Any]]] | None = None,
        pc_error: set[str] | None = None,
    ) -> None:
        self._functions = functions
        self._pc = pc or {}
        self._pc_error = pc_error or set()

    def get_paginator(self, name: str):  # noqa: ANN201 - boto3 shape
        if name == "list_provisioned_concurrency_configs":
            return _FakePCPaginator(self._pc, self._pc_error)
        return _FakePaginator([{"Functions": self._functions}])

    def get_function_configuration(self, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        return {"VpcConfig": {}, "ReservedConcurrentExecutions": None}


class _FakeCloudWatch:
    """Returns canned metric datapoints, or raises a canned error."""

    def __init__(
        self,
        invocations: float = 0.0,
        pc_util_max: float | None = None,
        error: Exception | None = None,
    ) -> None:
        self._invocations = invocations
        self._pc_util_max = pc_util_max
        self._error = error

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        metric = kwargs.get("MetricName")
        if metric == "Invocations":
            return {"Datapoints": [{"Sum": self._invocations}]}
        if metric == "ProvisionedConcurrencyUtilization":
            if self._pc_util_max is None:
                return {"Datapoints": []}
            return {"Datapoints": [{"Maximum": self._pc_util_max}]}
        return {"Datapoints": []}


def _client_factory(lam: Any, cw: Any):
    def _client(name: str, **_kw: Any):
        return {"lambda": lam, "cloudwatch": cw}[name]

    return _client


# --------------------------------------------------------------------------- #
# C7 — normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("arn:aws:lambda:eu-west-1:1:function:myFn", "myFn"),
        ("arn:aws:lambda:eu-west-1:1:function:myFn:PROD", "myFn"),
        ("arn:aws:lambda:eu-west-1:1:function:myFn:42", "myFn"),
        ("myFn", "myFn"),
        ("myFn:alias", "myFn"),
        ("", ""),
    ],
)
def test_normalize_lambda_fn_name(value: str, expected: str) -> None:
    assert _normalize_lambda_fn_name(value) == expected


def test_qualified_coh_arn_dedupes_against_co(monkeypatch: pytest.MonkeyPatch) -> None:
    fn = "checkout-handler"
    # Cost Hub returns a *qualified* ARN (alias suffix) — must still dedup.
    cost_hub = [{"resourceArn": f"arn:aws:lambda:eu-west-1:1:function:{fn}:PROD",
                 "estimatedMonthlySavings": 5.00}]
    co = [{"resource_name": fn, "estimatedMonthlySavings": 4.50}]

    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: list(co))
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": []})

    ctx = _recording_ctx(cost_hub_splits={"lambda": [dict(r) for r in cost_hub]})
    findings = LambdaModule().scan(ctx)

    assert findings.total_monthly_savings == 5.00
    assert len(findings.sources["compute_optimizer"].recommendations) == 0


# --------------------------------------------------------------------------- #
# counted == rendered — CoH recs get a PascalCase EstimatedSavings string so the
# reporter renders the dollar instead of the "Cost optimization" placeholder.
# --------------------------------------------------------------------------- #
def test_coh_rec_normalized_for_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    cost_hub = [{"resourceArn": "arn:aws:lambda:eu-west-1:1:function:fwd", "estimatedMonthlySavings": 7.513}]
    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [])
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": []})

    ctx = _recording_ctx(cost_hub_splits={"lambda": [dict(r) for r in cost_hub]})
    findings = LambdaModule().scan(ctx)

    rec = findings.sources["cost_optimization_hub"].recommendations[0]
    # The card renders this string instead of the "Cost optimization" placeholder.
    assert rec["EstimatedSavings"] == "$7.51/month"
    assert rec["EstimatedMonthlySavings"] == 7.51
    assert rec["Counted"] is True
    # The counted dollar is unchanged (summed from the camelCase source key).
    assert findings.total_monthly_savings == 7.513


# --------------------------------------------------------------------------- #
# C1 — opt-in placeholder → warning, dropped from counts
# --------------------------------------------------------------------------- #
def test_co_opt_in_placeholder_becomes_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    placeholder = {
        "ResourceId": "compute-optimizer-service",
        "Recommendation": "Enable AWS Compute Optimizer for Lambda memory-rightsizing recommendations",
        "estimatedMonthlySavings": 0.0,
    }
    monkeypatch.setattr(
        adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [dict(placeholder)]
    )
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": []})

    ctx = _recording_ctx()
    findings = LambdaModule().scan(ctx)

    # Placeholder dropped: zero recs counted, zero savings.
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0
    assert findings.sources["compute_optimizer"].count == 0
    # A warning was surfaced for the opt-in gap.
    assert any("Compute Optimizer is not enabled" in msg for _svc, msg in ctx.warnings)


# --------------------------------------------------------------------------- #
# C2 — Lambda CO AccessDenied → permission_issue (not silent)
# --------------------------------------------------------------------------- #
def test_lambda_co_access_denied_is_permission_issue() -> None:
    from services.advisor import get_lambda_compute_optimizer_recommendations

    class _DeniedCO:
        def get_lambda_function_recommendations(self, **_kw: Any) -> dict[str, Any]:
            raise Exception("AccessDeniedException: not authorized")

    ctx = _recording_ctx()
    ctx.client = lambda name, **_kw: _DeniedCO()

    recs = get_lambda_compute_optimizer_recommendations(ctx)

    assert recs == []
    assert ctx.permissions, "AccessDenied must be recorded via ctx.permission_issue"
    svc, action, _msg = ctx.permissions[0]
    assert svc == "lambda"
    assert action == "compute-optimizer:GetLambdaFunctionRecommendations"


# --------------------------------------------------------------------------- #
# C3 / C4 — PC saving is metric-gated and architecture-aware
# --------------------------------------------------------------------------- #
def _pc_rec(arch: str, *, mem: int = 1024, count: int = 2, util: float | None) -> dict[str, Any]:
    rec = {
        "FunctionName": f"pc-fn-{arch}",
        "MemorySize": mem,
        "Architecture": arch,
        "ProvisionedConcurrency": count,
        "CheckCategory": "Lambda Provisioned Concurrency",
    }
    if util is not None:
        rec["MaxUtilization"] = util
    return rec


def _expected_pc(rate: float, mem_mb: int, count: int, util: float) -> float:
    mem_gb = mem_mb / 1024
    return mem_gb * rate * 730 * 3600 * count * (1.0 - util)


def test_pc_saving_metric_gated_x86(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _pc_rec("x86_64", util=0.25)
    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [])
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": [rec]})

    ctx = _recording_ctx()
    findings = LambdaModule().scan(ctx)

    expected = _expected_pc(_LAMBDA_PC_PRICE_PER_GB_SEC, 1024, 2, 0.25)
    assert findings.total_monthly_savings == pytest.approx(expected, rel=1e-6)
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(round(expected, 2))
    assert emitted.get("Counted") is not False  # counted
    assert emitted["AuditBasis"]["architecture"] == "x86_64"
    assert emitted["AuditBasis"]["max_utilization"] == 0.25


def test_pc_saving_arm_uses_cheaper_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _pc_rec("arm64", util=0.25)
    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [])
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": [rec]})

    ctx = _recording_ctx()
    findings = LambdaModule().scan(ctx)

    expected = _expected_pc(_LAMBDA_PC_PRICE_PER_GB_SEC_ARM, 1024, 2, 0.25)
    assert findings.total_monthly_savings == pytest.approx(expected, rel=1e-6)
    # arm64 PC strictly cheaper than x86 PC for identical config.
    assert expected < _expected_pc(_LAMBDA_PC_PRICE_PER_GB_SEC, 1024, 2, 0.25)


def test_pc_without_utilization_metric_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _pc_rec("x86_64", util=None)  # no metric (fast mode / no datapoints)
    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [])
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": [rec]})

    ctx = _recording_ctx()
    findings = LambdaModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0
    emitted = findings.sources["enhanced_checks"].recommendations[0]
    assert emitted["EstimatedMonthlySavings"] == 0.0
    assert emitted["Counted"] is False  # advisory, rendered but not counted
    assert "ProvisionedConcurrencyUtilization" in emitted["PricingWarning"]


def test_pc_region_multiplier_applied_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _pc_rec("x86_64", util=0.0)
    monkeypatch.setattr(adapter_mod, "get_lambda_compute_optimizer_recommendations", lambda c: [])
    monkeypatch.setattr(adapter_mod, "get_enhanced_lambda_checks", lambda c: {"recommendations": [rec]})

    ctx = _recording_ctx(pricing_multiplier=1.5)
    findings = LambdaModule().scan(ctx)

    base = _expected_pc(_LAMBDA_PC_PRICE_PER_GB_SEC, 1024, 2, 0.0)
    assert findings.total_monthly_savings == pytest.approx(base * 1.5, rel=1e-6)


# --------------------------------------------------------------------------- #
# C5 / C6 — shim fast_mode + silent-failure classification
# --------------------------------------------------------------------------- #
def _func(name: str, arch: str = "x86_64", mem: int = 512, runtime: str = "python3.12") -> dict[str, Any]:
    return {
        "FunctionName": name,
        "FunctionArn": f"arn:aws:lambda:eu-west-1:1:function:{name}",
        "MemorySize": mem,
        "Timeout": 30,
        "Runtime": runtime,
        "Architectures": [arch],
    }


def test_fast_mode_skips_cloudwatch_and_warns_once() -> None:
    lam = _FakeLambdaClient(
        functions=[_func("a"), _func("b")],
        pc={"a": [{"AllocatedProvisionedConcurrentExecutions": 2}]},
    )
    # If the shim hits CloudWatch in fast mode, this would raise.
    cw = _FakeCloudWatch(error=AssertionError("CloudWatch must not be called in fast mode"))
    ctx = _recording_ctx(fast_mode=True)
    ctx.client = _client_factory(lam, cw)

    result = shim_mod.get_enhanced_lambda_checks(ctx)
    recs = result["recommendations"]

    # PC rec emitted but with no utilization metric (→ adapter makes it advisory).
    pc = [r for r in recs if r["CheckCategory"] == "Lambda Provisioned Concurrency"]
    assert len(pc) == 1
    assert "MaxUtilization" not in pc[0]
    # No ARM recs (CW skipped). Exactly one fast-mode warning.
    assert not [r for r in recs if r["CheckCategory"] == "Lambda ARM Migration"]
    assert sum("Fast mode" in msg for _svc, msg in ctx.warnings) == 1


def test_pc_config_error_does_not_skip_arm_check() -> None:
    # 'a' raises on PC-config read; ARM check must still run for it.
    lam = _FakeLambdaClient(functions=[_func("a")], pc_error={"a"})
    cw = _FakeCloudWatch(invocations=500.0)  # > ARM_MIN_WEEKLY_INVOCATIONS
    ctx = _recording_ctx()
    ctx.client = _client_factory(lam, cw)

    result = shim_mod.get_enhanced_lambda_checks(ctx)
    recs = result["recommendations"]

    assert [r for r in recs if r["CheckCategory"] == "Lambda ARM Migration"], (
        "ARM check must run even after a PC-config read error"
    )
    assert ctx.warnings, "PC-config failure must be surfaced via ctx.warn"


def test_cloudwatch_access_denied_is_permission_issue() -> None:
    lam = _FakeLambdaClient(
        functions=[_func("a")],
        pc={"a": [{"AllocatedProvisionedConcurrentExecutions": 1}]},
    )
    cw = _FakeCloudWatch(error=Exception("AccessDeniedException: no cloudwatch"))
    ctx = _recording_ctx()
    ctx.client = _client_factory(lam, cw)

    shim_mod.get_enhanced_lambda_checks(ctx)

    assert ctx.permissions, "CloudWatch AccessDenied must be a permission_issue"
    assert any(action == "cloudwatch:GetMetricStatistics" for _s, action, _m in ctx.permissions)


def test_pc_utilization_attached_when_metric_present() -> None:
    lam = _FakeLambdaClient(
        functions=[_func("a")],
        pc={"a": [{"AllocatedProvisionedConcurrentExecutions": 3}]},
    )
    cw = _FakeCloudWatch(pc_util_max=0.4)
    ctx = _recording_ctx()
    ctx.client = _client_factory(lam, cw)

    result = shim_mod.get_enhanced_lambda_checks(ctx)
    pc = [r for r in result["recommendations"] if r["CheckCategory"] == "Lambda Provisioned Concurrency"]
    assert pc[0]["MaxUtilization"] == 0.4
    assert pc[0]["Architecture"] == "x86_64"


# --------------------------------------------------------------------------- #
# L3 — ListProvisionedConcurrencyConfigs is paginated (no silent truncation)
# --------------------------------------------------------------------------- #
def test_pc_configs_collected_across_all_pages() -> None:
    # 'a' has two PC configs returned on separate pages; the shim must walk
    # every page rather than reading only the first.
    lam = _FakeLambdaClient(
        functions=[_func("a")],
        pc={"a": [
            {"AllocatedProvisionedConcurrentExecutions": 2},
            {"AllocatedProvisionedConcurrentExecutions": 5},
        ]},
    )
    cw = _FakeCloudWatch(pc_util_max=0.4)
    ctx = _recording_ctx()
    ctx.client = _client_factory(lam, cw)

    result = shim_mod.get_enhanced_lambda_checks(ctx)
    pc = [r for r in result["recommendations"] if r["CheckCategory"] == "Lambda Provisioned Concurrency"]

    # One rec per PC config across both pages — neither page is dropped.
    assert len(pc) == 2
    assert {r["ProvisionedConcurrency"] for r in pc} == {2, 5}

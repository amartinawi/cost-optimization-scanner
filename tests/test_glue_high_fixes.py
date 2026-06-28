"""Unit tests for the Glue adapter HIGH cost-audit fix (glue H2).

glue H2: a READY dev endpoint reached the adapter with no DPU fields, so it was
counted as $0 while its hardcoded ``"$316/month"`` string was never counted
(counted != rendered). The fix prices the endpoint from its own provisioned DPU
footprint (``WorkerType``/``NumberOfWorkers`` via the WorkerType->DPU multiplier,
legacy ``NumberOfNodes``, else the 5-DPU AWS default) x $0.44/DPU-hour x 730 hr,
single-sourcing the displayed string from the counted dollar with an
``AuditBasis``.

Rate validated live (AWS Pricing API, 2026-06-27): usagetype
``USE1-DEVED-DPU-Hour`` = $0.4400/DPU-Hour (SKU H5SXAYMQH485TMM7).

Drives the pure DPU-footprint logic, the ``scan()`` path (monkeypatched
enhanced-checks helper), and the shim->adapter integration path (fake boto3
glue client), proving each counted dollar / $0 advisory with explicit
assertions. Mirrors tests/test_lambda_audit_fixes.py /
tests/test_audit_fixes_counted_dollars.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.glue as glue_adapter
import services.glue as glue_shim
from services._savings import parse_dollar_savings
from services.adapters.glue import (
    DEFAULT_DEV_ENDPOINT_DPU,
    DEV_ENDPOINT_MONTHLY_HOURS,
    GLUE_DPU_HOURLY,
    GlueModule,
    _dev_endpoint_dpu,
)

_DEV_CATEGORY = "Glue Dev Endpoints"
_JOB_CATEGORY = "Glue Job Rightsizing"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
def _ctx(*, pricing_multiplier: float = 1.0, client: Any = None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_multiplier=pricing_multiplier,
        region="us-east-1",
        fast_mode=False,
        warnings=[],
        permission_issues=[],
    )
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permission_issues.append(
        (service, msg, action)
    )
    ctx.client = client or (lambda name, **kw: None)
    return ctx


def _result(recs_by_category: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a shim result whose ``recommendations`` share objects with ``checks``.

    Mirrors the real shim so adapter mutations on the flat ``recommendations``
    list are observable through the per-category ``checks`` SourceBlocks.
    """
    checks = dict(recs_by_category)
    all_recs: list[dict[str, Any]] = []
    for recs in checks.values():
        all_recs.extend(recs)
    return {"recommendations": all_recs, "checks": checks}


def _patch_checks(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    monkeypatch.setattr(glue_adapter, "get_enhanced_glue_checks", lambda ctx: result)


class _FakeGluePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeGlueClient:
    """Minimal boto3 Glue client driving the enhanced-checks shim."""

    def __init__(
        self,
        jobs: list[dict[str, Any]] | None = None,
        dev_endpoints: list[dict[str, Any]] | None = None,
        crawlers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._jobs = jobs or []
        self._dev_endpoints = dev_endpoints or []
        self._crawlers = crawlers or []

    def get_paginator(self, name: str) -> _FakeGluePaginator:
        if name == "get_jobs":
            return _FakeGluePaginator([{"Jobs": self._jobs}])
        if name == "get_crawlers":
            return _FakeGluePaginator([{"Crawlers": self._crawlers}])
        raise AssertionError(f"unexpected paginator: {name}")

    def get_dev_endpoints(self) -> dict[str, Any]:
        return {"DevEndpoints": self._dev_endpoints}


def _dev_rec(**kw: Any) -> dict[str, Any]:
    rec = {
        "EndpointName": kw.pop("name", "ep1"),
        "Status": "READY",
        "WorkerType": kw.pop("worker_type", None),
        "NumberOfWorkers": kw.pop("num_workers", None),
        "NumberOfNodes": kw.pop("num_nodes", None),
        "CheckCategory": _DEV_CATEGORY,
    }
    rec.update(kw)
    return rec


# --------------------------------------------------------------------------- #
# Pure logic — WorkerType->DPU footprint resolution (glue C1/H3 multiplier)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rec,expected_dpu,expected_basis",
    [
        ({"WorkerType": "G.2X", "NumberOfWorkers": 4}, 8.0, "worker_type"),
        ({"WorkerType": "Standard", "NumberOfWorkers": 2}, 2.0, "worker_type"),
        ({"WorkerType": "G.1X", "NumberOfWorkers": 5}, 5.0, "worker_type"),
        ({"WorkerType": "G.025X", "NumberOfWorkers": 2}, 0.5, "worker_type"),
        ({"NumberOfNodes": 3}, 3.0, "number_of_nodes"),
        ({}, DEFAULT_DEV_ENDPOINT_DPU, "default_5_dpu"),
        # Zero workers is falsy -> fall through to default (not a 0-DPU endpoint).
        ({"WorkerType": "G.1X", "NumberOfWorkers": 0}, DEFAULT_DEV_ENDPOINT_DPU, "default_5_dpu"),
        # Unknown worker type -> multiplier unknown -> fall through, never guess.
        ({"WorkerType": "Nope", "NumberOfWorkers": 3}, DEFAULT_DEV_ENDPOINT_DPU, "default_5_dpu"),
        # Worker footprint absent but legacy node count present.
        ({"WorkerType": "G.2X", "NumberOfWorkers": None, "NumberOfNodes": 2}, 2.0, "number_of_nodes"),
    ],
)
def test_dev_endpoint_dpu_footprint(rec: dict[str, Any], expected_dpu: float, expected_basis: str) -> None:
    dpu, basis = _dev_endpoint_dpu(rec)
    assert dpu == pytest.approx(expected_dpu)
    assert basis == expected_basis


def test_dev_endpoint_g2x_multiplier_not_raw_worker_count() -> None:
    # Regression for glue C1/H3: a G.2X worker is 2 DPU, not 1 — treating
    # NumberOfWorkers as a raw DPU count would under-price by 2x.
    dpu, _ = _dev_endpoint_dpu({"WorkerType": "G.2X", "NumberOfWorkers": 10})
    assert dpu == 20.0


# --------------------------------------------------------------------------- #
# scan() — dev endpoint priced from its DPU footprint and COUNTED (glue H2)
# --------------------------------------------------------------------------- #
def test_default_dev_endpoint_counted_at_five_dpu(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _dev_rec()  # no footprint -> 5-DPU default
    _patch_checks(monkeypatch, _result({"dev_endpoints": [rec]}))

    findings = GlueModule().scan(_ctx())

    expected = round(5.0 * GLUE_DPU_HOURLY * DEV_ENDPOINT_MONTHLY_HOURS, 2)  # 1606.0
    assert expected == 1606.0
    assert findings.total_monthly_savings == pytest.approx(expected)
    emitted = findings.sources["dev_endpoints"].recommendations[0]
    assert emitted["Counted"] is True
    assert emitted["EstimatedMonthlySavings"] == pytest.approx(expected)
    # Counted == rendered: the displayed string carries the same dollar.
    assert emitted["EstimatedSavings"] == "$1,606.00/month"
    assert "316" not in emitted["EstimatedSavings"]
    assert emitted["AuditBasis"]["dpu_count"] == 5.0
    assert emitted["AuditBasis"]["dpu_basis"] == "default_5_dpu"
    assert emitted["AuditBasis"]["rate_per_dpu_hour"] == 0.44


def test_worker_type_dev_endpoint_counted_with_multiplier(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _dev_rec(worker_type="G.2X", num_workers=4)  # 8 DPU
    _patch_checks(monkeypatch, _result({"dev_endpoints": [rec]}))

    findings = GlueModule().scan(_ctx())

    expected = round(8.0 * GLUE_DPU_HOURLY * DEV_ENDPOINT_MONTHLY_HOURS, 2)  # 2569.6
    assert findings.total_monthly_savings == pytest.approx(expected)
    emitted = findings.sources["dev_endpoints"].recommendations[0]
    assert emitted["EstimatedSavings"] == "$2,569.60/month"
    assert emitted["AuditBasis"]["dpu_count"] == 8.0
    assert emitted["AuditBasis"]["dpu_basis"] == "worker_type"


def test_legacy_node_count_dev_endpoint_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _dev_rec(num_nodes=3)  # 3 DPU via NumberOfNodes
    _patch_checks(monkeypatch, _result({"dev_endpoints": [rec]}))

    findings = GlueModule().scan(_ctx())

    expected = round(3.0 * GLUE_DPU_HOURLY * DEV_ENDPOINT_MONTHLY_HOURS, 2)  # 963.6
    assert findings.total_monthly_savings == pytest.approx(expected)
    assert findings.sources["dev_endpoints"].recommendations[0]["AuditBasis"]["dpu_basis"] == "number_of_nodes"


def test_pricing_multiplier_applied_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _dev_rec()  # 5 DPU default
    _patch_checks(monkeypatch, _result({"dev_endpoints": [rec]}))

    findings = GlueModule().scan(_ctx(pricing_multiplier=1.5))

    base = 5.0 * GLUE_DPU_HOURLY * DEV_ENDPOINT_MONTHLY_HOURS
    assert findings.total_monthly_savings == pytest.approx(round(base * 1.5, 2))  # 2409.0
    assert findings.sources["dev_endpoints"].recommendations[0]["AuditBasis"]["pricing_multiplier"] == 1.5


def test_counted_equals_rendered_string(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _dev_rec(worker_type="G.1X", num_workers=10)  # 10 DPU
    _patch_checks(monkeypatch, _result({"dev_endpoints": [rec]}))

    findings = GlueModule().scan(_ctx())
    emitted = findings.sources["dev_endpoints"].recommendations[0]
    # The number parsed back out of the rendered string equals the counted dollar
    # and the tab headline — no string-vs-number desync.
    parsed = parse_dollar_savings(emitted["EstimatedSavings"])
    assert parsed == pytest.approx(emitted["EstimatedMonthlySavings"])
    assert parsed == pytest.approx(findings.total_monthly_savings)


# --------------------------------------------------------------------------- #
# scan() — job rightsizing stays $0 advisory; count hygiene (glue C2)
# --------------------------------------------------------------------------- #
def test_job_rightsizing_is_zero_advisory_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    dev = _dev_rec()  # counted
    job = {
        "JobName": "etl-1",
        "MaxCapacity": 20,
        "WorkerType": None,
        "NumberOfWorkers": 0,
        "CheckCategory": _JOB_CATEGORY,
    }
    _patch_checks(monkeypatch, _result({"dev_endpoints": [dev], "job_rightsizing": [job]}))

    findings = GlueModule().scan(_ctx())

    # Only the dev endpoint dollar is summed.
    assert findings.total_monthly_savings == pytest.approx(1606.0)
    # Count hygiene: the $0 advisory job rec is excluded from the headline count.
    assert findings.total_recommendations == 1

    job_rec = findings.sources["job_rightsizing"].recommendations[0]
    assert job_rec["Counted"] is False
    assert job_rec["EstimatedMonthlySavings"] == 0.0
    assert job_rec["EstimatedSavings"].startswith("$0.00/month")


# --------------------------------------------------------------------------- #
# Shim — $316 string removed; DPU footprint fields carried (glue H2)
# --------------------------------------------------------------------------- #
def test_shim_drops_hardcoded_316_and_carries_dpu_fields() -> None:
    glue_client = _FakeGlueClient(
        dev_endpoints=[
            {"EndpointName": "ep1", "Status": "READY", "WorkerType": "G.1X", "NumberOfWorkers": 5},
            {"EndpointName": "ep2", "Status": "PROVISIONING"},  # not READY -> skipped
        ]
    )
    ctx = _ctx(client=lambda name, **kw: glue_client)

    result = glue_shim.get_enhanced_glue_checks(ctx)
    dev = [r for r in result["recommendations"] if r["CheckCategory"] == _DEV_CATEGORY]

    assert len(dev) == 1  # only the READY endpoint
    rec = dev[0]
    # The hardcoded "$316/month per endpoint" string is gone; the adapter sets it.
    assert "EstimatedSavings" not in rec or "316" not in str(rec.get("EstimatedSavings"))
    assert "316" not in rec["Recommendation"]
    # DPU footprint is carried for the adapter to price.
    assert rec["WorkerType"] == "G.1X"
    assert rec["NumberOfWorkers"] == 5


# --------------------------------------------------------------------------- #
# Integration — shim -> adapter end-to-end (no monkeypatched checks)
# --------------------------------------------------------------------------- #
def test_integration_shim_to_adapter_counts_dev_endpoint() -> None:
    glue_client = _FakeGlueClient(
        jobs=[{"Name": "big-job", "MaxCapacity": 20, "WorkerType": None, "NumberOfWorkers": 0}],
        dev_endpoints=[{"EndpointName": "ep1", "Status": "READY"}],  # default 5 DPU
        crawlers=[{"Name": "c1", "Schedule": {"ScheduleExpression": "cron(0 1 * * ? *)"}}],
    )
    ctx = _ctx(client=lambda name, **kw: glue_client)

    findings = GlueModule().scan(ctx)

    # Dev endpoint counted at the 5-DPU default; job rightsizing is $0 advisory.
    assert findings.total_monthly_savings == pytest.approx(1606.0)
    assert findings.total_recommendations == 1
    dev = findings.sources["dev_endpoints"].recommendations[0]
    assert dev["Counted"] is True
    assert dev["EstimatedSavings"] == "$1,606.00/month"
    job = findings.sources["job_rightsizing"].recommendations[0]
    assert job["Counted"] is False


# --------------------------------------------------------------------------- #
# Shim — per-API error isolation + classification (glue L1)
# --------------------------------------------------------------------------- #
class _ClientError(Exception):
    """Minimal botocore-style ClientError carrying an Error.Code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _RaisingGlueClient(_FakeGlueClient):
    """Glue client whose named operation raises, others behave normally."""

    def __init__(self, *, fail_op: str, error: Exception, **kw: Any) -> None:
        super().__init__(**kw)
        self._fail_op = fail_op
        self._error = error

    def get_paginator(self, name: str) -> _FakeGluePaginator:
        if name == self._fail_op:
            raise self._error
        return super().get_paginator(name)

    def get_dev_endpoints(self) -> dict[str, Any]:
        if self._fail_op == "get_dev_endpoints":
            raise self._error
        return super().get_dev_endpoints()


def test_get_jobs_access_denied_isolated_and_classified() -> None:
    # GetJobs AccessDenied must not abort the remaining APIs and must be routed
    # to ctx.permission_issue (IAM gap), never silently swallowed.
    glue_client = _RaisingGlueClient(
        fail_op="get_jobs",
        error=_ClientError("AccessDenied"),
        dev_endpoints=[{"EndpointName": "ep1", "Status": "READY", "WorkerType": "G.1X", "NumberOfWorkers": 5}],
        crawlers=[{"Name": "c1", "Schedule": {"ScheduleExpression": "cron(0 1 * * ? *)"}}],
    )
    ctx = _ctx(client=lambda name, **kw: glue_client)

    result = glue_shim.get_enhanced_glue_checks(ctx)

    # The denial landed on permission_issue with the GetJobs context, not warn.
    assert ctx.warnings == []
    assert len(ctx.permission_issues) == 1
    svc, msg, action = ctx.permission_issues[0]
    assert svc == "glue"
    assert "GetJobs" in msg
    assert action == "AccessDenied"
    # The dev-endpoint API still ran: its READY endpoint reached the result.
    dev = [r for r in result["recommendations"] if r["CheckCategory"] == _DEV_CATEGORY]
    assert len(dev) == 1


def test_get_dev_endpoints_failure_isolated_and_warned() -> None:
    # A non-permission failure on GetDevEndpoints routes to ctx.warn and leaves
    # the GetJobs results intact (per-block isolation in the other direction).
    glue_client = _RaisingGlueClient(
        fail_op="get_dev_endpoints",
        error=RuntimeError("boom"),
        jobs=[{"Name": "big-job", "MaxCapacity": 20, "WorkerType": None, "NumberOfWorkers": 0}],
    )
    ctx = _ctx(client=lambda name, **kw: glue_client)

    result = glue_shim.get_enhanced_glue_checks(ctx)

    assert ctx.permission_issues == []
    assert len(ctx.warnings) == 1
    svc, msg = ctx.warnings[0]
    assert svc == "glue"
    assert "GetDevEndpoints" in msg
    # GetJobs ran before the failing call and its rightsizing rec survived.
    jobs = [r for r in result["recommendations"] if r["CheckCategory"] == _JOB_CATEGORY]
    assert len(jobs) == 1

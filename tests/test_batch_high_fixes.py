"""Unit tests for the Batch adapter cost-audit HIGH fix (batch C1/H1).

batch C1/H1 — ``is_fargate`` must be derived from ``computeResources.type``
("FARGATE" / "FARGATE_SPOT"), NOT from ``computeResources.instanceTypes``.
A Fargate compute environment has an EMPTY ``instanceTypes`` list, so the old
``any(t == "FARGATE" for t in instanceTypes)`` test was always False and every
Fargate CE was misclassified into the EC2 branch: the Fargate-Spot lever never
fired and Fargate CEs were wrongly probed for EC2 Spot / Graviton.

These tests drive the pure logic (``get_enhanced_batch_checks``) and the
``scan()`` path (``BatchModule``) with a SimpleNamespace ctx + fake boto3
paginators, and prove the advisory-$0 dollar contract on the scan output.

There is no load-bearing rate here: the Batch adapter demotes every rec to a
$0 advisory (``Counted=False``, ``EstimatedMonthlySavings=0.0``), so no counted
dollar depends on a Pricing-API rate — only the classification correctness and
the advisory-$0 contract are asserted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.adapters.batch import BatchModule
from services.batch_svc import get_enhanced_batch_checks


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs: Any):  # noqa: ANN201 - boto3 shape
        return iter(self._pages)


class _FakeBatchClient:
    """Minimal boto3 Batch client driving the enhanced-checks shim."""

    def __init__(
        self,
        compute_envs: list[dict[str, Any]],
        job_defs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._ce_pages = [{"computeEnvironments": compute_envs}]
        self._jd_pages = [{"jobDefinitions": job_defs or []}]

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == "describe_compute_environments":
            return _FakePaginator(self._ce_pages)
        if name == "describe_job_definitions":
            return _FakePaginator(self._jd_pages)
        raise AssertionError(f"unexpected paginator {name!r}")


class _BoomClient:
    """Raises on describe_compute_environments to exercise the safe-fail path."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_paginator(self, _name: str) -> _FakePaginator:
        raise self._exc


def _ctx(client: Any) -> SimpleNamespace:
    ctx = SimpleNamespace(pricing_multiplier=1.0, warnings=[], permissions=[])
    ctx.warn = lambda msg, service=None, **k: ctx.warnings.append((service, msg))
    ctx.permission_issue = lambda msg, service=None, action=None, **k: ctx.permissions.append(
        (service, action, msg)
    )
    ctx.client = lambda _name: client
    return ctx


def _access_denied(op: str) -> Exception:
    exc = Exception(f"AccessDenied: not authorized to perform {op}")
    exc.response = {"Error": {"Code": "AccessDenied"}}  # type: ignore[attr-defined]
    return exc


def _ce(
    name: str,
    compute_type: str,
    *,
    instance_types: list[str] | None = None,
    allocation_strategy: str = "BEST_FIT",
    state: str = "ENABLED",
    ce_type: str = "MANAGED",
) -> dict[str, Any]:
    return {
        "computeEnvironmentName": name,
        "type": ce_type,
        "state": state,
        "computeResources": {
            "type": compute_type,
            "instanceTypes": instance_types or [],
            "allocationStrategy": allocation_strategy,
        },
    }


def _categories(recs: list[dict[str, Any]]) -> list[str]:
    return [r.get("CheckCategory") for r in recs]


# --------------------------------------------------------------------------- #
# batch C1/H1 — Fargate is read from computeResources.type
# --------------------------------------------------------------------------- #
def test_fargate_ce_fires_fargate_spot_lever_not_ec2() -> None:
    """A FARGATE CE (empty instanceTypes) yields the Fargate-Spot lever only."""
    result = get_enhanced_batch_checks(_ctx(_FakeBatchClient([_ce("fg", "FARGATE")])))
    cats = _categories(result["recommendations"])

    assert "Batch Fargate Spot Optimization" in cats
    # Misclassification guard: a Fargate CE must NEVER reach the EC2 branch.
    assert "Batch Spot Optimization" not in cats
    assert "Batch Graviton Migration" not in cats


def test_fargate_spot_ce_emits_no_recommendation() -> None:
    """A CE already on FARGATE_SPOT is optimal — no lever should fire."""
    result = get_enhanced_batch_checks(
        _ctx(_FakeBatchClient([_ce("fgspot", "FARGATE_SPOT")]))
    )
    assert result["recommendations"] == []


def test_ec2_ce_fires_spot_and_graviton_not_fargate() -> None:
    """An EC2-type CE keeps the EC2 Spot + Graviton levers, no Fargate lever."""
    result = get_enhanced_batch_checks(
        _ctx(_FakeBatchClient([_ce("ec2ce", "EC2", instance_types=["m5.large"])]))
    )
    cats = _categories(result["recommendations"])

    assert "Batch Spot Optimization" in cats
    assert "Batch Graviton Migration" in cats
    assert "Batch Fargate Spot Optimization" not in cats


def test_ec2_spot_optimized_ce_skips_spot_lever() -> None:
    """EC2 CE already on SPOT_CAPACITY_OPTIMIZED skips the Spot lever."""
    result = get_enhanced_batch_checks(
        _ctx(
            _FakeBatchClient(
                [
                    _ce(
                        "ec2opt",
                        "SPOT",
                        instance_types=["m6g.large"],
                        allocation_strategy="SPOT_CAPACITY_OPTIMIZED",
                    )
                ]
            )
        )
    )
    cats = _categories(result["recommendations"])

    assert "Batch Spot Optimization" not in cats
    # m6g.* is already Graviton -> no Graviton migration rec.
    assert "Batch Graviton Migration" not in cats
    assert "Batch Fargate Spot Optimization" not in cats


def test_disabled_ce_emits_nothing() -> None:
    """A non-ENABLED CE is skipped entirely."""
    result = get_enhanced_batch_checks(
        _ctx(_FakeBatchClient([_ce("off", "FARGATE", state="DISABLED")]))
    )
    assert result["recommendations"] == []


# --------------------------------------------------------------------------- #
# scan() path — advisory-$0 contract on the corrected Fargate classification
# --------------------------------------------------------------------------- #
def test_scan_fargate_rec_is_zero_advisory() -> None:
    """The corrected Fargate lever flows through scan() as a $0 advisory."""
    findings = BatchModule().scan(_ctx(_FakeBatchClient([_ce("fg", "FARGATE")])))

    assert findings.total_monthly_savings == 0.0
    recs = list(findings.sources["enhanced_checks"].recommendations)
    assert recs, "expected the Fargate-Spot lever to survive into scan output"
    assert any(r.get("CheckCategory") == "Batch Fargate Spot Optimization" for r in recs)

    for rec in recs:
        assert rec["Counted"] is False
        assert rec["EstimatedMonthlySavings"] == 0.0
        assert rec["EstimatedSavings"].startswith("$0.00/month")
        assert "AuditBasis" in rec

    # Count hygiene: advisory recs render (SourceBlock has them) but are excluded
    # from the rec-count headline.
    assert findings.sources["enhanced_checks"].count == len(recs)
    assert findings.total_recommendations == 0


# --------------------------------------------------------------------------- #
# Safe-fail — AccessDenied surfaces a permission gap, no fabricated dollar
# --------------------------------------------------------------------------- #
def test_describe_compute_environments_access_denied_classified() -> None:
    """An AccessDenied on DescribeComputeEnvironments is a permission gap."""
    ctx = _ctx(_BoomClient(_access_denied("batch:DescribeComputeEnvironments")))
    findings = BatchModule().scan(ctx)

    assert findings.total_monthly_savings == 0.0
    assert findings.total_recommendations == 0
    assert any(svc == "batch" for svc, _action, _msg in ctx.permissions)

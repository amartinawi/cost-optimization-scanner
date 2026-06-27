"""HIGH cost-correctness fixes for the containers (ECS/ECR) adapter.

Covers **containers H1**: ECS Compute Optimizer recs are deduped against Cost
Hub BEFORE summing/counting, enforcing authority CoH > CO > heuristic so one
Fargate service never contributes savings twice. Mirrors the SimpleNamespace
ctx + monkeypatched-source style of ``tests/test_containers_adapter.py`` /
``tests/test_lambda_audit_fixes.py``.

No new rate is introduced by this fix: the CoH and CO dollars are returned by
AWS (region-priced upstream); the adapter only drops the duplicate so it is not
counted twice. The pure dedup key (``normalize_resource_name``) is asserted to
converge a CoH ARN and a CO ``resource_id`` onto the same service name.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.containers as containers_adapter
from services.adapters.containers import ContainersModule
from services.containers_logic import normalize_resource_name


# --------------------------------------------------------------------------- #
# Test doubles (mirror tests/test_containers_adapter.py)
# --------------------------------------------------------------------------- #
def _pricing_engine() -> MagicMock:
    pe = MagicMock()
    pe.get_fargate_vcpu_hourly.side_effect = lambda architecture="x86", os="linux": 0.04048
    pe.get_fargate_gb_hourly.side_effect = lambda architecture="x86", os="linux": 0.004445
    pe.get_fargate_windows_os_hourly.return_value = 0.046
    return pe


def _ctx(**kw) -> SimpleNamespace:
    base = dict(
        cost_hub_splits={"containers": []},
        pricing_multiplier=1.0,
        fast_mode=False,
        pricing_engine=_pricing_engine(),
    )
    base.update(kw)
    ns = SimpleNamespace(**base)
    ns.warn = MagicMock()
    ns.permission_issue = MagicMock()
    return ns


def _patch_sources(monkeypatch, enhanced, co=None) -> None:
    monkeypatch.setattr(containers_adapter, "get_container_services_analysis", lambda ctx: {})
    monkeypatch.setattr(
        containers_adapter, "get_enhanced_container_checks", lambda ctx: {"recommendations": enhanced}
    )
    monkeypatch.setattr(
        containers_adapter, "get_ecs_compute_optimizer_recommendations", lambda ctx: co or []
    )


def _fargate_heuristic(service: str) -> dict:
    """A metric-backed Fargate rightsizing heuristic that quantifies to a real $."""
    return {
        "ServiceName": service,
        "ClusterName": "clu",
        "Cpu": 2048,        # 2 vCPU
        "Memory": 8192,     # 8 GB
        "TaskCount": 3,
        "LaunchType": "FARGATE",
        "Architecture": "x86",
        "OperatingSystem": "linux",
        "CheckCategory": "ECS Rightsizing - Metric-Backed",
    }


# --------------------------------------------------------------------------- #
# Pure dedup key — CoH ARN and CO resource_id converge
# --------------------------------------------------------------------------- #
def test_coh_arn_and_co_resource_id_converge_on_same_key():
    # The whole H1 fix relies on these normalizing to the same string.
    coh_key = normalize_resource_name("arn:aws:ecs:us-east-1:1:service/clu/web")
    co_key = normalize_resource_name("clu/web")
    assert coh_key == co_key == "web"


# --------------------------------------------------------------------------- #
# containers H1 — CO is deduped against CoH BEFORE summing/counting
# --------------------------------------------------------------------------- #
def test_co_rec_covered_by_coh_is_dropped_before_counting(monkeypatch):
    # Same ECS service surfaced by BOTH CoH (EcsService) and CO. Pre-fix this
    # double-counted ($100 + $42.50). Post-fix CoH wins and CO is dropped.
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    co = [{"resource_id": "clu/web", "resource_name": "web", "estimatedMonthlySavings": 42.5}]
    _patch_sources(monkeypatch, [], co=co)
    findings = ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))

    # CO duplicate dropped: not summed, not counted, not rendered.
    assert findings.sources["compute_optimizer"].count == 0
    assert findings.sources["compute_optimizer"].recommendations == ()
    assert findings.total_monthly_savings == pytest.approx(100.0)
    assert findings.total_recommendations == 1  # only the CoH rec


def test_co_rec_not_in_coh_survives_and_counts(monkeypatch):
    # A CO rec for a DIFFERENT service must NOT be dropped (no over-dedup).
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    co = [{"resource_id": "clu/api", "resource_name": "api", "estimatedMonthlySavings": 42.5}]
    _patch_sources(monkeypatch, [], co=co)
    findings = ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))

    assert findings.sources["compute_optimizer"].count == 1
    assert findings.total_monthly_savings == pytest.approx(142.5)
    assert findings.total_recommendations == 2


def test_co_only_savings_unaffected_when_no_coh(monkeypatch):
    # With no CoH, a CO rec is fully counted (regression guard for the dedup).
    co = [{"resource_id": "clu/api", "resource_name": "api", "estimatedMonthlySavings": 42.5}]
    _patch_sources(monkeypatch, [], co=co)
    findings = ContainersModule().scan(_ctx())
    assert findings.sources["compute_optimizer"].count == 1
    assert findings.total_monthly_savings == pytest.approx(42.5)


def test_authority_chain_coh_over_co_over_heuristic(monkeypatch):
    # All THREE sources reference the same service "web": CoH counts it once,
    # the CO duplicate and the heuristic duplicate are both dropped.
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    co = [{"resource_id": "clu/web", "resource_name": "web", "estimatedMonthlySavings": 42.5}]
    enhanced = [_fargate_heuristic("web")]
    _patch_sources(monkeypatch, enhanced, co=co)
    findings = ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))

    assert findings.sources["compute_optimizer"].count == 0   # CO dropped (covered by CoH)
    assert findings.sources["enhanced_checks"].count == 0      # heuristic dropped (covered)
    assert findings.total_monthly_savings == pytest.approx(100.0)
    assert findings.total_recommendations == 1                 # one Fargate service, one saving


def test_heuristic_dropped_when_covered_by_surviving_co(monkeypatch):
    # CoH empty; CO covers "api". The heuristic for "api" is dropped (CO > heuristic),
    # and the surviving CO dollar is the only counted saving.
    co = [{"resource_id": "clu/api", "resource_name": "api", "estimatedMonthlySavings": 50.0}]
    enhanced = [_fargate_heuristic("api")]
    _patch_sources(monkeypatch, enhanced, co=co)
    findings = ContainersModule().scan(_ctx())

    assert findings.sources["compute_optimizer"].count == 1
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_monthly_savings == pytest.approx(50.0)


def test_counted_equals_rendered_for_co_block(monkeypatch):
    # The number summed into the headline equals the dollar rendered on the CO
    # cards: sum over the rendered recommendations == CO's headline contribution.
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    co = [
        {"resource_id": "clu/web", "resource_name": "web", "estimatedMonthlySavings": 42.5},  # dup -> dropped
        {"resource_id": "clu/api", "resource_name": "api", "estimatedMonthlySavings": 30.0},  # kept
        {"resource_id": "clu/job", "resource_name": "job", "estimatedMonthlySavings": 12.0},  # kept
    ]
    _patch_sources(monkeypatch, [], co=co)
    findings = ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))

    rendered = findings.sources["compute_optimizer"].recommendations
    rendered_sum = sum(float(r["estimatedMonthlySavings"]) for r in rendered)
    assert len(rendered) == 2  # the "web" duplicate is gone
    assert rendered_sum == pytest.approx(42.0)
    # Headline == CoH (100) + rendered CO (42); the dropped 42.5 is never summed.
    assert findings.total_monthly_savings == pytest.approx(142.0)


def test_coh_input_list_not_mutated(monkeypatch):
    # Immutability: dedup builds new lists; the shared CoH bucket is untouched.
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    co = [{"resource_id": "clu/web", "resource_name": "web", "estimatedMonthlySavings": 42.5}]
    _patch_sources(monkeypatch, [], co=co)
    ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))
    assert coh == [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]

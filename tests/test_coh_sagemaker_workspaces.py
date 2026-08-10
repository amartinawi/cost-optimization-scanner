"""Rank-7 residue — the last two real CoH types that had a consuming adapter.

``SageMakerEndpoint`` and ``WorkSpaces`` are in AWS's 25-value ResourceType
enum, but the orchestrator had no route for either, so their AWS-computed
dollars fell into ``unbucketed_types`` and were dropped.

E2 says the wire-up is three layers — the bucket must be in ``_HUB_SERVICES``,
the type must be in ``type_map``, AND an adapter must read
``ctx.cost_hub_splits[<bucket>]`` — with the bucket name equal to the consuming
module's ``key``. All three are asserted here, because a break in any one is
silent.

``MemoryDBCluster`` and ``DocumentDBCluster`` stay unrouted on purpose: neither
has an adapter or a report tab, so routing them would land AWS's dollars in a
bucket nothing renders.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services.adapters.sagemaker import SageMakerModule
from services.adapters.workspaces import WorkspacesModule


def _coh(resource_id: str, savings: float, *, rtype: str, action: str = "Rightsize") -> dict[str, Any]:
    return {
        "recommendationId": f"rec-{resource_id}",
        "resourceId": resource_id,
        "resourceArn": f"arn:aws:fake:us-east-1:1:thing/{resource_id}",
        "currentResourceType": rtype,
        "actionType": action,
        "estimatedMonthlySavings": savings,
    }


# --------------------------------------------------------------------------- #
# E2 — the three-layer wire-up
# --------------------------------------------------------------------------- #
def _orchestrator_src() -> str:
    return Path("core/scan_orchestrator.py").read_text()


@pytest.mark.parametrize(
    ("coh_type", "bucket", "module"),
    [("SageMakerEndpoint", "sagemaker", SageMakerModule), ("WorkSpaces", "workspaces", WorkspacesModule)],
)
def test_type_is_wired_through_all_three_layers(coh_type: str, bucket: str, module: type) -> None:
    src = _orchestrator_src()
    hub_block = src.split("_HUB_SERVICES = {", 1)[1].split("}", 1)[0]
    type_block = src.split("type_map = {", 1)[1].split("}", 1)[0]

    assert f'"{bucket}"' in hub_block, "bucket missing from _HUB_SERVICES"
    assert f'"{coh_type}": "{bucket}"' in type_block, "type missing from type_map"
    # The bucket name MUST equal the consuming module's key, or the
    # `bucket in selected` gate never fires (the eks vs eks_cost bug).
    assert module.key == bucket


def test_memorydb_and_documentdb_stay_unrouted() -> None:
    """Routing a type into a bucket no adapter reads is dead data, not a fix."""
    type_block = _orchestrator_src().split("type_map = {", 1)[1].split("}", 1)[0]
    for coh_type in ("MemoryDBCluster", "DocumentDBCluster"):
        assert f'"{coh_type}"' not in type_block
    # ...and the reason is written down where the next reader will look.
    assert "MemoryDBCluster" in _orchestrator_src()


def test_no_route_points_at_a_bucket_that_is_never_created() -> None:
    src = _orchestrator_src()
    hub = set(re.findall(r'"([a-z_]+)"', src.split("_HUB_SERVICES = {", 1)[1].split("}", 1)[0]))
    routed = set(re.findall(r'"[A-Za-z0-9]+": "([a-z_]+)"', src.split("type_map = {", 1)[1].split("}", 1)[0]))
    assert routed - hub == set()


# --------------------------------------------------------------------------- #
# SageMaker
# --------------------------------------------------------------------------- #
class _FakeSageMaker:
    def __init__(self, endpoints: list[str]) -> None:
        self._endpoints = endpoints

    def get_paginator(self, name: str) -> Any:
        if name == "list_endpoints":
            return SimpleNamespace(
                paginate=lambda **kw: [
                    {"Endpoints": [{"EndpointName": n, "EndpointStatus": "InService"} for n in self._endpoints]}
                ]
            )
        return SimpleNamespace(paginate=lambda **kw: [{}])

    def describe_endpoint(self, EndpointName: str) -> dict[str, Any]:  # noqa: N803
        return {
            "EndpointName": EndpointName,
            "EndpointStatus": "InService",
            "ProductionVariants": [
                {"VariantName": "v1", "CurrentInstanceCount": 1, "CurrentWeight": 1.0}
            ],
            "EndpointConfigName": "cfg",
        }

    def describe_endpoint_config(self, EndpointConfigName: str) -> dict[str, Any]:  # noqa: N803
        return {
            "ProductionVariants": [
                {"VariantName": "v1", "InstanceType": "ml.m5.large", "InitialInstanceCount": 1}
            ]
        }

    def list_notebook_instances(self, **kw: Any) -> dict[str, Any]:
        return {"NotebookInstances": []}

    def list_training_jobs(self, **kw: Any) -> dict[str, Any]:
        return {"TrainingJobSummaries": []}


def _sm_ctx(coh: list[dict[str, Any]], endpoints: list[str]) -> SimpleNamespace:
    ctx = SimpleNamespace(
        pricing_engine=SimpleNamespace(get_sagemaker_instance_monthly=lambda t: 200.0),
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={"sagemaker": coh},
        commitment_coverage=None,
        warnings=[],
    )
    clients = {"sagemaker": _FakeSageMaker(endpoints), "cloudwatch": None}
    ctx.client = lambda name, region=None: clients.get(name)
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None
    return ctx


def test_sagemaker_coh_rec_is_rendered_and_counted() -> None:
    findings = SageMakerModule().scan(_sm_ctx([_coh("ep-1", 310.0, rtype="SageMakerEndpoint")], []))
    block = findings.sources["cost_optimization_hub"]
    assert block.count == 1
    rec = block.recommendations[0]
    assert rec["EstimatedMonthlySavings"] == pytest.approx(310.0)
    assert rec["endpoint_name"] == "ep-1"
    assert findings.total_monthly_savings == pytest.approx(310.0)


def test_sagemaker_purchase_recs_are_filtered_out() -> None:
    """RI/SP purchase recs belong to the commitment tab, not here."""
    coh = [_coh("ep-1", 310.0, rtype="SageMakerSavingsPlans", action="PurchaseSavingsPlans")]
    findings = SageMakerModule().scan(_sm_ctx(coh, []))
    assert findings.sources["cost_optimization_hub"].count == 0


def test_empty_sagemaker_bucket_changes_nothing() -> None:
    findings = SageMakerModule().scan(_sm_ctx([], []))
    assert findings.sources["cost_optimization_hub"].count == 0
    assert findings.total_monthly_savings == 0.0


# --------------------------------------------------------------------------- #
# WorkSpaces
# --------------------------------------------------------------------------- #
def _ws_ctx(coh: list[dict[str, Any]], local: list[dict[str, Any]], monkeypatch) -> SimpleNamespace:
    import services.adapters.workspaces as mod

    monkeypatch.setattr(
        mod, "get_enhanced_workspaces_checks", lambda _c: {"recommendations": local, "checks": {}}
    )
    ctx = SimpleNamespace(
        pricing_engine=None,
        pricing_multiplier=1.0,
        region="us-east-1",
        fast_mode=True,
        cost_hub_splits={"workspaces": coh},
        commitment_coverage=None,
        warnings=[],
    )
    ctx.client = lambda name, region=None: None
    ctx.warn = lambda *a, **k: None
    ctx.permission_issue = lambda *a, **k: None
    return ctx


def _ws_local(workspace_id: str) -> dict[str, Any]:
    return {
        "WorkspaceId": workspace_id,
        "CheckCategory": "Unused WorkSpaces",
        "ComputeTypeName": "STANDARD",
        "RunningMode": "ALWAYS_ON",
        "Recommendation": "Unused",
    }


def test_workspaces_coh_rec_is_rendered_and_counted(monkeypatch) -> None:
    ctx = _ws_ctx([_coh("ws-1", 44.0, rtype="WorkSpaces")], [], monkeypatch)
    findings = WorkspacesModule().scan(ctx)
    block = findings.sources["cost_optimization_hub"]
    assert block.count == 1
    assert block.recommendations[0]["WorkspaceId"] == "ws-1"
    assert findings.total_monthly_savings == pytest.approx(44.0)


def test_coh_covered_workspace_suppresses_the_local_lever(monkeypatch) -> None:
    """CoH > heuristic: the same WorkSpace must not be counted by both."""
    ctx = _ws_ctx([_coh("ws-1", 44.0, rtype="WorkSpaces")], [_ws_local("ws-1")], monkeypatch)
    findings = WorkspacesModule().scan(ctx)
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_monthly_savings == pytest.approx(44.0)


def test_an_uncovered_workspace_keeps_its_local_lever(monkeypatch) -> None:
    ctx = _ws_ctx([_coh("ws-1", 44.0, rtype="WorkSpaces")], [_ws_local("ws-2")], monkeypatch)
    findings = WorkspacesModule().scan(ctx)
    ids = {r["WorkspaceId"] for r in findings.sources["enhanced_checks"].recommendations}
    assert ids == {"ws-2"}


def test_coh_covered_endpoint_suppresses_the_local_idle_lever(monkeypatch) -> None:
    """CoH > heuristic. Without this the same endpoint is counted twice: once by
    AWS's computed dollar and once by this tab's full-cost idle lever."""
    import services.adapters.sagemaker as mod

    local = [
        {
            "endpoint_name": "ep-1",
            "CheckCategory": "Idle SageMaker Endpoint",
            "EstimatedMonthlySavings": 200.0,
            "EstimatedSavings": "$200.00/month",
        },
        {
            "endpoint_name": "ep-2",
            "CheckCategory": "Idle SageMaker Endpoint",
            "EstimatedMonthlySavings": 150.0,
            "EstimatedSavings": "$150.00/month",
        },
    ]
    monkeypatch.setattr(mod, "_check_idle_endpoints", lambda *a, **k: (list(local), 2))
    monkeypatch.setattr(mod, "_check_idle_notebooks", lambda *a, **k: ([], 0))
    monkeypatch.setattr(mod, "_check_spot_training", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_check_multi_model_consolidation", lambda *a, **k: [])

    findings = mod.SageMakerModule().scan(_sm_ctx([_coh("ep-1", 310.0, rtype="SageMakerEndpoint")], []))

    idle_names = {r["endpoint_name"] for r in findings.sources["idle_endpoints"].recommendations}
    assert idle_names == {"ep-2"}, "ep-1 is owned by Cost Optimization Hub"
    # 310 (CoH, ep-1) + 150 (local, ep-2) — ep-1's $200 local figure is gone.
    assert findings.total_monthly_savings == pytest.approx(460.0)


def test_sagemaker_savings_plan_demotes_the_coh_rec_too(monkeypatch) -> None:
    """A SageMaker SP is pre-paid spend that continues after the endpoint goes,
    so AWS's on-demand figure is no more realizable than the local one."""
    import services.adapters.sagemaker as mod
    from services.commitment_coverage import CommitmentCoverage

    monkeypatch.setattr(mod, "_check_idle_endpoints", lambda *a, **k: ([], 0))
    monkeypatch.setattr(mod, "_check_idle_notebooks", lambda *a, **k: ([], 0))
    monkeypatch.setattr(mod, "_check_spot_training", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_check_multi_model_consolidation", lambda *a, **k: [])

    ctx = _sm_ctx([_coh("ep-1", 310.0, rtype="SageMakerEndpoint")], [])
    ctx.commitment_coverage = CommitmentCoverage(has_sagemaker_sp=True)

    findings = mod.SageMakerModule().scan(ctx)
    rec = findings.sources["cost_optimization_hub"].recommendations[0]
    assert rec["Counted"] is False
    assert findings.total_monthly_savings == 0.0

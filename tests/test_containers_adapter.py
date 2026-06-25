"""Tests for the containers (ECS/ECR) audit-remediation fixes.

Covers the valid-Fargate-combo snapping, architecture/OS/Windows pricing legs,
cross-source dedup by normalized service name, the Compute-Optimizer opt-in
placeholder→warning conversion, EC2-launch-type exclusion, and the
counted==rendered ($0 → advisory) discipline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.adapters.containers as containers_adapter
from services.adapters.containers import ContainersModule, _heuristic_resource_key
from services.containers_logic import (
    compute_untagged_reclaimable_bytes,
    dedupe_by_authority,
    fargate_task_hourly,
    normalize_resource_name,
    parse_manifest_layers,
    snap_down_fargate,
    snap_to_valid_fargate,
)


# --------------------------------------------------------------------------- #
# Pure helpers — valid Fargate combinations
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "vcpu, mem, expected",
    [
        (0.3, 0.6, (0.5, 1.0)),     # snaps up to a legal combo
        (0.25, 0.5, (0.25, 0.5)),   # already legal
        (1.0, 5.0, (1.0, 5.0)),     # 1 vCPU allows 2..8 GB
        (2.0, 100.0, (16.0, 104.0)),  # 16 vCPU allows 32..120 in 8GB steps -> 104
    ],
)
def test_snap_to_valid_fargate(vcpu, mem, expected):
    assert snap_to_valid_fargate(vcpu, mem) == expected


def test_snap_down_fargate_one_tier():
    # 2 vCPU/8 GB drops to 1 vCPU at its minimum memory (2 GB).
    assert snap_down_fargate(2.0, 8.0) == (1.0, 2.0)


def test_snap_down_fargate_smallest_returns_none():
    # Already at the smallest Fargate size -> nothing to downsize.
    assert snap_down_fargate(0.25, 0.5) is None


def test_fargate_task_hourly_legs():
    # vCPU leg + GB leg, no Windows fee.
    cost = fargate_task_hourly(1.0, 2.0, 0.04048, 0.004445)
    assert cost == pytest.approx(0.04048 + 2 * 0.004445)


def test_fargate_task_hourly_windows_os_fee():
    # Windows adds a per-vCPU OS license fee on top of the compute legs.
    cost = fargate_task_hourly(2.0, 4.0, 0.046552, 0.0051117, windows_os_rate=0.046)
    assert cost == pytest.approx(2 * 0.046552 + 4 * 0.0051117 + 2 * 0.046)


def test_normalize_resource_name():
    assert normalize_resource_name("arn:aws:ecs:us-east-1:1:service/clu/web") == "web"
    assert normalize_resource_name("clu/web") == "web"
    assert normalize_resource_name("web") == "web"
    assert normalize_resource_name("") == ""


def test_dedupe_by_authority_drops_covered_heuristics():
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web"}]
    co = [{"resource_id": "clu/api"}]
    heur = [
        {"ServiceName": "web"},   # covered by CoH -> dropped
        {"ServiceName": "api"},   # covered by CO -> dropped
        {"ServiceName": "worker"},  # unique -> kept
    ]
    kept = dedupe_by_authority(
        coh, co, heur,
        coh_key=lambda r: r.get("resourceArn", ""),
        co_key=lambda r: r.get("resource_id", ""),
        heuristic_key=_heuristic_resource_key,
    )
    assert [r["ServiceName"] for r in kept] == ["worker"]


# --------------------------------------------------------------------------- #
# Adapter behaviour
# --------------------------------------------------------------------------- #
def _pricing_engine():
    pe = MagicMock()
    pe.get_fargate_vcpu_hourly.side_effect = lambda architecture="x86", os="linux": {
        ("x86", "linux"): 0.04048,
        ("arm", "linux"): 0.03238,
        ("x86", "windows"): 0.046552,
    }[("arm" if architecture in ("arm", "arm64") else "x86", "windows" if str(os).startswith("win") else "linux")]
    pe.get_fargate_gb_hourly.side_effect = lambda architecture="x86", os="linux": {
        ("x86", "linux"): 0.004445,
        ("arm", "linux"): 0.00356,
        ("x86", "windows"): 0.0051117,
    }[("arm" if architecture in ("arm", "arm64") else "x86", "windows" if str(os).startswith("win") else "linux")]
    pe.get_fargate_windows_os_hourly.return_value = 0.046
    return pe


def _ctx(**kw):
    base = dict(
        cost_hub_splits={"containers": []},
        pricing_multiplier=1.0,
        fast_mode=False,
        pricing_engine=_pricing_engine(),
    )
    base.update(kw)
    ns = SimpleNamespace(**base)
    ns.warn = MagicMock()
    return ns


def _patch_sources(monkeypatch, enhanced, co=None):
    monkeypatch.setattr(containers_adapter, "get_container_services_analysis", lambda ctx: {})
    monkeypatch.setattr(containers_adapter, "get_enhanced_container_checks", lambda ctx: {"recommendations": enhanced})
    monkeypatch.setattr(containers_adapter, "get_ecs_compute_optimizer_recommendations", lambda ctx: co or [])


def test_fargate_rightsizing_quantified_and_counted(monkeypatch):
    rec = {
        "ServiceName": "web",
        "ClusterName": "clu",
        "Cpu": 2048,            # 2 vCPU
        "Memory": 8192,         # 8 GB
        "TaskCount": 3,
        "LaunchType": "FARGATE",
        "Architecture": "x86",
        "OperatingSystem": "linux",
        "CheckCategory": "ECS Rightsizing - Metric-Backed",
    }
    _patch_sources(monkeypatch, [rec])
    findings = ContainersModule().scan(_ctx())

    out = findings.sources["enhanced_checks"].recommendations[0]
    # current 2vCPU/8GB -> target 1vCPU/2GB, x3 tasks, 730h.
    cur = 2 * 0.04048 + 8 * 0.004445
    tgt = 1 * 0.04048 + 2 * 0.004445
    expected = (cur - tgt) * 730 * 3
    assert out["EstimatedMonthlySavings"] == pytest.approx(round(expected, 2))
    assert out["Counted"] is True
    assert findings.total_monthly_savings == pytest.approx(round(expected, 2))
    assert "AuditBasis" in out


def test_ec2_launch_type_not_fargate_priced(monkeypatch):
    rec = {
        "ServiceName": "web", "ClusterName": "clu",
        "Cpu": 2048, "Memory": 8192, "TaskCount": 3,
        "LaunchType": "EC2",  # EC2 domain — must not be Fargate-priced here
        "CheckCategory": "ECS Rightsizing - Metric-Backed",
    }
    _patch_sources(monkeypatch, [rec])
    findings = ContainersModule().scan(_ctx())
    # EC2-launch-type tasks are EC2 domain → $0 here → dropped (not shown).
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_monthly_savings == 0.0


def test_arm_priced_below_x86(monkeypatch):
    def make(arch):
        return {
            "ServiceName": f"svc-{arch}", "ClusterName": "clu",
            "Cpu": 2048, "Memory": 8192, "TaskCount": 1,
            "LaunchType": "FARGATE", "Architecture": arch, "OperatingSystem": "linux",
            "CheckCategory": "ECS Rightsizing - Metric-Backed",
        }
    _patch_sources(monkeypatch, [make("x86")])
    x86_sav = ContainersModule().scan(_ctx()).total_monthly_savings
    _patch_sources(monkeypatch, [make("arm")])
    arm_sav = ContainersModule().scan(_ctx()).total_monthly_savings
    assert 0 < arm_sav < x86_sav  # ARM rates are lower


def test_zero_savings_finding_is_dropped(monkeypatch):
    # An ECR rec with no quantifiable reclaim (and no Advisory flag) is dropped,
    # not rendered as $0 noise.
    rec = {"RepositoryName": "myrepo", "ImageCount": 50, "CheckCategory": "ECR Lifecycle Management"}
    _patch_sources(monkeypatch, [rec])
    findings = ContainersModule().scan(_ctx())
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_recommendations == 0
    assert findings.total_monthly_savings == 0.0


def test_explicit_advisory_finding_is_kept(monkeypatch):
    # An oversized-repo advisory (Advisory=True) is kept as non-counted.
    rec = {
        "RepositoryName": "huge", "ImageCount": 5000,
        "CheckCategory": "ECR Lifecycle Management", "Advisory": True,
    }
    _patch_sources(monkeypatch, [rec])
    findings = ContainersModule().scan(_ctx())
    out = findings.sources["enhanced_checks"].recommendations[0]
    assert out["EstimatedMonthlySavings"] == 0.0
    assert out["Counted"] is False
    assert findings.total_monthly_savings == 0.0


def test_compute_optimizer_optin_placeholder_warned_not_counted(monkeypatch):
    placeholder = {"ResourceId": "compute-optimizer-service", "estimatedMonthlySavings": 0.0}
    _patch_sources(monkeypatch, [], co=[placeholder])
    ctx = _ctx()
    findings = ContainersModule().scan(ctx)
    assert findings.sources["compute_optimizer"].count == 0  # placeholder dropped
    assert findings.total_recommendations == 0
    ctx.warn.assert_called_once()


def test_compute_optimizer_real_savings_counted(monkeypatch):
    co_rec = {"resource_id": "clu/api", "estimatedMonthlySavings": 42.5}
    _patch_sources(monkeypatch, [], co=[co_rec])
    findings = ContainersModule().scan(_ctx())
    assert findings.sources["compute_optimizer"].count == 1
    assert findings.total_monthly_savings == pytest.approx(42.5)


def test_ecs_co_helper_filters_zero_savings(monkeypatch):
    # The advisor drops "Optimized"/no-action CO findings ($0 savings).
    import services.advisor as advisor

    raw = {
        "ecsServiceRecommendations": [
            {"serviceArn": "arn:aws:ecs:::service/clu/web",
             "serviceRecommendationOptions": [{"savingsOpportunity": {"estimatedMonthlySavings": {"value": 12.0}}}]},
            {"serviceArn": "arn:aws:ecs:::service/clu/optimized",
             "serviceRecommendationOptions": [{"savingsOpportunity": {"estimatedMonthlySavings": {"value": 0.0}}}]},
        ]
    }
    co_client = MagicMock()
    co_client.get_ecs_service_recommendations.return_value = raw
    ctx = SimpleNamespace(pricing_multiplier=1.0)
    ctx.client = lambda name, region=None: co_client if name == "compute-optimizer" else None
    out = advisor.get_ecs_compute_optimizer_recommendations(ctx)
    assert [r["resource_name"] for r in out] == ["web"]  # $0 'optimized' dropped


def test_cost_hub_dedups_heuristic_for_same_service(monkeypatch):
    coh = [{"resourceArn": "arn:aws:ecs:::service/clu/web", "estimatedMonthlySavings": 100.0}]
    heur = {
        "ServiceName": "web", "ClusterName": "clu",
        "Cpu": 2048, "Memory": 8192, "TaskCount": 3,
        "LaunchType": "FARGATE", "Architecture": "x86", "OperatingSystem": "linux",
        "CheckCategory": "ECS Rightsizing - Metric-Backed",
    }
    _patch_sources(monkeypatch, [heur])
    findings = ContainersModule().scan(_ctx(cost_hub_splits={"containers": coh}))
    # The heuristic for 'web' is dropped (covered by CoH); only CoH's $100 counts.
    assert findings.sources["enhanced_checks"].count == 0
    assert findings.total_monthly_savings == pytest.approx(100.0)


def test_fast_mode_sets_flag():
    assert ContainersModule().reads_fast_mode is True


# --------------------------------------------------------------------------- #
# CLI alias resolution
# --------------------------------------------------------------------------- #
def test_cli_aliases_resolve_ecs_ecr_eks():
    from core.filtering import resolve_cli_keys, unrecognized_tokens
    from services import ALL_MODULES

    assert resolve_cli_keys(ALL_MODULES, {"ecs"}, None) == {"containers"}
    assert resolve_cli_keys(ALL_MODULES, {"ecr"}, None) == {"containers"}
    assert resolve_cli_keys(ALL_MODULES, {"eks"}, None) == {"eks_cost"}
    # Unknown tokens are reported (not silently scanned as nothing).
    assert unrecognized_tokens(ALL_MODULES, {"ecs", "bogus"}) == {"bogus"}
    assert unrecognized_tokens(ALL_MODULES, {"ecs"}) == set()


# --------------------------------------------------------------------------- #
# ECR deduplicated layer storage
# --------------------------------------------------------------------------- #
def test_parse_manifest_layers_image():
    m = {"layers": [{"digest": "L1", "size": 100}, {"digest": "L2", "size": 50}],
         "config": {"digest": "C", "size": 5}}
    blobs, children = parse_manifest_layers(m)
    assert set(blobs) == {("L1", 100), ("L2", 50), ("C", 5)}
    assert children == []


def test_parse_manifest_layers_index():
    m = {"manifests": [{"digest": "child-a"}, {"digest": "child-b"}]}
    blobs, children = parse_manifest_layers(m)
    assert blobs == []
    assert children == ["child-a", "child-b"]


def test_reclaimable_excludes_shared_base_layers():
    # Tagged + untagged share base layer L1; only untagged-unique blobs count.
    manifests = {
        "t": {"layers": [{"digest": "L1", "size": 100}, {"digest": "L2", "size": 50}], "config": {"digest": "Ct", "size": 1}},
        "u": {"layers": [{"digest": "L1", "size": 100}, {"digest": "L3", "size": 30}], "config": {"digest": "Cu", "size": 2}},
    }
    imgs = [{"digest": "t", "tagged": True}, {"digest": "u", "tagged": False}]
    assert compute_untagged_reclaimable_bytes(imgs, lambda d: manifests.get(d)) == 32  # L3 + Cu


def test_reclaimable_zero_when_untagged_shares_everything():
    manifests = {
        "t": {"layers": [{"digest": "L1", "size": 100}], "config": {"digest": "C", "size": 1}},
        "u": {"layers": [{"digest": "L1", "size": 100}], "config": {"digest": "C", "size": 1}},
    }
    imgs = [{"digest": "t", "tagged": True}, {"digest": "u", "tagged": False}]
    assert compute_untagged_reclaimable_bytes(imgs, lambda d: manifests.get(d)) == 0


def test_reclaimable_tagged_index_protects_untagged_child():
    # A tagged manifest list points at an untagged child manifest; the child's
    # layers must NOT be counted reclaimable.
    manifests = {
        "idx": {"manifests": [{"digest": "child"}]},
        "child": {"layers": [{"digest": "LX", "size": 99}], "config": {"digest": "cc", "size": 1}},
    }
    imgs = [{"digest": "idx", "tagged": True}, {"digest": "child", "tagged": False}]
    assert compute_untagged_reclaimable_bytes(imgs, lambda d: manifests.get(d)) == 0


def test_ecr_repo_reclaimable_uses_batched_fetch():
    """End-to-end shim test: batched batch_get_image + layer dedup → ReclaimableBytes."""
    import json as _json
    from types import SimpleNamespace as _NS
    from services.containers import _ecr_repo_reclaimable

    manifests = {
        "sha-tag": {"layers": [{"digest": "L1", "size": 100}, {"digest": "L2", "size": 50}], "config": {"digest": "Ct", "size": 1}},
        "sha-unt": {"layers": [{"digest": "L1", "size": 100}, {"digest": "L3", "size": 30}], "config": {"digest": "Cu", "size": 2}},
    }
    calls = {"batch": 0}

    class _Pag:
        def paginate(self, repositoryName):
            return [{"imageDetails": [
                {"imageDigest": "sha-tag", "imageTags": ["v1"]},
                {"imageDigest": "sha-unt"},  # untagged
            ]}]

    class _Ecr:
        def get_paginator(self, op):
            assert op == "describe_images"
            return _Pag()

        def batch_get_image(self, repositoryName, imageIds, acceptedMediaTypes):
            calls["batch"] += 1
            return {"images": [
                {"imageId": {"imageDigest": iid["imageDigest"]},
                 "imageManifest": _json.dumps(manifests[iid["imageDigest"]])}
                for iid in imageIds if iid["imageDigest"] in manifests
            ]}

    ctx = _NS(fast_mode=False)
    ctx.warn = MagicMock()
    ctx.permission_issue = MagicMock()
    rec = _ecr_repo_reclaimable(ctx, _Ecr(), "myrepo")
    assert rec["ReclaimableBytes"] == 32  # L3 + Cu; L1 shared with tagged excluded
    assert rec["UntaggedCount"] == 1
    # All top-level manifests fetched in ONE batched call (not one per image).
    assert calls["batch"] == 1


def test_adapter_prices_ecr_reclaimable_bytes(monkeypatch):
    gib = 3.0
    rec = {
        "RepositoryName": "myrepo",
        "ReclaimableBytes": int(gib * 1024**3),
        "CheckCategory": "ECR Lifecycle Management",
    }
    _patch_sources(monkeypatch, [rec])
    ctx = _ctx()
    ctx.pricing_engine.get_ecr_storage_gb_month.return_value = 0.10
    findings = ContainersModule().scan(ctx)
    out = findings.sources["enhanced_checks"].recommendations[0]
    assert out["EstimatedMonthlySavings"] == pytest.approx(round(gib * 0.10, 2))
    assert out["Counted"] is True
    assert "AuditBasis" in out

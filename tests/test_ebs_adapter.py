"""Unit tests for the EBS adapter — pricing, cross-source dedup, $0-placeholder
handling, Cost Hub wiring, snapshot routing, and usage-based IOPS rightsizing.

All decision logic is exercised without AWS: pure functions in
``services.ebs_logic`` are tested directly, and the adapter is driven with
monkeypatched source helpers + a ``SimpleNamespace`` context.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.adapters.ebs as ebs_adapter
from services.adapters.ebs import EbsModule, _coh_is_renderable, _gp2_to_gp3_savings_per_gb
from services.ebs_logic import (
    dedupe_by_authority,
    is_actionable_co_finding,
    normalize_volume_id,
    partition_enhanced_recs,
    recommend_iops_from_usage,
)


class _FakeEngine:
    """Minimal PricingEngine stand-in returning fixed gp2/gp3 rates."""

    def __init__(self, gp2: float = 0.10, gp3: float = 0.08) -> None:
        self._gp2 = gp2
        self._gp3 = gp3

    def get_ebs_monthly_price_per_gb(self, volume_type: str) -> float:
        return {"gp2": self._gp2, "gp3": self._gp3}.get(volume_type, 0.0)

    def get_ebs_iops_monthly_price(self, volume_type: str) -> float:
        return 0.005


def _ctx(**overrides):
    """Build a SimpleNamespace ScanContext with collected warnings/permissions."""
    warns: list = []
    perms: list = []
    base = dict(
        region="us-east-1",
        pricing_multiplier=1.0,
        pricing_engine=_FakeEngine(),
        fast_mode=False,
        old_snapshot_days=90,
        cost_hub_splits={"ebs": []},
        warn=lambda message, service="": warns.append((service, message)),
        permission_issue=lambda message, service="", action=None: perms.append((service, action, message)),
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns._warns = warns  # type: ignore[attr-defined]
    ns._perms = perms  # type: ignore[attr-defined]
    return ns


# --------------------------------------------------------------------------- #
# gp2 → gp3 delta (region-correct, no double multiplier)
# --------------------------------------------------------------------------- #
class TestGp2ToGp3Delta:
    def test_live_path_returns_difference(self):
        ctx = _ctx(pricing_engine=_FakeEngine(gp2=0.10, gp3=0.08))
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx(0.02)

    def test_live_path_ignores_pricing_multiplier(self):
        ctx = _ctx(pricing_engine=_FakeEngine(gp2=0.121, gp3=0.0968), pricing_multiplier=1.10)
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx(0.121 - 0.0968)

    def test_live_path_never_negative(self):
        ctx = _ctx(pricing_engine=_FakeEngine(gp2=0.05, gp3=0.10))
        assert _gp2_to_gp3_savings_per_gb(ctx) == 0.0

    def test_fallback_path_applies_multiplier(self):
        ctx = _ctx(pricing_engine=None, pricing_multiplier=1.10)
        # Fallback = (gp2 0.10 − gp3 0.08) × multiplier.
        assert _gp2_to_gp3_savings_per_gb(ctx) == pytest.approx((0.10 - 0.08) * 1.10)


# --------------------------------------------------------------------------- #
# Pure decision logic
# --------------------------------------------------------------------------- #
class TestEbsLogic:
    @pytest.mark.parametrize(
        "finding,expected",
        [
            ("NotOptimized", True),
            ("Optimized", False),
            ("UnderProvisioned", False),
            ("under_provisioned", False),
            ("", True),
        ],
    )
    def test_is_actionable_co_finding(self, finding, expected):
        assert is_actionable_co_finding(finding) is expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("vol-123", "vol-123"),
            ("arn:aws:ec2:us-east-1:1:volume/vol-abc", "vol-abc"),
            ("something/vol-xyz", "vol-xyz"),
            ("", ""),
        ],
    )
    def test_normalize_volume_id(self, raw, expected):
        assert normalize_volume_id(raw) == expected

    def test_partition_enhanced_recs(self):
        recs = [
            {"CheckCategory": "Volume Type Optimization", "VolumeId": "vol-gp2"},
            {"CheckCategory": "Old Snapshots", "SnapshotId": "snap-1"},
            {"CheckCategory": "Orphaned Snapshots", "SnapshotId": "snap-2"},
            {"CheckCategory": "Over Provisioned Iops", "VolumeId": "vol-op"},
            {"CheckCategory": "Unattached Volumes", "VolumeId": "vol-un"},
        ]
        gp2, snaps, other = partition_enhanced_recs(recs)
        assert [r["VolumeId"] for r in gp2] == ["vol-gp2"]
        assert {r["SnapshotId"] for r in snaps} == {"snap-1", "snap-2"}
        assert [r["VolumeId"] for r in other] == ["vol-op"]  # unattached excluded

    def test_dedupe_by_authority(self):
        coh = [{"resourceId": "vol-x"}]
        co = [
            {"volumeArn": "arn:.../vol-x", "finding": "NotOptimized"},  # dropped (CoH wins)
            {"volumeArn": "arn:.../vol-y", "finding": "NotOptimized"},  # kept
        ]
        heuristic = [{"VolumeId": "vol-x"}, {"VolumeId": "vol-y"}, {"VolumeId": "vol-z"}]
        co_kept, (heur_kept,) = dedupe_by_authority(coh, co, [heuristic])
        assert [normalize_volume_id(r["volumeArn"]) for r in co_kept] == ["vol-y"]
        # vol-x covered by CoH, vol-y covered by CO → only vol-z survives.
        assert [r["VolumeId"] for r in heur_kept] == ["vol-z"]

    @pytest.mark.parametrize(
        "prov,peak,baseline,expected",
        [
            (10000, 1000, 3000, 3000),   # peak*1.3=1300 < baseline 3000 → recommend baseline
            (10000, 5000, 3000, 6500),   # ceil(5000*1.3)=6500
            (4000, 4000, 100, None),     # peak*1.3 >= provisioned → not over-provisioned
            (0, 100, 0, None),           # no provisioned IOPS
        ],
    )
    def test_recommend_iops_from_usage(self, prov, peak, baseline, expected):
        assert recommend_iops_from_usage(prov, peak, baseline=baseline) == expected


# --------------------------------------------------------------------------- #
# _coh_is_renderable mirrors the EBS Cost-Hub render filter
# --------------------------------------------------------------------------- #
class TestCohRenderable:
    def test_keeps_actionable_ebs_volume(self):
        rec = {"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}}, "finding": "NotOptimized"}
        assert _coh_is_renderable(rec) is True

    def test_drops_optimized(self):
        rec = {"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}}, "finding": "Optimized"}
        assert _coh_is_renderable(rec) is False

    def test_drops_non_ebs(self):
        rec = {"actionType": "Rightsize", "currentResourceDetails": {"ec2Instance": {}}}
        assert _coh_is_renderable(rec) is False

    def test_drops_no_action(self):
        assert _coh_is_renderable({"currentResourceDetails": {"ebsVolume": {}}}) is False


# --------------------------------------------------------------------------- #
# Adapter integration with monkeypatched sources
# --------------------------------------------------------------------------- #
def _co_rec(vol_id: str, savings: float, finding: str = "NotOptimized") -> dict:
    return {
        "volumeArn": f"arn:aws:ec2:us-east-1:1:volume/{vol_id}",
        "finding": finding,
        "volumeRecommendationOptions": [
            {"rank": 1, "savingsOpportunity": {"estimatedMonthlySavings": {"value": savings}}}
        ],
    }


def _patch_sources(monkeypatch, *, enhanced=None, co=None, unattached=None):
    monkeypatch.setattr(
        ebs_adapter, "compute_ebs_checks", lambda *a, **k: {"recommendations": enhanced or []}
    )
    monkeypatch.setattr(ebs_adapter, "get_ebs_compute_optimizer_recs", lambda *a, **k: co or [])
    monkeypatch.setattr(ebs_adapter, "get_unattached_volumes", lambda *a, **k: unattached or [])
    monkeypatch.setattr(ebs_adapter, "get_ebs_volume_count", lambda ctx: {})


class TestEbsAdapter:
    def test_multi_source_savings_and_counts(self, monkeypatch):
        enhanced = [
            {"VolumeId": "vol-gp2", "Size": 50, "CheckCategory": "Volume Type Optimization"},
            {"VolumeId": "vol-op", "CheckCategory": "Over Provisioned Iops", "EstimatedSavings": "$7.00/month"},
            {"SnapshotId": "snap-1", "CheckCategory": "Old Snapshots", "EstimatedSavings": "$5.00/month (max estimate)"},
        ]
        co = [_co_rec("vol-co", 50.0)]
        unattached = [{"VolumeId": "vol-un", "Size": 100, "VolumeType": "gp2", "EstimatedMonthlyCost": 10.0}]
        coh = [{"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}},
                "finding": "NotOptimized", "resourceId": "vol-coh", "estimatedMonthlySavings": 100.0}]
        _patch_sources(monkeypatch, enhanced=enhanced, co=co, unattached=unattached)
        ctx = _ctx(cost_hub_splits={"ebs": coh})

        findings = EbsModule().scan(ctx)

        # CoH 100 + CO 50 + unattached 10 + gp2(50×0.02=1) + over-provisioned 7 = 168
        assert findings.total_monthly_savings == pytest.approx(168.0)
        # 5 counted opportunities; snapshot NOT counted.
        assert findings.total_recommendations == 5
        assert findings.sources["cost_optimization_hub"].count == 1
        assert findings.sources["ebs_snapshots"].count == 1
        # gp2 per-volume savings string is written by the adapter.
        assert findings.sources["gp2_migration"].recommendations[0]["EstimatedSavings"] == "$1.00/month"
        assert "AuditBasis" in findings.sources["gp2_migration"].recommendations[0]

    def test_optin_placeholder_warns_not_counted(self, monkeypatch):
        placeholder = {
            "ResourceId": "compute-optimizer-service",
            "Recommendation": "Enable AWS Compute Optimizer for EBS rightsizing recommendations",
            "estimatedMonthlySavings": 0.0,
        }
        _patch_sources(monkeypatch, co=[placeholder])
        ctx = _ctx()

        findings = EbsModule().scan(ctx)

        assert findings.sources["compute_optimizer"].count == 0
        assert findings.total_recommendations == 0
        assert findings.total_monthly_savings == 0.0
        assert any(svc == "ebs" and "Compute Optimizer is not enabled" in msg for svc, msg in ctx._warns)

    def test_cost_hub_recs_consumed(self, monkeypatch):
        """Regression for the silently-dropped cost_hub_splits['ebs'] bucket."""
        _patch_sources(monkeypatch)
        coh = [{"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}},
                "finding": "NotOptimized", "resourceId": "vol-coh", "estimatedMonthlySavings": 42.0}]
        ctx = _ctx(cost_hub_splits={"ebs": coh})

        findings = EbsModule().scan(ctx)

        assert findings.sources["cost_optimization_hub"].count == 1
        assert findings.total_monthly_savings == pytest.approx(42.0)
        assert findings.total_recommendations == 1

    def test_dedup_same_volume_across_sources(self, monkeypatch):
        """One volume surfaced by CoH + CO + heuristic is counted once (CoH wins)."""
        enhanced = [{"VolumeId": "vol-x", "CheckCategory": "Over Provisioned Iops", "EstimatedSavings": "$9.00/month"}]
        co = [_co_rec("vol-x", 50.0)]
        unattached = [{"VolumeId": "vol-x", "Size": 100, "VolumeType": "gp2", "EstimatedMonthlyCost": 10.0}]
        coh = [{"actionType": "Rightsize", "currentResourceDetails": {"ebsVolume": {}},
                "finding": "NotOptimized", "resourceId": "vol-x", "estimatedMonthlySavings": 100.0}]
        _patch_sources(monkeypatch, enhanced=enhanced, co=co, unattached=unattached)
        ctx = _ctx(cost_hub_splits={"ebs": coh})

        findings = EbsModule().scan(ctx)

        assert findings.total_monthly_savings == pytest.approx(100.0)
        assert findings.total_recommendations == 1
        assert findings.sources["compute_optimizer"].count == 0
        assert findings.sources["unattached_volumes"].count == 0
        assert findings.sources["enhanced_checks"].count == 0


# --------------------------------------------------------------------------- #
# Advisor: actionable filtering + failure recording
# --------------------------------------------------------------------------- #
class _FakeCOClient:
    def __init__(self, recs=None, exc=None):
        self._recs = recs or []
        self._exc = exc

    def get_ebs_volume_recommendations(self, **kw):
        if self._exc:
            raise self._exc
        return {"volumeRecommendations": self._recs}


def _advisor_ctx(co_client):
    warns: list = []
    perms: list = []
    ns = SimpleNamespace(
        client=lambda name, region=None: co_client,
        warn=lambda message, service="": warns.append((service, message)),
        permission_issue=lambda message, service="", action=None: perms.append((service, action, message)),
    )
    ns._warns = warns
    ns._perms = perms
    return ns


class TestAdvisorEbs:
    def test_drops_optimized_and_under_provisioned(self):
        from services.advisor import get_ebs_compute_optimizer_recommendations

        client = _FakeCOClient(
            recs=[
                {"volumeArn": "a", "finding": "Optimized"},
                {"volumeArn": "b", "finding": "NotOptimized"},
                {"volumeArn": "c", "finding": "UnderProvisioned"},
            ]
        )
        recs = get_ebs_compute_optimizer_recommendations(_advisor_ctx(client))
        assert [r["volumeArn"] for r in recs] == ["b"]

    def test_optin_returns_placeholder(self):
        from services.advisor import get_ebs_compute_optimizer_recommendations

        client = _FakeCOClient(exc=Exception("OptInRequiredException: not registered"))
        recs = get_ebs_compute_optimizer_recommendations(_advisor_ctx(client))
        assert len(recs) == 1 and recs[0]["ResourceId"] == "compute-optimizer-service"

    def test_access_denied_recorded_as_permission_issue(self):
        from services.advisor import get_ebs_compute_optimizer_recommendations

        ctx = _advisor_ctx(_FakeCOClient(exc=Exception("AccessDeniedException: nope")))
        recs = get_ebs_compute_optimizer_recommendations(ctx)
        assert recs == []
        assert any(svc == "ebs" for svc, _action, _msg in ctx._perms)

    def test_other_error_recorded_as_warning(self):
        from services.advisor import get_ebs_compute_optimizer_recommendations

        ctx = _advisor_ctx(_FakeCOClient(exc=Exception("ThrottlingException: slow down")))
        recs = get_ebs_compute_optimizer_recommendations(ctx)
        assert recs == []
        assert any(svc == "ebs" for svc, _msg in ctx._warns)


# --------------------------------------------------------------------------- #
# Over-provisioned IOPS — evidence-based, fast-mode gated
# --------------------------------------------------------------------------- #
class _FakeVolPaginator:
    def __init__(self, volumes):
        self._volumes = volumes

    def paginate(self, **kw):
        return [{"Volumes": self._volumes}]


class _FakeEc2:
    def __init__(self, volumes):
        self._volumes = volumes

    def get_paginator(self, name):
        return _FakeVolPaginator(self._volumes)


class _FakeCloudWatch:
    def __init__(self, peak_ops):
        self._peak_ops = peak_ops  # target peak IOPS

    def get_metric_statistics(self, **kw):
        from services.ebs import _IOPS_METRIC_PERIOD_SECONDS

        if self._peak_ops is None:
            return {"Datapoints": []}
        # Sum over the period that yields the target peak IOPS on the read metric.
        if kw["MetricName"] == "VolumeReadOps":
            return {"Datapoints": [{"Sum": self._peak_ops * _IOPS_METRIC_PERIOD_SECONDS}]}
        return {"Datapoints": [{"Sum": 0}]}


def _iops_ctx(cw, fast_mode=False):
    from services.ebs import _scan_over_provisioned_iops  # noqa: F401

    warns: list = []
    ns = SimpleNamespace(
        fast_mode=fast_mode,
        region="us-east-1",
        pricing_engine=None,  # use fallback rates
        client=lambda name, region=None: cw,
        warn=lambda message, service="": warns.append((service, message)),
    )
    ns._warns = warns
    return ns


class TestOverProvisionedIops:
    def test_fast_mode_skips_and_warns(self):
        from services.ebs import _scan_over_provisioned_iops

        checks = {"over_provisioned_iops": []}
        ctx = _iops_ctx(_FakeCloudWatch(0), fast_mode=True)
        _scan_over_provisioned_iops(ctx, _FakeEc2([]), 1.0, checks)
        assert checks["over_provisioned_iops"] == []
        assert any("fast" in msg.lower() for _svc, msg in ctx._warns)

    def test_no_cloudwatch_data_skips_with_warning(self):
        from services.ebs import _scan_over_provisioned_iops

        vol = {"VolumeId": "vol-1", "VolumeType": "gp3", "Iops": 10000, "Size": 100}
        checks = {"over_provisioned_iops": []}
        ctx = _iops_ctx(_FakeCloudWatch(None))  # no datapoints
        _scan_over_provisioned_iops(ctx, _FakeEc2([vol]), 1.0, checks)
        assert checks["over_provisioned_iops"] == []
        assert any("no CloudWatch" in msg for _svc, msg in ctx._warns)

    def test_evidence_based_recommendation_emitted(self):
        from services.ebs import _scan_over_provisioned_iops

        # gp3 with 10000 provisioned IOPS but only ~1000 peak observed.
        vol = {"VolumeId": "vol-1", "VolumeType": "gp3", "Iops": 10000, "Size": 100}
        checks = {"over_provisioned_iops": []}
        ctx = _iops_ctx(_FakeCloudWatch(1000))  # peak 1000 IOPS
        _scan_over_provisioned_iops(ctx, _FakeEc2([vol]), 1.0, checks)
        assert len(checks["over_provisioned_iops"]) == 1
        rec = checks["over_provisioned_iops"][0]
        # recommended = max(3000, ceil(1000*1.3)) = 3000; billable drops 7000 IOPS.
        assert rec["RecommendedIOPS"] == 3000
        assert rec["CurrentIOPS"] == 10000
        assert "AuditBasis" in rec
        # 7000 IOPS × $0.005 fallback = $35.00
        assert rec["EstimatedSavings"] == "$35.00/month"

    def test_well_sized_volume_not_flagged(self):
        from services.ebs import _scan_over_provisioned_iops

        vol = {"VolumeId": "vol-1", "VolumeType": "gp3", "Iops": 4000, "Size": 100}
        checks = {"over_provisioned_iops": []}
        ctx = _iops_ctx(_FakeCloudWatch(3500))  # peak 3500 → 3500*1.3 > 4000
        _scan_over_provisioned_iops(ctx, _FakeEc2([vol]), 1.0, checks)
        assert checks["over_provisioned_iops"] == []


# --------------------------------------------------------------------------- #
# gp2→gp3 net savings (IOPS parity) + io2 tiers + throughput pricing
# --------------------------------------------------------------------------- #
from services.ebs_logic import gp2_baseline_iops, gp2_to_gp3_net_savings  # noqa: E402


class TestGp2NetSavings:
    @pytest.mark.parametrize("size,expected", [(50, 150), (2000, 6000), (6000, 16000), (10, 100)])
    def test_gp2_baseline_iops(self, size, expected):
        assert gp2_baseline_iops(size) == expected

    def test_small_volume_full_delta(self):
        # 50 GB: baseline 150 < 3000 → no IOPS cost → full storage delta.
        assert gp2_to_gp3_net_savings(50, 0.02, 0.005) == pytest.approx(1.00)

    def test_large_volume_nets_iops_cost(self):
        # 2000 GB: baseline 6000 → provision 3000 IOPS × $0.005 = $15 off $40 storage.
        assert gp2_to_gp3_net_savings(2000, 0.02, 0.005) == pytest.approx(25.0)

    def test_never_negative(self):
        assert gp2_to_gp3_net_savings(2000, 0.001, 0.005) == 0.0


class TestIo2TieredPricing:
    def _engine(self):
        from unittest.mock import MagicMock
        from core.pricing_engine import PricingEngine

        client = MagicMock()
        client.get_products.side_effect = Exception("no api")  # force fallback tiers
        return PricingEngine("us-east-1", client, fallback_multiplier=1.0)

    def test_tiered_cost_100k_iops(self):
        # 32000×0.065 + 32000×0.0455 + 36000×0.032 = 2080 + 1456 + 1152
        assert self._engine().get_ebs_io2_iops_cost(100_000) == pytest.approx(4688.0)

    def test_base_tier_only(self):
        assert self._engine().get_ebs_io2_iops_cost(10_000) == pytest.approx(650.0)  # 10000×0.065

    def test_throughput_fallback_rate(self):
        assert self._engine().get_ebs_throughput_monthly_price("gp3") == pytest.approx(0.04)


# --------------------------------------------------------------------------- #
# Cross-tab snapshot dedup (EBS cedes AMI-backed snapshots to the AMI tab)
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone  # noqa: E402


class _SnapPaginator:
    def __init__(self, data):
        self._data = data

    def paginate(self, **kw):
        return [self._data]


class _FakeEc2Snapshots:
    def __init__(self, images, snapshots):
        self._images = images
        self._snapshots = snapshots

    def get_paginator(self, op):
        return _SnapPaginator(
            {
                "describe_images": {"Images": self._images},
                "describe_snapshots": {"Snapshots": self._snapshots},
                "describe_volumes": {"Volumes": []},
                "describe_instances": {"Reservations": []},
            }[op]
        )


def _old_snap(sid, desc=""):
    return {"SnapshotId": sid, "VolumeSize": 100, "StartTime": datetime(2020, 1, 1, tzinfo=timezone.utc), "Description": desc}


class TestSnapshotCrossTabDedup:
    def test_ami_backed_skipped_orphan_kept_standalone_old(self):
        from services.ebs import compute_ebs_checks

        images = [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami-existing"}}]}]
        snapshots = [
            _old_snap("snap-ami-existing", "Created by CreateImage(ami-1) for ami-1"),  # AMI tab owns → skip
            _old_snap("snap-ami-orphan", "Created by CreateImage(ami-gone) for ami-gone"),  # deregistered → orphaned
            _old_snap("snap-standalone", "nightly backup"),  # standalone → old
        ]
        ec2 = _FakeEc2Snapshots(images, snapshots)
        ctx = SimpleNamespace(
            region="us-east-1", pricing_engine=None, pricing_multiplier=1.0, fast_mode=True,
            old_snapshot_days=90,
            client=lambda name, region=None: ec2 if name == "ec2" else None,
            warn=lambda message, service="": None,
        )

        result = compute_ebs_checks(ctx, 1.0, 90)
        old_ids = {r["SnapshotId"] for r in result["old_snapshots"]}
        orphan_ids = {r["SnapshotId"] for r in result["orphaned_snapshots"]}

        assert "snap-ami-existing" not in old_ids and "snap-ami-existing" not in orphan_ids  # ceded to AMI tab
        assert orphan_ids == {"snap-ami-orphan"}
        assert old_ids == {"snap-standalone"}


class TestSnapshotAdvisoryAndSizing:
    """EBS snapshot recs are $0 Counted=False advisories sized on actual bytes."""

    def _ctx(self, ec2):
        return SimpleNamespace(
            region="us-east-1", pricing_engine=None, pricing_multiplier=1.0, fast_mode=True,
            old_snapshot_days=90,
            client=lambda name, region=None: ec2 if name == "ec2" else None,
            warn=lambda message, service="": None,
        )

    def test_snapshot_is_zero_dollar_advisory_with_potential(self):
        from services.ebs import compute_ebs_checks

        # No FullSnapshotSizeInBytes → VolumeSize=100 upper bound × $0.05 = $5.00.
        ec2 = _FakeEc2Snapshots([], [_old_snap("snap-standalone", "nightly backup")])
        result = compute_ebs_checks(self._ctx(ec2), 1.0, 90)
        rec = result["old_snapshots"][0]
        # Advisory: never summed into the headline.
        assert rec["Counted"] is False
        assert rec["EstimatedMonthlySavings"] == 0.0
        # Recoverable potential disclosed, string leads with $0.00 advisory.
        assert rec["PotentialMonthlySavings"] == 5.00
        assert rec["EstimatedSavings"].startswith("$0.00")
        assert "5.00" in rec["EstimatedSavings"]

    def test_full_snapshot_size_preferred_over_volume_size(self):
        from services.ebs import compute_ebs_checks

        # Actual stored bytes = 20 GB; VolumeSize provisioned = 100 GB. The
        # potential must use the ACTUAL bytes (20 × $0.05 = $1.00), not the
        # 100 GB upper bound ($5.00) — the AMI FullSnapshotSizeInBytes fix ported.
        snap = _old_snap("snap-incremental", "nightly backup")
        snap["FullSnapshotSizeInBytes"] = 20 * 1024**3
        ec2 = _FakeEc2Snapshots([], [snap])
        result = compute_ebs_checks(self._ctx(ec2), 1.0, 90)
        rec = result["old_snapshots"][0]
        assert rec["PotentialMonthlySavings"] == 1.00  # 20 GB, not the 100 GB upper bound
        assert rec["Counted"] is False

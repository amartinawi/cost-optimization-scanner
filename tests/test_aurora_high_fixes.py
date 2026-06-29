"""HIGH cost-correctness fixes for the Aurora adapter (and the paired RDS leg).

Findings proven here (rates validated live against the AWS Pricing API,
us-east-1, 2026-06-19):

- aurora H1 — the I/O-Optimized STORAGE premium is taken from
  ``PricingEngine.get_aurora_io_storage_premium_per_gb()`` ($0.225 − $0.10 =
  $0.125/GB-Mo), not the old ~5x-low 0.025 constant.
- aurora H2 — the I/O-tier saving nets the per-member I/O-Optimized INSTANCE
  premium ($0.338 vs $0.260/hr db.r6g.large → $56.94/mo); for a cluster with
  provisioned members that premium can flip a "saving" into a net loss.
- aurora H3 + rds H1 — a provisioned Aurora member already counted by the RDS
  tab (Cost Optimization Hub or Compute Optimizer) is suppressed on the Aurora
  tab, so the same instance is never double-counted. One shared ``covered`` set.

The tests drive the pure ``_check_io_tier`` logic and the ``scan()`` path with a
SimpleNamespace ctx + fake boto3 paginators + a fake PricingEngine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.adapters.aurora import (
    AuroraModule,
    _check_io_tier,
    _check_provisioned_instances,
)
from services.adapters.rds import RdsModule

# Live-validated rates (us-east-1, AWS Pricing API 2026-06-19).
IO_OPT_STORAGE_PREMIUM_PER_GB = 0.125  # 0.225 − 0.10
IO_OPT_R6G_LARGE_INSTANCE_PREMIUM = 56.94  # (0.338 − 0.260) * 730
IO_PER_MILLION = 0.20  # Aurora:StorageIOUsage $0.0000002/IO


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _IoCW:
    """CloudWatch fake: Sum datapoints for the IOPs metrics, an Average
    datapoint for VolumeBytesUsed (the billed-storage gauge read by AUR-01).

    ``vol_bytes=None`` models the metric being unavailable (no datapoints), which
    the adapter treats as "storage unmeasurable -> skip the lever" (fail safe).
    """

    def __init__(self, read_sum: float, write_sum: float, vol_bytes: float | None = None) -> None:
        self._sums = {"VolumeReadIOPs": read_sum, "VolumeWriteIOPs": write_sum}
        self._vol_bytes = vol_bytes

    def get_metric_statistics(self, **kw):
        metric = kw["MetricName"]
        if metric == "VolumeBytesUsed":
            if self._vol_bytes is None:
                return {"Datapoints": []}
            return {"Datapoints": [{"Average": self._vol_bytes}]}
        return {"Datapoints": [{"Sum": self._sums.get(metric, 0.0)}]}


class _EmptyCW:
    def get_metric_statistics(self, **kw):
        return {"Datapoints": []}


class _IoPE:
    """PricingEngine fake for the I/O-tier legs (live-validated rates)."""

    def get_aurora_io_storage_premium_per_gb(self) -> float:
        return IO_OPT_STORAGE_PREMIUM_PER_GB

    def get_aurora_io_rate_per_million(self) -> float:
        return IO_PER_MILLION

    def get_aurora_io_instance_premium_monthly(
        self, engine, instance_class, *, multi_az=False, license_model=None
    ) -> float:
        return {"db.r6g.large": IO_OPT_R6G_LARGE_INSTANCE_PREMIUM}.get(instance_class, 0.0)


class _ScanPE:
    """PricingEngine fake for the scan()/Graviton dedup path."""

    PRICES = {"db.r5.8xlarge": 3737.60, "db.r6g.8xlarge": 3344.86}

    def get_aurora_acu_hourly(self) -> float:
        return 0.12

    def get_rds_instance_monthly_price(self, engine, instance_class, *a, **k) -> float:
        return self.PRICES.get(instance_class, 0.0)

    def get_aurora_io_storage_premium_per_gb(self) -> float:
        return IO_OPT_STORAGE_PREMIUM_PER_GB

    def get_aurora_io_rate_per_million(self) -> float:
        return IO_PER_MILLION

    def get_aurora_io_instance_premium_monthly(self, engine, instance_class, *a, **k) -> float:
        return 0.0


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return list(self._pages)


class _FakeRds:
    def __init__(self, clusters, instances):
        self._clusters = clusters
        self._instances = instances

    def get_paginator(self, op):
        if op == "describe_db_clusters":
            return _Paginator([{"DBClusters": self._clusters}])
        if op == "describe_db_instances":
            return _Paginator([{"DBInstances": self._instances}])
        raise AssertionError(f"unexpected paginator {op}")


def _aurora_inst(identifier: str, cls: str = "db.r5.8xlarge", cluster: str = "c1") -> dict:
    return {
        "DBInstanceIdentifier": identifier,
        "DBInstanceClass": cls,
        "Engine": "aurora-mysql",
        "DBInstanceStatus": "available",
        "DBClusterIdentifier": cluster,
    }


def _scan_ctx(rds, cw, *, cost_hub_splits=None, rds_covered=None):
    ns = SimpleNamespace(
        region="us-east-1",
        account_id="1",
        pricing_multiplier=1.0,
        fast_mode=True,  # Graviton-only path; no CW needed for the dedup proof.
        pricing_engine=_ScanPE(),
        cost_hub_splits=cost_hub_splits or {},
    )
    ns.client = lambda name, region=None: {"rds": rds, "cloudwatch": cw}.get(name)
    ns.warn = lambda *a, **k: None
    ns.permission_issue = lambda *a, **k: None
    if rds_covered is not None:
        ns.rds_covered_instance_ids = rds_covered
    return ns


def _instance_recs(findings):
    return list(findings.sources["instance_optimization"].recommendations)


def _io_ctx():
    """Minimal ctx for the direct ``_check_io_tier`` calls (only needs ``warn``)."""
    return SimpleNamespace(warn=lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# aurora H1 + H2 — _check_io_tier counted dollar
# --------------------------------------------------------------------------- #
def test_io_tier_counts_live_storage_premium_and_instance_premium():
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1,  # AUR-01: placeholder, must be ignored.
    }
    members = [_aurora_inst("m1", cls="db.r6g.large")]
    # 14d read sum = 14e9, write = 0 -> monthly_io = 14e9/14*30 = 30e9.
    # Billed storage = 1,000 GB from VolumeBytesUsed (1e12 bytes), NOT AllocatedStorage.
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=1_000_000_000_000)

    recs = _check_io_tier(_io_ctx(), cluster, cw, _IoPE(), members, 1.0, fast_mode=False)
    assert len(recs) == 1
    rec = recs[0]

    monthly_io = 30_000_000_000
    standard_io_cost = (monthly_io / 1_000_000) * IO_PER_MILLION  # $6,000
    storage_premium = 1000 * IO_OPT_STORAGE_PREMIUM_PER_GB  # $125 (H1)
    instance_premium = IO_OPT_R6G_LARGE_INSTANCE_PREMIUM  # $56.94 (H2)
    expected = round(standard_io_cost - storage_premium - instance_premium, 2)

    assert rec["monthly_savings"] == expected == 5818.06
    # H1: storage premium is the live $0.125/GB-Mo ($125 on 1,000 GB), NOT the
    # old 0.025 constant ($25).
    assert storage_premium == 125.0 and storage_premium != 25.0
    assert rec["AuditBasis"]["rate"]["io_opt_storage_premium_per_gb_mo"] == 0.125
    assert rec["AuditBasis"]["inputs"]["storage_premium"] == 125.0
    # H2: the per-member instance premium is included and one member was repriced.
    assert rec["AuditBasis"]["rate"]["io_opt_instance_premium_monthly"] == 56.94
    assert rec["AuditBasis"]["rate"]["members_repriced"] == 1
    # counted == rendered: the EstimatedSavings string matches monthly_savings.
    assert rec["EstimatedSavings"] == f"${expected:.2f}/mo"
    assert float(rec["EstimatedSavings"].lstrip("$").rstrip("/mo")) == rec["monthly_savings"]


def test_io_tier_instance_premium_flips_marginal_saving_to_net_loss():
    """H2: the same cluster counts a saving with no members, but the provisioned
    member's I/O-Optimized instance premium correctly suppresses it (net loss)."""
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1,  # AUR-01 placeholder, ignored.
    }
    # monthly_io = 70e6/14*30 = 150e6 -> standard_io_cost = $30.
    # storage_premium = 100*0.125 = $12.5 (100 GB from VolumeBytesUsed = 1e11 bytes).
    cw = _IoCW(read_sum=70_000_000, write_sum=0, vol_bytes=100_000_000_000)

    # No members -> only the H1 storage premium applies: 30 - 12.5 = 17.5 (>$10).
    no_member = _check_io_tier(_io_ctx(), cluster, cw, _IoPE(), [], 1.0, fast_mode=False)
    assert len(no_member) == 1
    assert no_member[0]["monthly_savings"] == 17.5

    # One provisioned member -> add the $56.94 instance premium: 30 - 12.5 -
    # 56.94 = -39.44 -> net loss -> no rec emitted (H2 prevents a false saving).
    with_member = _check_io_tier(
        _io_ctx(), cluster, cw, _IoPE(), [_aurora_inst("m1", cls="db.r6g.large")], 1.0, fast_mode=False
    )
    assert with_member == []


def test_io_tier_offline_uses_validated_fallback_not_old_constant():
    """pe=None falls back to the corrected $0.125/GB-Mo premium, not 0.025."""
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1,  # AUR-01 placeholder, ignored.
    }
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=1_000_000_000_000)
    recs = _check_io_tier(_io_ctx(), cluster, cw, None, [], 1.0, fast_mode=False)
    assert len(recs) == 1
    # 6000 - 1000*0.125 = 5875 (would be 5975 under the old 0.025 constant).
    # pe=None -> io rate falls back to IO_COST_PER_MILLION * multiplier = 0.20.
    assert recs[0]["monthly_savings"] == 5875.0
    assert recs[0]["AuditBasis"]["rate"]["io_opt_storage_premium_per_gb_mo"] == 0.125


# --------------------------------------------------------------------------- #
# AUR-01/02/03 — storage from VolumeBytesUsed, iopt1 guard, live regional I/O rate
# --------------------------------------------------------------------------- #
def test_io_tier_uses_volume_bytes_used_not_allocated_storage():
    """AUR-01: storage_gb comes from CloudWatch VolumeBytesUsed, never from the
    AllocatedStorage=1 placeholder AWS returns for Aurora auto-managed storage.

    The old bug priced the I/O-Optimized storage premium on 1 GB ($0.15),
    under-subtracting it and overstating the saving. With 150 GB measured the
    premium is 150 * 0.125 = $18.75, not $0.125."""
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1,  # the misleading placeholder
    }
    # monthly_io = 14e9/14*30 = 30e9 -> standard_io_cost = $6,000.
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=150_000_000_000)  # 150 GB
    recs = _check_io_tier(_io_ctx(), cluster, cw, _IoPE(), [], 1.0, fast_mode=False)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["AuditBasis"]["inputs"]["storage_gb"] == 150.0
    assert rec["AuditBasis"]["inputs"]["storage_premium"] == round(150 * 0.125, 2) == 18.75
    # saving = 6000 - 18.75 = 5981.25 (the old 1 GB bug gave 5999.85, +$18.60 too high).
    assert rec["monthly_savings"] == 5981.25


def test_io_tier_skips_when_storage_unmeasurable():
    """AUR-01 fail-safe: no VolumeBytesUsed datapoint -> no rec (never credit a
    saving against an unbounded I/O-Optimized storage premium)."""
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1000,  # present, but must NOT be used as a fallback
    }
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=None)
    assert _check_io_tier(_io_ctx(), cluster, cw, _IoPE(), [], 1.0, fast_mode=False) == []


def test_io_tier_skips_already_io_optimized_cluster():
    """AUR-02: a cluster already on StorageType=aurora-iopt1 has no
    Standard->I/O-Optimized transition to recommend."""
    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "StorageType": "aurora-iopt1",
    }
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=1_000_000_000_000)
    assert _check_io_tier(_io_ctx(), cluster, cw, _IoPE(), [], 1.0, fast_mode=False) == []


def test_io_tier_uses_live_regional_io_rate_not_scaled_constant():
    """AUR-03: standard I/O priced at the live regional rate from PricingEngine
    ($0.22/M Frankfurt), not the us-east-1 $0.20 constant * pricing_multiplier
    (which over-scaled to $0.224 at multiplier 1.12)."""

    class _FrankfurtPE(_IoPE):
        def get_aurora_io_rate_per_million(self) -> float:
            return 0.22  # live EUC1-Aurora:StorageIOUsage

    cluster = {
        "DBClusterIdentifier": "c1",
        "Engine": "aurora-mysql",
        "EngineVersion": "8.0",
        "AllocatedStorage": 1,
    }
    # monthly_io = 30e9 -> standard_io_cost = 30000 * 0.22 = $6,600 (not 30000*0.224=$6,720).
    cw = _IoCW(read_sum=14_000_000_000, write_sum=0, vol_bytes=1_000_000_000_000)
    recs = _check_io_tier(_io_ctx(), cluster, cw, _FrankfurtPE(), [], 1.12, fast_mode=False)
    assert len(recs) == 1
    assert recs[0]["AuditBasis"]["rate"]["io_per_million_usd"] == 0.22
    assert recs[0]["AuditBasis"]["inputs"]["standard_io_cost"] == 6600.0


# --------------------------------------------------------------------------- #
# aurora H3 + rds H1 — cross-adapter dedup (one shared covered set)
# --------------------------------------------------------------------------- #
def test_scan_baseline_both_members_produce_recs_when_uncovered():
    rds = _FakeRds(
        clusters=[{"DBClusterIdentifier": "c1", "Engine": "aurora-mysql"}],
        instances=[_aurora_inst("covered-inst"), _aurora_inst("free-inst")],
    )
    findings = AuroraModule().scan(_scan_ctx(rds, _EmptyCW()))
    ids = {r["DBInstanceIdentifier"] for r in _instance_recs(findings)}
    assert ids == {"covered-inst", "free-inst"}  # nothing suppressed


def test_scan_suppresses_member_covered_by_rds_cost_hub():
    """aurora H3: an Aurora member surfaced by RDS Cost Optimization Hub is not
    re-counted on the Aurora tab (read directly from ctx.cost_hub_splits)."""
    rds = _FakeRds(
        clusters=[{"DBClusterIdentifier": "c1", "Engine": "aurora-mysql"}],
        instances=[_aurora_inst("covered-inst"), _aurora_inst("free-inst")],
    )
    coh = {"rds": [{"resourceArn": "arn:aws:rds:us-east-1:1:db:covered-inst"}]}
    ctx = _scan_ctx(rds, _EmptyCW(), cost_hub_splits=coh)
    findings = AuroraModule().scan(ctx)

    recs = _instance_recs(findings)
    ids = {r["DBInstanceIdentifier"] for r in recs}
    assert ids == {"free-inst"}  # covered-inst suppressed (RDS owns it)
    assert "free-inst" in {r["DBInstanceIdentifier"] for r in recs}
    assert ctx.aurora_member_suppressed_ids == {"covered-inst"}


def test_scan_suppresses_member_covered_by_rds_compute_optimizer_set():
    """rds H1 leg: the normalized-id set RdsModule publishes
    (ctx.rds_covered_instance_ids, CoH + Compute Optimizer) suppresses the same
    member here even when cost_hub_splits is empty."""
    rds = _FakeRds(
        clusters=[{"DBClusterIdentifier": "c1", "Engine": "aurora-mysql"}],
        instances=[_aurora_inst("co-inst"), _aurora_inst("free-inst")],
    )
    ctx = _scan_ctx(rds, _EmptyCW(), rds_covered={"co-inst"})
    findings = AuroraModule().scan(ctx)
    ids = {r["DBInstanceIdentifier"] for r in _instance_recs(findings)}
    assert ids == {"free-inst"}
    assert ctx.aurora_member_suppressed_ids == {"co-inst"}


def test_rds_publishes_covered_instance_ids(monkeypatch):
    """rds H1: RdsModule.scan publishes the normalized ids it counts (CoH +
    Compute Optimizer) onto ctx so Aurora (runs later) can dedup against them."""
    import services.adapters.rds as rds_adapter

    monkeypatch.setattr(rds_adapter, "get_rds_compute_optimizer_recommendations", lambda ctx: [])
    monkeypatch.setattr(rds_adapter, "get_enhanced_rds_checks", lambda *a, **k: {"recommendations": []})
    monkeypatch.setattr(rds_adapter, "get_rds_instance_count", lambda ctx: {})

    coh = [{"resourceArn": "arn:aws:rds:us-east-1:1:db:aurora-prod-1", "estimatedMonthlySavings": 50.0}]
    co = [{"resourceArn": "arn:aws:rds:us-east-1:1:db:co-inst"}]
    monkeypatch.setattr(rds_adapter, "resolve_rds_findings", lambda *a, **k: (coh, co, [], 100.0, 2))

    ctx = SimpleNamespace(
        region="us-east-1",
        account_id="1",
        pricing_multiplier=1.0,
        old_snapshot_days=90,
        fast_mode=True,
        cost_hub_splits={},
        pricing_engine=None,
        client=lambda name, region=None: None,
        warn=lambda *a, **k: None,
        permission_issue=lambda *a, **k: None,
    )
    RdsModule().scan(ctx)
    assert ctx.rds_covered_instance_ids == {"aurora-prod-1", "co-inst"}

"""Aurora Serverless v2 cost optimization adapter.

Analyzes Aurora DB clusters for:
    - Serverless v2 ACU waste (max vs actual utilization)
    - I/O tier analysis (Standard vs I/O-Optimized)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._aws_errors import record_aws_error
from services._base import BaseServiceModule
from services.rds_logic import normalize_rds_arn

logger = logging.getLogger(__name__)

# Aurora Standard per-request I/O charge that I/O-Optimized eliminates.
# Validated live us-east-1 (Pricing API 2026-06-19): Aurora:StorageIOUsage
# $0.0000002/IO = $0.20 per 1M I/O requests.
IO_COST_PER_MILLION: float = 0.20
# Offline fallback for the Aurora I/O-Optimized STORAGE premium over Standard
# ($/GB-Mo). Validated live us-east-1: IO-OptimizedStorageUsage $0.225 −
# StorageUsage $0.10 = $0.125/GB-Mo (the old hardcoded 0.025 was ~5x low —
# aurora H1). Used ONLY when no PricingEngine is available; otherwise the live
# region-priced premium comes from
# PricingEngine.get_aurora_io_storage_premium_per_gb().
IO_OPTIMIZED_STORAGE_PREMIUM_FALLBACK_PER_GB: float = 0.125
# DescribeDBClusters' engine filter rejects the deprecated generic "aurora"
# value ("Unrecognized engine name: aurora"), which made the whole paginated
# call fail and silently zeroed every Aurora cluster finding (live-audit H1).
# Only the concrete engine names are valid filter values.
AURORA_ENGINES: tuple[str, ...] = ("aurora-mysql", "aurora-postgresql")
HOURS_PER_MONTH: int = 730


def _describe_aurora_clusters(rds: Any, ctx: Any) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    try:
        paginator = rds.get_paginator("describe_db_clusters")
        for page in paginator.paginate(Filters=[{"Name": "engine", "Values": list(AURORA_ENGINES)}]):
            for cluster in page.get("DBClusters", []):
                clusters.append(cluster)
    except Exception:
        try:
            resp = rds.describe_db_clusters(Filters=[{"Name": "engine", "Values": list(AURORA_ENGINES)}])
            clusters = resp.get("DBClusters", [])
        except Exception as e:
            # H4 — classify before swallowing: an AccessDenied/throttle here must
            # surface (else the whole Aurora tab empties silently with no signal).
            record_aws_error(ctx, e, service="aurora", context="rds:DescribeDBClusters failed")
    return clusters


def _describe_aurora_instances(rds: Any, ctx: Any) -> list[dict[str, Any]]:
    """Describe DB instances once (paginated), classifying errors before swallowing.

    Shared by the I/O-tier instance-premium leg (aurora H2) and the
    provisioned-instance rightsizing/Graviton checks so describe_db_instances is
    called once per scan. An AccessDenied/throttle surfaces via record_aws_error
    rather than silently emptying the instance checks.
    """
    instances: list[dict[str, Any]] = []
    try:
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            instances.extend(page.get("DBInstances", []))
    except Exception as e:
        record_aws_error(ctx, e, service="aurora", context="rds:DescribeDBInstances failed")
    return instances


def _record_suppressed_member(ctx: Any, instance_id: str) -> None:
    """Record an Aurora member whose heuristic rec was suppressed (RDS owns it).

    Cross-adapter audit trail for aurora H3 / rds H1: the id is added to
    ``ctx.aurora_member_suppressed_ids`` so a reviewer can see which members the
    Aurora tab deferred to the RDS tab (Cost Optimization Hub / Compute
    Optimizer) instead of double-counting.
    """
    existing = getattr(ctx, "aurora_member_suppressed_ids", None)
    if isinstance(existing, set):
        ctx.aurora_member_suppressed_ids = existing | {instance_id}
    else:
        ctx.aurora_member_suppressed_ids = {instance_id}


_CW_PERIOD_1D: int = 86400  # AWS CloudWatch max Period for ≤15-day queries.


def _get_cloudwatch_avg(
    cw: Any, namespace: str, metric: str, dimensions: list[dict[str, str]], days: int = 14
) -> float | None:
    """Average of a CW metric over the lookback window.

    Uses Period=86400 (1 day) — the CloudWatch maximum — and averages
    the per-day datapoints. Larger Period values are silently rejected
    by the GetMetricStatistics API.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=now,
            Period=_CW_PERIOD_1D,
            Statistics=["Average"],
        )
        dps = resp.get("Datapoints", [])
        if dps:
            return sum(d["Average"] for d in dps) / len(dps)
    except Exception:
        pass
    return None


def _get_cloudwatch_sum(
    cw: Any, namespace: str, metric: str, dimensions: list[dict[str, str]], days: int = 14
) -> float | None:
    """Sum of a CW metric over the lookback window.

    Uses Period=86400 (1 day) — the CloudWatch maximum — and sums the
    per-day datapoints to produce a total over the requested window.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=now,
            Period=_CW_PERIOD_1D,
            Statistics=["Sum"],
        )
        dps = resp.get("Datapoints", [])
        if dps:
            return sum(d["Sum"] for d in dps)
    except Exception:
        pass
    return None


def _get_cloudwatch_avg_max(cw: Any, metric: str, instance_id: str, days: int = 14) -> tuple[float, float] | None:
    """Return (avg, peak) of a per-instance RDS metric, or None when unavailable."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName=metric,
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
            StartTime=start,
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        dps = resp.get("Datapoints", [])
        if dps:
            avg = sum(d["Average"] for d in dps) / len(dps)
            peak = max(d["Maximum"] for d in dps)
            return avg, peak
    except Exception:
        pass
    return None


def _check_provisioned_instances(
    ctx: Any,
    rds: Any,
    cw: Any,
    fast_mode: bool,
    *,
    covered: set[str] | None = None,
    instances: list[dict[str, Any]] | None = None,
    cluster_storage_type: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Rightsizing (peak-aware) + Graviton recs for provisioned Aurora instances.

    Aurora Serverless v2 and the cluster control plane are handled elsewhere;
    this covers the provisioned member instances the cluster checks miss. Both
    levers price the live current→target delta and compound without overlap:
    rightsizing moves to a smaller same-arch class, Graviton then prices the
    x86→ARM delta on that (possibly rightsized) class. Graviton on Aurora is a
    managed-engine class change (no app rebuild), so it is low-risk.

    ``covered`` (aurora H3 / rds H1) is the set of normalized RDS instance ids
    the RDS tab already counts (Cost Optimization Hub / Compute Optimizer); any
    member in it is skipped here so the same instance is never counted on two
    tabs (single owner = RDS, authority CoH > CO > heuristic). ``instances`` may
    be pre-fetched by the caller to avoid a duplicate describe_db_instances call;
    when ``None`` the instances are described internally.

    ``cluster_storage_type`` maps each cluster id to whether its StorageType is
    ``aurora-iopt1`` (I/O-Optimized). It is threaded into every member price
    lookup so I/O-Optimized members price at the ~30%-higher I/O-Optimized
    instance SKU rather than the Standard SKU (aurora L3).
    """
    from services.aurora_logic import (
        graviton_equivalent,
        is_graviton_family,
        parse_instance_class,
        rightsize_target_size,
    )

    covered = covered or set()
    cluster_storage_type = cluster_storage_type or {}
    recs: list[dict[str, Any]] = []
    if instances is None:
        try:
            instances = []
            paginator = rds.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                instances.extend(page.get("DBInstances", []))
        except Exception as e:
            ctx.warn(f"[aurora] describe_db_instances failed: {e}", "aurora")
            return recs

    if fast_mode:
        # Graviton needs no metrics, but rightsizing does; warn once and still
        # surface Graviton below using the non-metric path.
        ctx.warn("fast mode: skipping Aurora instance rightsizing metric reads", "aurora")

    pe = ctx.pricing_engine
    for inst in instances:
        engine = (inst.get("Engine") or "").lower()
        if not engine.startswith("aurora"):
            continue
        if inst.get("DBInstanceStatus") != "available":
            continue
        instance_id = inst.get("DBInstanceIdentifier", "")
        # H3 / rds H1: a member already counted by the RDS tab (CoH or Compute
        # Optimizer) must not be re-counted by this heuristic. RDS (CoH > CO)
        # outranks the Aurora heuristic, so suppress the duplicate entirely.
        if instance_id and normalize_rds_arn(instance_id) in covered:
            _record_suppressed_member(ctx, instance_id)
            continue
        cls = inst.get("DBInstanceClass", "")
        parsed = parse_instance_class(cls)
        if not parsed:
            continue
        family, size, vcpu = parsed

        # aurora L3: I/O-Optimized clusters (StorageType=aurora-iopt1) price their
        # member instances at the ~30%-higher I/O-Optimized SKU; Standard members
        # at the cheaper Standard SKU. Resolve per-cluster so every price lookup
        # below uses the matching SKU.
        aurora_io_opt = cluster_storage_type.get(inst.get("DBClusterIdentifier", ""), False)

        try:
            cur_price = pe.get_rds_instance_monthly_price(engine, cls, aurora_io_optimized=aurora_io_opt)
        except Exception:
            cur_price = 0.0
        if not cur_price or cur_price <= 0:
            continue

        # --- Rightsizing (peak-aware, metric-gated) ---------------------------
        rightsized_size = size
        rightsized_class = cls
        if not fast_mode:
            metrics = _get_cloudwatch_avg_max(cw, "CPUUtilization", instance_id)
            if metrics is None:
                ctx.warn(
                    f"[aurora] no CPUUtilization data for {instance_id}; skipping rightsizing",
                    "aurora",
                )
            else:
                avg_cpu, peak_cpu = metrics
                target_size = rightsize_target_size(vcpu, peak_cpu)
                if target_size and avg_cpu < 50:
                    cand_class = f"{family}.{target_size}"
                    try:
                        cand_price = pe.get_rds_instance_monthly_price(
                            engine, cand_class, aurora_io_optimized=aurora_io_opt
                        )
                    except Exception:
                        cand_price = 0.0
                    if cand_price and 0 < cand_price < cur_price:
                        rightsized_size, rightsized_class = target_size, cand_class
                        recs.append(
                            {
                                "cluster_id": instance_id,
                                "DBInstanceIdentifier": instance_id,
                                "resource_id": instance_id,
                                "engine": engine,
                                "check_type": "instance_rightsizing",
                                "CheckCategory": "Aurora Instance Rightsizing",
                                "CurrentSize": cls,
                                "TargetSize": cand_class,
                                "current_value": f"{cls} (avg CPU {avg_cpu:.0f}%, peak {peak_cpu:.0f}% / 14d)",
                                "recommended_value": f"Downsize to {cand_class}",
                                "monthly_savings": round(cur_price - cand_price, 2),
                                # F4 — carry the numeric counted dollar + Counted flag
                                # (mirrors every other counted adapter) so a multi-rec
                                # Aurora group's card sums correctly instead of falling
                                # back to only the first rec's free-text figure.
                                "EstimatedMonthlySavings": round(cur_price - cand_price, 2),
                                "Counted": True,
                                "Recommendation": f"Downsize {cls} → {cand_class} (peak-aware)",
                                "EstimatedSavings": f"${cur_price - cand_price:.2f}/mo",
                                "reason": (
                                    f"{instance_id} averages {avg_cpu:.0f}% CPU (peak {peak_cpu:.0f}%) over 14d; "
                                    f"{cand_class} covers the peak with headroom — saves ${cur_price - cand_price:.2f}/mo"
                                ),
                            }
                        )

        # --- Graviton migration (on the possibly-rightsized class) ------------
        if not is_graviton_family(family):
            grav_family = graviton_equivalent(family)
            if grav_family:
                grav_class = f"{grav_family}.{rightsized_size}"
                try:
                    base_price = pe.get_rds_instance_monthly_price(
                        engine, rightsized_class, aurora_io_optimized=aurora_io_opt
                    )
                    grav_price = pe.get_rds_instance_monthly_price(
                        engine, grav_class, aurora_io_optimized=aurora_io_opt
                    )
                except Exception:
                    base_price = grav_price = 0.0
                if grav_price and 0 < grav_price < base_price:
                    # The Graviton $ delta is priced on the (possibly rightsized)
                    # class so it never overlaps the separate rightsizing rec. But
                    # the CARD must show the ACTUAL deployed class — labeling the
                    # hypothetical post-rightsize size as "current" misrepresents the
                    # instance (aurora graviton CurrentSize fix). Disclose the basis.
                    was_rightsized = rightsized_class != cls
                    basis_note = (
                        f" (delta priced on the rightsized {rightsized_class}; "
                        "rightsizing is recommended first)"
                        if was_rightsized
                        else ""
                    )
                    recs.append(
                        {
                            "cluster_id": instance_id,
                            "DBInstanceIdentifier": instance_id,
                            "resource_id": instance_id,
                            "engine": engine,
                            "check_type": "instance_graviton",
                            "CheckCategory": "Aurora Graviton Migration",
                            "CurrentSize": cls,
                            "PricingBasisSize": rightsized_class,
                            "TargetSize": grav_class,
                            "current_value": (
                                f"x86 {cls}" + (f" → rightsize to {rightsized_class}" if was_rightsized else "")
                            ),
                            "recommended_value": f"Migrate to Graviton {grav_class}",
                            "monthly_savings": round(base_price - grav_price, 2),
                            "EstimatedMonthlySavings": round(base_price - grav_price, 2),
                            "Counted": True,
                            "Recommendation": f"Migrate {cls} → {grav_class} (Graviton/ARM){basis_note}",
                            "EstimatedSavings": f"${base_price - grav_price:.2f}/mo",
                            "reason": (
                                f"{instance_id}: Aurora Graviton ({grav_class}) is a managed-engine class change "
                                f"(no app rebuild) — saves ${base_price - grav_price:.2f}/mo{basis_note}"
                            ),
                        }
                    )

    return recs


def _check_serverless_v2(
    cluster: dict[str, Any],
    cw: Any,
    fast_mode: bool,
) -> list[dict[str, Any]]:
    # The Serverless v2 lever is a $0 advisory (lowering MaxCapacity saves
    # nothing without consumed-vs-Min history), so no pricing inputs are needed —
    # only the cluster config and the CloudWatch ACUUtilization read.
    recs: list[dict[str, Any]] = []
    scaling = cluster.get("ServerlessV2ScalingConfiguration")
    if not scaling:
        return recs

    cluster_id = cluster["DBClusterIdentifier"]
    engine = cluster.get("Engine", "")
    engine_version = cluster.get("EngineVersion", "")
    max_acu = scaling.get("MaxCapacity", 0)
    min_acu = scaling.get("MinCapacity", 0)

    avg_util: float | None = None
    if not fast_mode:
        dims = [{"Name": "DBClusterIdentifier", "Value": cluster_id}]
        # Aurora Serverless v2 publishes ``ACUUtilization`` (canonical metric).
        # The previous ``ServerlessV2CapacityUtilization`` name matched nothing.
        avg_util = _get_cloudwatch_avg(cw, "AWS/RDS", "ACUUtilization", dims)

    if avg_util is None:
        return recs

    # Serverless v2 bills CONSUMED ACU (≈ utilization × MaxCapacity, floored at
    # MinCapacity), NOT the MaxCapacity ceiling. The previous ``max_acu ×
    # (1 − util)`` credited the unbilled ceiling-to-actual gap as savings — but
    # lowering MaxCapacity saves $0 unless the workload continuously hits the
    # ceiling (aurora C1). The realized lever is an over-high MinCapacity
    # (billed unconditionally); that needs consumed-vs-Min history we don't
    # have at scan time. Emit a $0 advisory so the flag renders without
    # inventing a dollar.
    recs.append(
        {
            "cluster_id": cluster_id,
            "engine": engine,
            "engine_version": engine_version,
            "check_type": "serverless_v2_acu_waste",
            "Counted": False,
            "current_value": f"Max ACU {max_acu}, Min ACU {min_acu}, Avg ACUUtilization {avg_util:.1f}%",
            "recommended_value": (
                "Review MinCapacity vs observed consumption; lower MinCapacity if it exceeds "
                "sustained demand (lowering MaxCapacity alone does not reduce Serverless v2 cost)"
            ),
            "monthly_savings": 0.0,
            "reason": (
                f"Serverless v2 bills consumed ACU; the Max/Util gap ({avg_util:.1f}% of "
                f"{max_acu}) is not a realized saving without consumed-vs-Min history"
            ),
            "AuditBasis": {
                "metric": "AWS/RDS ACUUtilization",
                "observed_utilization_pct": round(avg_util, 2),
                "billing_model": "consumed ACU (MinCapacity floor), not MaxCapacity ceiling",
                "unmeasured_inputs": ["consumed_acu_history_vs_min"],
                "reason": "lowering Max alone saves $0; needs consumed-vs-Min evidence",
            },
        }
    )

    return recs


def _io_instance_premium(pe: Any, members: list[dict[str, Any]]) -> tuple[float, int]:
    """Σ monthly I/O-Optimized instance premium over a cluster's available members.

    aurora H2: switching a cluster to I/O-Optimized raises every provisioned
    member's instance rate by ~30% (validated db.r6g.large Aurora MySQL us-east-1:
    $0.338 io-opt vs $0.260 std → $56.94/mo premium). Each member is priced via
    :meth:`PricingEngine.get_aurora_io_instance_premium_monthly` (returns the
    region-correct ``io_opt − std`` delta, or 0.0 when unresolved). Returns
    ``(total_premium, members_priced)``; ``0.0`` when ``pe`` is None so no
    premium is fabricated offline.
    """
    if pe is None:
        return 0.0, 0
    total = 0.0
    count = 0
    for m in members:
        if (m.get("DBInstanceStatus") or "") != "available":
            continue
        m_engine = (m.get("Engine") or "").lower()
        m_class = m.get("DBInstanceClass") or ""
        if not m_engine.startswith("aurora") or not m_class:
            continue
        try:
            prem = pe.get_aurora_io_instance_premium_monthly(
                m_engine,
                m_class,
                multi_az=bool(m.get("MultiAZ", False)),
                license_model=m.get("LicenseModel"),
            )
        except Exception:
            prem = 0.0
        if prem and prem > 0:
            total += prem
            count += 1
    return total, count


def _check_io_tier(
    ctx: Any,
    cluster: dict[str, Any],
    cw: Any,
    pe: Any,
    members: list[dict[str, Any]],
    pricing_multiplier: float,
    fast_mode: bool,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    if fast_mode:
        ctx.warn("fast mode: skipping Aurora I/O-tier check metric reads", "aurora")
        return recs

    cluster_id = cluster["DBClusterIdentifier"]
    engine = cluster.get("Engine", "")
    engine_version = cluster.get("EngineVersion", "")
    dims = [{"Name": "DBClusterIdentifier", "Value": cluster_id}]

    # aurora AUR-02: a cluster already on the I/O-Optimized tier has no
    # Standard→I/O-Optimized transition to recommend; emitting one would be a
    # fabricated Counted=True dollar with a wrong "Standard I/O" current_value.
    if (cluster.get("StorageType") or "") == "aurora-iopt1":
        return recs

    read_io = _get_cloudwatch_sum(cw, "AWS/RDS", "VolumeReadIOPs", dims)
    write_io = _get_cloudwatch_sum(cw, "AWS/RDS", "VolumeWriteIOPs", dims)

    if read_io is None or write_io is None:
        return recs

    total_io = read_io + write_io
    daily_io_avg = total_io / 14.0 if total_io > 0 else 0
    monthly_io = daily_io_avg * 30.0

    # aurora AUR-01: Aurora manages storage automatically, so
    # describe_db_clusters returns the fixed placeholder AllocatedStorage=1.
    # The billed storage (which sizes the I/O-Optimized storage premium that is
    # *subtracted* from the saving) must come from CloudWatch VolumeBytesUsed
    # (bytes → GB at 1e9). Without it the premium cannot be bounded, so the
    # lever is skipped (fail safe — never credit a saving against a 1 GB guess).
    vol_bytes = _get_cloudwatch_avg(cw, "AWS/RDS", "VolumeBytesUsed", dims)
    storage_gb = (vol_bytes / 1_000_000_000.0) if vol_bytes else 0.0

    if storage_gb <= 0 or monthly_io <= 0:
        return recs

    # Per-request I/O charge eliminated by I/O-Optimized (Standard tier). The
    # Aurora Standard I/O rate is region-correct from PricingEngine (live
    # <region>-Aurora:StorageIOUsage: $0.20/M us-east-1, $0.22/M Frankfurt/
    # Ireland) and must NOT be re-multiplied by pricing_multiplier; the constant
    # path is the region-scaled fallback only (aurora AUR-03).
    if pe is not None:
        try:
            io_rate_per_million = pe.get_aurora_io_rate_per_million()
        except Exception:
            io_rate_per_million = IO_COST_PER_MILLION * pricing_multiplier
    else:
        io_rate_per_million = IO_COST_PER_MILLION * pricing_multiplier
    standard_io_cost = (monthly_io / 1_000_000) * io_rate_per_million

    # Added cost of the I/O-Optimized configuration = storage premium (H1) +
    # instance-fleet premium (H2). Both legs are region-correct from PricingEngine
    # and must NOT be re-multiplied by pricing_multiplier.
    if pe is not None:
        try:
            storage_premium_per_gb = pe.get_aurora_io_storage_premium_per_gb()
        except Exception:
            storage_premium_per_gb = IO_OPTIMIZED_STORAGE_PREMIUM_FALLBACK_PER_GB * pricing_multiplier
    else:
        storage_premium_per_gb = IO_OPTIMIZED_STORAGE_PREMIUM_FALLBACK_PER_GB * pricing_multiplier
    storage_premium = storage_gb * storage_premium_per_gb

    instance_premium, members_repriced = _io_instance_premium(pe, members)

    optimized_premium = storage_premium + instance_premium
    savings = standard_io_cost - optimized_premium

    if savings > 10.0:
        recs.append(
            {
                "cluster_id": cluster_id,
                "engine": engine,
                "engine_version": engine_version,
                "check_type": "io_tier_optimization",
                "current_value": f"Standard I/O, ~{monthly_io:,.0f} ops/month, {storage_gb:.0f} GB storage",
                "recommended_value": "Switch to I/O-Optimized storage tier",
                "monthly_savings": round(savings, 2),
                "EstimatedMonthlySavings": round(savings, 2),
                "Counted": True,
                "EstimatedSavings": f"${savings:.2f}/mo",
                "reason": (
                    f"I/O-Optimized tier saves ${savings:.2f}/mo over Standard "
                    f"({monthly_io:,.0f} I/O ops, {storage_gb:.0f} GB storage); nets the "
                    f"${optimized_premium:.2f}/mo I/O-Optimized storage + instance premium"
                ),
                "AuditBasis": {
                    "rate": {
                        "io_per_million_usd": round(io_rate_per_million, 6),
                        "io_opt_storage_premium_per_gb_mo": round(storage_premium_per_gb, 6),
                        "io_opt_instance_premium_monthly": round(instance_premium, 2),
                        "members_repriced": members_repriced,
                    },
                    "region_multiplier": pricing_multiplier,
                    "metric_window": (
                        "14d AWS/RDS VolumeRead/WriteIOPs sum scaled to 30d; "
                        "storage from 14d VolumeBytesUsed avg"
                    ),
                    "formula": (
                        "standard_io_cost − (storage_gb × io_opt_storage_premium "
                        "+ Σ member io_opt instance premium)"
                    ),
                    "inputs": {
                        "monthly_io": round(monthly_io, 2),
                        "storage_gb": round(storage_gb, 2),
                        "standard_io_cost": round(standard_io_cost, 2),
                        "storage_premium": round(storage_premium, 2),
                    },
                },
            }
        )

    return recs


class AuroraModule(BaseServiceModule):
    """ServiceModule adapter for Aurora Serverless v2 cost optimization.

    Analyzes Aurora clusters for ACU waste and I/O-Optimized vs Standard
    storage tier selection.
    """

    key: str = "aurora"
    cli_aliases: tuple[str, ...] = ("aurora",)
    display_name: str = "Aurora"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="Aurora Clusters", source_path="extras.cluster_count", formatter="int"),
        StatCardSpec(label="Serverless v2", source_path="extras.serverless_cluster_count", formatter="int"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_type", label_path="check_type")

    requires_cloudwatch: bool = True
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        return ("rds", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:

        rds = ctx.client("rds")
        cw = ctx.client("cloudwatch")

        if not rds:
            return ServiceFindings(
                service_name="Aurora",
                total_recommendations=0,
                total_monthly_savings=0.0,
                sources={},
                extras={"cluster_count": 0, "serverless_cluster_count": 0, "global_cluster_count": 0},
            )

        multiplier = ctx.pricing_multiplier
        fast_mode = getattr(ctx, "fast_mode", False)
        pe = getattr(ctx, "pricing_engine", None)

        clusters = _describe_aurora_clusters(rds, ctx)

        # aurora L3: per-cluster I/O-Optimized flag (StorageType=aurora-iopt1) so
        # member-instance pricing in _check_provisioned_instances uses the matching
        # (Standard vs I/O-Optimized) instance SKU.
        cluster_storage_type: dict[str, bool] = {
            c["DBClusterIdentifier"]: c.get("StorageType", "") == "aurora-iopt1"
            for c in clusters
            if c.get("DBClusterIdentifier")
        }

        # Describe member instances once: feeds both the I/O-tier instance
        # premium (H2) and the provisioned-instance rightsizing/Graviton checks.
        instances = _describe_aurora_instances(rds, ctx)
        members_by_cluster: dict[str, list[dict[str, Any]]] = {}
        for inst in instances:
            cid = inst.get("DBClusterIdentifier")
            if cid:
                members_by_cluster.setdefault(cid, []).append(inst)

        # H3 / rds H1 — single-owner dedup. Any Aurora member already counted by
        # the RDS tab is suppressed here. Sources, both keyed by normalized id:
        #   * Cost Optimization Hub — prefetched into ctx.cost_hub_splits['rds']
        #     before any adapter runs (order-independent).
        #   * RDS Compute Optimizer + CoH counted ids — published by
        #     RdsModule.scan via ctx.rds_covered_instance_ids (RDS runs first).
        covered: set[str] = set()
        for r in getattr(ctx, "cost_hub_splits", {}).get("rds", []):
            nid = normalize_rds_arn(r.get("resourceArn") or r.get("resourceId") or "")
            if nid:
                covered.add(nid)
        rds_covered = getattr(ctx, "rds_covered_instance_ids", None)
        if isinstance(rds_covered, set):
            covered |= rds_covered

        serverless_recs: list[dict[str, Any]] = []
        io_recs: list[dict[str, Any]] = []

        serverless_count = 0
        global_count = 0

        for cluster in clusters:
            try:
                if cluster.get("ServerlessV2ScalingConfiguration"):
                    serverless_count += 1
                if cluster.get("GlobalClusterIdentifier"):
                    global_count += 1

                serverless_recs.extend(_check_serverless_v2(cluster, cw, fast_mode))
                cluster_id = cluster.get("DBClusterIdentifier", "")
                io_recs.extend(
                    _check_io_tier(
                        ctx, cluster, cw, pe, members_by_cluster.get(cluster_id, []), multiplier, fast_mode
                    )
                )
            except Exception as e:
                logger.warning(f"[aurora] cluster check failed: {e}")
                ctx.warn(f"[aurora] cluster check failed: {e}", "aurora")
                continue

        # Provisioned member instances (rightsizing + Graviton) — the cluster
        # checks above only cover Serverless v2 and the I/O tier. Instances
        # covered by the RDS tab are suppressed via ``covered`` (no double count).
        instance_recs = _check_provisioned_instances(
            ctx,
            rds,
            cw,
            fast_mode,
            covered=covered,
            instances=instances,
            cluster_storage_type=cluster_storage_type,
        )

        all_recs = serverless_recs + io_recs + instance_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        return ServiceFindings(
            service_name="Aurora",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "serverless_v2": SourceBlock(
                    count=len(serverless_recs),
                    recommendations=tuple(serverless_recs),
                ),
                "io_tier_analysis": SourceBlock(
                    count=len(io_recs),
                    recommendations=tuple(io_recs),
                ),
                "instance_optimization": SourceBlock(
                    count=len(instance_recs),
                    recommendations=tuple(instance_recs),
                ),
            },
            extras={
                "cluster_count": len(clusters),
                "serverless_cluster_count": serverless_count,
                "global_cluster_count": global_count,
            },
            optimization_descriptions={
                "serverless_v2": {
                    "title": "Serverless v2 ACU Waste",
                    "description": "Clusters with max ACU significantly above actual utilization",
                },
                "io_tier_analysis": {
                    "title": "I/O Tier Analysis",
                    "description": "Compare Standard vs I/O-Optimized storage tier costs",
                },
                "instance_optimization": {
                    "title": "Instance Rightsizing & Graviton",
                    "description": "Peak-aware downsizing and Graviton migration for provisioned Aurora instances",
                },
            },
        )

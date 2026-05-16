"""Aurora Serverless v2 cost optimization adapter.

Analyzes Aurora DB clusters for:
    - Serverless v2 ACU waste (max vs actual utilization)
    - I/O tier analysis (Standard vs I/O-Optimized)
    - Clone/snapshot sprawl
    - Global DB replica lag
    - Backtrack window cost risk
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

logger = logging.getLogger(__name__)

ACU_HOURLY_FALLBACK: float = 0.06
IO_COST_PER_MILLION: float = 0.20
IO_OPTIMIZED_PREMIUM_PER_GB: float = 0.025
AURORA_ENGINES: tuple[str, ...] = ("aurora", "aurora-mysql", "aurora-postgresql")
HOURS_PER_MONTH: int = 730


def _get_acu_hourly(ctx: Any) -> float:
    """Return the per-ACU hourly price already region-correct.

    Live PricingEngine returns a region-priced value (no multiplier needed).
    Fallback applies ctx.pricing_multiplier internally so the caller can
    treat the return value uniformly without per-source casing. Callers
    MUST NOT re-multiply by ctx.pricing_multiplier downstream.
    """
    try:
        price = ctx.pricing_engine.get_aurora_acu_hourly()
        if price and price > 0:
            return price
    except Exception:
        pass
    return ACU_HOURLY_FALLBACK * ctx.pricing_multiplier


def _describe_aurora_clusters(rds: Any) -> list[dict[str, Any]]:
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
        except Exception:
            pass
    return clusters


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


def _check_serverless_v2(
    cluster: dict[str, Any],
    rds: Any,
    cw: Any,
    acu_hourly: float,
    pricing_multiplier: float,
    fast_mode: bool,
) -> list[dict[str, Any]]:
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
        avg_util = _get_cloudwatch_avg(cw, "AWS/RDS", "ServerlessV2CapacityUtilization", dims)

    if avg_util is None:
        return recs

    waste_ratio = max(0.0, 1.0 - (avg_util / 100.0)) if max_acu > 0 else 0.0
    wasted_acu = max_acu * waste_ratio
    # `acu_hourly` from `_get_acu_hourly` is already region-correct (live
    # path returns AWS Pricing API value; fallback path applies the
    # multiplier internally). MUST NOT re-multiply by pricing_multiplier.
    _ = pricing_multiplier  # explicit: not applied here, see helper docstring.
    monthly_savings = wasted_acu * acu_hourly * HOURS_PER_MONTH

    if monthly_savings > 1.0:
        recs.append(
            {
                "cluster_id": cluster_id,
                "engine": engine,
                "engine_version": engine_version,
                "check_type": "serverless_v2_acu_waste",
                "current_value": f"Max ACU {max_acu}, Avg Utilization {avg_util:.1f}%",
                "recommended_value": f"Reduce Max ACU to {max(int(min_acu), int(max_acu * (avg_util / 100.0) * 1.2))}",
                "monthly_savings": round(monthly_savings, 2),
                "reason": f"Serverless v2 cluster averaging {avg_util:.1f}% of {max_acu} max ACU "
                f"({wasted_acu:.1f} ACU wasted)",
            }
        )

    return recs


def _check_io_tier(
    cluster: dict[str, Any],
    cw: Any,
    pricing_multiplier: float,
    fast_mode: bool,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    if fast_mode:
        return recs

    cluster_id = cluster["DBClusterIdentifier"]
    engine = cluster.get("Engine", "")
    engine_version = cluster.get("EngineVersion", "")
    dims = [{"Name": "DBClusterIdentifier", "Value": cluster_id}]

    read_io = _get_cloudwatch_sum(cw, "AWS/RDS", "VolumeReadIOPs", dims)
    write_io = _get_cloudwatch_sum(cw, "AWS/RDS", "VolumeWriteIOPs", dims)

    if read_io is None or write_io is None:
        return recs

    total_io = read_io + write_io
    daily_io_avg = total_io / 14.0 if total_io > 0 else 0
    monthly_io = daily_io_avg * 30.0

    storage_gb = 0.0
    try:
        allocated = cluster.get("AllocatedStorage", 0)
        if allocated:
            storage_gb = float(allocated)
    except (TypeError, ValueError):
        pass

    if storage_gb <= 0 or monthly_io <= 0:
        return recs

    standard_io_cost = (monthly_io / 1_000_000) * IO_COST_PER_MILLION
    optimized_premium = storage_gb * IO_OPTIMIZED_PREMIUM_PER_GB
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
                "monthly_savings": round(savings * pricing_multiplier, 2),
                "reason": f"I/O-Optimized tier saves ${savings:.2f}/mo over Standard "
                f"({monthly_io:,.0f} I/O ops, {storage_gb:.0f} GB storage)",
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

    grouping = GroupingSpec(by="check_category", label_path="check_type")

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

        acu_hourly = _get_acu_hourly(ctx)
        multiplier = ctx.pricing_multiplier
        fast_mode = getattr(ctx, "fast_mode", False)

        clusters = _describe_aurora_clusters(rds)

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

                serverless_recs.extend(_check_serverless_v2(cluster, rds, cw, acu_hourly, multiplier, fast_mode))
                io_recs.extend(_check_io_tier(cluster, cw, multiplier, fast_mode))
            except Exception as e:
                logger.warning(f"[aurora] cluster check failed: {e}")
                continue

        all_recs = serverless_recs + io_recs
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
            },
        )

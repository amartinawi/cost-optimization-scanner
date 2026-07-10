"""ElastiCache cost optimization checks.

Extracted from CostOptimizer.get_enhanced_elasticache_checks() as a free function.
This module will later become ElastiCacheModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

LOW_CPU_THRESHOLD: int = 20

# Peak `DatabaseMemoryUsagePercentage` above which a one-size-down node cannot be
# guaranteed to hold the working set. AWS's node ladder leaves the smaller size
# with only ~36-48% of the current maxmemory (cache.t4g.micro is 36% of
# cache.t4g.small; cache.r5.large is 50% of cache.r5.xlarge), so 35% is the
# family-agnostic bound below which the data provably fits. Deliberately
# conservative: rejecting a valid downsize under-reports (safe), while a rec that
# would OOM the cache and be reverted is not a realizable saving at all.
MAX_MEMORY_HEADROOM_PCT: float = 35.0


def _memory_signals(cloudwatch: Any, cluster_id: str, start_time: Any, end_time: Any) -> tuple[float | None, float | None]:
    """Peak memory usage % and total evictions for a cache cluster.

    Returns ``(None, None)`` when the metrics cannot be read — the caller then
    withholds the downsize lever rather than assuming the working set fits. An
    unreadable signal is not evidence of headroom (lesson C8).
    """
    peak_pct: float | None = None
    evictions: float | None = None
    try:
        mem = cloudwatch.get_metric_statistics(
            Namespace="AWS/ElastiCache",
            MetricName="DatabaseMemoryUsagePercentage",
            Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Maximum"],
        )
        points = mem.get("Datapoints") or []
        if points:
            peak_pct = max(dp["Maximum"] for dp in points)
    except Exception:  # noqa: BLE001 — absence of evidence, not evidence of headroom
        return None, None

    try:
        evict = cloudwatch.get_metric_statistics(
            Namespace="AWS/ElastiCache",
            MetricName="Evictions",
            Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Sum"],
        )
        evictions = sum(dp["Sum"] for dp in (evict.get("Datapoints") or []))
    except Exception:  # noqa: BLE001
        return peak_pct, None

    return peak_pct, evictions

GRAVITON_FAMILIES: tuple[str, ...] = (
    "m7g",
    "r7g",
    "m6g",
    "r6g",
    "c7g",
    "c6g",
    "t4g",
)

SMALLEST_SIZES: dict[str, str] = {
    "t2": "nano",
    "t3": "nano",
    "t4g": "nano",
    "m5": "large",
    "m6i": "large",
    "m7g": "large",
    "r5": "large",
    "r6g": "large",
    "r7g": "large",
    "c5": "large",
    "c6g": "large",
    "c7g": "large",
}


def get_enhanced_elasticache_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced ElastiCache cost optimization checks.

    The Underutilized-cluster downsizing lever is gated on a CloudWatch
    ``CPUUtilization`` read (14-day average). In ``fast_mode`` that per-cluster
    metric read is skipped (one warning) and the lever is suppressed entirely —
    no guessed downsize saving is emitted without the metric, mirroring
    ``rds.py`` / ``file_systems.py`` (elasticache H3).
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "reserved_nodes": [],
        "underutilized_clusters": [],
        "old_engine_versions": [],
        "valkey_migration": [],
        "graviton_migration": [],
    }

    fast_mode = bool(getattr(ctx, "fast_mode", False))

    try:
        elasticache = ctx.client("elasticache")
        # Skip CloudWatch entirely in fast mode so --fast makes no per-cluster
        # metric calls (fast-mode contract — elasticache H3).
        cloudwatch = ctx.client("cloudwatch") if not fast_mode else None
        if fast_mode:
            ctx.warn(
                "[elasticache] fast mode: Underutilized-cluster downsizing needs "
                "CloudWatch CPUUtilization metrics and was skipped.",
                "elasticache",
            )

        paginator = elasticache.get_paginator("describe_cache_clusters")
        for page in paginator.paginate(ShowCacheNodeInfo=True):
            for cluster in page["CacheClusters"]:
                cluster_id = cluster["CacheClusterId"]
                engine = cluster.get("Engine", "")
                engine_version = cluster.get("EngineVersion", "")
                node_type = cluster.get("CacheNodeType", "")
                num_nodes = cluster.get("NumCacheNodes", 0)
                status = cluster.get("CacheClusterStatus", "")

                if status != "available":
                    continue

                if engine.lower() == "redis":
                    checks["valkey_migration"].append(
                        {
                            "ClusterId": cluster_id,
                            "Engine": engine,
                            "EngineVersion": engine_version,
                            "NodeType": node_type,
                            "NumNodes": num_nodes,
                            "Recommendation": (
                                "Consider migrating to ElastiCache for Valkey"
                                " (open-source Redis fork with feature parity)"
                            ),
                            "EstimatedSavings": (
                                "Valkey is ~20% cheaper than Redis for identical node types;"
                                " migrate for cost savings and open-source security updates"
                            ),
                            "CheckCategory": "Valkey Migration",
                        }
                    )

                is_graviton = any(node_type.startswith(f"cache.{family}") for family in GRAVITON_FAMILIES)

                if not is_graviton:
                    checks["graviton_migration"].append(
                        {
                            "ClusterId": cluster_id,
                            "Engine": engine,
                            "NodeType": node_type,
                            # NumNodes so the adapter prices the Graviton node-price
                            # delta across every node, not just one (elasticache H1).
                            "NumNodes": num_nodes,
                            "Recommendation": "Migrate to Graviton instances",
                            "EstimatedSavings": "Estimated: 20-40% price-performance improvement",
                            "CheckCategory": "Graviton Migration",
                        }
                    )

                # "Old Engine Version" finding removed: pure version-freshness nudge
                # with no cost saving tied (Redis 7 upgrade is free; engine cost is identical).

                if num_nodes >= 2:
                    checks["reserved_nodes"].append(
                        {
                            "ClusterId": cluster_id,
                            "Engine": engine,
                            "NodeType": node_type,
                            "NumNodes": num_nodes,
                            "Recommendation": "Consider Reserved Nodes for stable workloads (1-3 year commitment)",
                            "EstimatedSavings": "30-60% vs On-Demand for committed usage",
                            "CheckCategory": "Reserved Nodes Opportunity",
                        }
                    )

                if fast_mode:
                    # CloudWatch skipped — no underutilized downsizing lever
                    # without a measured CPU signal (elasticache H3).
                    continue

                try:
                    end_time = datetime.now(UTC)
                    start_time = end_time - timedelta(days=14)

                    cpu_response = cloudwatch.get_metric_statistics(
                        Namespace="AWS/ElastiCache",
                        MetricName="CPUUtilization",
                        Dimensions=[{"Name": "CacheClusterId", "Value": cluster_id}],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,
                        Statistics=["Average"],
                    )

                    if cpu_response["Datapoints"]:
                        avg_cpu = sum(dp["Average"] for dp in cpu_response["Datapoints"]) / len(
                            cpu_response["Datapoints"]
                        )

                        if avg_cpu < LOW_CPU_THRESHOLD:
                            if node_type.startswith("cache."):
                                family_size = node_type.replace("cache.", "")
                                if "." in family_size:
                                    family, size = family_size.split(".", 1)

                                    if family in SMALLEST_SIZES and size == SMALLEST_SIZES[family]:
                                        continue

                            # Low CPU alone does not make a downsize executable: the
                            # working set must also FIT the smaller node. One size
                            # down leaves only ~36-48% of the current maxmemory
                            # (t4g.micro is 36% of t4g.small; r5.large is 50% of
                            # r5.xlarge), so a node above MAX_MEMORY_HEADROOM_PCT
                            # cannot be guaranteed to fit. A rec that would have to
                            # be reverted is not a realizable saving.
                            peak_mem_pct, evictions = _memory_signals(cloudwatch, cluster_id, start_time, end_time)
                            memory_ok = (
                                peak_mem_pct is not None
                                and peak_mem_pct <= MAX_MEMORY_HEADROOM_PCT
                                and (evictions or 0) == 0
                            )

                            checks["underutilized_clusters"].append(
                                {
                                    "ClusterId": cluster_id,
                                    "Engine": engine,
                                    "NodeType": node_type,
                                    # NumNodes so the downsize delta prices every
                                    # node, not just one (elasticache H1).
                                    "NumNodes": num_nodes,
                                    "AvgCPU": round(avg_cpu, 2),
                                    "PeakMemoryUsagePct": round(peak_mem_pct, 2) if peak_mem_pct is not None else None,
                                    "Evictions": evictions,
                                    "MemoryHeadroomOk": memory_ok,
                                    "Recommendation": "Downsize node type or consider smaller instance family",
                                    "EstimatedSavings": "30-50%",
                                    "CheckCategory": "Underutilized Cluster",
                                }
                            )
                except Exception as e:
                    # Classify (AccessDenied -> permission_issue, else warn); a
                    # failed metric read drops only this cluster's downsize lever,
                    # never a fabricated saving (rule #4).
                    record_aws_error(
                        ctx,
                        e,
                        service="elasticache",
                        context=f"CloudWatch CPUUtilization read failed for cluster {cluster_id}",
                    )
                    continue

    except Exception as e:
        ctx.warn(f"Could not analyze ElastiCache: {e}", "elasticache")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

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

LOW_CPU_THRESHOLD: int = 20

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
    """Get enhanced ElastiCache cost optimization checks."""
    checks: dict[str, list[dict[str, Any]]] = {
        "reserved_nodes": [],
        "underutilized_clusters": [],
        "old_engine_versions": [],
        "valkey_migration": [],
        "graviton_migration": [],
    }

    try:
        elasticache = ctx.client("elasticache")
        cloudwatch = ctx.client("cloudwatch")

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
                            "NodeType": node_type,
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
                            "NodeType": node_type,
                            "NumNodes": num_nodes,
                            "Recommendation": "Consider Reserved Nodes for stable workloads (1-3 year commitment)",
                            "EstimatedSavings": "30-60% vs On-Demand for committed usage",
                            "CheckCategory": "Reserved Nodes Opportunity",
                        }
                    )

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

                            checks["underutilized_clusters"].append(
                                {
                                    "ClusterId": cluster_id,
                                    "NodeType": node_type,
                                    "AvgCPU": round(avg_cpu, 2),
                                    "Recommendation": "Downsize node type or consider smaller instance family",
                                    "EstimatedSavings": "30-50%",
                                    "CheckCategory": "Underutilized Cluster",
                                }
                            )
                except Exception as e:
                    logger.warning(f"Warning: Could not get metrics for cluster {cluster_id}: {e}")
                    continue

    except Exception as e:
        ctx.warn(f"Could not analyze ElastiCache: {e}", "elasticache")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

"""MediaStore cost optimization checks.

Extracted from CostOptimizer.get_enhanced_mediastore_checks() as a free function.
This module will later become MediastoreModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

MEDIASTORE_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "unused_containers": {
        "title": "Review Unused MediaStore Containers",
        "description": "Delete unused MediaStore containers to eliminate storage and request costs.",
        "action": "Review container usage and delete unused containers",
    }
}


def get_enhanced_mediastore_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced MediaStore cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {"unused_containers": [], "access_optimization": [], "cors_policies": []}

    try:
        mediastore = ctx.client("mediastore")
        paginator = mediastore.get_paginator("list_containers")
        containers = [c for page in paginator.paginate() for c in page.get("Containers", [])]

        for container in containers:
            container_name = container.get("Name")
            status = container.get("Status")

            if status == "ACTIVE":
                try:
                    end_time = datetime.now(UTC)
                    start_time = end_time - timedelta(days=14)

                    metrics_to_check = ["RequestCount", "BytesDownloaded", "BytesUploaded"]
                    total_activity = 0
                    # A read failure and a genuine zero look identical (total=0),
                    # so an active container whose activity reads merely failed
                    # would be flagged "no activity → consider deletion" with a
                    # non-zero saving. Track read health and abstain on any
                    # failure (mediastore C1).
                    activity_read_failed = False
                    activity_datapoints_seen = 0

                    cloudwatch = ctx.client("cloudwatch")
                    for metric_name in metrics_to_check:
                        try:
                            metrics = cloudwatch.get_metric_statistics(
                                Namespace="AWS/MediaStore",
                                MetricName=metric_name,
                                Dimensions=[{"Name": "ContainerName", "Value": container_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=86400,
                                Statistics=["Sum"],
                            )
                            dps = metrics.get("Datapoints", [])
                            activity_datapoints_seen += len(dps)
                            total_activity += sum(point["Sum"] for point in dps)
                        except Exception as e:
                            record_aws_error(
                                ctx,
                                e,
                                service="mediastore",
                                context=f"MediaStore {metric_name} metric for {container_name}",
                            )
                            activity_read_failed = True

                    # Skip this container entirely if ANY activity read failed —
                    # a failed read must not be interpreted as "no activity" and
                    # must not trigger a deletion recommendation.
                    if activity_read_failed:
                        continue

                    try:
                        size_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/MediaStore",
                            MetricName="BucketSizeBytes",
                            Dimensions=[{"Name": "ContainerName", "Value": container_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=["Average"],
                        )
                        storage_bytes = size_metrics["Datapoints"][-1]["Average"] if size_metrics["Datapoints"] else 0
                        storage_gb = storage_bytes / (1024**3)
                    except Exception:
                        storage_gb = 0

                    storage_cost_per_gb = 0.023 * ctx.pricing_multiplier
                    estimated_savings = storage_gb * storage_cost_per_gb
                    savings_str = f"${estimated_savings:.2f}/month" if estimated_savings > 0 else "$0.00/month"

                    # Only flag unused when every activity read succeeded AND
                    # returned no datapoints (confirmed idle), never on a
                    # failed/empty read that might mask real traffic.
                    if activity_datapoints_seen > 0 and total_activity == 0:
                        checks["unused_containers"].append(
                            {
                                "ContainerName": container_name,
                                "ActivityLast14Days": total_activity,
                                "EstimatedStorageGB": storage_gb,
                                "Recommendation": (
                                    f"Container shows no activity in last 14 days "
                                    f"({storage_gb:.1f} GB stored) - consider deletion"
                                ),
                                "EstimatedSavings": savings_str,
                                "CheckCategory": "Unused Resource Cleanup",
                            }
                        )
                except Exception as e:
                    ctx.warn(f"Could not get MediaStore metrics for {container_name}: {e}", "mediastore")
                    continue

    except Exception as e:
        ctx.warn(f"Could not analyze MediaStore resources: {e}", "mediastore")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}

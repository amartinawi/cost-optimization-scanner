"""MediaStore cost optimization checks.

Extracted from CostOptimizer.get_enhanced_mediastore_checks() as a free function.
This module will later become MediastoreModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

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
        response = mediastore.list_containers()
        containers = response.get("Containers", [])

        for container in containers:
            container_name = container.get("Name")
            status = container.get("Status")

            if status == "ACTIVE":
                try:
                    end_time = datetime.now(UTC)
                    start_time = end_time - timedelta(days=14)

                    metrics_to_check = ["RequestCount", "BytesDownloaded", "BytesUploaded"]
                    total_activity = 0

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
                            total_activity += sum(point["Sum"] for point in metrics.get("Datapoints", []))
                        except Exception:
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

                    try:
                        upload_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/MediaStore",
                            MetricName="BytesUploaded",
                            Dimensions=[{"Name": "ContainerName", "Value": container_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=["Sum"],
                        )
                        monthly_ingest_bytes = sum(dp["Sum"] for dp in upload_metrics.get("Datapoints", []))
                        monthly_ingest_gb = monthly_ingest_bytes / (1024**3)
                        ingest_cost = monthly_ingest_gb * 0.02
                    except Exception:
                        ingest_cost = 0

                    storage_cost_per_gb = 0.023 * ctx.pricing_multiplier
                    estimated_savings = (storage_gb * storage_cost_per_gb) + ingest_cost
                    savings_str = f"${estimated_savings:.2f}/month" if estimated_savings > 0 else "$0.00/month"

                    if total_activity == 0:
                        checks["unused_containers"].append(
                            {
                                "ContainerName": container_name,
                                "ActivityLast14Days": total_activity,
                                "EstimatedStorageGB": storage_gb,
                                "IngestCost": ingest_cost,
                                "Recommendation": (
                                    f"Container shows no activity in last 14 days "
                                    f"({storage_gb:.1f} GB stored, {ingest_cost:.2f} ingest) - consider deletion"
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

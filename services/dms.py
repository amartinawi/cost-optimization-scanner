"""DMS cost optimization checks.

Extracted from CostOptimizer.get_enhanced_dms_checks() as a free function.
This module will later become DmsModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/dms.py] DMS module active")

DMS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "instance_rightsizing": {
        "title": "Optimize DMS Instance Sizing",
        "description": "Right-size DMS instances or migrate to serverless for variable workloads.",
        "action": "Consider DMS Serverless or smaller instance types",
    }
}


def get_enhanced_dms_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced DMS cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "serverless_migration": [],
        "instance_rightsizing": [],
        "unused_instances": [],
    }

    try:
        dms = ctx.client("dms")

        paginator = dms.get_paginator("describe_replication_instances")

        for page in paginator.paginate():
            instances = page.get("ReplicationInstances", [])

            for instance in instances:
                instance_id = instance.get("ReplicationInstanceIdentifier")
                instance_class = instance.get("ReplicationInstanceClass")
                status = instance.get("ReplicationInstanceStatus")

                if status == "available" and instance_class and "large" in instance_class:
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        cloudwatch = ctx.client("cloudwatch")
                        cpu_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DMS",
                            MetricName="CPUUtilization",
                            Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": instance_id}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )

                        avg_cpu = sum(point["Average"] for point in cpu_metrics.get("Datapoints", [])) / max(
                            len(cpu_metrics.get("Datapoints", [])), 1
                        )

                        if avg_cpu < 30:
                            checks["instance_rightsizing"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceClass": instance_class,
                                    "AvgCPU": f"{avg_cpu:.1f}%",
                                    "Recommendation": (
                                        f"Low CPU utilization ({avg_cpu:.1f}%) "
                                        "- consider DMS Serverless or smaller instance"
                                    ),
                                    "EstimatedSavings": "$100/month potential",
                                    "CheckCategory": "Instance Optimization",
                                }
                            )

                            if avg_cpu < 5:
                                checks["unused_instances"].append(
                                    {
                                        "InstanceId": instance_id,
                                        "InstanceClass": instance_class,
                                        "AvgCPU": f"{avg_cpu:.1f}%",
                                        "Recommendation": "Very low CPU utilization - consider stopping if unused",
                                        "EstimatedSavings": "Full instance cost if terminated",
                                        "CheckCategory": "Unused DMS Instances",
                                    }
                                )
                    except Exception:
                        checks["instance_rightsizing"].append(
                            {
                                "InstanceId": instance_id,
                                "InstanceClass": instance_class,
                                "Recommendation": (
                                    "Review replication instance utilization "
                                    "- consider DMS Serverless for variable workloads"
                                ),
                                "EstimatedSavings": "$100/month potential",
                                "CheckCategory": "Instance Optimization",
                                "Note": "Verify actual CPU and network utilization before downsizing",
                            }
                        )

        try:
            serverless_paginator = dms.get_paginator("describe_replication_configs")
            for page in serverless_paginator.paginate():
                serverless_configs = page.get("ReplicationConfigs", [])

                for config in serverless_configs:
                    config_id = config.get("ReplicationConfigIdentifier")
                    checks["serverless_migration"].append(
                        {
                            "ConfigId": config_id,
                            "Recommendation": "Monitor DMS Serverless usage patterns for cost optimization",
                            "EstimatedSavings": "Variable based on usage",
                            "CheckCategory": "Serverless Optimization",
                        }
                    )
        except Exception:
            pass

    except Exception as e:
        ctx.warn(f"Could not analyze DMS resources: {e}", "dms")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}

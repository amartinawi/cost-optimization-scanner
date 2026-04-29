"""MSK cost optimization checks.

Extracted from CostOptimizer.get_enhanced_msk_checks() as a free function.
This module will later become MskModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("\U0001f50d [services/msk.py] MSK module active")

MSK_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "cluster_rightsizing": {
        "title": "Optimize MSK Cluster Sizing",
        "description": "Right-size MSK clusters or consider serverless for variable workloads.",
        "action": "Consider MSK Serverless or smaller broker instances",
    }
}


def get_enhanced_msk_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced MSK cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "cluster_rightsizing": [],
        "serverless_migration": [],
        "storage_optimization": [],
    }

    try:
        kafka = ctx.client("kafka")
        paginator = kafka.get_paginator("list_clusters")
        for page in paginator.paginate():
            clusters = page.get("ClusterInfoList", [])

            for cluster in clusters:
                cluster_name = cluster.get("ClusterName")
                state = cluster.get("State")
                broker_node_group = cluster.get("BrokerNodeGroupInfo", {})
                instance_type = broker_node_group.get("InstanceType")

                if state == "ACTIVE" and instance_type and "large" in instance_type:
                    checks["cluster_rightsizing"].append(
                        {
                            "ClusterName": cluster_name,
                            "InstanceType": instance_type,
                            "Recommendation": (
                                "Review cluster utilization - consider MSK Serverless for variable workloads"
                            ),
                            "EstimatedSavings": "$200/month potential",
                            "CheckCategory": "Cluster Rightsizing",
                            "Note": "Verify actual throughput and utilization before downsizing",
                        }
                    )

                storage_info = broker_node_group.get("StorageInfo", {})
                ebs_storage = storage_info.get("EBSStorageInfo", {})
                volume_size = ebs_storage.get("VolumeSize", 0)

                if volume_size > 1000:
                    checks["storage_optimization"].append(
                        {
                            "ClusterName": cluster_name,
                            "VolumeSize": f"{volume_size} GB",
                            "Recommendation": "Large EBS volumes - review retention policies and consider gp3 volumes",
                            "EstimatedSavings": "20% with gp3 migration + retention optimization",
                            "CheckCategory": "MSK Storage Optimization",
                        }
                    )

        try:
            paginator_v2 = kafka.get_paginator("list_clusters_v2")
            for page in paginator_v2.paginate():
                serverless_clusters = page.get("ClusterInfoList", [])

                for cluster in serverless_clusters:
                    if cluster.get("ClusterType") == "SERVERLESS":
                        cluster_name = cluster.get("ClusterName")
                        checks["serverless_migration"].append(
                            {
                                "ClusterName": cluster_name,
                                "ClusterType": "Serverless",
                                "Recommendation": "Monitor serverless usage patterns for cost optimization",
                                "EstimatedSavings": "Variable based on usage",
                                "CheckCategory": "Serverless Optimization",
                            }
                        )
        except Exception:
            pass

    except Exception as e:
        ctx.warn(f"Could not analyze MSK resources: {e}", "msk")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}

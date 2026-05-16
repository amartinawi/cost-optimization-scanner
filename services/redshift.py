"""Redshift cost optimization checks.

Extracted from CostOptimizer.get_enhanced_redshift_checks() as a free function.
This module will later become RedshiftModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.scan_context import ScanContext

REDSHIFT_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "reserved_instances": {
        "title": "Purchase Redshift Reserved Instances",
        "description": "Save up to 24% with Redshift Reserved Instances for predictable workloads.",
        "action": "Purchase 1-year or 3-year Reserved Instances",
    }
}


def get_enhanced_redshift_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced Redshift cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "reserved_instances": [],
        "serverless_optimization": [],
        "cluster_rightsizing": [],
        "pause_resume_scheduling": [],
        "storage_optimization": [],
    }

    try:
        redshift = ctx.client("redshift")
        paginator = redshift.get_paginator("describe_clusters")
        clusters: list[dict[str, Any]] = []
        for page in paginator.paginate():
            clusters.extend(page.get("Clusters", []))

        for cluster in clusters:
            cluster_id = cluster.get("ClusterIdentifier")
            node_type = cluster.get("NodeType")
            cluster_status = cluster.get("ClusterStatus")
            number_of_nodes = cluster.get("NumberOfNodes", 1)

            if cluster_status == "available" and cluster.get("ClusterCreateTime") and number_of_nodes >= 2:
                create_time = cluster.get("ClusterCreateTime")
                if isinstance(create_time, str):  # noqa: SIM108
                    cluster_age_days = 30
                else:
                    cluster_age_days = (datetime.now(UTC) - create_time).days  # type: ignore[operator]

                if cluster_age_days > 30:
                    checks["reserved_instances"].append(
                        {
                            "ClusterIdentifier": cluster_id,
                            "NodeType": node_type,
                            "NumberOfNodes": number_of_nodes,
                            "ClusterAge": f"{cluster_age_days} days",
                            "Recommendation": (
                                f"Consider Reserved Instances for stable cluster"
                                f" (running {cluster_age_days} days) - 24% savings potential"
                            ),
                            "EstimatedSavings": f"${number_of_nodes * 150:.2f}/month with 1-year RI",
                            "CheckCategory": "Reserved Instance Optimization",
                            "Note": "Suitable for predictable, long-running workloads",
                        }
                    )

            if number_of_nodes > 3:
                checks["cluster_rightsizing"].append(
                    {
                        "ClusterIdentifier": cluster_id,
                        "CurrentNodes": number_of_nodes,
                        "Recommendation": "Analyze query performance and consider reducing cluster size",
                        "EstimatedSavings": f"${(number_of_nodes - 2) * 100:.2f}/month potential",
                        "CheckCategory": "Cluster Rightsizing",
                    }
                )

        try:
            redshift_serverless = ctx.client("redshift-serverless")
            paginator = redshift_serverless.get_paginator("list_workgroups")
            for page in paginator.paginate():
                workgroups = page.get("workgroups", [])

                for workgroup in workgroups:
                    workgroup_name = workgroup.get("workgroupName")
                    status = workgroup.get("status")

                    if status == "AVAILABLE":
                        checks["serverless_optimization"].append(
                            {
                                "WorkgroupName": workgroup_name,
                                "Recommendation": (
                                    "Consider Serverless Reservations for 24% savings on predictable workloads"
                                ),
                                "EstimatedSavings": "$150/month with reservations",
                                "CheckCategory": "Serverless Optimization",
                            }
                        )
        except Exception:
            pass

    except Exception as e:
        ctx.warn(f"Could not analyze Redshift resources: {e}", "redshift")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}

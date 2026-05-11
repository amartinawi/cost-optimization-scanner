"""Cost Optimization Hub and Compute Advisor recommendations.

Consolidates all AWS advisory-service calls (Cost Optimization Hub,
Compute Optimizer for EC2/EBS/RDS) into a single module because they
share the same boto3 client patterns and advisory role.

Extracted from:
  - cost_optimizer.py  get_detailed_cost_hub_recommendations()
  - cost_optimizer.py  get_compute_optimizer_recommendations()
  - services/ec2.py    get_compute_optimizer_recommendations()
  - services/ebs.py    get_ebs_compute_optimizer_recs()
  - services/rds.py    get_rds_compute_optimizer_recommendations()
"""

from __future__ import annotations

import logging
from typing import Any

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)


def _compute_optimizer_opt_in_rec(service_label: str, action_summary: str) -> dict[str, Any]:
    """Build a synthetic Compute-Optimizer-disabled placeholder rec.

    The placeholder carries zero quantified savings (estimatedMonthlySavings = 0.0)
    so downstream aggregators read the same flat-float schema as real AWS responses
    rather than parsing a prose string.
    """
    return {
        "ResourceId": "compute-optimizer-service",
        "ResourceType": "Service Configuration",
        "Issue": "AWS Compute Optimizer not enabled",
        "Recommendation": f"Enable AWS Compute Optimizer for {service_label} {action_summary} recommendations",
        "estimatedMonthlySavings": 0.0,
        "currencyCode": "USD",
        "Action": (
            "Go to AWS Compute Optimizer console and opt-in to receive"
            f" {service_label} {action_summary} recommendations"
        ),
        "Priority": "Medium",
        "Service": "Compute Optimizer",
    }


def get_detailed_cost_hub_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get detailed recommendations from Cost Optimization Hub (all resource types)."""
    recommendations: list[dict[str, Any]] = []

    cost_hub = ctx.client("cost-optimization-hub", region="us-east-1")
    if not cost_hub:
        print("ℹ️ Cost Optimization Hub unavailable - continuing with other optimization sources")
        return recommendations

    try:
        response = cost_hub.list_recommendations(
            filter={"regions": [ctx.region]},
            maxResults=100,
        )

        for rec in response.get("items", []):
            try:
                detailed = cost_hub.get_recommendation(recommendationId=rec["recommendationId"])
                recommendations.append(detailed)
            except Exception:
                recommendations.append(rec)

        while response.get("nextToken"):
            response = cost_hub.list_recommendations(
                filter={"regions": [ctx.region]},
                nextToken=response["nextToken"],
                maxResults=100,
            )
            for rec in response.get("items", []):
                try:
                    detailed = cost_hub.get_recommendation(recommendationId=rec["recommendationId"])
                    recommendations.append(detailed)
                except Exception:
                    recommendations.append(rec)

    except Exception as e:
        print(f"Warning: Cost Optimization Hub error: {e}")
    return recommendations


def get_ec2_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get EC2 recommendations from Compute Optimizer."""
    compute_optimizer = ctx.client("compute-optimizer")
    recommendations: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_ec2_instance_recommendations()
        recommendations.extend(response["instanceRecommendations"])

        while response.get("nextToken"):
            response = compute_optimizer.get_ec2_instance_recommendations(nextToken=response["nextToken"])
            recommendations.extend(response["instanceRecommendations"])
    except Exception as e:
        logger.warning("Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            recommendations.append(_compute_optimizer_opt_in_rec("EC2", "rightsizing"))
    return recommendations


def get_ebs_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get EBS recommendations from Compute Optimizer."""
    compute_optimizer = ctx.client("compute-optimizer")
    recommendations: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_ebs_volume_recommendations()
        recommendations.extend(response["volumeRecommendations"])

        while response.get("nextToken"):
            response = compute_optimizer.get_ebs_volume_recommendations(nextToken=response["nextToken"])
            recommendations.extend(response["volumeRecommendations"])
    except Exception as e:
        logger.warning("EBS Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            recommendations.append(_compute_optimizer_opt_in_rec("EBS", "rightsizing"))
    return recommendations


def get_rds_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get RDS recommendations from Compute Optimizer."""
    compute_optimizer = ctx.client("compute-optimizer")
    recommendations: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_rds_database_recommendations()
        recommendations.extend(response["rdsDBRecommendations"])

        while response.get("nextToken"):
            response = compute_optimizer.get_rds_database_recommendations(nextToken=response["nextToken"])
            recommendations.extend(response["rdsDBRecommendations"])
    except Exception as e:
        logger.warning("RDS Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            recommendations.append(_compute_optimizer_opt_in_rec("RDS", "rightsizing"))
    return recommendations

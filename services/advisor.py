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

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/advisor.py] Advisor module active")


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
        print(f"Warning: Compute Optimizer not available: {e}")
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            opt_in_recommendation = {
                "ResourceId": "compute-optimizer-service",
                "ResourceType": "Service Configuration",
                "Issue": "AWS Compute Optimizer not enabled",
                "Recommendation": ("Enable AWS Compute Optimizer for EC2 recommendations"),
                "EstimatedMonthlySavings": ("Variable - up to 25% on EC2 instances"),
                "Action": ("Go to AWS Compute Optimizer console and opt-in to receive EC2 rightsizing recommendations"),
                "Priority": "Medium",
                "Service": "Compute Optimizer",
            }
            recommendations.append(opt_in_recommendation)
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
        print(f"Warning: EBS Compute Optimizer not available: {e}")
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            opt_in_recommendation = {
                "ResourceId": "compute-optimizer-service",
                "ResourceType": "Service Configuration",
                "Issue": "AWS Compute Optimizer not enabled",
                "Recommendation": "Enable AWS Compute Optimizer for EBS recommendations",
                "EstimatedMonthlySavings": "Variable - up to 20% on EBS volumes",
                "Action": ("Go to AWS Compute Optimizer console and opt-in to receive EBS rightsizing recommendations"),
                "Priority": "Medium",
                "Service": "Compute Optimizer",
            }
            recommendations.append(opt_in_recommendation)
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
        print(f"Warning: RDS Compute Optimizer not available: {e}")
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            opt_in_recommendation = {
                "ResourceId": "compute-optimizer-service",
                "ResourceType": "Service Configuration",
                "Issue": "AWS Compute Optimizer not enabled",
                "Recommendation": ("Enable AWS Compute Optimizer for RDS recommendations"),
                "EstimatedMonthlySavings": "Variable - up to 25% on RDS instances",
                "Action": ("Go to AWS Compute Optimizer console and opt-in to receive RDS rightsizing recommendations"),
                "Priority": "Medium",
                "Service": "Compute Optimizer",
            }
            recommendations.append(opt_in_recommendation)
    return recommendations

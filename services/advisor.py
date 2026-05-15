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
        logger.info("Cost Optimization Hub unavailable - continuing with other optimization sources")
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
        ctx.warn(f"Cost Optimization Hub error: {e}", service="cost_optimization_hub")
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


def get_lambda_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get Lambda function recommendations from Compute Optimizer.

    Returns normalized recommendation dicts ready for inline rendering in
    the Lambda tab. Returns the opt-in placeholder when CO is not enabled.
    """
    compute_optimizer = ctx.client("compute-optimizer")
    if not compute_optimizer:
        return []
    raw: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_lambda_function_recommendations()
        raw.extend(response.get("lambdaFunctionRecommendations", []))
        while response.get("nextToken"):
            response = compute_optimizer.get_lambda_function_recommendations(nextToken=response["nextToken"])
            raw.extend(response.get("lambdaFunctionRecommendations", []))
    except Exception as e:
        logger.warning("Lambda Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            return [_compute_optimizer_opt_in_rec("Lambda", "memory-rightsizing")]
        return []
    return [_normalize_lambda_co_rec(r, ctx.pricing_multiplier) for r in raw]


def get_ecs_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get ECS service recommendations from Compute Optimizer.

    Returns normalized recommendation dicts ready for inline rendering in
    the Containers tab. Returns the opt-in placeholder when CO is not enabled.
    """
    compute_optimizer = ctx.client("compute-optimizer")
    if not compute_optimizer:
        return []
    raw: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_ecs_service_recommendations()
        raw.extend(response.get("ecsServiceRecommendations", []))
        while response.get("nextToken"):
            response = compute_optimizer.get_ecs_service_recommendations(nextToken=response["nextToken"])
            raw.extend(response.get("ecsServiceRecommendations", []))
    except Exception as e:
        logger.warning("ECS Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            return [_compute_optimizer_opt_in_rec("ECS", "task-rightsizing")]
        return []
    return [_normalize_ecs_co_rec(r, ctx.pricing_multiplier) for r in raw]


def get_asg_compute_optimizer_recommendations(
    ctx: ScanContext,
) -> list[dict[str, Any]]:
    """Get Auto Scaling Group recommendations from Compute Optimizer.

    Returns normalized recommendation dicts ready for inline rendering in
    the EC2 tab. Returns the opt-in placeholder when CO is not enabled.
    """
    compute_optimizer = ctx.client("compute-optimizer")
    if not compute_optimizer:
        return []
    raw: list[dict[str, Any]] = []
    try:
        response = compute_optimizer.get_auto_scaling_group_recommendations()
        raw.extend(response.get("autoScalingGroupRecommendations", []))
        while response.get("nextToken"):
            response = compute_optimizer.get_auto_scaling_group_recommendations(nextToken=response["nextToken"])
            raw.extend(response.get("autoScalingGroupRecommendations", []))
    except Exception as e:
        logger.warning("ASG Compute Optimizer not available: %s", e)
        if "OptInRequiredException" in str(e) or "not registered" in str(e):
            return [_compute_optimizer_opt_in_rec("Auto Scaling Group", "instance-rightsizing")]
        return []
    return [_normalize_asg_co_rec(r, ctx.pricing_multiplier) for r in raw]


def _normalize_lambda_co_rec(raw: dict[str, Any], pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Normalize a raw Lambda Compute Optimizer recommendation.

    AWS Compute Optimizer returns estimated savings already priced in the
    resource's home region. ``pricing_multiplier`` is accepted for caller
    back-compat but NOT applied to the savings value — doing so would
    double-count the regional adjustment.
    """
    _ = pricing_multiplier  # See docstring; intentionally unused.
    savings = 0.0
    for opt in raw.get("memorySizeRecommendationOptions", []):
        if "savingsOpportunity" in opt:
            savings = opt["savingsOpportunity"].get("estimatedMonthlySavings", {}).get("value", 0.0)
            break

    arn = raw.get("functionArn", "")
    fn_part = arn.split(":function:")[-1] if ":function:" in arn else arn
    fn_name = fn_part.split(":")[0] or arn

    recommended_memory = 0
    options = raw.get("memorySizeRecommendationOptions", [])
    if options:
        recommended_memory = options[0].get("memorySize", 0)

    return {
        "resource_id": fn_name,
        "resource_name": fn_name,
        "resource_type": "Lambda Function",
        "finding": raw.get("finding", ""),
        "current_config": {
            "memorySize": raw.get("currentMemorySize", 0),
            "runtime": raw.get("currentExecutionType", ""),
        },
        "recommended_config": {"memorySize": recommended_memory},
        "estimatedMonthlySavings": round(savings, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


def _normalize_ecs_co_rec(raw: dict[str, Any], pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Normalize a raw ECS Compute Optimizer recommendation.

    AWS CO savings are region-priced upstream; ``pricing_multiplier`` is
    accepted for back-compat but not applied (see ``_normalize_lambda_co_rec``).
    """
    _ = pricing_multiplier
    savings = 0.0
    service_options = raw.get("serviceRecommendationOptions", [])
    if service_options:
        savings = service_options[0].get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0.0)

    svc_id = raw.get("serviceArn", "").split(":service/")[-1] or raw.get("serviceArn", "")
    svc_name = svc_id.split("/")[-1] if "/" in svc_id else svc_id
    return {
        "resource_id": svc_id,
        "resource_name": svc_name,
        "resource_type": "ECS Service",
        "finding": raw.get("finding", ""),
        "current_config": raw.get("currentServiceConfiguration", {}),
        "recommended_config": service_options[0].get("containerRecommendations", []) if service_options else [],
        "estimatedMonthlySavings": round(savings, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


def _normalize_asg_co_rec(raw: dict[str, Any], pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Normalize a raw ASG Compute Optimizer recommendation.

    AWS CO savings are region-priced upstream; ``pricing_multiplier`` is
    accepted for back-compat but not applied (see ``_normalize_lambda_co_rec``).
    """
    _ = pricing_multiplier
    savings = 0.0
    options = raw.get("instanceRecommendationOptions", [])
    if options:
        savings = options[0].get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0.0)

    asg_arn = raw.get("autoScalingGroupArn", "")
    asg_name = raw.get("autoScalingGroupName", "") or (
        asg_arn.split("autoScalingGroupName/")[-1] if "autoScalingGroupName/" in asg_arn else asg_arn
    )
    return {
        "resource_id": asg_name,
        "resource_name": asg_name,
        "resource_type": "Auto Scaling Group",
        "finding": raw.get("finding", ""),
        "current_config": {
            "instanceType": raw.get("currentConfiguration", {}).get("instanceType", ""),
            "desiredCapacity": raw.get("currentConfiguration", {}).get("desiredCapacity", 0),
        },
        "recommended_config": {"instanceType": options[0].get("configuration", {}).get("instanceType", "")}
        if options
        else {},
        "estimatedMonthlySavings": round(savings, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }

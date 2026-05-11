"""Multi-source adapter for Compute Optimizer covering EBS, Lambda, ECS, and ASG.

Deliberately excludes EC2 instance recommendations to avoid double-counting
with the existing EC2 adapter (`services/adapters/ec2.py`), which already
consumes `get_ec2_compute_optimizer_recommendations()` from `services/advisor.py`.

Covers:
    - EBS volume recommendations (reuses advisor.py helper)
    - Lambda function recommendations (direct API call)
    - ECS service recommendations (direct API call)
    - Auto Scaling Group recommendations (direct API call)

Uses the AWS Compute Optimizer API which provides ML-driven rightsizing
recommendations based on 14-day CloudWatch utilization data. Compute
Optimizer has its own ML engine, so this adapter sets
``requires_cloudwatch = False``.
"""

from __future__ import annotations

from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule
from services.advisor import get_ebs_compute_optimizer_recommendations


def _fetch_lambda_recommendations(client: Any) -> list[dict[str, Any]]:
    """Fetch Lambda function recommendations from Compute Optimizer with pagination.

    Args:
        client: boto3 compute-optimizer client.

    Returns:
        List of Lambda function recommendation dicts.
    """
    recs: list[dict[str, Any]] = []
    try:
        response = client.get_lambda_function_recommendations()
        recs.extend(response.get("lambdaFunctionRecommendations", []))
        while response.get("nextToken"):
            response = client.get_lambda_function_recommendations(nextToken=response["nextToken"])
            recs.extend(response.get("lambdaFunctionRecommendations", []))
    except Exception as e:
        print(f"Warning: Lambda Compute Optimizer not available: {e}")
    return recs


def _fetch_ecs_recommendations(client: Any) -> list[dict[str, Any]]:
    """Fetch ECS service recommendations from Compute Optimizer with pagination.

    Args:
        client: boto3 compute-optimizer client.

    Returns:
        List of ECS service recommendation dicts.
    """
    recs: list[dict[str, Any]] = []
    try:
        response = client.get_ecs_service_recommendations()
        recs.extend(response.get("ecsServiceRecommendations", []))
        while response.get("nextToken"):
            response = client.get_ecs_service_recommendations(nextToken=response["nextToken"])
            recs.extend(response.get("ecsServiceRecommendations", []))
    except Exception as e:
        print(f"Warning: ECS Compute Optimizer not available: {e}")
    return recs


def _fetch_asg_recommendations(client: Any) -> list[dict[str, Any]]:
    """Fetch Auto Scaling Group recommendations from Compute Optimizer with pagination.

    Args:
        client: boto3 compute-optimizer client.

    Returns:
        List of ASG recommendation dicts.
    """
    recs: list[dict[str, Any]] = []
    try:
        response = client.get_auto_scaling_group_recommendations()
        recs.extend(response.get("autoScalingGroupRecommendations", []))
        while response.get("nextToken"):
            response = client.get_auto_scaling_group_recommendations(nextToken=response["nextToken"])
            recs.extend(response.get("autoScalingGroupRecommendations", []))
    except Exception as e:
        print(f"Warning: ASG Compute Optimizer not available: {e}")
    return recs


def _normalize_lambda_rec(raw: dict[str, Any], pricing_multiplier: float) -> dict[str, Any]:
    """Normalize a raw Lambda Compute Optimizer recommendation into canonical form.

    Args:
        raw: Raw recommendation dict from the Compute Optimizer API.
        pricing_multiplier: Regional pricing multiplier from ScanContext.

    Returns:
        Normalized recommendation dict with standard keys.
    """
    savings = 0.0
    for opt in raw.get("utilizationImprovementMetrics", []):
        if "estimatedMonthlySavings" in opt:
            savings = opt["estimatedMonthlySavings"].get("value", 0.0)
            break
    for opt in raw.get("memorySizeRecommendationOptions", []):
        if "savingsOpportunity" in opt:
            savings = opt["savingsOpportunity"].get("estimatedMonthlySavings", {}).get("value", savings)
            break

    arn = raw.get("functionArn", "")
    fn_part = arn.split(":function:")[-1] if ":function:" in arn else arn
    fn_name = fn_part.split(":")[0] or arn  # strip :$LATEST or :version-number qualifier

    return {
        "resource_id": fn_name,
        "resource_name": fn_name,
        "resource_type": "Lambda Function",
        "finding": raw.get("finding", ""),
        "current_config": {
            "memorySize": raw.get("currentMemorySize", 0),
            "runtime": raw.get("currentExecutionType", ""),
        },
        "recommended_config": {"memorySize": raw.get("memorySizeRecommendationOptions", [{}])[0].get("memorySize", 0)}
        if raw.get("memorySizeRecommendationOptions")
        else {},
        "estimatedMonthlySavings": round(savings * pricing_multiplier, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


def _normalize_ebs_rec(raw: dict[str, Any], pricing_multiplier: float) -> dict[str, Any]:
    """Normalize a raw EBS Compute Optimizer recommendation into canonical form.

    Args:
        raw: Raw recommendation dict from the Compute Optimizer API.
        pricing_multiplier: Regional pricing multiplier from ScanContext.

    Returns:
        Normalized recommendation dict with standard keys.
    """
    savings = 0.0
    volume_options = raw.get("volumeRecommendationOptions", [])
    if volume_options:
        savings = volume_options[0].get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0.0)

    volume_id = raw.get("volumeArn", "").split(":volume/")[-1] or raw.get("volumeArn", "")
    return {
        "resource_id": volume_id,
        "resource_name": volume_id,
        "resource_type": "EBS Volume",
        "finding": raw.get("finding", ""),
        "current_config": {
            "volumeType": raw.get("currentConfiguration", {}).get("volumeType", ""),
            "volumeSize": raw.get("currentConfiguration", {}).get("volumeSize", 0),
        },
        "recommended_config": {
            "volumeType": volume_options[0].get("configuration", {}).get("volumeType", ""),
            "volumeSize": volume_options[0].get("configuration", {}).get("volumeSize", 0),
        }
        if volume_options
        else {},
        "estimatedMonthlySavings": round(savings * pricing_multiplier, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


def _normalize_ecs_rec(raw: dict[str, Any], pricing_multiplier: float) -> dict[str, Any]:
    """Normalize a raw ECS Compute Optimizer recommendation into canonical form.

    Args:
        raw: Raw recommendation dict from the Compute Optimizer API.
        pricing_multiplier: Regional pricing multiplier from ScanContext.

    Returns:
        Normalized recommendation dict with standard keys.
    """
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
        "estimatedMonthlySavings": round(savings * pricing_multiplier, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


def _normalize_asg_rec(raw: dict[str, Any], pricing_multiplier: float) -> dict[str, Any]:
    """Normalize a raw ASG Compute Optimizer recommendation into canonical form.

    Args:
        raw: Raw recommendation dict from the Compute Optimizer API.
        pricing_multiplier: Regional pricing multiplier from ScanContext.

    Returns:
        Normalized recommendation dict with standard keys.
    """
    savings = 0.0
    options = raw.get("instanceRecommendationOptions", [])
    if options:
        savings = options[0].get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0.0)

    asg_name = raw.get("autoScalingGroupName", "") or (
        raw.get("autoScalingGroupArn", "").split("autoScalingGroupName/")[-1]
        if "autoScalingGroupName/" in raw.get("autoScalingGroupArn", "")
        else raw.get("autoScalingGroupArn", "")
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
        "estimatedMonthlySavings": round(savings * pricing_multiplier, 2),
        "lookback_period_days": raw.get("lookBackPeriodInDays", 14),
    }


class ComputeOptimizerModule(BaseServiceModule):
    """ServiceModule adapter for AWS Compute Optimizer.

    Covers EBS, Lambda, ECS, and Auto Scaling Group rightsizing
    recommendations. EC2 instance recommendations are intentionally
    excluded to avoid double-counting with the EC2 adapter which
    already calls ``get_ec2_compute_optimizer_recommendations()``
    via ``services/advisor.py``.
    """

    key: str = "compute_optimizer"
    cli_aliases: tuple[str, ...] = ("compute_optimizer", "co")
    display_name: str = "Compute Optimizer"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="EBS Recommendations", source_path="sources.ebs_recommendations.count", formatter="int"),
        StatCardSpec(
            label="Lambda Recommendations", source_path="sources.lambda_recommendations.count", formatter="int"
        ),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="resource_type", label_path="resource_type")

    requires_cloudwatch: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Compute Optimizer scanning."""
        return ("compute-optimizer",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Compute Optimizer for EBS, Lambda, ECS, and ASG recommendations.

        EC2 recommendations are skipped to avoid double-counting with the
        dedicated EC2 adapter. When Compute Optimizer is not opted in, returns
        an empty ServiceFindings with ``opt_in_required=True`` in extras.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with ebs_recommendations, lambda_recommendations,
            ecs_recommendations, asg_recommendations, and summary sources.
        """
        print("\U0001f50d [services/adapters/compute_optimizer.py] Compute Optimizer module active")

        multiplier = ctx.pricing_multiplier
        client = ctx.client("compute-optimizer")
        opt_in_required = False

        if not client:
            return ServiceFindings(
                service_name="Compute Optimizer",
                total_recommendations=0,
                total_monthly_savings=0.0,
                sources={},
                extras={"opt_in_required": True, "resource_counts": {}},
            )

        # -- EBS recommendations (reuses advisor.py helper) ------------------
        try:
            raw_ebs = get_ebs_compute_optimizer_recommendations(ctx)
        except Exception as e:
            print(f"Warning: EBS Compute Optimizer error: {e}")
            raw_ebs = []

        # Check for opt-in requirement sentinel from advisor.py
        if len(raw_ebs) == 1 and raw_ebs[0].get("ResourceId") == "compute-optimizer-service":
            opt_in_required = True
            raw_ebs = []

        # -- Lambda recommendations -------------------------------------------
        raw_lambda: list[dict[str, Any]] = []
        try:
            raw_lambda = _fetch_lambda_recommendations(client)
        except Exception as e:
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_required = True
            print(f"Warning: Lambda Compute Optimizer error: {e}")

        # -- ECS recommendations ----------------------------------------------
        raw_ecs: list[dict[str, Any]] = []
        try:
            raw_ecs = _fetch_ecs_recommendations(client)
        except Exception as e:
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_required = True
            print(f"Warning: ECS Compute Optimizer error: {e}")

        # -- ASG recommendations ----------------------------------------------
        raw_asg: list[dict[str, Any]] = []
        try:
            raw_asg = _fetch_asg_recommendations(client)
        except Exception as e:
            if "OptInRequiredException" in str(e) or "not registered" in str(e):
                opt_in_required = True
            print(f"Warning: ASG Compute Optimizer error: {e}")

        # -- Normalize recommendations ----------------------------------------
        ebs_recs = [_normalize_ebs_rec(r, multiplier) for r in raw_ebs]
        lambda_recs = [_normalize_lambda_rec(r, multiplier) for r in raw_lambda]
        ecs_recs = [_normalize_ecs_rec(r, multiplier) for r in raw_ecs]
        asg_recs = [_normalize_asg_rec(r, multiplier) for r in raw_asg]

        ebs_savings = sum(r["estimatedMonthlySavings"] for r in ebs_recs)
        lambda_savings = sum(r["estimatedMonthlySavings"] for r in lambda_recs)
        ecs_savings = sum(r["estimatedMonthlySavings"] for r in ecs_recs)
        asg_savings = sum(r["estimatedMonthlySavings"] for r in asg_recs)
        total_savings = ebs_savings + lambda_savings + ecs_savings + asg_savings
        total_recs = len(ebs_recs) + len(lambda_recs) + len(ecs_recs) + len(asg_recs)

        finding_types: dict[str, int] = {}
        for rec in ebs_recs + lambda_recs + ecs_recs + asg_recs:
            ft = rec.get("finding", "Unknown")
            finding_types[ft] = finding_types.get(ft, 0) + 1

        resource_counts = {
            "ebs": len(ebs_recs),
            "lambda": len(lambda_recs),
            "ecs": len(ecs_recs),
            "asg": len(asg_recs),
        }

        return ServiceFindings(
            service_name="Compute Optimizer",
            total_recommendations=total_recs,
            total_monthly_savings=round(total_savings, 2),
            sources={
                "ebs_recommendations": SourceBlock(
                    count=len(ebs_recs),
                    recommendations=tuple(ebs_recs),
                    extras={
                        "finding_types": {
                            k: v for k, v in finding_types.items() if k in {r["finding"] for r in ebs_recs}
                        }
                    },
                ),
                "lambda_recommendations": SourceBlock(
                    count=len(lambda_recs),
                    recommendations=tuple(lambda_recs),
                    extras={
                        "finding_types": {
                            k: v for k, v in finding_types.items() if k in {r["finding"] for r in lambda_recs}
                        }
                    },
                ),
                "ecs_recommendations": SourceBlock(
                    count=len(ecs_recs),
                    recommendations=tuple(ecs_recs),
                ),
                "asg_recommendations": SourceBlock(
                    count=len(asg_recs),
                    recommendations=tuple(asg_recs),
                ),
                "summary": SourceBlock(
                    count=0,
                    recommendations=(),
                    extras={
                        "is_aggregate": True,
                        "note": "Aggregate summary — no individual recommendations; see per-resource sources for details.",
                        "total_monthly_savings": round(total_savings, 2),
                        "savings_by_resource": {
                            "ebs": round(ebs_savings, 2),
                            "lambda": round(lambda_savings, 2),
                            "ecs": round(ecs_savings, 2),
                            "asg": round(asg_savings, 2),
                        },
                        "finding_types": finding_types,
                    },
                ),
            },
            extras={
                "opt_in_required": opt_in_required,
                "resource_counts": resource_counts,
                "ebs_count": len(ebs_recs),
                "lambda_count": len(lambda_recs),
                "ecs_count": len(ecs_recs),
                "asg_count": len(asg_recs),
            },
        )

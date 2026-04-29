"""App Runner cost optimization checks.

Extracted from CostOptimizer.get_enhanced_apprunner_checks() as a free function.
This module will later become AppRunnerModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("\U0001f50d [services/apprunner.py] AppRunner module active")

APPRUNNER_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "auto_scaling_optimization": {
        "title": "Optimize App Runner Auto Scaling",
        "description": "Review auto-scaling settings and concurrency limits for cost efficiency.",
        "action": "Optimize auto-scaling configuration",
    }
}


def get_enhanced_apprunner_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced App Runner cost optimization checks"""
    print("\U0001f50d [services/apprunner.py] AppRunner module active")
    checks: dict[str, list[dict[str, Any]]] = {
        "auto_scaling_optimization": [],
        "instance_rightsizing": [],
        "unused_services": [],
    }

    try:
        response = ctx.client("apprunner").list_services()
        services = response.get("ServiceSummaryList", [])

        for service in services:
            service_name = service.get("ServiceName")
            status = service.get("Status")

            if status == "RUNNING":
                service_arn = service.get("ServiceArn")

                try:
                    service_details = ctx.client("apprunner").describe_service(ServiceArn=service_arn)
                    service_config = service_details.get("Service", {})
                    instance_config = service_config.get("InstanceConfiguration", {})

                    instance_type = instance_config.get("InstanceRoleArn", "")  # noqa: F841
                    if "large" in str(instance_config) or instance_config.get("Memory", "1 GB") != "1 GB":
                        checks["auto_scaling_optimization"].append(
                            {
                                "ServiceName": service_name,
                                "Recommendation": "Review auto-scaling settings and instance configuration for cost optimization",  # noqa: E501
                                "EstimatedSavings": "$30/month potential",
                                "CheckCategory": "Auto Scaling Optimization",
                                "Note": "Monitor actual CPU and memory usage before adjusting",
                            }
                        )
                except Exception:
                    pass

    except Exception as e:
        ctx.warn(f"Could not analyze App Runner resources: {e}", service="apprunner")

    all_recommendations: list[dict[str, Any]] = []
    for category, recs in checks.items():  # noqa: B007
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, "checks": checks}

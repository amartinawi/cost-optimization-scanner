"""API Gateway cost optimization checks.

Extracted from CostOptimizer.get_enhanced_api_gateway_checks() as a free function.
This module will later become ApiGatewayModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

from core.scan_context import ScanContext

REST_PER_M = 3.50
HTTP_PER_M = 1.00
SAVINGS_PER_M = REST_PER_M - HTTP_PER_M

print("🔍 [services/api_gateway.py] API Gateway module active")

API_GATEWAY_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "rest_vs_http": {
        "title": "Migrate Simple REST APIs to HTTP API",
        "description": "REST APIs with ≤10 resources can migrate to cheaper HTTP APIs for 10-30% cost reduction.",
        "action": "Review simple REST APIs and migrate to HTTP API where feature compatibility allows",
    },
    "unused_stages": {
        "title": "Remove Unused API Gateway Stages",
        "description": "Unused stages incur ongoing costs and should be cleaned up.",
        "action": "Review and delete stages that are no longer in use",
    },
    "caching_opportunities": {
        "title": "Enable API Gateway Caching",
        "description": "Stages without cache clusters generate unnecessary backend calls that increase costs.",
        "action": "Enable caching on stages with repetitive request patterns",
    },
    "throttling_optimization": {
        "title": "Optimize API Gateway Throttling",
        "description": "Proper throttling configuration prevents cost spikes from uncontrolled traffic.",
        "action": "Review and configure appropriate throttling limits",
    },
    "request_validation": {
        "title": "Review Request Validation Cost Impact",
        "description": "Request validation can be tuned to reduce processing costs.",
        "action": "Optimize request validation configurations for cost efficiency",
    },
}


def get_enhanced_api_gateway_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced API Gateway cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "rest_vs_http": [],
        "unused_stages": [],
        "caching_opportunities": [],
        "throttling_optimization": [],
        "request_validation": [],
    }

    try:
        apigateway = ctx.client("apigateway")
        paginator = apigateway.get_paginator("get_rest_apis")
        for page in paginator.paginate():
            for api in page.get("items", []):
                api_id = api.get("id")
                api_name = api.get("name", "Unknown")

                try:
                    resources = apigateway.get_resources(restApiId=api_id)
                    resource_count = len(resources.get("items", []))

                    if resource_count <= 10:
                        monthly_requests = 0.0
                        if not ctx.fast_mode:
                            try:
                                from datetime import datetime

                                cw = ctx.client("cloudwatch")
                                end = datetime.now(timezone.utc)
                                start = end - timedelta(days=30)
                                resp = cw.get_metric_statistics(
                                    Namespace="AWS/ApiGateway",
                                    MetricName="Count",
                                    Dimensions=[{"Name": "ApiName", "Value": api_name}],
                                    StartTime=start,
                                    EndTime=end,
                                    Period=2592000,
                                    Statistics=["Sum"],
                                )
                                monthly_requests = sum(dp["Sum"] for dp in resp.get("Datapoints", []))
                            except Exception:
                                monthly_requests = 0.0

                        estimated_savings = (
                            (monthly_requests / 1_000_000) * SAVINGS_PER_M if monthly_requests > 0 else 0.0
                        )

                        checks["rest_vs_http"].append(
                            {
                                "ApiId": api_id,
                                "ApiName": api_name,
                                "ApiType": "REST",
                                "ResourceCount": resource_count,
                                "Recommendation": "Simple API - consider migrating to HTTP API for lower cost",
                                "EstimatedSavings": "10-30% cost reduction for simple APIs",
                                "EstimatedMonthlySavings": estimated_savings,
                                "MonthlyRequests": monthly_requests,
                                "CheckCategory": "API Gateway Type Optimization",
                            }
                        )
                except Exception:
                    pass

                # API Gateway Caching finding removed: "Reduced backend costs" with no
                # quantification — caching actually adds API Gateway cost ($0.020-$3.80/hr
                # by cache size); whether net savings exist depends on backend pricing
                # not measured here.
                _ = api_id

    except Exception as e:
        ctx.warn(f"Could not perform API Gateway checks: {e}", "api_gateway")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}

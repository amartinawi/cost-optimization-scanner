"""CloudFront cost optimization checks.

Extracted from CostOptimizer.get_enhanced_cloudfront_checks() as a free function.
This module will later become CloudfrontModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/cloudfront.py] CloudFront module active")


def get_enhanced_cloudfront_checks(ctx: ScanContext) -> dict[str, Any]:
    """Enhanced CloudFront cost optimization checks with traffic-based gating.

    Analyzes CloudFront distributions for cost optimization opportunities:
    - Price class optimization (only for active distributions with >1000 requests/week)
    - Low traffic distribution identification
    - Origin Shield necessity analysis
    - Geographic distribution analysis for price class recommendations

    Uses CloudWatch request metrics for intelligent gating:
    - Only suggests price class changes for distributions with significant traffic
    - Analyzes 7-day request patterns to validate activity
    - Prevents recommendations for inactive or low-traffic distributions
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "price_class_optimization": [],
        "low_traffic_distributions": [],
        "origin_shield_unnecessary": [],
    }

    try:
        cloudfront = ctx.client("cloudfront")
        paginator = cloudfront.get_paginator("list_distributions")
        for page in paginator.paginate():
            for dist in page.get("DistributionList", {}).get("Items", []):
                dist_id = dist.get("Id")
                domain_name = dist.get("DomainName", "Unknown")
                price_class = dist.get("PriceClass", "PriceClass_All")
                status = dist.get("Status", "Unknown")
                enabled = dist.get("Enabled", True)

                if price_class == "PriceClass_All" and enabled:
                    try:
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=7)

                        cloudwatch = ctx.client("cloudwatch")
                        request_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/CloudFront",
                            MetricName="Requests",
                            Dimensions=[{"Name": "DistributionId", "Value": dist_id}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=["Sum"],
                        )

                        total_requests = sum(dp["Sum"] for dp in request_metrics.get("Datapoints", []))

                        if total_requests > 1000:
                            checks["price_class_optimization"].append(
                                {
                                    "DistributionId": dist_id,
                                    "DomainName": domain_name,
                                    "Status": status,
                                    "CurrentPriceClass": price_class,
                                    "WeeklyRequests": f"{total_requests:.0f}",
                                    "Recommendation": (
                                        f"Active distribution ({total_requests:.0f} requests/week)"
                                        " - consider PriceClass_100/200 if users are regional"
                                    ),
                                    "EstimatedSavings": "20-50% on data transfer costs for regional traffic",
                                    "CheckCategory": "CloudFront Price Class Optimization",
                                }
                            )
                    except Exception:
                        pass

                if not enabled:
                    checks["low_traffic_distributions"].append(
                        {
                            "DistributionId": dist_id,
                            "DomainName": domain_name,
                            "Status": status,
                            "PriceClass": price_class,
                            "Enabled": enabled,
                            "Recommendation": "Disabled distribution - consider deletion to eliminate costs",
                            "EstimatedSavings": "100% of distribution costs",
                            "CheckCategory": "CloudFront Unused Distribution",
                        }
                    )

                try:
                    dist_config = cloudfront.get_distribution_config(Id=dist_id)
                    origins = dist_config.get("DistributionConfig", {}).get("Origins", {}).get("Items", [])

                    for origin in origins:
                        origin_shield = origin.get("OriginShield", {})
                        if origin_shield.get("Enabled", False):
                            checks["origin_shield_unnecessary"].append(
                                {
                                    "DistributionId": dist_id,
                                    "DomainName": domain_name,
                                    "Status": status,
                                    "PriceClass": price_class,
                                    "OriginShieldRegion": origin_shield.get("OriginShieldRegion", "Unknown"),
                                    "Recommendation": (
                                        "Origin Shield adds costs - review necessity for traffic patterns"
                                    ),
                                    "EstimatedSavings": "Variable based on cache hit improvement vs additional costs",
                                    "CheckCategory": "CloudFront Origin Shield Review",
                                }
                            )
                except Exception as e:
                    ctx.warn(f"Error analyzing CloudFront distribution {dist_id}: {e}", "cloudfront")

    except Exception as e:
        ctx.warn(f"Could not perform CloudFront checks: {e}", "cloudfront")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}

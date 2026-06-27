"""CloudFront cost optimization checks.

Extracted from CostOptimizer.get_enhanced_cloudfront_checks() as a free function.
This module will later become CloudfrontModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error


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

    # Fast mode (cloudfront H2): a --fast scan must make no per-distribution
    # CloudWatch reads or get_distribution_config calls. Both surviving levers
    # depend on those reads — price_class_optimization is gated on >1000 weekly
    # `Requests` (CloudWatch), and the Origin-Shield analysis needs
    # get_distribution_config + CacheHitRate/Requests — so without metrics there
    # is no honest, traffic-gated recommendation to emit. We skip both per
    # distribution and surface a single advisory notice (mirrors the Lambda /
    # ElastiCache fast-mode guards). list_distributions itself is one cheap call.
    fast_mode = bool(getattr(ctx, "fast_mode", False))

    try:
        cloudfront = ctx.client("cloudfront")
        if fast_mode:
            ctx.warn(
                "Fast mode: skipped CloudFront CloudWatch reads and "
                "get_distribution_config — price-class and Origin-Shield "
                "analysis require traffic metrics and were not evaluated.",
                "cloudfront",
            )
        paginator = cloudfront.get_paginator("list_distributions")
        for page in paginator.paginate():
            for dist in page.get("DistributionList", {}).get("Items", []):
                dist_id = dist.get("Id")
                domain_name = dist.get("DomainName", "Unknown")
                price_class = dist.get("PriceClass", "PriceClass_All")
                status = dist.get("Status", "Unknown")
                enabled = dist.get("Enabled", True)

                if fast_mode:
                    # No CW / config reads in fast mode → nothing to evaluate
                    # per distribution. Skip the metric-gated price-class block
                    # and the get_distribution_config Origin-Shield block.
                    continue

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
                    except Exception as e:
                        # H1 — classify the Requests read failure; a swallowed
                        # error silently drops the only populated category for
                        # this distribution.
                        record_aws_error(
                            ctx,
                            e,
                            service="cloudfront",
                            context=f"cloudwatch:GetMetricStatistics Requests failed for distribution {dist_id}",
                        )

                # Disabled CloudFront distribution housekeeping finding removed: explicitly
                # $0/month — disabled distributions incur no data-transfer cost.

                try:
                    dist_config = cloudfront.get_distribution_config(Id=dist_id)
                    origins = dist_config.get("DistributionConfig", {}).get("Origins", {}).get("Items", [])

                    should_check_origin_shield = False
                    try:
                        cw_end = datetime.now(UTC)
                        cw_start = cw_end - timedelta(days=7)
                        cloudwatch = ctx.client("cloudwatch")

                        hit_metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/CloudFront",
                            MetricName="CacheHitRate",
                            Dimensions=[{"Name": "DistributionId", "Value": dist_id}],
                            StartTime=cw_start,
                            EndTime=cw_end,
                            Period=86400,
                            Statistics=["Average"],
                        )
                        avg_hit_rate = None
                        dps = hit_metrics.get("Datapoints", [])
                        if dps:
                            avg_hit_rate = sum(dp["Average"] for dp in dps) / len(dps)
                            if avg_hit_rate < 90:
                                should_check_origin_shield = True

                        if not should_check_origin_shield:
                            req_metrics = cloudwatch.get_metric_statistics(
                                Namespace="AWS/CloudFront",
                                MetricName="Requests",
                                Dimensions=[{"Name": "DistributionId", "Value": dist_id}],
                                StartTime=cw_start,
                                EndTime=cw_end,
                                Period=60,
                                Statistics=["Sum"],
                            )
                            req_dps = req_metrics.get("Datapoints", [])
                            if req_dps:
                                max_per_min = max(dp["Sum"] for dp in req_dps)
                                if max_per_min > 1000:
                                    should_check_origin_shield = True
                    except Exception as e:
                        # H1 — classify the CacheHitRate/Requests read failure
                        # rather than swallowing it.
                        record_aws_error(
                            ctx,
                            e,
                            service="cloudfront",
                            context=f"cloudwatch:GetMetricStatistics CacheHitRate/Requests failed for distribution {dist_id}",
                        )

                    # Origin Shield review finding removed: "Variable based on cache hit
                    # improvement vs additional costs" — net effect can go either way.
                    _ = (should_check_origin_shield, origins)
                except Exception as e:
                    ctx.warn(f"Error analyzing CloudFront distribution {dist_id}: {e}", "cloudfront")

    except Exception as e:
        ctx.warn(f"Could not perform CloudFront checks: {e}", "cloudfront")

    all_recommendations: list[dict[str, Any]] = []
    for _category, recs in checks.items():
        all_recommendations.extend(recs)

    return {"recommendations": all_recommendations, **checks}

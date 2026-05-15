"""CloudWatch and CloudTrail cost optimization checks.

Extracted from CostOptimizer.get_cloudwatch_checks() and
CostOptimizer.get_cloudtrail_checks() as free functions.
This module will later become MonitoringModule (T-322) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

print("🔍 [services/monitoring.py] Monitoring module active")

# CloudWatch Logs storage list price (us-east-1): $0.03/GB-month
# Source: AWS Pricing API SKU JRHJQ2UMPUB5K73A (verified 2026-05).
# Region-scaled via `pricing_multiplier` at the per-rec emit site.
CW_LOGS_GB_MONTH: float = 0.03

# CloudWatch custom metrics tiered pricing (us-east-1):
#   First 10,000 metrics:  $0.30/metric/month
#   Next 240,000:          $0.10/metric/month
#   Above 250,000:         $0.05/metric/month
# Source: https://aws.amazon.com/cloudwatch/pricing/ (verified 2026-05).
CW_CUSTOM_METRIC_TIER_1: float = 0.30
CW_CUSTOM_METRIC_TIER_2: float = 0.10
CW_CUSTOM_METRIC_TIER_3: float = 0.05
CW_CUSTOM_METRIC_TIER_1_LIMIT: int = 10_000
CW_CUSTOM_METRIC_TIER_2_LIMIT: int = 250_000


def _cw_custom_metrics_monthly_cost(count: int) -> float:
    """Return CloudWatch custom metrics monthly cost for `count` metrics
    applying AWS-published tiered pricing breakpoints. Region-scaled by
    the caller via `pricing_multiplier`.
    """
    if count <= 0:
        return 0.0
    tier_1 = min(count, CW_CUSTOM_METRIC_TIER_1_LIMIT) * CW_CUSTOM_METRIC_TIER_1
    tier_2 = max(
        0,
        min(count, CW_CUSTOM_METRIC_TIER_2_LIMIT) - CW_CUSTOM_METRIC_TIER_1_LIMIT,
    ) * CW_CUSTOM_METRIC_TIER_2
    tier_3 = max(0, count - CW_CUSTOM_METRIC_TIER_2_LIMIT) * CW_CUSTOM_METRIC_TIER_3
    return tier_1 + tier_2 + tier_3


MONITORING_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "never_expiring_logs": {
        "title": "Set Log Retention Policies",
        "description": "Log groups without retention policies grow indefinitely, increasing storage costs.",
        "action": "Set retention policy (e.g., 30 days) on all log groups",
    },
    "excessive_logging": {
        "title": "Reduce Excessive Log Storage",
        "description": "Large log groups may indicate excessive logging or missing log level controls.",
        "action": "Review log levels and reduce retention for non-essential logs",
    },
    "unused_custom_metrics": {
        "title": "Optimize Custom Metrics",
        "description": "High volumes of custom metrics incur significant monthly charges.",
        "action": "Review and reduce unnecessary custom metrics",
    },
    "unused_alarms": {
        "title": "Clean Up Stale Alarms",
        "description": "Alarms with persistent insufficient data may be monitoring unavailable metrics.",
        "action": "Delete or reconfigure alarms with prolonged insufficient data",
    },
    "multi_region_trails": {
        "title": "Optimize Multi-Region Trails",
        "description": "Multi-region CloudTrail trails replicate events to all regions, increasing costs.",
        "action": "Use single-region trails where multi-region is not required",
    },
    "data_events_all_s3": {
        "title": "Scope S3 Data Events",
        "description": "Data events for all S3 objects generate very high volume and cost.",
        "action": "Limit data events to specific buckets",
    },
    "data_events_all_lambda": {
        "title": "Scope Lambda Data Events",
        "description": "Data events for all Lambda functions increase CloudTrail costs.",
        "action": "Limit data events to specific functions",
    },
}


def get_cloudwatch_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Category 9: CloudWatch optimization checks.

    Args:
        ctx: Scan context with cloudwatch + logs clients.
        pricing_multiplier: Regional pricing multiplier applied to per-rec
            $ values. us-east-1 ≈ 1.0; eu-west-1 ≈ 1.08; etc.
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "never_expiring_logs": [],
        "excessive_logging": [],
        "unused_custom_metrics": [],
        "high_resolution_metrics": [],
        "unused_alarms": [],
        "duplicate_metrics": [],
    }

    try:
        logs = ctx.client("logs")
        log_groups: list[dict[str, Any]] = []
        log_groups_params: dict[str, Any] = {}
        while True:
            log_groups_response = logs.describe_log_groups(**log_groups_params)
            log_groups.extend(log_groups_response.get("logGroups", []))
            next_token = log_groups_response.get("nextToken")
            if not next_token:
                break
            log_groups_params["nextToken"] = next_token

        for log_group in log_groups:
            log_group_name = log_group.get("logGroupName")
            retention_days = log_group.get("retentionInDays")
            stored_bytes = log_group.get("storedBytes", 0)

            if retention_days is None:
                stored_gb = stored_bytes / (1024**3)
                monthly_savings = stored_gb * CW_LOGS_GB_MONTH * pricing_multiplier
                checks["never_expiring_logs"].append(
                    {
                        "LogGroupName": log_group_name,
                        "StoredBytes": stored_bytes,
                        "StoredGB": round(stored_gb, 2),
                        "Recommendation": "Set retention policy to prevent unlimited log growth",
                        "EstimatedSavings": f"${monthly_savings:.2f}/month if 30-day retention enforced",
                        "EstimatedMonthlySavings": round(monthly_savings, 2),
                        "CheckCategory": "Never-Expiring Log Groups",
                    }
                )

            # Excessive log storage finding removed: emitted no concrete $ — "review log
            # level and retention" without quantifying storage savings.

        try:
            cloudwatch = ctx.client("cloudwatch")
            paginator = cloudwatch.get_paginator("describe_alarms")
            for page in paginator.paginate():
                alarms = page.get("MetricAlarms", [])

                for alarm in alarms:
                    alarm_name = alarm.get("AlarmName")
                    state_reason = alarm.get("StateReason", "")
                    alarm_config_updated = alarm.get("AlarmConfigurationUpdatedTimestamp")

                    # Unused CloudWatch Alarms finding removed: health/operational signal
                    # with no EstimatedSavings field — not a cost recommendation.
                    _ = (state_reason, alarm_config_updated, alarm_name)

        except Exception as e:
            print(f"Warning: Could not analyze CloudWatch alarms: {e}")

        try:
            cloudwatch = ctx.client("cloudwatch")
            paginator = cloudwatch.get_paginator("list_metrics")
            metrics: list[dict[str, Any]] = []
            for page in paginator.paginate():
                metrics.extend(page.get("Metrics", []))

            namespace_counts: dict[str, int] = {}
            for metric in metrics:
                namespace = metric.get("Namespace", "")
                if not namespace.startswith("AWS/"):
                    namespace_counts[namespace] = namespace_counts.get(namespace, 0) + 1

            for namespace, count in namespace_counts.items():
                if count > 100:
                    # Tiered AWS pricing — savings = current_cost - cost(50%-reduced count).
                    current_cost = _cw_custom_metrics_monthly_cost(count)
                    reduced_cost = _cw_custom_metrics_monthly_cost(count // 2)
                    monthly_savings = (current_cost - reduced_cost) * pricing_multiplier
                    checks["unused_custom_metrics"].append(
                        {
                            "Namespace": namespace,
                            "MetricCount": count,
                            "Recommendation": f"High number of custom metrics ({count}) - review necessity",
                            "EstimatedSavings": f"${monthly_savings:.2f}/month if reduced by 50%",
                            "EstimatedMonthlySavings": round(monthly_savings, 2),
                            "CheckCategory": "Excessive Custom Metrics",
                        }
                    )

        except Exception as e:
            print(f"Warning: Could not analyze custom metrics: {e}")

    except Exception as e:
        print(f"Warning: Could not perform CloudWatch checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}


def get_cloudtrail_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 10: CloudTrail optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "multi_region_trails": [],
        "data_events_all_s3": [],
        "data_events_all_lambda": [],
        "duplicate_trails": [],
        "expensive_storage_trails": [],
        "unused_insights": [],
    }

    try:
        cloudtrail = ctx.client("cloudtrail")
        trails_response = cloudtrail.describe_trails()
        trails = trails_response.get("trailList", [])

        trail_names: set[str] = set()

        for trail_index, trail in enumerate(trails):
            trail_name = trail.get("Name")
            trail_arn = trail.get("TrailARN")
            is_multi_region = trail.get("IsMultiRegionTrail", False)
            s3_bucket = trail.get("S3BucketName")

            trail_names.add(trail_name)

            # Multi-region CloudTrail finding removed: emitted $0/month with a generic
            # "~90% less" percentage — no concrete per-account savings.
            _ = (is_multi_region, trail_arn, s3_bucket)

            try:
                selectors_response = cloudtrail.get_event_selectors(TrailName=trail_name)
                event_selectors = selectors_response.get("EventSelectors", [])

                for selector in event_selectors:
                    data_resources = selector.get("DataResources", [])

                    for resource in data_resources:
                        resource_type = resource.get("Type")
                        values = resource.get("Values", [])

                        # S3 and Lambda data-events findings removed: each emitted $0/month
                        # with percentage-range savings ("80-95%" / "significant") — no
                        # concrete per-account quantification.
                        _ = (resource_type, values)

            except ClientError as e:
                if e.response["Error"]["Code"] != "TrailNotFoundException":
                    print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")
            except Exception as e:
                print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")

            try:
                insights_response = cloudtrail.get_insight_selectors(TrailName=trail_name)
                insight_selectors = insights_response.get("InsightSelectors", [])

                # CloudTrail Insights finding removed: emitted a generic per-event AWS
                # rate ($0.35/100K) without per-account event-volume math.
                _ = insight_selectors

            except ClientError as e:
                if e.response["Error"]["Code"] != "TrailNotFoundException":
                    print(f"Warning: Could not check insights for {trail_name}: {e}")
            except Exception as e:
                print(f"Warning: Could not check insights for {trail_name}: {e}")

        # Multiple CloudTrail Trails finding removed: emitted $0/month with a generic
        # "consolidate overlapping trails" suggestion — no concrete savings.

    except Exception as e:
        print(f"Warning: Could not perform CloudTrail checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

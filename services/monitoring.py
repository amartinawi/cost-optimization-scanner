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


def get_cloudwatch_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 9: CloudWatch optimization checks"""
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
                checks["never_expiring_logs"].append(
                    {
                        "LogGroupName": log_group_name,
                        "StoredBytes": stored_bytes,
                        "StoredGB": round(stored_bytes / (1024**3), 2),
                        "Recommendation": "Set retention policy to prevent unlimited log growth",
                        "EstimatedSavings": f"${stored_bytes * 0.57 / (1024**3):.2f}/month with 30-day retention",
                        "CheckCategory": "Never-Expiring Log Groups",
                    }
                )

            if stored_bytes > 10 * 1024**3:
                checks["excessive_logging"].append(
                    {
                        "LogGroupName": log_group_name,
                        "StoredGB": round(stored_bytes / (1024**3), 2),
                        "RetentionDays": retention_days,
                        "Recommendation": "Large log group - review log level and retention",
                        "EstimatedSavings": "Reduce log level or retention period",
                        "CheckCategory": "Excessive Log Storage",
                    }
                )

        try:
            cloudwatch = ctx.client("cloudwatch")
            paginator = cloudwatch.get_paginator("describe_alarms")
            for page in paginator.paginate():
                alarms = page.get("MetricAlarms", [])

                for alarm in alarms:
                    alarm_name = alarm.get("AlarmName")
                    state_reason = alarm.get("StateReason", "")
                    alarm_config_updated = alarm.get("AlarmConfigurationUpdatedTimestamp")

                    if "Insufficient Data" in state_reason and alarm_config_updated:
                        if isinstance(alarm_config_updated, str):
                            continue

                        age_days = (datetime.now(UTC) - alarm_config_updated).days
                        if age_days > 7:
                            checks["unused_alarms"].append(
                                {
                                    "AlarmName": alarm_name,
                                    "StateReason": state_reason,
                                    "AgeDays": age_days,
                                    "Recommendation": (
                                        f"Alarm has insufficient data for {age_days} days"
                                        " - review metric availability or delete"
                                    ),
                                    "CheckCategory": "Unused CloudWatch Alarms",
                                }
                            )

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
                    checks["unused_custom_metrics"].append(
                        {
                            "Namespace": namespace,
                            "MetricCount": count,
                            "Recommendation": f"High number of custom metrics ({count}) - review necessity",
                            "EstimatedSavings": f"${count * 0.30:.2f}/month if reduced by 50%",
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

            # First CloudTrail trail in each region is free; only flag additional trails
            if is_multi_region and trail_index > 0:
                checks["multi_region_trails"].append(
                    {
                        "TrailName": trail_name,
                        "TrailARN": trail_arn,
                        "S3Bucket": s3_bucket,
                        "Recommendation": "Multi-region trail - verify if all regions needed",
                        "EstimatedSavings": "$0.00/month — single-region trail costs ~90% less",
                        "CheckCategory": "Multi-Region CloudTrail",
                    }
                )

            try:
                selectors_response = cloudtrail.get_event_selectors(TrailName=trail_name)
                event_selectors = selectors_response.get("EventSelectors", [])

                for selector in event_selectors:
                    data_resources = selector.get("DataResources", [])

                    for resource in data_resources:
                        resource_type = resource.get("Type")
                        values = resource.get("Values", [])

                        if resource_type == "AWS::S3::Object" and "arn:aws:s3:::*/*" in values:
                            checks["data_events_all_s3"].append(
                                {
                                    "TrailName": trail_name,
                                    "ResourceType": resource_type,
                                    "Recommendation": "Data events enabled for all S3 buckets - very expensive",
                                    "EstimatedSavings": "$0.00/month — limit to specific buckets for 80-95% savings",
                                    "CheckCategory": "S3 Data Events All Buckets",
                                }
                            )

                        if resource_type == "AWS::Lambda::Function" and "arn:aws:lambda:*" in str(values):
                            checks["data_events_all_lambda"].append(
                                {
                                    "TrailName": trail_name,
                                    "ResourceType": resource_type,
                                    "Recommendation": "Data events enabled for all Lambda functions - expensive",
                                    "EstimatedSavings": "$0.00/month — limit to specific functions for significant savings",
                                    "CheckCategory": "Lambda Data Events All Functions",
                                }
                            )

            except ClientError as e:
                if e.response["Error"]["Code"] != "TrailNotFoundException":
                    print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")
            except Exception as e:
                print(f"Warning: Could not analyze event selectors for {trail_name}: {e}")

            try:
                insights_response = cloudtrail.get_insight_selectors(TrailName=trail_name)
                insight_selectors = insights_response.get("InsightSelectors", [])

                if insight_selectors:
                    checks["unused_insights"].append(
                        {
                            "TrailName": trail_name,
                            "InsightTypes": [s.get("InsightType") for s in insight_selectors],
                            "Recommendation": "CloudTrail Insights enabled - verify usage and value",
                            "EstimatedSavings": "$0.35 per 100,000 events if unused",
                            "CheckCategory": "CloudTrail Insights",
                        }
                    )

            except ClientError as e:
                if e.response["Error"]["Code"] != "TrailNotFoundException":
                    print(f"Warning: Could not check insights for {trail_name}: {e}")
            except Exception as e:
                print(f"Warning: Could not check insights for {trail_name}: {e}")

        if len(trail_names) > 2:
            checks["duplicate_trails"].append(
                {
                    "TrailCount": len(trail_names),
                    "TrailNames": list(trail_names),
                    "Recommendation": (
                        f"{len(trail_names)} trails detected - review event selectors to avoid duplication"
                    ),
                    "EstimatedSavings": "$0.00/month — consolidate overlapping trails to reduce costs",
                    "CheckCategory": "Multiple CloudTrail Trails",
                }
            )

    except Exception as e:
        print(f"Warning: Could not perform CloudTrail checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

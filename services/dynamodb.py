"""DynamoDB cost optimization checks.

Extracted from CostOptimizer.get_dynamodb_table_analysis(),
get_dynamodb_optimization_descriptions(), and
get_enhanced_dynamodb_checks() as free functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.scan_context import ScanContext

print("\U0001f50d [services/dynamodb.py] DynamoDB module active")

DYNAMODB_CHECK_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "billing_mode_optimization": {
        "title": "Optimize DynamoDB Billing Mode",
        "description": "Choose between Provisioned and On-Demand billing based on traffic patterns.",
        "action": (
            "1. Use Provisioned for predictable, steady workloads\n"
            "2. Use On-Demand for unpredictable, spiky traffic\n"
            "3. Monitor CloudWatch metrics for usage patterns\n"
            "4. Estimated savings: 20-60% with proper mode selection"
        ),
    },
    "capacity_rightsizing": {
        "title": "Rightsize Provisioned Capacity",
        "description": "Adjust read/write capacity units based on actual usage to avoid over-provisioning.",
        "action": (
            "1. Monitor consumed vs provisioned capacity\n"
            "2. Use Auto Scaling for dynamic adjustment\n"
            "3. Reduce unused capacity units\n"
            "4. Estimated savings: 30-70% through rightsizing"
        ),
    },
    "reserved_capacity": {
        "title": "Purchase DynamoDB Reserved Capacity",
        "description": "Save up to 76% on DynamoDB costs with reserved capacity for predictable workloads.",
        "action": (
            "1. Analyze baseline capacity requirements\n"
            "2. Purchase 1-year or 3-year reserved capacity\n"
            "3. Apply to tables with steady usage\n"
            "4. Estimated savings: 53-76% vs On-Demand"
        ),
    },
    "data_lifecycle": {
        "title": "Implement Data Lifecycle Management",
        "description": "Archive or delete old data to reduce storage costs and improve performance.",
        "action": (
            "1. Identify old or unused data\n"
            "2. Implement TTL for automatic expiration\n"
            "3. Archive historical data to S3\n"
            "4. Estimated savings: 40-80% on storage costs"
        ),
    },
    "global_tables_optimization": {
        "title": "Optimize Global Tables Configuration",
        "description": "Review Global Tables setup to ensure cost-effective multi-region replication.",
        "action": (
            "1. Evaluate necessity of each region\n"
            "2. Use consistent read where possible\n"
            "3. Optimize cross-region replication\n"
            "4. Estimated savings: 20-50% on replication costs"
        ),
    },
}

# AWS DynamoDB pricing (us-east-1, verified via Pricing API 2026-05).
# Provisioned: $0.00013/RCU-hr (SKU 4V475Q49DCKGXQZ2), $0.00065/WCU-hr (SKU R6PXMNYCEDGZ2EYN).
# Region-scaled via pricing_multiplier at the per-rec emit site.
_PROVISIONED_RCU_COST: float = 0.00013 * 730  # = $0.0949/RCU-month
_PROVISIONED_WCU_COST: float = 0.00065 * 730  # = $0.4745/WCU-month

# On-demand: AWS lists per *million* request units ($0.125/M reads,
# $0.625/M writes). Per-unit values were 10x too high prior to fix.
_ON_DEMAND_RCU_PER_REQUEST: float = 0.125 / 1_000_000  # = $0.000000125/RRU
_ON_DEMAND_WCU_PER_REQUEST: float = 0.625 / 1_000_000  # = $0.000000625/WRU

# Per-opportunity savings factors. Each factor is applied to the table's
# current monthly cost to estimate savings if the recommendation is acted
# on. Factors are AWS-documented midpoints rather than arbitrary constants.
DYNAMODB_SAVINGS_FACTORS: dict[str, float] = {
    "reserved_capacity": 0.66,       # AWS-published 53-76% midpoint
    "rightsize_provisioned": 0.40,   # measured-utilization fallback midpoint
    "billing_mode_switch": 0.40,     # AWS-published 20-60% midpoint
    "unused_table": 1.00,            # deleting an empty table = 100% saved
    "data_lifecycle": 0.60,          # AWS-published 40-80% TTL/archive midpoint
    "default": 0.30,                 # conservative fallback when category unknown
}

_HIGH_CAPACITY_THRESHOLD: int = 100
_LARGE_TABLE_BYTES: int = 1024**3
_VERY_LARGE_TABLE_BYTES: int = 10 * 1024**3
_ON_DEMAND_ANALYSIS_DAYS: int = 14
_PROVISIONED_ANALYSIS_DAYS: int = 7
_UTILIZATION_LOW_THRESHOLD: float = 20.0
_PROPOSED_BUFFER: float = 1.2
_UTILIZATION_HIGH_THRESHOLD: float = 0.7
_VARIABILITY_THRESHOLD: float = 3.0
_MIN_AVG_READ: float = 5.0
_MIN_AVG_WRITE: float = 1.0


def get_dynamodb_table_analysis(ctx: ScanContext) -> dict[str, Any]:
    """Get DynamoDB table analysis for cost optimization"""
    try:
        dynamodb = ctx.client("dynamodb")
        paginator = dynamodb.get_paginator("list_tables")
        table_names: list[str] = []
        for page in paginator.paginate():
            table_names.extend(page.get("TableNames", []))

        analysis: dict[str, Any] = {
            "total_tables": len(table_names),
            "provisioned_tables": [],
            "on_demand_tables": [],
            "optimization_opportunities": [],
        }

        for table_name in table_names:
            try:
                table_response = dynamodb.describe_table(TableName=table_name)
                table = table_response["Table"]

                table_info: dict[str, Any] = {
                    "TableName": table_name,
                    "BillingMode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
                    "TableStatus": table.get("TableStatus", "UNKNOWN"),
                    "ItemCount": table.get("ItemCount", 0),
                    "TableSizeBytes": table.get("TableSizeBytes", 0),
                    "ReadCapacityUnits": 0,
                    "WriteCapacityUnits": 0,
                    "EstimatedMonthlyCost": 0,
                    "OptimizationOpportunities": [],
                }

                if table_info["BillingMode"] == "PROVISIONED":
                    provisioned_throughput = table.get("ProvisionedThroughput", {})
                    table_info["ReadCapacityUnits"] = provisioned_throughput.get("ReadCapacityUnits", 0)
                    table_info["WriteCapacityUnits"] = provisioned_throughput.get("WriteCapacityUnits", 0)

                    monthly_cost = (table_info["ReadCapacityUnits"] * _PROVISIONED_RCU_COST) + (
                        table_info["WriteCapacityUnits"] * _PROVISIONED_WCU_COST
                    )
                    table_info["EstimatedMonthlyCost"] = round(monthly_cost, 2)

                    analysis["provisioned_tables"].append(table_name)

                    if (
                        table_info["ReadCapacityUnits"] > _HIGH_CAPACITY_THRESHOLD
                        or table_info["WriteCapacityUnits"] > _HIGH_CAPACITY_THRESHOLD
                    ):
                        table_info["OptimizationOpportunities"].append(
                            "Consider On-Demand billing for unpredictable workloads"
                        )

                    if table_info["ItemCount"] == 0:
                        table_info["OptimizationOpportunities"].append("Empty table - consider deletion if unused")

                else:
                    analysis["on_demand_tables"].append(table_name)
                    try:
                        cloudwatch = ctx.client("cloudwatch")
                        cw_end = datetime.now(UTC)
                        cw_start = cw_end - timedelta(days=_ON_DEMAND_ANALYSIS_DAYS)
                        r_resp = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DynamoDB",
                            MetricName="ConsumedReadCapacityUnits",
                            Dimensions=[{"Name": "TableName", "Value": table_name}],
                            StartTime=cw_start,
                            EndTime=cw_end,
                            Period=3600,
                            Statistics=["Average"],
                        )
                        w_resp = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DynamoDB",
                            MetricName="ConsumedWriteCapacityUnits",
                            Dimensions=[{"Name": "TableName", "Value": table_name}],
                            StartTime=cw_start,
                            EndTime=cw_end,
                            Period=3600,
                            Statistics=["Average"],
                        )
                        r_dps = r_resp.get("Datapoints", [])
                        w_dps = w_resp.get("Datapoints", [])
                        if r_dps and w_dps:
                            avg_r = sum(d["Average"] for d in r_dps) / len(r_dps)
                            avg_w = sum(d["Average"] for d in w_dps) / len(w_dps)
                            # avg_r/avg_w are CW "Average ConsumedXxxCapacityUnits"
                            # over the 1-hour Period; multiply by hours/month to
                            # estimate the monthly request volume, then apply the
                            # per-request rate.
                            table_info["EstimatedMonthlyCost"] = round(
                                (avg_r * _ON_DEMAND_RCU_PER_REQUEST + avg_w * _ON_DEMAND_WCU_PER_REQUEST) * 730, 2
                            )
                        else:
                            # No CW data points available; emit 0 with a warning
                            # rather than fabricating a $25/month constant.
                            table_info["EstimatedMonthlyCost"] = 0.0
                            table_info["PricingWarning"] = "on-demand cost unavailable: no CloudWatch data"
                    except Exception:
                        table_info["EstimatedMonthlyCost"] = 0.0
                        table_info["PricingWarning"] = "on-demand cost unavailable: CloudWatch query failed"
                    table_info["OptimizationOpportunities"].append(
                        "Monitor usage patterns to consider Provisioned mode for steady workloads"
                    )

                if table_info["TableSizeBytes"] > _LARGE_TABLE_BYTES:
                    table_info["OptimizationOpportunities"].append(
                        "Large table - consider data archiving or compression strategies"
                    )

                analysis["optimization_opportunities"].append(table_info)

            except Exception as e:
                ctx.warn(f"Could not analyze table {table_name}: {e}", "dynamodb")

        return analysis

    except Exception as e:
        ctx.warn(f"Could not analyze DynamoDB tables: {e}", "dynamodb")
        return {
            "total_tables": 0,
            "provisioned_tables": [],
            "on_demand_tables": [],
            "optimization_opportunities": [],
        }


def get_dynamodb_optimization_descriptions() -> dict[str, dict[str, str]]:
    """Get descriptions for DynamoDB cost optimization opportunities"""
    return {
        "dynamodb_table_analysis": {
            "title": "DynamoDB Table Analysis",
            "description": (
                "Analysis of DynamoDB table configurations including billing mode,"
                " provisioned capacity, and reserved capacity opportunities."
            ),
            "action": (
                "1. Review table billing mode (Provisioned vs On-Demand)\n"
                "2. Rightsize provisioned RCU/WCU based on usage\n"
                "3. Consider reserved capacity for steady workloads\n"
                "4. Estimated savings: 20-76% depending on optimization type"
            ),
        },
        "enhanced_checks": {
            "title": "Enhanced DynamoDB Checks",
            "description": (
                "Additional DynamoDB checks including data lifecycle management,"
                " global tables optimization, and unused table detection."
            ),
            "action": (
                "1. Implement TTL for automatic data expiration\n"
                "2. Archive old data to S3\n"
                "3. Review global tables replication configuration\n"
                "4. Estimated savings: 20-80% on storage costs"
            ),
        },
    }


def get_enhanced_dynamodb_checks(ctx: ScanContext) -> dict[str, Any]:
    """Get enhanced DynamoDB cost optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "billing_mode_optimization": [],
        "capacity_rightsizing": [],
        "reserved_capacity": [],
        "data_lifecycle": [],
        "global_tables_optimization": [],
        "unused_tables": [],
        "over_provisioned_capacity": [],
    }

    try:
        dynamodb = ctx.client("dynamodb")
        paginator = dynamodb.get_paginator("list_tables")
        table_names: list[str] = []
        for page in paginator.paginate():
            table_names.extend(page.get("TableNames", []))

        for table_name in table_names:
            try:
                table_response = dynamodb.describe_table(TableName=table_name)
                table = table_response["Table"]

                billing_mode = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
                table_status = table.get("TableStatus")
                item_count = table.get("ItemCount", 0)
                table_size_bytes = table.get("TableSizeBytes", 0)

                if table_status != "ACTIVE":
                    continue

                if item_count == 0:
                    checks["unused_tables"].append(
                        {
                            "TableName": table_name,
                            "ItemCount": item_count,
                            "TableSizeBytes": table_size_bytes,
                            "Recommendation": "Empty table - consider deletion if unused",
                            "EstimatedSavings": "100% of table costs",
                            "CheckCategory": "Unused DynamoDB Tables",
                        }
                    )

                if billing_mode == "PROVISIONED":
                    provisioned_throughput = table.get("ProvisionedThroughput", {})
                    read_capacity = provisioned_throughput.get("ReadCapacityUnits", 0)
                    write_capacity = provisioned_throughput.get("WriteCapacityUnits", 0)

                    if read_capacity > _HIGH_CAPACITY_THRESHOLD or write_capacity > _HIGH_CAPACITY_THRESHOLD:
                        try:
                            cloudwatch = ctx.client("cloudwatch")
                            end_time = datetime.now(UTC)
                            start_time = end_time - timedelta(days=_PROVISIONED_ANALYSIS_DAYS)

                            read_response = cloudwatch.get_metric_statistics(
                                Namespace="AWS/DynamoDB",
                                MetricName="ConsumedReadCapacityUnits",
                                Dimensions=[{"Name": "TableName", "Value": table_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            write_response = cloudwatch.get_metric_statistics(
                                Namespace="AWS/DynamoDB",
                                MetricName="ConsumedWriteCapacityUnits",
                                Dimensions=[{"Name": "TableName", "Value": table_name}],
                                StartTime=start_time,
                                EndTime=end_time,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            )

                            read_datapoints = read_response.get("Datapoints", [])
                            write_datapoints = write_response.get("Datapoints", [])

                            if read_datapoints and write_datapoints:
                                avg_read_consumed = sum(dp["Average"] for dp in read_datapoints) / len(read_datapoints)
                                avg_write_consumed = sum(dp["Average"] for dp in write_datapoints) / len(
                                    write_datapoints
                                )

                                read_utilization = (avg_read_consumed / read_capacity) * 100 if read_capacity > 0 else 0
                                write_utilization = (
                                    (avg_write_consumed / write_capacity) * 100 if write_capacity > 0 else 0
                                )

                                if (
                                    read_utilization < _UTILIZATION_LOW_THRESHOLD
                                    or write_utilization < _UTILIZATION_LOW_THRESHOLD
                                ):
                                    recommendation_text = (
                                        f"Low utilization detected "
                                        f"(Read: {read_utilization:.1f}%, "
                                        f"Write: {write_utilization:.1f}%) "
                                        "- consider reducing capacity"
                                    )
                                else:
                                    recommendation_text = (
                                        f"Utilization acceptable "
                                        f"(Read: {read_utilization:.1f}%, "
                                        f"Write: {write_utilization:.1f}%) "
                                        "- monitor usage patterns"
                                    )
                            else:
                                recommendation_text = "High provisioned capacity - validate with CloudWatch metrics"

                        except Exception:
                            recommendation_text = "High provisioned capacity - CloudWatch analysis recommended"

                        checks["over_provisioned_capacity"].append(
                            {
                                "TableName": table_name,
                                "ReadCapacityUnits": read_capacity,
                                "WriteCapacityUnits": write_capacity,
                                "Recommendation": recommendation_text,
                                "EstimatedSavings": "Variable based on actual usage",
                                "CheckCategory": "DynamoDB Over-Provisioned Capacity",
                            }
                        )

                    if read_capacity >= _HIGH_CAPACITY_THRESHOLD and write_capacity >= _HIGH_CAPACITY_THRESHOLD:
                        checks["reserved_capacity"].append(
                            {
                                "TableName": table_name,
                                "ReadCapacityUnits": read_capacity,
                                "WriteCapacityUnits": write_capacity,
                                "Recommendation": "Consider Reserved Capacity for predictable workloads",
                                "EstimatedSavings": "53-76% vs On-Demand",
                                "CheckCategory": "DynamoDB Reserved Capacity",
                            }
                        )

                else:
                    try:
                        cloudwatch = ctx.client("cloudwatch")
                        end_time = datetime.now(UTC)
                        start_time = end_time - timedelta(days=_ON_DEMAND_ANALYSIS_DAYS)

                        read_response = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DynamoDB",
                            MetricName="ConsumedReadCapacityUnits",
                            Dimensions=[{"Name": "TableName", "Value": table_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average", "Maximum"],
                        )

                        write_response = cloudwatch.get_metric_statistics(
                            Namespace="AWS/DynamoDB",
                            MetricName="ConsumedWriteCapacityUnits",
                            Dimensions=[{"Name": "TableName", "Value": table_name}],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average", "Maximum"],
                        )

                        read_datapoints = read_response.get("Datapoints", [])
                        write_datapoints = write_response.get("Datapoints", [])

                        if read_datapoints and write_datapoints:
                            avg_read = sum(dp["Average"] for dp in read_datapoints) / len(read_datapoints)
                            max_read = max(dp["Maximum"] for dp in read_datapoints)
                            avg_write = sum(dp["Average"] for dp in write_datapoints) / len(write_datapoints)
                            max_write = max(dp["Maximum"] for dp in write_datapoints)

                            proposed_read = avg_read * _PROPOSED_BUFFER
                            proposed_write = avg_write * _PROPOSED_BUFFER

                            read_utilization = avg_read / proposed_read if proposed_read > 0 else 0
                            write_utilization = avg_write / proposed_write if proposed_write > 0 else 0
                            read_variability = max_read / avg_read if avg_read > 0 else float("inf")
                            write_variability = max_write / avg_write if avg_write > 0 else float("inf")

                            if (
                                read_utilization > _UTILIZATION_HIGH_THRESHOLD
                                and write_utilization > _UTILIZATION_HIGH_THRESHOLD
                                and read_variability < _VARIABILITY_THRESHOLD
                                and write_variability < _VARIABILITY_THRESHOLD
                                and avg_read > _MIN_AVG_READ
                                and avg_write > _MIN_AVG_WRITE
                            ):
                                checks["billing_mode_optimization"].append(
                                    {
                                        "TableName": table_name,
                                        "CurrentBillingMode": billing_mode,
                                        "AvgReadCapacity": f"{avg_read:.1f} RCU",
                                        "AvgWriteCapacity": f"{avg_write:.1f} WCU",
                                        "ReadUtilization": f"{read_utilization:.1%}",
                                        "WriteUtilization": f"{write_utilization:.1%}",
                                        "Recommendation": (
                                            f"Steady high usage detected over "
                                            f"{_ON_DEMAND_ANALYSIS_DAYS} days "
                                            f"(Read: {avg_read:.1f} RCU at "
                                            f"{read_utilization:.0%} utilization, "
                                            f"Write: {avg_write:.1f} WCU at "
                                            f"{write_utilization:.0%} utilization) "
                                            "- switch to Provisioned mode"
                                        ),
                                        "EstimatedSavings": "Up to 60% for predictable traffic patterns",
                                        "CheckCategory": "DynamoDB Billing Mode - Metric-Backed",
                                        "MetricsPeriod": f"{_ON_DEMAND_ANALYSIS_DAYS} days",
                                    }
                                )
                        else:
                            checks["billing_mode_optimization"].append(
                                {
                                    "TableName": table_name,
                                    "CurrentBillingMode": billing_mode,
                                    "TableSizeGB": round(table_size_bytes / (_LARGE_TABLE_BYTES), 2),
                                    "Recommendation": (
                                        "Enable CloudWatch metrics to analyze "
                                        "usage patterns for billing mode optimization"
                                    ),
                                    "EstimatedSavings": (
                                        "Enable monitoring first - potential 60% savings "
                                        "with Provisioned mode for steady workloads"
                                    ),
                                    "CheckCategory": "DynamoDB Monitoring Required",
                                }
                            )

                    except Exception:
                        checks["billing_mode_optimization"].append(
                            {
                                "TableName": table_name,
                                "CurrentBillingMode": billing_mode,
                                "TableSizeGB": round(table_size_bytes / (_LARGE_TABLE_BYTES), 2),
                                "Recommendation": (
                                    "Enable CloudWatch metrics to validate "
                                    "usage patterns before switching to Provisioned mode"
                                ),
                                "EstimatedSavings": (
                                    "CloudWatch analysis required - potential 60% savings for steady workloads"
                                ),
                                "CheckCategory": "DynamoDB CloudWatch Required",
                            }
                        )

                if table_size_bytes > _VERY_LARGE_TABLE_BYTES:
                    checks["data_lifecycle"].append(
                        {
                            "TableName": table_name,
                            "TableSizeGB": round(table_size_bytes / (_LARGE_TABLE_BYTES), 2),
                            "Recommendation": "Large table - implement TTL for old data or archive to S3",
                            "EstimatedSavings": "40-80% on storage costs",
                            "CheckCategory": "DynamoDB Data Lifecycle",
                        }
                    )

            except Exception as e:
                ctx.warn(f"Could not analyze DynamoDB table {table_name}: {e}", "dynamodb")

    except Exception as e:
        ctx.warn(f"Could not perform enhanced DynamoDB checks: {e}", "dynamodb")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        for item in items:
            recommendations.append(item)

    return {"recommendations": recommendations, **checks}

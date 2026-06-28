"""DynamoDB cost optimization checks.

Extracted from CostOptimizer.get_dynamodb_table_analysis(),
get_dynamodb_optimization_descriptions(), and
get_enhanced_dynamodb_checks() as free functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

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

# CheckCategories that are commitment levers (a future PURCHASE, not a rightsizing
# saving). The adapter demotes these to $0 advisories so they are never summed
# into the per-service rightsizing headline — Reserved Capacity dollars are owned
# by Commitment Analysis (DynamoDB H2; mirrors RDS ADVISORY_CATEGORIES).
DYNAMODB_ADVISORY_CATEGORIES: frozenset = frozenset({"DynamoDB Reserved Capacity"})

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


def _sum_gsi_throughput(table: dict[str, Any]) -> tuple[int, int, list[dict[str, Any]]]:
    """Sum provisioned RCU/WCU across a table's Global Secondary Indexes.

    DynamoDB bills each GSI's provisioned throughput on top of the base table
    (DynamoDB H3). Returns ``(gsi_read_total, gsi_write_total, per_gsi)`` where
    ``per_gsi`` carries each index's name and provisioned RCU/WCU. On-demand GSIs
    (no ``ProvisionedThroughput``) contribute 0.
    """
    gsi_read = 0
    gsi_write = 0
    per_gsi: list[dict[str, Any]] = []
    for gsi in table.get("GlobalSecondaryIndexes", []) or []:
        throughput = gsi.get("ProvisionedThroughput", {}) or {}
        index_read = int(throughput.get("ReadCapacityUnits", 0) or 0)
        index_write = int(throughput.get("WriteCapacityUnits", 0) or 0)
        gsi_read += index_read
        gsi_write += index_write
        per_gsi.append(
            {
                "IndexName": gsi.get("IndexName", ""),
                "ReadCapacityUnits": index_read,
                "WriteCapacityUnits": index_write,
            }
        )
    return gsi_read, gsi_write, per_gsi


def _avg_consumed_capacity(
    cloudwatch: Any, table_name: str, days: int, gsi_name: str | None = None
) -> tuple[float | None, float | None]:
    """Average consumed read/write capacity over the window, or ``(None, None)``.

    Reads ``ConsumedRead/WriteCapacityUnits`` for the table (and, when ``gsi_name``
    is given, that GSI via the ``GlobalSecondaryIndexName`` dimension). Returns
    ``(None, None)`` when either metric has no datapoints so the caller abstains
    from claiming a rightsizing dollar rather than fabricating one (DynamoDB H1).
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(days=days)
    dimensions = [{"Name": "TableName", "Value": table_name}]
    if gsi_name:
        dimensions = dimensions + [{"Name": "GlobalSecondaryIndexName", "Value": gsi_name}]
    read_response = cloudwatch.get_metric_statistics(
        Namespace="AWS/DynamoDB",
        MetricName="ConsumedReadCapacityUnits",
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=3600,
        Statistics=["Average", "Maximum"],
    )
    write_response = cloudwatch.get_metric_statistics(
        Namespace="AWS/DynamoDB",
        MetricName="ConsumedWriteCapacityUnits",
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=3600,
        Statistics=["Average", "Maximum"],
    )
    read_dps = read_response.get("Datapoints", [])
    write_dps = write_response.get("Datapoints", [])
    if not read_dps or not write_dps:
        return None, None
    avg_read = sum(dp["Average"] for dp in read_dps) / len(read_dps)
    avg_write = sum(dp["Average"] for dp in write_dps) / len(write_dps)
    return avg_read, avg_write


def _rightsize_dimension(capacity: int, avg_consumed: float | None) -> tuple[int, float | None, bool]:
    """Rightsized target for one capacity dimension (DynamoDB H1).

    With no metric (``avg_consumed`` is ``None``) the target stays at the current
    capacity and no saving is claimed. When measured utilization is below the low
    threshold the target is ``ceil(avg_consumed x buffer)`` (never above current,
    never below 1); otherwise the target is the current capacity. Returns
    ``(target_capacity, utilization_pct, is_low_utilization)``.
    """
    if capacity <= 0 or avg_consumed is None:
        return capacity, None, False
    utilization = (avg_consumed / capacity) * 100
    if utilization < _UTILIZATION_LOW_THRESHOLD:
        target = min(capacity, max(1, ceil(avg_consumed * _PROPOSED_BUFFER)))
        return target, utilization, True
    return capacity, utilization, False


def _over_provisioned_text(
    total_read: int,
    total_write: int,
    metrics_available: bool,
    low_utilization: bool,
    read_util: float | None,
    write_util: float | None,
) -> str:
    """Human recommendation string for an over-provisioned-capacity rec."""
    if not metrics_available:
        return (
            f"High provisioned capacity (base+GSI {total_read} RCU / {total_write} WCU) "
            "- enable CloudWatch capacity metrics to quantify rightsizing"
        )
    read_str = f"{read_util:.1f}%" if read_util is not None else "n/a"
    write_str = f"{write_util:.1f}%" if write_util is not None else "n/a"
    if low_utilization:
        return f"Low utilization detected (Read: {read_str}, Write: {write_str}) - reduce capacity"
    return f"Utilization acceptable (Read: {read_str}, Write: {write_str}) - monitor usage patterns"


def _build_over_provisioned_rec(
    ctx: ScanContext,
    table_name: str,
    base_read: int,
    base_write: int,
    per_gsi: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the over-provisioned-capacity rec with metric-gated rightsizing data.

    Reads consumed-capacity metrics for the base table and each GSI, computes a
    rightsized target per component, and carries the totals/targets plus
    ``MetricsAvailable``/``LowUtilization`` flags so the adapter counts an exact
    ``current - target`` delta (DynamoDB H1) instead of a blanket factor. Per-GSI
    over-provisioning is surfaced in ``GsiOverProvisioned`` (DynamoDB H3). A
    CloudWatch failure is classified (never swallowed) and the rec abstains
    (``MetricsAvailable=False`` -> $0 advisory in the adapter).
    """
    total_read = base_read + sum(g["ReadCapacityUnits"] for g in per_gsi)
    total_write = base_write + sum(g["WriteCapacityUnits"] for g in per_gsi)
    metrics_available = False
    low_utilization = False
    target_read = base_read
    target_write = base_write
    base_read_util: float | None = None
    base_write_util: float | None = None
    gsi_breakdown: list[dict[str, Any]] = []

    try:
        cloudwatch = ctx.client("cloudwatch")
        avg_read, avg_write = _avg_consumed_capacity(cloudwatch, table_name, _PROVISIONED_ANALYSIS_DAYS)
        target_read, base_read_util, base_low_read = _rightsize_dimension(base_read, avg_read)
        target_write, base_write_util, base_low_write = _rightsize_dimension(base_write, avg_write)
        metrics_available = metrics_available or avg_read is not None
        low_utilization = low_utilization or base_low_read or base_low_write

        for gsi in per_gsi:
            g_avg_read, g_avg_write = _avg_consumed_capacity(
                cloudwatch, table_name, _PROVISIONED_ANALYSIS_DAYS, gsi["IndexName"]
            )
            g_target_read, g_read_util, g_low_read = _rightsize_dimension(gsi["ReadCapacityUnits"], g_avg_read)
            g_target_write, g_write_util, g_low_write = _rightsize_dimension(gsi["WriteCapacityUnits"], g_avg_write)
            target_read += g_target_read
            target_write += g_target_write
            metrics_available = metrics_available or g_avg_read is not None
            low_utilization = low_utilization or g_low_read or g_low_write
            gsi_breakdown.append(
                {
                    "IndexName": gsi["IndexName"],
                    "ReadCapacityUnits": gsi["ReadCapacityUnits"],
                    "WriteCapacityUnits": gsi["WriteCapacityUnits"],
                    "RightsizedReadCapacity": g_target_read,
                    "RightsizedWriteCapacity": g_target_write,
                    "ReadUtilization": round(g_read_util, 1) if g_read_util is not None else None,
                    "WriteUtilization": round(g_write_util, 1) if g_write_util is not None else None,
                    "OverProvisioned": bool(g_low_read or g_low_write),
                }
            )
    except Exception as exc:
        record_aws_error(
            ctx, exc, service="dynamodb", context=f"over-provisioned CloudWatch read for {table_name}"
        )
        metrics_available = False
        low_utilization = False
        target_read = total_read
        target_write = total_write
        base_read_util = None
        base_write_util = None
        gsi_breakdown = [
            {
                "IndexName": gsi["IndexName"],
                "ReadCapacityUnits": gsi["ReadCapacityUnits"],
                "WriteCapacityUnits": gsi["WriteCapacityUnits"],
                "OverProvisioned": False,
            }
            for gsi in per_gsi
        ]

    recommendation_text = _over_provisioned_text(
        total_read, total_write, metrics_available, low_utilization, base_read_util, base_write_util
    )

    return {
        "TableName": table_name,
        "ReadCapacityUnits": total_read,
        "WriteCapacityUnits": total_write,
        "BaseReadCapacityUnits": base_read,
        "BaseWriteCapacityUnits": base_write,
        "RightsizedReadCapacity": target_read,
        "RightsizedWriteCapacity": target_write,
        "MetricsAvailable": metrics_available,
        "LowUtilization": low_utilization,
        "ReadUtilization": round(base_read_util, 1) if base_read_util is not None else None,
        "WriteUtilization": round(base_write_util, 1) if base_write_util is not None else None,
        "MetricWindowDays": _PROVISIONED_ANALYSIS_DAYS,
        "Buffer": _PROPOSED_BUFFER,
        "GsiOverProvisioned": gsi_breakdown,
        "Recommendation": recommendation_text,
        "EstimatedSavings": "Variable based on actual usage",
        "CheckCategory": "DynamoDB Over-Provisioned Capacity",
    }


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

                # Mirror the enhanced_checks ACTIVE gate: CREATING/DELETING/
                # UPDATING tables are transient and should not produce optimization
                # opportunities (DynamoDB L4).
                if table_info.get("TableStatus") != "ACTIVE":
                    continue

                if table_info["BillingMode"] == "PROVISIONED":
                    provisioned_throughput = table.get("ProvisionedThroughput", {})
                    base_read = provisioned_throughput.get("ReadCapacityUnits", 0)
                    base_write = provisioned_throughput.get("WriteCapacityUnits", 0)

                    # DynamoDB H3: GSI provisioned throughput is billed on top of the
                    # base table. Sum each GlobalSecondaryIndexes[].ProvisionedThroughput
                    # so a table with small base capacity but large GSIs is not
                    # under-costed (and the GSIs become visible to the report).
                    gsi_read, gsi_write, gsi_throughput = _sum_gsi_throughput(table)

                    table_info["ReadCapacityUnits"] = base_read + gsi_read
                    table_info["WriteCapacityUnits"] = base_write + gsi_write
                    table_info["BaseReadCapacityUnits"] = base_read
                    table_info["BaseWriteCapacityUnits"] = base_write
                    table_info["GsiThroughput"] = gsi_throughput

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

                # DynamoDB L4: DescribeTable's ItemCount is updated by DynamoDB on
                # a ~6-hour cadence, so a value of 0 can lag an actively-written
                # table. This "Empty table" rec is therefore advisory only — the
                # adapter renders it as a $0 Counted=False nudge (never summed), so
                # the staleness is benign for dollars; operators should corroborate
                # against CloudWatch Consumed*CapacityUnits before deleting.
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
                    base_read = provisioned_throughput.get("ReadCapacityUnits", 0)
                    base_write = provisioned_throughput.get("WriteCapacityUnits", 0)

                    # DynamoDB H3: include GSI provisioned throughput so a table with
                    # small base capacity but large GSIs is neither under-costed nor
                    # left with over-provisioned indexes invisible.
                    gsi_read, gsi_write, per_gsi = _sum_gsi_throughput(table)
                    total_read = base_read + gsi_read
                    total_write = base_write + gsi_write

                    if total_read > _HIGH_CAPACITY_THRESHOLD or total_write > _HIGH_CAPACITY_THRESHOLD:
                        # DynamoDB H1: carry measured utilization + a rightsized target
                        # so the adapter counts an exact current-minus-target delta,
                        # gated on low utilization, instead of a blanket factor.
                        checks["over_provisioned_capacity"].append(
                            _build_over_provisioned_rec(ctx, table_name, base_read, base_write, per_gsi)
                        )

                    if total_read >= _HIGH_CAPACITY_THRESHOLD and total_write >= _HIGH_CAPACITY_THRESHOLD:
                        # DynamoDB H2: Reserved Capacity is a commitment lever. The
                        # adapter demotes it to a $0 advisory (owned by Commitment
                        # Analysis) so it is never summed into rightsizing savings.
                        checks["reserved_capacity"].append(
                            {
                                "TableName": table_name,
                                "ReadCapacityUnits": total_read,
                                "WriteCapacityUnits": total_write,
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

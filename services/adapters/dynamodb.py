"""Multi-source adapter for DynamoDB with table analysis and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dynamodb import (
    DYNAMODB_SAVINGS_FACTORS,
    get_dynamodb_optimization_descriptions,
    get_dynamodb_table_analysis,
    get_enhanced_dynamodb_checks,
)

# AWS DynamoDB hourly provisioned rates (us-east-1, verified via Pricing API
# SKU 4V475Q49DCKGXQZ2 / R6PXMNYCEDGZ2EYN). Region-scaled via pricing_multiplier
# at the per-rec emit site below.
_DYNAMODB_RCU_HOURLY: float = 0.00013
_DYNAMODB_WCU_HOURLY: float = 0.00065
_HOURS_PER_MONTH: int = 730


def _enhanced_savings_factor(check_category: str) -> float:
    """Map a shim-emitted CheckCategory to its documented savings factor.

    Categories the shim emits (see services/dynamodb.py:get_enhanced_dynamodb_checks):
      "Unused DynamoDB Tables", "DynamoDB Over-Provisioned Capacity",
      "DynamoDB Reserved Capacity", "DynamoDB Billing Mode - Metric-Backed",
      "DynamoDB Monitoring Required", "DynamoDB CloudWatch Required",
      "DynamoDB Data Lifecycle".
    """
    category_lower = check_category.lower()
    if "unused" in category_lower:
        return DYNAMODB_SAVINGS_FACTORS["unused_table"]
    if "reserved" in category_lower:
        return DYNAMODB_SAVINGS_FACTORS["reserved_capacity"]
    if "over-provisioned" in category_lower:
        return DYNAMODB_SAVINGS_FACTORS["rightsize_provisioned"]
    if "billing mode" in category_lower:
        return DYNAMODB_SAVINGS_FACTORS["billing_mode_switch"]
    if "data lifecycle" in category_lower:
        return DYNAMODB_SAVINGS_FACTORS["data_lifecycle"]
    return DYNAMODB_SAVINGS_FACTORS["default"]


class DynamoDbModule(BaseServiceModule):
    """ServiceModule adapter for DynamoDB. Multi-source savings strategy."""

    key: str = "dynamodb"
    cli_aliases: tuple[str, ...] = ("dynamodb",)
    display_name: str = "DynamoDB"
    # Shim hits cloudwatch.get_metric_statistics for ConsumedReadCapacityUnits
    # and ConsumedWriteCapacityUnits per table.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for DynamoDB scanning."""
        return ("dynamodb", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan DynamoDB tables for cost optimization opportunities.

        Consults table analysis and enhanced checks modules for capacity
        rightsizing, billing mode review, and CloudWatch metric-backed analysis.
        Per-rec savings derive from AWS-documented per-opportunity factors in
        DYNAMODB_SAVINGS_FACTORS rather than flat multipliers.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with "dynamodb_table_analysis" and "enhanced_checks" SourceBlocks.
        """

        dynamodb_data = get_dynamodb_table_analysis(ctx)
        enhanced_result = get_enhanced_dynamodb_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = dynamodb_data.get("optimization_opportunities", [])
        multiplier = ctx.pricing_multiplier

        # Dedupe table_analysis vs enhanced_checks by TableName: a single table
        # that appears in both should contribute savings via the higher-fidelity
        # enhanced_checks path. opt_opps is the base layer; enhanced overrides.
        enhanced_table_names = {rec.get("TableName") for rec in enhanced_recs if rec.get("TableName")}

        savings = 0.0
        for rec in opt_opps:
            if rec.get("TableName") in enhanced_table_names:
                # Will be summed via enhanced loop below.
                continue
            rcu = rec.get("ReadCapacityUnits", 0)
            wcu = rec.get("WriteCapacityUnits", 0)

            if rcu > 0 or wcu > 0:
                # Provisioned table baseline cost; default rightsize factor.
                monthly_current = (
                    (rcu * _DYNAMODB_RCU_HOURLY + wcu * _DYNAMODB_WCU_HOURLY)
                    * _HOURS_PER_MONTH
                    * multiplier
                )
                savings += monthly_current * DYNAMODB_SAVINGS_FACTORS["default"]
            else:
                # On-demand table; EstimatedMonthlyCost was computed in the shim
                # using the (now-corrected) per-request rates and does not yet
                # carry the regional multiplier.
                cost = rec.get("EstimatedMonthlyCost", 0)
                if cost > 0:
                    savings += cost * multiplier * DYNAMODB_SAVINGS_FACTORS["default"]

        # Enhanced checks: each rec maps to a documented savings factor based
        # on its CheckCategory; rcu/wcu of the underlying table drive the base.
        for rec in enhanced_recs:
            factor = _enhanced_savings_factor(rec.get("CheckCategory", ""))
            rcu = rec.get("ReadCapacityUnits", 0)
            wcu = rec.get("WriteCapacityUnits", 0)
            if rcu > 0 or wcu > 0:
                monthly_current = (
                    (rcu * _DYNAMODB_RCU_HOURLY + wcu * _DYNAMODB_WCU_HOURLY)
                    * _HOURS_PER_MONTH
                    * multiplier
                )
                rec_savings = monthly_current * factor
            else:
                # No capacity data on this rec (likely an unused / data-lifecycle
                # check). Skip rather than fabricate a constant.
                rec_savings = 0.0
            rec["EstimatedMonthlySavings"] = round(rec_savings, 2)
            savings += rec_savings

        total_recs = len(opt_opps) + len(enhanced_recs)

        return ServiceFindings(
            service_name="DynamoDB",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "dynamodb_table_analysis": SourceBlock(count=len(opt_opps), recommendations=tuple(opt_opps)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=get_dynamodb_optimization_descriptions(),
            total_count=dynamodb_data.get("total_tables", 0),
            extras={
                "table_counts": {
                    "total": dynamodb_data.get("total_tables", 0),
                    "provisioned": len(dynamodb_data.get("provisioned_tables", [])),
                    "on_demand": len(dynamodb_data.get("on_demand_tables", [])),
                }
            },
        )

"""Multi-source adapter for DynamoDB with table analysis and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dynamodb import (
    get_dynamodb_optimization_descriptions,
    get_dynamodb_table_analysis,
    get_enhanced_dynamodb_checks,
)


class DynamoDbModule(BaseServiceModule):
    """ServiceModule adapter for DynamoDB. Multi-source savings strategy."""

    key: str = "dynamodb"
    cli_aliases: tuple[str, ...] = ("dynamodb",)
    display_name: str = "DynamoDB"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for DynamoDB scanning."""
        return ("dynamodb",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan DynamoDB tables for cost optimization opportunities.

        Consults table analysis and enhanced checks modules for capacity
        rightsizing, billing mode review, and CloudWatch metric-backed analysis.
        Savings calculated from RCU/WCU capacity pricing with reserved capacity discount.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with "dynamodb_table_analysis" and "enhanced_checks" SourceBlocks.
        """
        print("\U0001f50d [services/adapters/dynamodb.py] DynamoDB module active")

        dynamodb_data = get_dynamodb_table_analysis(ctx)
        enhanced_result = get_enhanced_dynamodb_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = dynamodb_data.get("optimization_opportunities", [])
        DYNAMODB_RCU_HOURLY = 0.000147
        DYNAMODB_WCU_HOURLY = 0.000735

        savings = 0.0
        for rec in opt_opps:
            rcu = rec.get("ReadCapacityUnits", 0)
            wcu = rec.get("WriteCapacityUnits", 0)

            if rcu > 0 or wcu > 0:
                monthly_current = (rcu * DYNAMODB_RCU_HOURLY + wcu * DYNAMODB_WCU_HOURLY) * 730 * ctx.pricing_multiplier
                savings += monthly_current * 0.23
            else:
                cost = rec.get("EstimatedMonthlyCost", 0)
                savings += cost * 0.30

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
            extras={
                "table_counts": {
                    "total": dynamodb_data.get("total_tables", 0),
                    "provisioned": len(dynamodb_data.get("provisioned_tables", [])),
                    "on_demand": len(dynamodb_data.get("on_demand_tables", [])),
                }
            },
        )

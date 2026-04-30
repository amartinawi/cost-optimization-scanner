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
    key: str = "dynamodb"
    cli_aliases: tuple[str, ...] = ("dynamodb",)
    display_name: str = "DynamoDB"

    def required_clients(self) -> tuple[str, ...]:
        return ("dynamodb",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/dynamodb.py] DynamoDB module active")

        dynamodb_data = get_dynamodb_table_analysis(ctx)
        enhanced_result = get_enhanced_dynamodb_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = dynamodb_data.get("optimization_opportunities", [])
        savings = sum(rec.get("EstimatedMonthlyCost", 0) * 0.3 for rec in opt_opps)

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

"""Multi-source adapter for Lambda with Cost Hub and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.lambda_svc import LAMBDA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_lambda_checks


class LambdaModule(BaseServiceModule):
    key: str = "lambda"
    cli_aliases: tuple[str, ...] = ("lambda",)
    display_name: str = "Lambda"

    def required_clients(self) -> tuple[str, ...]:
        return ("lambda",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/lambda.py] Lambda module active")

        cost_hub_recs = ctx.cost_hub_splits.get("lambda", [])
        enhanced_result = get_enhanced_lambda_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        savings = 0.0
        savings += sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)
        for rec in enhanced_recs:
            est = rec.get("EstimatedSavings", "")
            if "Up to 90%" in est:
                savings += 50
            elif "memory optimization" in est.lower():
                savings += 10
            elif "Eliminate unused costs" in est:
                savings += 5

        total_recs = len(cost_hub_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="Lambda",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=LAMBDA_OPTIMIZATION_DESCRIPTIONS,
        )

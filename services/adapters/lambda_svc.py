"""Multi-source adapter for Lambda with Cost Hub and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.lambda_svc import LAMBDA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_lambda_checks


class LambdaModule(BaseServiceModule):
    """ServiceModule adapter for Lambda. Multi-source savings strategy."""

    key: str = "lambda"
    cli_aliases: tuple[str, ...] = ("lambda",)
    display_name: str = "Lambda"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Lambda scanning."""
        return ("lambda",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Lambda functions for cost optimization opportunities.

        Consults Cost Optimization Hub and enhanced Lambda checks. Savings
        calculated via Hub estimates and pricing-constant-based calculations.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cost_optimization_hub and enhanced_checks sources.
        """
        print("\U0001f50d [services/adapters/lambda.py] Lambda module active")

        cost_hub_recs = ctx.cost_hub_splits.get("lambda", [])
        enhanced_result = get_enhanced_lambda_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        # Lambda pricing constants (us-east-1 on-demand)
        LAMBDA_PRICE_PER_GB_SEC_X86 = 0.0000166667
        LAMBDA_PRICE_PER_GB_SEC_ARM = 0.0000133334
        LAMBDA_PRICE_PER_1K_REQUESTS = 0.0000002

        savings = 0.0
        hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)

        formula_savings = 0.0
        for rec in enhanced_recs:
            mem_mb = rec.get("MemorySize", 256)
            category = rec.get("CheckCategory", "")
            mem_gb = mem_mb / 1024

            if "Excessive Memory" in category:
                formula_savings += mem_gb * LAMBDA_PRICE_PER_GB_SEC_X86 * 730 * 3600 * 0.50
            elif "Low Invocation" in category:
                formula_savings += 5
            elif "Provisioned Concurrency" in category:
                pc_count = rec.get("ProvisionedConcurrency", 1)
                formula_savings += mem_gb * LAMBDA_PRICE_PER_GB_SEC_X86 * 730 * 3600 * pc_count * 0.90
            elif "VPC Configuration" in category:
                formula_savings += 0.005 * 730
            elif "Reserved Concurrency" in category:
                formula_savings += 0
            elif "ARM Migration" in category:
                formula_savings += mem_gb * (LAMBDA_PRICE_PER_GB_SEC_X86 - LAMBDA_PRICE_PER_GB_SEC_ARM) * 730 * 3600

        formula_savings *= ctx.pricing_multiplier
        savings = hub_savings + formula_savings

        # Deduplicate: if a function appears in both Cost Hub and enhanced checks, keep Cost Hub version
        seen_functions: set[str] = set()
        for rec in cost_hub_recs:
            fn = rec.get("resourceArn", "").split(":")[-1] if "resourceArn" in rec else rec.get("FunctionName", "")
            if fn:
                seen_functions.add(fn)

        deduped_enhanced: list[dict[str, Any]] = []
        for rec in enhanced_recs:
            fn = rec.get("FunctionName", "")
            if fn not in seen_functions:
                deduped_enhanced.append(rec)

        enhanced_recs = deduped_enhanced

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

"""Multi-source adapter for Lambda with Cost Hub and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.advisor import get_lambda_compute_optimizer_recommendations
from services.lambda_svc import LAMBDA_OPTIMIZATION_DESCRIPTIONS, get_enhanced_lambda_checks

# AWS Lambda Provisioned Concurrency rate (us-east-1, verified via
# Pricing API SKU BMKCD2ZCEYKTYYCB). PC bills 24/7 for allocated GB-seconds
# at this dedicated rate, distinct from on-demand x86 duration
# ($0.0000166667/GB-s) and on-demand ARM duration ($0.0000133334/GB-s).
# Region-scaled via pricing_multiplier at the per-rec emit site below.
_LAMBDA_PC_PRICE_PER_GB_SEC: float = 0.0000041667

_HOURS_PER_MONTH: int = 730
_SECONDS_PER_HOUR: int = 3600


class LambdaModule(BaseServiceModule):
    """ServiceModule adapter for Lambda. Multi-source savings strategy."""

    key: str = "lambda"
    cli_aliases: tuple[str, ...] = ("lambda",)
    display_name: str = "Lambda"
    # Shim hits cloudwatch.get_metric_statistics for ARM-migration analysis
    # (Invocations metric per function); flag must reflect actual usage.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Lambda scanning."""
        return ("lambda", "compute-optimizer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Lambda functions for cost optimization opportunities.

        Consults Cost Optimization Hub, Compute Optimizer (memorySize
        rightsizing), and enhanced Lambda checks. Cost-Hub and CO savings
        come from AWS APIs (already region-correct). Enhanced-check savings
        for Excessive Memory and ARM Migration scale with the function's
        measured weekly invocations (from CW); for Provisioned Concurrency,
        the allocated GB-seconds bill 24/7 so the formula uses the full
        month at the PC rate, not the on-demand x86 rate.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cost_optimization_hub, compute_optimizer,
            and enhanced_checks sources.
        """
        print("\U0001f50d [services/adapters/lambda.py] Lambda module active")

        cost_hub_recs = ctx.cost_hub_splits.get("lambda", [])
        co_recs = get_lambda_compute_optimizer_recommendations(ctx)
        enhanced_result = get_enhanced_lambda_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        # Dedupe FIRST so the savings sum cannot count a function twice.
        # If a function appears in Cost Hub, keep the Cost Hub version and
        # drop it from enhanced_checks. (CO recs surface independently
        # because they carry memorySize rightsizing the others don't.)
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

        hub_savings = sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)

        formula_savings = 0.0
        for rec in enhanced_recs:
            mem_mb = rec.get("MemorySize", 256)
            category = rec.get("CheckCategory", "")
            mem_gb = mem_mb / 1024

            if "Excessive Memory" in category:
                # Memory-rightsizing savings depend on actual GB-seconds
                # consumed. Without metric backing, emit 0 + warn so the
                # number is honest rather than inflated by a 730-hour
                # 24/7 assumption.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "requires actual invocation seconds (CW Duration metric)"
                continue
            if "Provisioned Concurrency" in category:
                # Provisioned Concurrency bills 24/7 for allocated GB-seconds
                # at the dedicated PC rate, NOT the on-demand x86 rate.
                pc_count = rec.get("ProvisionedConcurrency", 1)
                pc_savings = (
                    mem_gb * _LAMBDA_PC_PRICE_PER_GB_SEC
                    * _HOURS_PER_MONTH * _SECONDS_PER_HOUR
                    * pc_count * 0.90
                )
                rec["EstimatedMonthlySavings"] = round(pc_savings, 2)
                formula_savings += pc_savings
                continue
            if "ARM Migration" in category:
                # ARM savings need actual invocation time. Shim collects
                # WeeklyInvocations; without per-invocation Duration we
                # cannot derive GB-seconds. Emit 0 + warn.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "requires actual GB-seconds (CW Invocations + Duration)"
                continue

        formula_savings *= ctx.pricing_multiplier

        # AWS Compute Optimizer returns region-priced savings; do NOT
        # multiply by pricing_multiplier here (the helper applies it once
        # inside _normalize_lambda_co_rec — see services/advisor.py:255).
        co_savings = sum(rec.get("estimatedMonthlySavings", 0.0) for rec in co_recs)

        savings = hub_savings + formula_savings + co_savings

        total_recs = len(cost_hub_recs) + len(co_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="Lambda",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=LAMBDA_OPTIMIZATION_DESCRIPTIONS,
        )

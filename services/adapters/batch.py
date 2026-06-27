"""Flat-rate adapter for Batch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.batch_svc import BATCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_batch_checks

BATCH_COMPUTE_FALLBACK_MONTHLY: float = 150.0


class BatchModule(BaseServiceModule):
    """ServiceModule adapter for AWS Batch. Flat-rate savings strategy."""

    key: str = "batch"
    cli_aliases: tuple[str, ...] = ("batch",)
    display_name: str = "Batch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Batch scanning."""
        return ("batch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Batch compute environments for cost optimization opportunities.

        Detects Fargate vs EC2 compute environments. Fargate CEs get Fargate Spot
        recommendations (70% savings); EC2 CEs get Spot + Graviton recommendations.
        Type-specific savings rates: Spot=0.70, Graviton=0.10, default=0.30.
        All savings multiplied by ctx.pricing_multiplier for regional adjustment.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
        result = get_enhanced_batch_checks(ctx)
        recs = result.get("recommendations", [])
        # Batch CEs are bursty (minvCpus→maxvCpus); without a run-hours signal
        # (CloudWatch CPUUtilization datapoint count or Batch ListJobs runtime)
        # multiplying by a hardcoded 730 (24/7) overstates savings for idle CEs
        # and understates for saturated ones. Demote every rec to a $0 advisory
        # so the lever renders without an invented dollar (batch C2 / batch C1).
        for rec in recs:
            rec["Counted"] = False
            rec["EstimatedMonthlySavings"] = 0.0
            rec["EstimatedSavings"] = (
                "$0.00/month — advisory: enable Spot/Fargate-Spot/Graviton; "
                "realized saving is run-hour dependent and needs Batch ListJobs "
                "or CloudWatch CPUUtilization coverage to quantify"
            )
            rec["AuditBasis"] = {
                "unmeasured_inputs": ["run_hours_per_month", "minvCpus_floor"],
                "reason": "24/7 assumption rejected; bursty CE — advisory per cost-scope rule",
            }
        savings = 0.0

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Batch",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=BATCH_OPTIMIZATION_DESCRIPTIONS,
        )

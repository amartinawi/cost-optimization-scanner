"""Advisory-only adapter for Batch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.batch_svc import BATCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_batch_checks


class BatchModule(BaseServiceModule):
    """ServiceModule adapter for AWS Batch. Advisory-only savings strategy."""

    key: str = "batch"
    cli_aliases: tuple[str, ...] = ("batch",)
    display_name: str = "Batch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Batch scanning."""
        return ("batch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Batch compute environments for cost optimization opportunities.

        Detects Spot / Fargate-Spot / Graviton opportunities on Batch compute
        environments. Batch CEs are bursty (minvCpus->maxvCpus) and the scanner
        has no per-CE run-hours signal, so every recommendation is emitted as a
        $0 ``Counted=False`` advisory (it renders, but no invented dollar enters
        the headline). The prior flat per-rec rates (Spot=0.70, Graviton=0.10,
        default=0.30 x a hardcoded 730) are removed (batch C1/C2).

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
        # Count hygiene (mirror mediastore H1 / lambda): advisory ($0 Counted=False)
        # recs render but are excluded from the rec-count headline.
        counted_recs = sum(1 for r in recs if r.get("Counted") is not False)

        return ServiceFindings(
            service_name="Batch",
            total_recommendations=counted_recs,
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=BATCH_OPTIMIZATION_DESCRIPTIONS,
        )

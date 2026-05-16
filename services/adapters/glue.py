"""DPU-based pricing adapter for Glue."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.glue import GLUE_OPTIMIZATION_DESCRIPTIONS, get_enhanced_glue_checks


class GlueModule(BaseServiceModule):
    """ServiceModule adapter for AWS Glue. DPU-based savings strategy."""

    key: str = "glue"
    cli_aliases: tuple[str, ...] = ("glue",)
    display_name: str = "Glue"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Glue scanning."""
        return ("glue",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Glue jobs, dev endpoints, and crawlers for cost optimization.

        Consults the glue service module for DPU sizing, dev endpoint cleanup,
        and crawler schedule review. Savings use DPU-based pricing ($0.44/DPU-hour
        us-east-1 baseline), estimating 160 DPU-hours/month per job with 30% rightsizing.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        result = get_enhanced_glue_checks(ctx)
        recs = result.get("recommendations", [])

        # AWS Glue G.1X DPU-hour rate (us-east-1, $0.44/DPU-hr). Region-scaled
        # via pricing_multiplier at the emit site.
        GLUE_DPU_HOURLY: float = 0.44
        # Conservative rightsize factor; without per-job DPU-utilization
        # metrics from `glue.get_job_runs` we cannot derive precise savings.
        GLUE_RIGHTSIZE_FACTOR: float = 0.30
        # Job-run-hours/month assumption when actual aggregate from
        # get_job_runs isn't available. 160 hrs/mo = 8 hr/day × 20
        # workdays is reasonable for batch ETL; documented in the rec.
        ASSUMED_MONTHLY_DPU_HOURS: int = 160

        savings = 0.0
        for rec in recs:
            dpu_count = rec.get("MaxCapacity", rec.get("NumberOfWorkers"))
            if dpu_count is None:
                # Don't fabricate a 10-DPU default; skip the rec's savings
                # contribution and surface a warning.
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "MaxCapacity / NumberOfWorkers not set on rec"
                continue
            try:
                dpu_count = float(dpu_count)
            except (TypeError, ValueError):
                rec["EstimatedMonthlySavings"] = 0.0
                rec["PricingWarning"] = "DPU count not numeric"
                continue

            monthly_cost = (
                GLUE_DPU_HOURLY
                * dpu_count
                * ASSUMED_MONTHLY_DPU_HOURS
                * ctx.pricing_multiplier
            )
            rec_savings = monthly_cost * GLUE_RIGHTSIZE_FACTOR
            rec["EstimatedMonthlySavings"] = round(rec_savings, 2)
            savings += rec_savings

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="Glue",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=GLUE_OPTIMIZATION_DESCRIPTIONS,
        )

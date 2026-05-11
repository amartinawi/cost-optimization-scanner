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
        print("\U0001f50d [services/adapters/glue.py] Glue module active")
        result = get_enhanced_glue_checks(ctx)
        recs = result.get("recommendations", [])

        GLUE_DPU_HOURLY = 0.44
        savings = 0.0
        for rec in recs:
            dpu_count = rec.get("MaxCapacity", rec.get("NumberOfWorkers", 10))
            try:
                dpu_count = float(dpu_count)
            except (TypeError, ValueError):
                dpu_count = 10.0
            monthly_cost = (
                GLUE_DPU_HOURLY * dpu_count * 160 * ctx.pricing_multiplier
            )  # 160 hrs/month = 8 hrs/day × 20 workdays
            savings += monthly_cost * 0.30

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="Glue",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=GLUE_OPTIMIZATION_DESCRIPTIONS,
        )

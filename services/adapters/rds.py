"""Multi-source adapter for RDS with Compute Optimizer and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import compute_optimizer_savings, parse_dollar_savings
from services.rds import (
    RDS_OPTIMIZATION_DESCRIPTIONS,
    get_enhanced_rds_checks,
    get_rds_compute_optimizer_recommendations,
    get_rds_instance_count,
)


class RdsModule(BaseServiceModule):
    """ServiceModule adapter for RDS. Multi-source savings strategy."""

    key: str = "rds"
    cli_aliases: tuple[str, ...] = ("rds",)
    display_name: str = "RDS"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for RDS scanning."""
        return ("rds", "compute-optimizer")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan RDS instances for cost optimization opportunities.

        Consults Compute Optimizer and enhanced RDS checks. Savings
        aggregated from Hub estimates and parsed dollar-amount strings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with compute_optimizer and enhanced_checks sources.
        """
        print("\U0001f50d [services/adapters/rds.py] RDS module active")

        co_recs = []
        try:
            co_recs = get_rds_compute_optimizer_recommendations(ctx)
        except Exception as e:
            print(f"Warning: [rds] Compute Optimizer check failed: {e}")

        enhanced_recs = []
        try:
            enhanced_result = get_enhanced_rds_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days)
            enhanced_recs = enhanced_result.get("recommendations", [])
        except Exception as e:
            print(f"Warning: [rds] enhanced checks failed: {e}")

        rds_counts = {}
        try:
            rds_counts = get_rds_instance_count(ctx)
        except Exception as e:
            print(f"Warning: [rds] instance count failed: {e}")

        savings = 0.0
        savings += sum(compute_optimizer_savings(r) for r in co_recs)
        for rec in enhanced_recs:
            est = rec.get("EstimatedSavings", "")
            if "$" in est and "/month" in est:
                savings += parse_dollar_savings(est)

        total_recs = len(co_recs) + len(enhanced_recs)

        return ServiceFindings(
            service_name="RDS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
            },
            optimization_descriptions=RDS_OPTIMIZATION_DESCRIPTIONS,
            extras={"instance_counts": rds_counts},
        )

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
        savings = 0.0
        multiplier = getattr(ctx, "pricing_multiplier", 1.0)
        for rec in recs:
            category = rec.get("CheckCategory", "")
            if "Fargate Spot" in category:
                rate = 0.70
            elif "Graviton" in category:
                # AWS Graviton list-price delta x86→arm is ~20%.
                rate = 0.20
            elif "Spot" in category:
                rate = 0.70
            else:
                rate = 0.30

            instance_types = rec.get("InstanceTypes", [])
            if ctx.pricing_engine is not None and instance_types:
                # get_ec2_hourly_price returns region-correct $/hr; do NOT
                # re-multiply by ctx.pricing_multiplier (L2.3.1).
                hourly = ctx.pricing_engine.get_ec2_hourly_price(instance_types[0])
                monthly = hourly * 730
                savings += monthly * rate
            # else: instance_types unknown; skip rather than fabricate
            # $150 fallback (was over/under-stated 1.5-36x depending on
            # actual instance type).
        _ = multiplier  # documented; live path is region-correct already.

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Batch",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=BATCH_OPTIMIZATION_DESCRIPTIONS,
        )

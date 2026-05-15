"""SPICE capacity pricing adapter for QuickSight."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.quicksight import QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_quicksight_checks


class QuicksightModule(BaseServiceModule):
    """ServiceModule adapter for QuickSight. SPICE capacity pricing strategy."""

    key: str = "quicksight"
    cli_aliases: tuple[str, ...] = ("quicksight",)
    display_name: str = "QuickSight"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for QuickSight scanning."""
        return ("quicksight",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan QuickSight SPICE capacity for cost optimization opportunities.

        Consults enhanced QuickSight checks. Savings calculated via SPICE
        capacity pricing (per-GB rates by edition) with flat-rate fallback.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/quicksight.py] QuickSight module active")
        result = get_enhanced_quicksight_checks(ctx)
        recs = result.get("recommendations", [])

        # AWS QuickSight SPICE storage is $0.38/GB-month for BOTH Standard
        # and Enterprise editions (verified via the QuickSight pricing page
        # 2026-05). Previous code used $0.25 for Standard — wrong by 34%.
        # Region-scaled via ctx.pricing_multiplier per L2.3.2.
        SPICE_PRICE_PER_GB: float = 0.38
        savings = 0.0
        for rec in recs:
            spice_price = SPICE_PRICE_PER_GB * ctx.pricing_multiplier
            unused_gb = rec.get("UnusedSpiceCapacityGB", 0)
            if unused_gb > 0:
                rec_savings = unused_gb * spice_price
                rec["EstimatedMonthlySavings"] = round(rec_savings, 2)
                savings += rec_savings
            else:
                # No unused-capacity figure on the rec; skip rather than
                # fabricate $30 fallback constant.
                rec.setdefault("EstimatedMonthlySavings", 0.0)
                rec.setdefault(
                    "PricingWarning",
                    "requires UnusedSpiceCapacityGB on rec for quantified savings",
                )

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="QuickSight",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS,
        )

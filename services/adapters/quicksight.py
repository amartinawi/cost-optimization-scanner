"""SPICE capacity pricing adapter for QuickSight."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.quicksight import (
    QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS,
    quicksight_spice_rate,
    get_enhanced_quicksight_checks,
)


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

        SPICE $/GB is edition-aware (quicksight C1): Standard $0.25, Enterprise
        $0.38 (us-east-1, live-validated SKUs R8PKSKFCHES8YSKK / T4GAEKP5WQQWCUD5).

        H3 — the dollar is single-sourced here: a region-scaled value
        (``unused_gb × quicksight_spice_rate(edition) × pricing_multiplier``) is
        rounded once and written to BOTH ``EstimatedMonthlySavings`` (the counted
        number) and ``EstimatedSavings`` (the card string), so the card and the
        headline always agree in every region. The shim no longer emits a
        competing non-region-scaled string. When the edition or used-GB cannot be
        resolved the rec is demoted to an honest $0 advisory whose string matches
        its number. New rec dicts are built (no in-place mutation of shim output).
        """
        result = get_enhanced_quicksight_checks(ctx)
        recs = result.get("recommendations", [])

        priced_recs: list[dict[str, Any]] = []
        savings = 0.0
        for rec in recs:
            edition = rec.get("Edition", "")
            unused_gb = rec.get("UnusedSpiceCapacityGB", 0)
            if unused_gb > 0 and edition:
                rate = quicksight_spice_rate(edition)
                # Single source of truth for the dollar: region-scaled, rounded
                # once, then summed AND rendered from the same value so the
                # counted number equals the card string (quicksight H3).
                rec_savings = round(unused_gb * rate * ctx.pricing_multiplier, 2)
                priced_recs.append(
                    dict(
                        rec,
                        EstimatedMonthlySavings=rec_savings,
                        EstimatedSavings=f"${rec_savings:.2f}/month ({edition} SPICE rate)",
                        AuditBasis={
                            "edition": edition,
                            "rate_per_gb_month": rate,
                            "region": getattr(ctx, "region", None),
                            "used_gb": rec.get("UsedCapacityGB"),
                            "total_gb": rec.get("TotalCapacityGB"),
                            "unused_gb": unused_gb,
                            "pricing_multiplier": ctx.pricing_multiplier,
                            "formula": "unused_gb * rate_per_gb_month * pricing_multiplier",
                        },
                    )
                )
                savings += rec_savings
            else:
                # Edition/used-GB unknown → cannot resolve the edition-correct
                # rate; emit an honest $0 advisory whose string matches the number.
                advisory = dict(
                    rec,
                    Counted=False,
                    EstimatedMonthlySavings=0.0,
                    EstimatedSavings=(
                        "$0.00/month — advisory: requires UnusedSpiceCapacityGB "
                        "+ Edition on rec for quantified savings"
                    ),
                )
                advisory.setdefault(
                    "PricingWarning",
                    "requires UnusedSpiceCapacityGB + Edition on rec for quantified savings",
                )
                priced_recs.append(advisory)

        sources = {
            "enhanced_checks": SourceBlock(count=len(priced_recs), recommendations=tuple(priced_recs))
        }

        return ServiceFindings(
            service_name="QuickSight",
            total_recommendations=len(priced_recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=QUICKSIGHT_OPTIMIZATION_DESCRIPTIONS,
        )

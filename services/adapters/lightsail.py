"""Bundle-based pricing adapter for Lightsail."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.lightsail import LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS, get_enhanced_lightsail_checks


class LightsailModule(BaseServiceModule):
    """ServiceModule adapter for Lightsail. Bundle-based savings strategy."""

    key: str = "lightsail"
    cli_aliases: tuple[str, ...] = ("lightsail",)
    display_name: str = "Lightsail"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Lightsail scanning."""
        return ("lightsail",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Lightsail instances and static IPs for cost optimization.

        Consults enhanced Lightsail checks. Savings calculated via bundle-based
        pricing when available, flat-rate heuristic as fallback.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with per-check-type SourceBlock entries.
        """
        print("\U0001f50d [services/adapters/lightsail.py] Lightsail module active")
        result = get_enhanced_lightsail_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        for rec in recs:
            bundle_name = rec.get("BundleId", "")
            if ctx.pricing_engine and bundle_name:
                monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonLightsail", bundle_name)
                savings += monthly if monthly > 0 else 12.0 * ctx.pricing_multiplier
            else:
                savings += 12.0 * ctx.pricing_multiplier

        checks = result.get("checks", {})
        sources = {k: SourceBlock(count=len(v), recommendations=tuple(v)) for k, v in checks.items()}

        return ServiceFindings(
            service_name="Lightsail",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=LIGHTSAIL_OPTIMIZATION_DESCRIPTIONS,
        )

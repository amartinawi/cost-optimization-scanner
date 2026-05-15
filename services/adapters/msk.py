"""Flat-rate adapter for MSK."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.msk import MSK_OPTIMIZATION_DESCRIPTIONS, get_enhanced_msk_checks


class MskModule(BaseServiceModule):
    """ServiceModule adapter for MSK. Flat-rate savings strategy."""

    key: str = "msk"
    cli_aliases: tuple[str, ...] = ("msk",)
    display_name: str = "MSK"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for MSK scanning."""
        return ("kafka",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan MSK clusters for cost optimization opportunities.

        Consults enhanced MSK checks. Savings calculated via flat-rate
        heuristic per recommendation.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        # TODO: Serverless migration comparison needs DCU-hour pricing ($0.06/DCU-hour).
        # Current recommendation is directional only, not cost-quantified.
        print("\U0001f50d [services/adapters/msk.py] MSK module active")
        result = get_enhanced_msk_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        if ctx.pricing_engine is not None:
            for rec in recs:
                instance_type = rec.get("InstanceType")
                num_brokers = rec.get("NumberOfBrokerNodes", 3)
                if instance_type:
                    # Broker price from PricingEngine is region-correct
                    # already; storage rate is a module-const so it needs
                    # the multiplier applied independently.
                    hourly = ctx.pricing_engine.get_msk_broker_hourly_price(instance_type)
                    broker_monthly = hourly * 730 * num_brokers
                    volume_size = rec.get("BrokerStorageGB", 100)
                    storage_monthly = (
                        volume_size * 0.10 * num_brokers * ctx.pricing_multiplier
                    )
                    savings += (broker_monthly + storage_monthly) * 0.30
                # else: instance_type unknown; skip rather than fabricate $150.
        # else: pricing engine unavailable; cannot quantify — emit 0 (no recs).

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="MSK",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=MSK_OPTIMIZATION_DESCRIPTIONS,
        )

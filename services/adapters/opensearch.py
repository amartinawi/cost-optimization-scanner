"""Keyword-rate adapter for OpenSearch."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.opensearch import OPENSEARCH_OPTIMIZATION_DESCRIPTIONS, get_enhanced_opensearch_checks

# OpenSearch-managed gp3 EBS storage rate ($/GB-month, us-east-1 baseline).
# Region-scaled via pricing_multiplier at the per-rec emit site.
GP3_PRICE_PER_GB_MONTH: float = 0.11
# Conservative midpoint of cold-tier / UltraWarm storage savings vs gp3.
STORAGE_SAVINGS_FACTOR: float = 0.20


class OpensearchModule(BaseServiceModule):
    """ServiceModule adapter for OpenSearch. Keyword-rate savings strategy."""

    key: str = "opensearch"
    cli_aliases: tuple[str, ...] = ("opensearch",)
    display_name: str = "OpenSearch"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for OpenSearch scanning."""
        return ("opensearch",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan OpenSearch domains for cost optimization opportunities.

        Consults enhanced OpenSearch checks. Savings calculated via keyword-rate
        heuristics matching Reserved, Graviton, and storage patterns.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/opensearch.py] OpenSearch module active")
        result = get_enhanced_opensearch_checks(ctx)
        recs = result.get("recommendations", [])

        savings = 0.0
        for rec in recs:
            est = rec.get("EstimatedSavings", "")
            if "Reserved" in est:
                ri_rate = 0.35
            elif "Graviton" in est or "20-40%" in est:
                ri_rate = 0.25
            elif "storage" in est.lower():
                ri_rate = 0.20
            else:
                ri_rate = 0.30

            instance_type = rec.get("InstanceType")
            instance_count = rec.get("InstanceCount", 1)

            if ctx.pricing_engine is not None and instance_type:
                # PricingEngine returns region-correct $/month; do NOT
                # re-multiply by ctx.pricing_multiplier (L2.3.1).
                monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonES", instance_type)
                savings += monthly * instance_count * ri_rate
            else:
                if "Reserved" in est:
                    savings += 300 * ctx.pricing_multiplier
                elif "Graviton" in est or "20-40%" in est:
                    savings += 120 * ctx.pricing_multiplier
                elif "storage" in est.lower():
                    savings += 50 * ctx.pricing_multiplier

            ebs_volume_size = rec.get("EBSVolumeSize", 0)
            if ebs_volume_size > 0:
                # gp3 rate is module-const → apply pricing_multiplier (L2.3.2).
                # Storage savings are NOT a function of instance ri_rate
                # (reserved capacity doesn't discount storage).
                storage_monthly = ebs_volume_size * GP3_PRICE_PER_GB_MONTH * ctx.pricing_multiplier
                savings += storage_monthly * STORAGE_SAVINGS_FACTOR

        return ServiceFindings(
            service_name="OpenSearch",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            optimization_descriptions=OPENSEARCH_OPTIMIZATION_DESCRIPTIONS,
        )

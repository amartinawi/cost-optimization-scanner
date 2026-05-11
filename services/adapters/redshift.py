"""Flat-rate adapter for Redshift."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.redshift import REDSHIFT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_redshift_checks

REDSHIFT_NODE_MONTHLY_FALLBACK: float = 200.0


class RedshiftModule(BaseServiceModule):
    """ServiceModule adapter for Redshift. Flat-rate savings strategy."""

    key: str = "redshift"
    cli_aliases: tuple[str, ...] = ("redshift",)
    display_name: str = "Redshift"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Redshift scanning."""
        return ("redshift",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Redshift clusters for cost optimization opportunities.

        Consults enhanced Redshift checks. Savings calculated via flat-rate
        heuristic per recommendation.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/redshift.py] Redshift module active")
        # TODO: RA3 node types charge managed storage at $0.024/GB/month.
        # Current calculation only covers instance pricing, not RMS storage.
        result = get_enhanced_redshift_checks(ctx)
        recs = result.get("recommendations", [])
        savings = 0.0
        if ctx.pricing_engine is not None:
            for rec in recs:
                node_type = rec.get("NodeType")
                num_nodes = rec.get("NumberOfNodes", 1)
                if node_type:
                    monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonRedshift", node_type)
                    savings += monthly * num_nodes * 0.24
                else:
                    savings += REDSHIFT_NODE_MONTHLY_FALLBACK * ctx.pricing_multiplier
        else:
            savings = REDSHIFT_NODE_MONTHLY_FALLBACK * len(recs)

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Redshift",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=REDSHIFT_OPTIMIZATION_DESCRIPTIONS,
        )

"""Keyword-rate adapter for ElastiCache."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.elasticache import get_enhanced_elasticache_checks


class ElasticacheModule(BaseServiceModule):
    """ServiceModule adapter for ElastiCache. Keyword-rate savings strategy."""

    key: str = "elasticache"
    cli_aliases: tuple[str, ...] = ("elasticache",)
    display_name: str = "ElastiCache"
    reads_fast_mode: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for ElastiCache scanning."""
        return ("elasticache", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan ElastiCache clusters for cost optimization opportunities.

        Consults the elasticache service module for Graviton migration, reserved
        nodes, underutilized clusters, engine version review, and Valkey evaluation.
        Savings calculated via keyword matching on EstimatedSavings text.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
        print("\U0001f50d [services/adapters/elasticache.py] ElastiCache module active")
        result = get_enhanced_elasticache_checks(ctx)
        recs = result.get("recommendations", [])

        savings = 0.0
        for rec in recs:
            est = rec.get("EstimatedSavings", "")
            if "Reserved" in est:
                ri_rate = 0.35
            elif "Graviton" in est or "20-40%" in est:
                ri_rate = 0.05
            elif "Valkey" in est:
                ri_rate = 0.20
            elif "Underutilized" in est:
                ri_rate = 0.40
            else:
                ri_rate = 0.20

            node_type = rec.get("NodeType")
            num_nodes = rec.get("NumNodes", 1)

            if ctx.pricing_engine is not None and node_type:
                monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)
                savings += monthly * num_nodes * ri_rate * ctx.pricing_multiplier
            else:
                if "Reserved" in est:
                    savings += 60 * ctx.pricing_multiplier
                elif "Graviton" in est or "20-40%" in est:
                    savings += 80 * ctx.pricing_multiplier
                elif "Valkey" in est:
                    savings += 50 * ctx.pricing_multiplier
                elif "Underutilized" in est:
                    savings += 100 * ctx.pricing_multiplier

        return ServiceFindings(
            service_name="ElastiCache",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
        )

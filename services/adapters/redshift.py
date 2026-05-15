"""Flat-rate adapter for Redshift."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.redshift import REDSHIFT_OPTIMIZATION_DESCRIPTIONS, get_enhanced_redshift_checks

REDSHIFT_NODE_MONTHLY_FALLBACK: float = 200.0

# Per-CheckCategory savings factors (AWS-documented midpoints).
# Reserved Instances: 30-75% range, midpoint 0.52.
# Pause/Resume of inactive clusters: full compute hours saved (100%).
# Default for unrecognized categories: conservative 0.24 midpoint.
REDSHIFT_SAVINGS_FACTORS: dict[str, float] = {
    "reserved": 0.52,
    "pause": 1.00,
    "default": 0.24,
}


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
        def _rs_factor(rec: dict[str, Any]) -> float:
            cat = (rec.get("CheckCategory") or "").lower()
            if "reserved" in cat:
                return REDSHIFT_SAVINGS_FACTORS["reserved"]
            if "pause" in cat or "unused" in cat or "inactive" in cat:
                return REDSHIFT_SAVINGS_FACTORS["pause"]
            return REDSHIFT_SAVINGS_FACTORS["default"]

        savings = 0.0
        if ctx.pricing_engine is not None:
            for rec in recs:
                node_type = rec.get("NodeType")
                num_nodes = rec.get("NumberOfNodes", 1)
                factor = _rs_factor(rec)
                if node_type:
                    # PricingEngine returns region-correct $/month;
                    # do NOT re-multiply by pricing_multiplier (L2.3.1).
                    monthly = ctx.pricing_engine.get_instance_monthly_price("AmazonRedshift", node_type)
                    if monthly > 0:
                        savings += monthly * num_nodes * factor
                # else: node_type unknown; skip rather than fabricate
                # REDSHIFT_NODE_MONTHLY_FALLBACK (was undocumented $200).
        # else: pricing engine unavailable; no quantified savings emitted.

        sources = {"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))}

        return ServiceFindings(
            service_name="Redshift",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources=sources,
            optimization_descriptions=REDSHIFT_OPTIMIZATION_DESCRIPTIONS,
        )

"""Multi-source adapter for EBS with Compute Optimizer, unattached volumes, and GP2 migration."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import compute_optimizer_savings, parse_dollar_savings
from services.ebs import (
    EBS_OPTIMIZATION_DESCRIPTIONS,
    compute_ebs_checks,
    get_ebs_compute_optimizer_recs,
    get_ebs_volume_count,
    get_unattached_volumes,
)

logger = logging.getLogger(__name__)


def _gp2_to_gp3_savings_per_gb(ctx: Any) -> float:
    """Return the $/GB-month difference between gp2 and gp3 for this region.

    Uses PricingEngine for region-correct prices (no additional multiplier
    required — PricingEngine values are already regional). Falls back to the
    us-east-1 delta of $0.10 − $0.08 = $0.02 scaled by `ctx.pricing_multiplier`
    when PricingEngine is unavailable.
    """
    if ctx.pricing_engine:
        gp2 = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
        gp3 = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
        return max(gp2 - gp3, 0.0)
    return 0.10 * 0.20 * ctx.pricing_multiplier


class EbsModule(BaseServiceModule):
    """ServiceModule adapter for EBS. Multi-source savings strategy."""

    key: str = "ebs"
    cli_aliases: tuple[str, ...] = ("ebs",)
    display_name: str = "EBS"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for EBS scanning."""
        return ("ec2", "compute-optimizer")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EBS volumes for cost optimization opportunities.

        Consults Compute Optimizer, unattached volume detection, GP2-to-GP3
        migration checks, and enhanced EBS service module.

        Per-source savings:

        - **compute_optimizer**: read via :func:`compute_optimizer_savings` from
          the nested ``volumeRecommendationOptions[N].savingsOpportunity.
          estimatedMonthlySavings.value`` AWS path.
        - **unattached_volumes**: ``EstimatedMonthlyCost`` (live, from
          ``PricingEngine.get_ebs_monthly_price_per_gb``).
        - **gp2_migration**: per-volume ``size × (gp2 − gp3)`` using region-correct
          PricingEngine prices. **No** ``pricing_multiplier`` applied to the live
          path (would double-multiply).
        - **enhanced_checks**: parsed from each rec's ``EstimatedSavings`` string.
          The ``"Unattached Volumes"`` category is filtered out here because the
          ``unattached_volumes`` source already carries those entries — see the
          audit note about `total_recommendations` double-count.
        """
        logger.debug("EBS adapter scan starting")

        enhanced_result = compute_ebs_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days)
        enhanced_recs = enhanced_result.get("recommendations", [])
        co_recs = get_ebs_compute_optimizer_recs(ctx, ctx.pricing_multiplier)
        unattached_volumes = get_unattached_volumes(ctx, ctx.pricing_multiplier)

        # Categories that have their own dedicated source / renderer.
        dedicated_categories = {"Volume Type Optimization", "Unattached Volumes"}

        gp2_recs = [r for r in enhanced_recs if r.get("CheckCategory") == "Volume Type Optimization"]
        other_recs = [r for r in enhanced_recs if r.get("CheckCategory") not in dedicated_categories]

        # Compute per-volume gp2→gp3 savings and stash on each rec so the
        # renderer can show real dollars instead of "20% cost reduction" prose.
        delta_per_gb = _gp2_to_gp3_savings_per_gb(ctx)
        gp2_total = 0.0
        for rec in gp2_recs:
            size = rec.get("Size", 0)
            per_vol = size * delta_per_gb
            rec["EstimatedSavings"] = f"${per_vol:.2f}/month"
            gp2_total += per_vol

        savings = 0.0
        savings += sum(v.get("EstimatedMonthlyCost", 0) for v in unattached_volumes)
        for rec in other_recs:
            savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))
        savings += gp2_total
        savings += sum(compute_optimizer_savings(r) for r in co_recs)

        total_recs = len(co_recs) + len(unattached_volumes) + len(gp2_recs) + len(other_recs)

        return ServiceFindings(
            service_name="EBS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "unattached_volumes": SourceBlock(
                    count=len(unattached_volumes), recommendations=tuple(unattached_volumes)
                ),
                "gp2_migration": SourceBlock(count=len(gp2_recs), recommendations=tuple(gp2_recs)),
                "enhanced_checks": SourceBlock(count=len(other_recs), recommendations=tuple(other_recs)),
            },
            optimization_descriptions=EBS_OPTIMIZATION_DESCRIPTIONS,
            extras={"volume_counts": get_ebs_volume_count(ctx)},
        )

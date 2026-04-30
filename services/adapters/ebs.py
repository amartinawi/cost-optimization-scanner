"""Multi-source adapter for EBS with Compute Optimizer, unattached volumes, and GP2 migration."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import parse_dollar_savings
from services.ebs import (
    EBS_OPTIMIZATION_DESCRIPTIONS,
    compute_ebs_checks,
    get_ebs_compute_optimizer_recs,
    get_ebs_volume_count,
    get_unattached_volumes,
)


class EbsModule(BaseServiceModule):
    key: str = "ebs"
    cli_aliases: tuple[str, ...] = ("ebs",)
    display_name: str = "EBS"

    def required_clients(self) -> tuple[str, ...]:
        return ("ec2", "compute-optimizer")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/ebs.py] EBS module active")

        enhanced_result = compute_ebs_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days)
        enhanced_recs = enhanced_result.get("recommendations", [])
        co_recs = get_ebs_compute_optimizer_recs(ctx, ctx.pricing_multiplier)
        unattached_volumes = get_unattached_volumes(ctx, ctx.pricing_multiplier)

        gp2_recs = [r for r in enhanced_recs if r.get("CheckCategory") == "Volume Type Optimization"]
        other_recs = [r for r in enhanced_recs if r.get("CheckCategory") != "Volume Type Optimization"]

        savings = 0.0
        savings += sum(v.get("EstimatedMonthlyCost", 0) for v in unattached_volumes)
        for rec in other_recs:
            savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))
        for rec in gp2_recs:
            size = rec.get("Size", 0)
            savings += size * 0.10 * 0.20
        savings += sum(r.get("estimatedMonthlySavings", 0) for r in co_recs)

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

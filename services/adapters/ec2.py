"""Multi-source adapter for EC2 with Cost Hub, Compute Optimizer, and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import parse_dollar_savings
from services.advisor import get_ec2_compute_optimizer_recommendations
from services.ec2 import get_advanced_ec2_checks, get_ec2_instance_count, get_enhanced_ec2_checks


class EC2Module(BaseServiceModule):
    key: str = "ec2"
    cli_aliases: tuple[str, ...] = ("ec2",)
    display_name: str = "EC2"

    def required_clients(self) -> tuple[str, ...]:
        return ("ec2", "compute-optimizer", "autoscaling")

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/ec2.py] EC2 module active")

        cost_hub_recs = ctx.cost_hub_splits.get("ec2", [])
        co_recs = get_ec2_compute_optimizer_recommendations(ctx)
        enhanced_result = get_enhanced_ec2_checks(ctx, ctx.pricing_multiplier)
        enhanced_recs = enhanced_result.get("recommendations", [])
        advanced_result = get_advanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode)
        advanced_recs = advanced_result.get("recommendations", [])

        savings = 0.0
        savings += sum(rec.get("estimatedMonthlySavings", 0) for rec in cost_hub_recs)
        co_total = sum(rec.get("estimatedMonthlySavings", 0) for rec in co_recs)
        if co_total > 0:
            savings += co_total
        for rec in enhanced_recs:
            savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))
        for rec in advanced_recs:
            savings += parse_dollar_savings(rec.get("EstimatedSavings", ""))

        total_recs = len(cost_hub_recs) + len(co_recs) + len(enhanced_recs) + len(advanced_recs)

        return ServiceFindings(
            service_name="EC2",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
                "advanced_ec2_checks": SourceBlock(count=len(advanced_recs), recommendations=tuple(advanced_recs)),
            },
            extras={"instance_count": get_ec2_instance_count(ctx)},
        )

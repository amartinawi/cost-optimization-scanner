"""Field-extraction adapter for AMI."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.ami import compute_ami_checks


class AmiModule(BaseServiceModule):
    key: str = "ami"
    cli_aliases: tuple[str, ...] = ("ami",)
    display_name: str = "AMI"

    def required_clients(self) -> tuple[str, ...]:
        return ("ec2",)

    def scan(self, ctx: Any) -> ServiceFindings:
        print("\U0001f50d [services/adapters/ami.py] AMI module active")
        result = compute_ami_checks(ctx, ctx.pricing_multiplier)
        recs = result.get("recommendations", [])
        savings = sum(rec.get("EstimatedMonthlySavings", 0) for rec in recs)

        return ServiceFindings(
            service_name="AMI",
            total_recommendations=len(recs),
            total_monthly_savings=savings,
            sources={"old_amis": SourceBlock(count=len(recs), recommendations=tuple(recs))},
            total_count=result.get("total_count", 0),
        )

"""Field-extraction adapter for AMI."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.ami import compute_ami_checks


class AmiModule(BaseServiceModule):
    """ServiceModule adapter for AMI. Field-extraction savings strategy."""

    key: str = "ami"
    cli_aliases: tuple[str, ...] = ("ami",)
    display_name: str = "AMI"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for AMI scanning."""
        return ("ec2", "autoscaling")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan AMI resources for cost optimization opportunities.

        Consults the ami service module for old/unused AMI detection.
        Savings calculated from per-recommendation EstimatedMonthlySavings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "old_amis" SourceBlock entry.
        """
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
            optimization_descriptions={
                "old_amis": {
                    "title": "Delete Old and Unused AMIs",
                    "description": (
                        "Old and unused AMIs retain associated EBS snapshots that incur"
                        " ongoing storage costs. Deregister unused AMIs and delete orphaned snapshots."
                    ),
                    "action": (
                        "1. Identify AMIs not associated with any running instance\n"
                        "2. Deregister unused AMIs via AWS Console or CLI\n"
                        "3. Delete orphaned snapshots left behind\n"
                        "4. Estimated savings: snapshot storage cost per GB-month"
                    ),
                },
            },
        )

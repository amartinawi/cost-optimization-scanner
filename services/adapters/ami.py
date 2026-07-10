"""Field-extraction adapter for AMI."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._reconcile import reconcile_against_billed
from services.advisor import get_ebs_snapshot_actuals
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

        Emits two mutually exclusive sources, split by age purely for
        confidence/presentation — both are unused (deletion-safe) AMIs and
        both carry quantified savings from EBS-snapshot storage:

          - ``old_amis``    — unused AMIs older than the snapshot-retention
                              threshold (stale, high confidence).
          - ``unused_amis`` — unused AMIs newer than that threshold but past
                              the action floor (verify before deletion).

        Args:
            ctx: ScanContext with region, clients, and pricing data.
        """
        result = compute_ami_checks(ctx, ctx.pricing_multiplier)
        old_recs = result.get("old_amis", [])
        unused_recs = result.get("unused_amis", [])

        # An AMI's saving is priced from its backing snapshot's FULL size, but EBS
        # snapshots bill only the unique changed blocks across a chain — so it is an
        # UPPER BOUND. Corroborate the whole region's AMI snapshot savings against
        # the billed EBS:SnapshotUsage pool and cap them at it. When Cost Explorer
        # cannot be read the bound is unsubstantiated and every rec is demoted to a
        # $0 advisory: removing evidence must never raise counted savings (C8).
        n_old = len(old_recs)
        billed = get_ebs_snapshot_actuals(ctx)
        reconciled, savings = reconcile_against_billed(
            old_recs + unused_recs,
            billed,
            pool_label="EBS snapshot storage",
            grant_hint="grant ce:GetCostAndUsage to corroborate against billed EBS:SnapshotUsage",
            on_contradiction=lambda msg: ctx.warn(msg, service="ami"),
        )
        old_recs, unused_recs = reconciled[:n_old], reconciled[n_old:]

        total_recs = len(old_recs) + len(unused_recs)

        return ServiceFindings(
            service_name="AMI",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "old_amis": SourceBlock(count=len(old_recs), recommendations=tuple(old_recs)),
                "unused_amis": SourceBlock(count=len(unused_recs), recommendations=tuple(unused_recs)),
            },
            total_count=result.get("total_count", 0),
            optimization_descriptions={
                "old_amis": {
                    "title": "Delete Old and Unused AMIs",
                    "description": (
                        "Old AMIs retain associated EBS snapshots that incur"
                        " ongoing storage costs. Deregister and delete orphaned snapshots."
                    ),
                    "action": (
                        "1. Identify AMIs older than 90 days\n"
                        "2. Deregister via AWS Console or CLI\n"
                        "3. Delete orphaned snapshots left behind\n"
                        "4. Savings = snapshot storage cost per GB-month"
                    ),
                },
                "unused_amis": {
                    "title": "Review Unused AMIs",
                    "description": (
                        "AMIs not referenced by any running instance, launch"
                        " template, or ASG. Savings = backing EBS-snapshot"
                        " storage cost per GB-month (max estimate)."
                    ),
                    "action": (
                        "1. Verify the AMI is truly orphaned\n"
                        "2. Deregister via AWS Console or CLI\n"
                        "3. Delete the backing snapshots"
                    ),
                },
            },
        )

"""Multi-source adapter for EC2 with Cost Hub, Compute Optimizer, and enhanced checks."""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import compute_optimizer_savings, parse_dollar_savings
from services.advisor import (
    get_asg_compute_optimizer_recommendations,
    get_ec2_compute_optimizer_recommendations,
)
from services.ec2 import get_advanced_ec2_checks, get_ec2_instance_count, get_enhanced_ec2_checks

logger = logging.getLogger(__name__)


def _coh_is_renderable(rec: dict[str, Any]) -> bool:
    """Mirror the reporter's EC2 Cost-Hub render filter.

    The reporter (``_filter_ec2_recommendations``) drops EBS-volume recs,
    Reserved-Instance purchase recs, and N/A-resource recs from the EC2 table.
    Applying the same predicate here means the savings/count the adapter reports
    match exactly what the EC2 tab renders — no counted-but-not-shown dollars.
    """
    if rec.get("actionType") and "ebsVolume" in (rec.get("currentResourceDetails") or {}):
        return False
    if rec.get("actionType") == "PurchaseReservedInstances":
        return False
    if rec.get("actionType") and rec.get("resourceId") == "N/A":
        return False
    return True


def _coh_instance_id(rec: dict[str, Any]) -> str:
    """Instance id for a Cost Optimization Hub EC2 recommendation."""
    return str(rec.get("resourceId", "") or "")


def _co_instance_id(rec: dict[str, Any]) -> str:
    """Instance id for a Compute Optimizer EC2 recommendation (from instanceArn)."""
    arn = str(rec.get("instanceArn", "") or "")
    return arn.split("/")[-1] if "/" in arn else arn


class EC2Module(BaseServiceModule):
    """ServiceModule adapter for EC2. Multi-source savings strategy."""

    key: str = "ec2"
    cli_aliases: tuple[str, ...] = ("ec2",)
    display_name: str = "EC2"
    reads_fast_mode: bool = True
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for EC2 scanning."""
        return ("ec2", "compute-optimizer", "autoscaling")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EC2 instances for cost optimization opportunities.

        Consults Cost Optimization Hub, Compute Optimizer, enhanced checks,
        and advanced EC2 service modules. Savings aggregated from all sources
        using estimatedMonthlySavings and parse_dollar_savings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with "cost_optimization_hub", "compute_optimizer",
            "enhanced_checks", and "advanced_ec2_checks" SourceBlock entries.
        """
        logger.debug("EC2 adapter scan starting")

        # --- Gather raw recommendations from every source ----------------------
        cost_hub_recs = [r for r in ctx.cost_hub_splits.get("ec2", []) if _coh_is_renderable(r)]
        co_raw = get_ec2_compute_optimizer_recommendations(ctx)
        # The advisor returns a synthetic $0 "enable Compute Optimizer" placeholder
        # when CO is not opted in. That is an informational signal, not a cost
        # recommendation — surface it as a warning instead of a $0-savings finding
        # that would inflate the recommendation count.
        if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_raw):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — EC2 rightsizing recommendations from "
                "Compute Optimizer are unavailable (enable it for additional savings detection).",
                service="ec2",
            )
        co_recs_all = [r for r in co_raw if r.get("ResourceId") != "compute-optimizer-service"]
        asg_co_recs = get_asg_compute_optimizer_recommendations(ctx)
        enhanced_recs = get_enhanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode).get(
            "recommendations", []
        )
        advanced_recs = get_advanced_ec2_checks(ctx, ctx.pricing_multiplier, ctx.fast_mode).get(
            "recommendations", []
        )

        # --- Cross-source de-duplication by instance id ------------------------
        # Cost Optimization Hub surfaces Compute Optimizer's own rightsizing
        # findings, and the heuristic CloudWatch checks re-detect the same
        # instances. Counting all three would inflate savings 2-3x for one
        # instance. Authority order: Cost Hub > Compute Optimizer > heuristics.
        covered: set[str] = {_coh_instance_id(r) for r in cost_hub_recs if _coh_instance_id(r)}

        co_recs = [r for r in co_recs_all if _co_instance_id(r) not in covered]
        covered |= {_co_instance_id(r) for r in co_recs if _co_instance_id(r)}

        # Heuristic recs (enhanced + advanced): drop any instance already covered
        # by an AWS source, then keep at most ONE finding per instance — the
        # highest-savings one — so overlapping checks (idle + prev-gen + cron …)
        # on the same instance never stack.
        best_by_instance: dict[str, tuple[str, dict[str, Any], float]] = {}
        for origin, rec in (
            [("enhanced", r) for r in enhanced_recs] + [("advanced", r) for r in advanced_recs]
        ):
            iid = str(rec.get("InstanceId", "") or "")
            if iid and iid in covered:
                continue
            sav = parse_dollar_savings(rec.get("EstimatedSavings", ""))
            if sav <= 0:
                continue
            key = iid or f"_anon_{id(rec)}"
            existing = best_by_instance.get(key)
            if existing is None or sav > existing[2]:
                best_by_instance[key] = (origin, rec, sav)

        enhanced_final = [rec for origin, rec, _ in best_by_instance.values() if origin == "enhanced"]
        advanced_final = [rec for origin, rec, _ in best_by_instance.values() if origin == "advanced"]

        # --- Savings (each instance counted once) ------------------------------
        savings = 0.0
        savings += sum(float(rec.get("estimatedMonthlySavings", 0.0) or 0.0) for rec in cost_hub_recs)
        savings += sum(compute_optimizer_savings(rec) for rec in co_recs)
        savings += sum(float(rec.get("estimatedMonthlySavings", 0.0) or 0.0) for rec in asg_co_recs)
        savings += sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in enhanced_final)
        savings += sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in advanced_final)

        enhanced_recs = enhanced_final
        advanced_recs = advanced_final
        total_recs = (
            len(cost_hub_recs)
            + len(co_recs)
            + len(asg_co_recs)
            + len(enhanced_recs)
            + len(advanced_recs)
        )

        return ServiceFindings(
            service_name="EC2",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(cost_hub_recs), recommendations=tuple(cost_hub_recs)),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "asg_compute_optimizer": SourceBlock(count=len(asg_co_recs), recommendations=tuple(asg_co_recs)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
                "advanced_ec2_checks": SourceBlock(count=len(advanced_recs), recommendations=tuple(advanced_recs)),
            },
            extras={"instance_count": get_ec2_instance_count(ctx)},
        )

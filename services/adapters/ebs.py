"""Multi-source adapter for EBS: Cost Hub, Compute Optimizer, unattached, gp2→gp3, enhanced checks."""

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
from services.ebs_logic import (
    dedupe_by_authority,
    gp2_baseline_iops,
    gp2_to_gp3_net_savings,
    partition_enhanced_recs,
)

logger = logging.getLogger(__name__)

# us-east-1 fallback $/GB-month for the gp2→gp3 delta when PricingEngine is down.
from core.pricing_engine import FALLBACK_EBS_GB_MONTH


def _gp2_to_gp3_savings_per_gb(ctx: Any) -> float:
    """Return the $/GB-month difference between gp2 and gp3 for this region.

    Uses PricingEngine for region-correct prices (no additional multiplier
    required — PricingEngine values are already regional). Falls back to the
    us-east-1 delta (``FALLBACK_EBS_GB_MONTH["gp2"] - [...]["gp3"]`` = $0.02)
    scaled by ``ctx.pricing_multiplier`` when PricingEngine is unavailable.
    """
    if ctx.pricing_engine:
        gp2 = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp2")
        gp3 = ctx.pricing_engine.get_ebs_monthly_price_per_gb("gp3")
        return max(gp2 - gp3, 0.0)
    delta = FALLBACK_EBS_GB_MONTH["gp2"] - FALLBACK_EBS_GB_MONTH["gp3"]
    return delta * ctx.pricing_multiplier


def _coh_is_renderable(rec: dict[str, Any]) -> bool:
    """Mirror the reporter's EBS Cost-Hub render filter (``_render_ebs_cost_hub``).

    Only EBS-volume recs that carry an ``actionType`` and are not already
    ``Optimized`` render in the EBS tab; applying the same predicate here keeps
    the counted savings/total in step with the rendered cards.
    """
    if "actionType" not in rec:
        return False
    if "ebsVolume" not in (rec.get("currentResourceDetails") or {}):
        return False
    return str(rec.get("finding", "")).lower() != "optimized"


def _drop_stale_delete_recs(ctx: Any, coh_recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop Cost-Hub 'delete' recs whose volume is no longer unattached.

    Cost Optimization Hub data lags live state, so it can recommend deleting a
    volume that has since been re-attached. Acting on that would destroy a live
    instance's storage. For deletion actions we re-check the live volume state
    and keep only volumes that are actually ``available`` (unattached). Non-delete
    actions (e.g. rightsizing an attached volume) are left untouched. On any
    error (e.g. ec2 permission) the recs pass through unchanged.
    """
    delete_recs = [r for r in coh_recs if "delete" in str(r.get("actionType", "")).lower()]
    if not delete_recs:
        return coh_recs
    vol_ids = [str(r.get("resourceId", "")) for r in delete_recs if str(r.get("resourceId", "")).startswith("vol-")]
    if not vol_ids:
        return coh_recs
    try:
        ec2 = ctx.client("ec2")
        available: set[str] = set()
        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate(VolumeIds=vol_ids):
            for v in page.get("Volumes", []):
                if v.get("State") == "available":
                    available.add(v.get("VolumeId", ""))
    except Exception as e:
        ctx.warn(f"could not verify EBS volume state for CoH delete recs: {e}", "ebs")
        return coh_recs

    kept: list[dict[str, Any]] = []
    for r in coh_recs:
        vid = str(r.get("resourceId", ""))
        if "delete" in str(r.get("actionType", "")).lower() and vid.startswith("vol-") and vid not in available:
            ctx.warn(
                f"Cost Hub recommends deleting {vid} but it is currently attached/in-use — "
                "dropping the stale recommendation.",
                "ebs",
            )
            continue
        kept.append(r)
    return kept


class EbsModule(BaseServiceModule):
    """ServiceModule adapter for EBS. Multi-source savings strategy."""

    key: str = "ebs"
    cli_aliases: tuple[str, ...] = ("ebs",)
    display_name: str = "EBS"
    reads_fast_mode: bool = True
    requires_cloudwatch: bool = True  # over-provisioned IOPS check reads AWS/EBS metrics.

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for EBS scanning."""
        return ("ec2", "compute-optimizer", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan EBS volumes for cost optimization opportunities.

        Consults Cost Optimization Hub (orchestrator-routed), Compute Optimizer,
        unattached-volume detection, gp2→gp3 migration, and enhanced checks.
        Findings are de-duplicated across sources by volume id with authority
        order **Cost Hub > Compute Optimizer > heuristics** so no volume's
        savings is counted twice.

        Per-source savings:

        - **cost_optimization_hub**: flat ``estimatedMonthlySavings`` (AWS-priced).
        - **compute_optimizer**: :func:`compute_optimizer_savings` from the nested
          ``volumeRecommendationOptions[N].savingsOpportunity`` path.
        - **unattached_volumes**: ``EstimatedMonthlyCost`` (live PricingEngine).
        - **gp2_migration**: per-volume ``size × (gp2 − gp3)`` (region-correct).
        - **enhanced_checks**: parsed from each rec's ``EstimatedSavings`` string.

        Snapshot findings are emitted under ``ebs_snapshots`` for the dedicated
        Snapshots tab and are intentionally **excluded** from this tab's counted
        savings and totals (so the EBS headline matches its rendered cards).
        """
        logger.debug("EBS adapter scan starting")

        # --- Gather raw recommendations from every source ----------------------
        coh_recs = [r for r in getattr(ctx, "cost_hub_splits", {}).get("ebs", []) if _coh_is_renderable(r)]
        # Drop stale CoH delete recs on volumes that are now attached (live check).
        coh_recs = _drop_stale_delete_recs(ctx, coh_recs)

        co_raw = get_ebs_compute_optimizer_recs(ctx)
        # The advisor returns a synthetic $0 "enable Compute Optimizer" placeholder
        # when CO is not opted in. That is an informational signal, not a cost
        # finding — surface it as a warning rather than a $0-savings recommendation
        # that would inflate the count and render to nothing.
        if any(r.get("ResourceId") == "compute-optimizer-service" for r in co_raw):
            ctx.warn(
                "AWS Compute Optimizer is not enabled — EBS rightsizing recommendations from "
                "Compute Optimizer are unavailable (enable it for additional savings detection).",
                service="ebs",
            )
        co_recs_all = [r for r in co_raw if r.get("ResourceId") != "compute-optimizer-service"]

        enhanced_recs = compute_ebs_checks(ctx, ctx.pricing_multiplier, ctx.old_snapshot_days).get(
            "recommendations", []
        )
        unattached_volumes = get_unattached_volumes(ctx, ctx.pricing_multiplier)

        gp2_recs, snapshot_recs, other_recs = partition_enhanced_recs(enhanced_recs)

        # Drop $0 "NotOptimized"/no-action Compute Optimizer recs — they carry no
        # savings and only inflate the count (mirrors the EC2/ECS CO helpers).
        co_recs_all = [r for r in co_recs_all if compute_optimizer_savings(r) > 0]

        # --- Cross-source de-duplication by volume id --------------------------
        co_recs, (unattached_kept, gp2_kept, other_kept) = dedupe_by_authority(
            coh_recs, co_recs_all, [unattached_volumes, gp2_recs, other_recs]
        )

        # ebs L3: the gp2→gp3, unattached, and other levers below already attach an
        # AuditBasis; do the same for the AWS-sourced CO/CoH recs so every counted
        # EBS dollar in the report is traceable to its origin and savings path.
        region = getattr(ctx, "region", "")
        for rec in co_recs:
            rec["AuditBasis"] = {
                "source": "Compute Optimizer",
                "savings_path": "volumeRecommendationOptions[0].savingsOpportunity."
                "estimatedMonthlySavings.value",
                "amount": round(compute_optimizer_savings(rec), 2),
                "region": region,
            }
        for rec in coh_recs:
            rec["AuditBasis"] = {
                "source": "Cost Optimization Hub",
                "savings_path": "estimatedMonthlySavings",
                "amount": round(float(rec.get("estimatedMonthlySavings", 0.0) or 0.0), 2),
                "region": region,
            }

        # Per-volume gp2→gp3 savings: region-correct storage delta NET of the gp3
        # IOPS that a large gp2 volume (baseline > 3,000 IOPS) must provision to
        # keep its performance — so the number is not overstated for >1 TB volumes.
        delta_per_gb = _gp2_to_gp3_savings_per_gb(ctx)
        if ctx.pricing_engine:
            gp3_iops_rate = ctx.pricing_engine.get_ebs_iops_monthly_price("gp3")
        else:
            gp3_iops_rate = 0.005 * ctx.pricing_multiplier
        gp2_total = 0.0
        for rec in gp2_kept:
            size = rec.get("Size", 0)
            per_vol = gp2_to_gp3_net_savings(size, delta_per_gb, gp3_iops_rate)
            rec["EstimatedSavings"] = f"${per_vol:.2f}/month"
            rec["AuditBasis"] = {
                "metric": "gp2→gp3 storage delta net of gp3 IOPS parity",
                "rate_per_gb_month": round(delta_per_gb, 6),
                "gp3_iops_rate_per_iops_month": round(gp3_iops_rate, 6),
                "gp2_baseline_iops": gp2_baseline_iops(size),
                "region": getattr(ctx, "region", ""),
                "basis": "size×(gp2−gp3 $/GB) − max(0, gp2_baseline_iops−3000)×gp3 IOPS rate; throughput parity not modelled",
            }
            gp2_total += per_vol

        savings = 0.0
        savings += sum(float(r.get("estimatedMonthlySavings", 0.0) or 0.0) for r in coh_recs)
        savings += sum(compute_optimizer_savings(r) for r in co_recs)
        savings += sum(v.get("EstimatedMonthlyCost", 0) for v in unattached_kept)
        savings += gp2_total
        savings += sum(parse_dollar_savings(r.get("EstimatedSavings", "")) for r in other_kept)

        # Snapshots render in the dedicated Snapshots tab and are NOT counted here.
        total_recs = (
            len(coh_recs) + len(co_recs) + len(unattached_kept) + len(gp2_kept) + len(other_kept)
        )

        return ServiceFindings(
            service_name="EBS",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "cost_optimization_hub": SourceBlock(count=len(coh_recs), recommendations=tuple(coh_recs)),
                "compute_optimizer": SourceBlock(count=len(co_recs), recommendations=tuple(co_recs)),
                "unattached_volumes": SourceBlock(
                    count=len(unattached_kept), recommendations=tuple(unattached_kept)
                ),
                "gp2_migration": SourceBlock(count=len(gp2_kept), recommendations=tuple(gp2_kept)),
                "enhanced_checks": SourceBlock(count=len(other_kept), recommendations=tuple(other_kept)),
                # Not counted toward EBS totals; consumed by the Snapshots tab.
                "ebs_snapshots": SourceBlock(count=len(snapshot_recs), recommendations=tuple(snapshot_recs)),
            },
            optimization_descriptions=EBS_OPTIMIZATION_DESCRIPTIONS,
            extras={"volume_counts": get_ebs_volume_count(ctx)},
        )

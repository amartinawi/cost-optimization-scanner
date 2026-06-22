"""EBS storage optimization checks.

Extracted from CostOptimizer EBS-related methods as free functions.
This module will later become EbsModule (T-325) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)

# Lookback window for usage-based IOPS rightsizing (matches Compute Optimizer's
# 14-day default). The sampling period is 900s (15 min) — the finest granularity
# that still fits a 14-day window inside CloudWatch's 1,440-datapoint cap
# (14d / 900s = 1,344 points), giving a truer peak than an hourly average.
_IOPS_LOOKBACK_DAYS: int = 14
_IOPS_METRIC_PERIOD_SECONDS: int = 900


def _observed_peak_iops(ctx: ScanContext, volume_id: str) -> float | None:
    """Peak observed IOPS for a volume from CloudWatch, or None when unavailable.

    Reads ``AWS/EBS`` ``VolumeReadOps`` + ``VolumeWriteOps`` (Sum) over the
    lookback window and returns the sum of the per-metric peaks divided by the
    sampling period. Summing independent peaks is a deliberate over-estimate of
    demand, so any rightsizing recommendation stays conservative. Returns None
    on any error or when no datapoints exist (caller must NOT emit a priced
    finding without evidence).
    """
    try:
        cw = ctx.client("cloudwatch")
        if not cw:
            return None
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_IOPS_LOOKBACK_DAYS)
        peak = 0.0
        saw_data = False
        for metric in ("VolumeReadOps", "VolumeWriteOps"):
            resp = cw.get_metric_statistics(
                Namespace="AWS/EBS",
                MetricName=metric,
                Dimensions=[{"Name": "VolumeId", "Value": volume_id}],
                StartTime=start,
                EndTime=end,
                Period=_IOPS_METRIC_PERIOD_SECONDS,
                Statistics=["Sum"],
            )
            datapoints = resp.get("Datapoints", [])
            if datapoints:
                saw_data = True
                peak += max(dp["Sum"] for dp in datapoints) / _IOPS_METRIC_PERIOD_SECONDS
        return peak if saw_data else None
    except Exception as e:
        logger.warning("[ebs] CloudWatch IOPS metric check failed for %s: %s", volume_id, e)
        return None


def _snapshot_price_per_gb(ctx: ScanContext, pricing_multiplier: float) -> float:
    """$/GB-month for EBS Standard Snapshots, region-correct when possible."""
    if ctx.pricing_engine:
        return ctx.pricing_engine.get_ebs_snapshot_price_per_gb()
    return 0.05 * pricing_multiplier


def _gp3_iops_price(ctx: ScanContext, pricing_multiplier: float) -> float:
    """$/IOPS-month for gp3 provisioned IOPS, region-correct when possible."""
    if ctx.pricing_engine:
        return ctx.pricing_engine.get_ebs_iops_monthly_price("gp3")
    return 0.005 * pricing_multiplier


def _io2_iops_cost(ctx: ScanContext, iops: int, pricing_multiplier: float) -> float:
    """Total $/month for `iops` provisioned IOPS on io2, respecting AWS tiers.

    Tiers (us-east-1): $0.065 for 0–32,000, $0.0455 for 32,001–64,000,
    $0.032 for > 64,000. Uses PricingEngine when available so the region
    is taken into account.
    """
    if iops <= 0:
        return 0.0
    if ctx.pricing_engine:
        return ctx.pricing_engine.get_ebs_io2_iops_cost(iops)
    base = 0.065 * pricing_multiplier
    tier2 = 0.0455 * pricing_multiplier
    tier3 = 0.032 * pricing_multiplier
    cost = min(iops, 32000) * base
    if iops > 32000:
        cost += min(iops - 32000, 32000) * tier2
    if iops > 64000:
        cost += (iops - 64000) * tier3
    return cost

EBS_OPTIMIZATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "compute_optimizer": {
        "title": "Compute Optimizer EBS Recommendations",
        "description": (
            "AWS Compute Optimizer ML-driven rightsizing recommendations for EBS volumes"
            " based on 14-day CloudWatch utilization patterns."
        ),
        "action": (
            "1. Review Compute Optimizer finding and recommended volume type\n"
            "2. Modify volume via AWS Console or CLI (no downtime)\n"
            "3. Estimated savings: varies by recommendation"
        ),
    },
    "unattached_volumes": {
        "title": "Delete Unattached EBS Volumes",
        "description": (
            "Unattached EBS volumes continue to incur storage costs."
            " Delete volumes that are no longer needed after creating"
            " snapshots for backup if required."
        ),
        "action": (
            "1. Create snapshot if data recovery needed\n"
            "2. Delete unattached volume via AWS Console or CLI\n"
            "3. Estimated savings: 100% of volume storage cost"
            " (estimate based on current volume pricing)"
        ),
    },
    "gp2_migration": {
        "title": "Migrate gp2 to gp3 Volumes",
        "description": (
            "gp3 volumes offer up to 20% cost savings compared to gp2"
            " with better performance baseline (3,000 IOPS vs size-dependent IOPS)."
        ),
        "action": (
            "1. Modify volume type from gp2 to gp3 (no downtime)\n"
            "2. Optionally adjust IOPS and throughput independently\n"
            "3. Estimated savings: ~20% reduction in storage costs"
            " (estimate based on current AWS pricing)"
        ),
    },
    "enhanced_checks": {
        "title": "Enhanced EBS Checks",
        "description": (
            "Additional EBS volume checks including io1-to-io2 upgrades, volume rightsizing, and old snapshot cleanup."
        ),
        "action": (
            "1. Review individual recommendations for specifics\n"
            "2. Apply volume type changes or size adjustments as needed\n"
            "3. Estimated savings: varies by check type"
        ),
    },
    "gp2_to_gp3": {
        "title": "Migrate gp2 to gp3 Volumes",
        "description": (
            "gp3 volumes offer up to 20% cost savings compared to gp2"
            " with better performance baseline (3,000 IOPS vs size-dependent IOPS)."
        ),
        "action": (
            "1. Modify volume type from gp2 to gp3 (no downtime)\n"
            "2. Optionally adjust IOPS and throughput independently\n"
            "3. Estimated savings: ~20% reduction in storage costs"
            " (estimate based on current AWS pricing)"
        ),
    },
    "io1_to_io2": {
        "title": "Upgrade io1 to io2 Volumes",
        "description": (
            "io2 volumes provide better durability (99.999% vs 99.9%) and higher IOPS limits at the same price as io1."
        ),
        "action": (
            "1. Modify volume type from io1 to io2 (no downtime)\n"
            "2. Benefit from improved performance and durability\n"
            "3. Cost: Same price with better performance"
        ),
    },
    "volume_rightsizing": {
        "title": "Rightsize EBS Volumes",
        "description": (
            "Reduce volume size or IOPS allocation based on actual usage patterns to eliminate over-provisioning."
        ),
        "action": (
            "1. Analyze CloudWatch metrics for volume utilization\n"
            "2. Reduce volume size or IOPS if underutilized\n"
            "3. Estimated savings: 10-50% based on over-provisioning"
            " (estimate varies by usage pattern)"
        ),
    },
}


def get_ebs_volume_count(ctx: ScanContext) -> dict[str, int]:
    """Get EBS volume counts by state and type.

    Uses pagination to support accounts with unlimited volumes.
    Handles IAM permission errors and rate limiting gracefully.

    Returns:
        Dict with volume counts by state (attached/unattached) and type (gp2/gp3/io1/io2)
    """
    logger.debug("EBS module active")
    ec2 = ctx.client("ec2")
    empty: dict[str, int] = {
        "total": 0,
        "attached": 0,
        "unattached": 0,
        "gp2": 0,
        "gp3": 0,
        "io1": 0,
        "io2": 0,
    }
    try:
        paginator = ec2.get_paginator("describe_volumes")
        volumes: list[dict[str, Any]] = []
        for page in paginator.paginate():
            volumes.extend(page.get("Volumes", []))

        counts: dict[str, int] = {**empty, "total": len(volumes)}

        for volume in volumes:
            # Count by attachment state
            if volume["State"] == "in-use":
                counts["attached"] += 1
            else:
                counts["unattached"] += 1

            # Count by volume type
            vol_type = volume.get("VolumeType", "unknown")
            if vol_type in counts:
                counts[vol_type] += 1

        return counts
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("UnauthorizedOperation", "AccessDenied"):
            ctx.permission_issue(
                f"describe_volumes denied: {error_code}",
                service="ebs",
                action="ec2:DescribeVolumes",
            )
        elif error_code == "RequestLimitExceeded":
            ctx.warn("Rate limit exceeded for describe_volumes (retries exhausted)", service="ebs")
        else:
            ctx.warn(f"Could not get EBS volume count: {e}", service="ebs")
        return empty
    except Exception as e:
        ctx.warn(f"Unexpected error getting EBS volume count: {e}", service="ebs")
        return empty


def get_ebs_compute_optimizer_recs(ctx: ScanContext) -> list[dict[str, Any]]:
    """Get EBS recommendations from Compute Optimizer.

    Delegates to services.advisor — the canonical location for all
    advisory-service (Cost Hub / Compute Optimizer) functions.
    """
    from services.advisor import get_ebs_compute_optimizer_recommendations

    return get_ebs_compute_optimizer_recommendations(ctx)


def get_unattached_volumes(
    ctx: ScanContext,
    pricing_multiplier: float,
) -> list[dict[str, Any]]:
    """Get unattached EBS volumes for cost optimization.

    Identifies volumes in 'available' state (not attached to any instance).
    Uses pagination to support unlimited volumes.
    Calculates estimated monthly cost for each unattached volume.

    Returns:
        List of dicts with VolumeId, Size, VolumeType, CreateTime, EstimatedMonthlyCost
        Returns empty list on errors (with warning messages)
    """
    ec2 = ctx.client("ec2")
    unattached: list[dict[str, Any]] = []
    try:
        # First, get volume IDs attached to stopped instances to exclude them
        stopped_instance_volumes: set[str] = set()
        instance_paginator = ec2.get_paginator("describe_instances")
        for page in instance_paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]):
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    for bdm in instance.get("BlockDeviceMappings", []):
                        if "Ebs" in bdm:
                            stopped_instance_volumes.add(bdm["Ebs"]["VolumeId"])

        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
            for volume in page.get("Volumes", []):
                volume_id = volume["VolumeId"]
                # Skip volumes attached to stopped instances
                if volume_id in stopped_instance_volumes:
                    continue

                # Check if volume is truly unattached (not attached to stopped instances)
                attachments = volume.get("Attachments", [])
                if not attachments:  # Completely unattached
                    monthly_cost = _estimate_volume_cost(
                        volume["Size"],
                        volume["VolumeType"],
                        volume.get("Iops"),
                        volume.get("Throughput"),
                        pricing_multiplier,
                        ctx=ctx,
                    )
                    unattached.append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "Size": volume["Size"],
                            "VolumeType": volume["VolumeType"],
                            "CreateTime": volume["CreateTime"].isoformat(),
                            "EstimatedMonthlyCost": monthly_cost,
                            "AuditBasis": {
                                "metric": "full volume storage cost (100% on delete)",
                                "region": getattr(ctx, "region", ""),
                                "volume_type": volume["VolumeType"],
                                "size_gb": volume["Size"],
                                "basis": "EstimatedMonthlyCost = size_gb × $/GB-mo (+ provisioned IOPS/throughput)",
                            },
                        }
                    )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("UnauthorizedOperation", "AccessDenied"):
            ctx.permission_issue(
                f"describe_volumes (unattached scan) denied: {error_code}",
                service="ebs",
                action="ec2:DescribeVolumes",
            )
        elif error_code == "RequestLimitExceeded":
            ctx.warn(
                "Rate limit exceeded for describe_volumes (unattached scan; retries exhausted)",
                service="ebs",
            )
        else:
            ctx.warn(f"Could not get unattached volumes: {e}", service="ebs")
    except Exception as e:
        ctx.warn(f"Unexpected error getting unattached volumes: {e}", service="ebs")
    return unattached


def _estimate_volume_cost(
    size_gb: int,
    volume_type: str,
    iops: int | None = None,
    throughput: int | None = None,
    pricing_multiplier: float = 1.0,
    ctx: ScanContext | None = None,
) -> float:
    """Estimate monthly cost for EBS volume including IOPS and throughput.

    Uses January 2026 AWS pricing adjusted for region.

    Args:
        size_gb: Volume size in GB
        volume_type: EBS volume type (gp2, gp3, io1, io2, st1, sc1)
        iops: Provisioned IOPS (for gp3, io1, io2)
        throughput: Provisioned throughput in MB/s (for gp3)
        pricing_multiplier: Regional pricing multiplier
        ctx: ScanContext with optional pricing_engine for live pricing

    Returns:
        Estimated monthly cost in USD
    """
    _FALLBACK: dict[str, float] = {
        "gp2": 0.10,
        "gp3": 0.08,
        "io1": 0.125,
        "io2": 0.125,
        "st1": 0.045,
        "sc1": 0.025,
    }

    if ctx and ctx.pricing_engine:
        gb_price = ctx.pricing_engine.get_ebs_monthly_price_per_gb(volume_type)
    else:
        gb_price = _FALLBACK.get(volume_type, 0.10) * pricing_multiplier

    base_cost = size_gb * gb_price

    if iops and volume_type in ["gp3", "io1", "io2"]:
        if volume_type == "gp3":
            # gp3 bills only IOPS above the free 3,000 baseline. Use the
            # region-correct rate via PricingEngine rather than a constant.
            extra_iops = max(0, iops - 3000)
            base_cost += (
                extra_iops * _gp3_iops_price(ctx, pricing_multiplier)
                if ctx
                else extra_iops * 0.005 * pricing_multiplier
            )
        elif volume_type == "io2":
            # io2 IOPS pricing is tiered above 32,000 — a flat rate over-counts.
            base_cost += (
                _io2_iops_cost(ctx, iops, pricing_multiplier) if ctx else iops * 0.065 * pricing_multiplier
            )
        else:  # io1 — flat $/IOPS-month
            if ctx and ctx.pricing_engine:
                iops_price = ctx.pricing_engine.get_ebs_iops_monthly_price("io1")
            else:
                iops_price = 0.065 * pricing_multiplier
            base_cost += iops * iops_price

    if throughput and volume_type == "gp3":
        # gp3 bills provisioned throughput above the free 125 MiB/s baseline,
        # priced region-correct via PricingEngine (fallback $0.04/MiBps-mo).
        extra_throughput = max(0, throughput - 125)
        if ctx and ctx.pricing_engine:
            tp_rate = ctx.pricing_engine.get_ebs_throughput_monthly_price("gp3")
        else:
            tp_rate = 0.04 * pricing_multiplier
        base_cost += extra_throughput * tp_rate

    return base_cost


def _current_ami_snapshot_ids(ctx: ScanContext) -> set[str]:
    """Snapshot ids that back a currently-registered self-owned AMI.

    These are priced by the AMI adapter (per the AMI's BlockDeviceMappings), so
    the EBS snapshot checks skip them to avoid counting the same storage twice
    across the EBS Snapshots tab and the AMI tab. Returns an empty set on any
    error so the EBS scan still runs (it would simply not dedup).
    """
    ids: set[str] = set()
    try:
        ec2 = ctx.client("ec2")
        paginator = ec2.get_paginator("describe_images")
        for page in paginator.paginate(Owners=["self"]):
            for ami in page.get("Images", []):
                for bdm in ami.get("BlockDeviceMappings", []):
                    snap = bdm.get("Ebs", {}).get("SnapshotId")
                    if snap:
                        ids.add(snap)
    except Exception as e:
        ctx.warn(f"Could not list AMI-backed snapshots for cross-tab dedup: {e}", service="ebs")
    return ids


def _scan_over_provisioned_iops(
    ctx: ScanContext,
    ec2: Any,
    pricing_multiplier: float,
    checks: dict[str, Any],
) -> None:
    """Flag volumes whose provisioned IOPS exceed observed CloudWatch demand.

    Reads each candidate's peak IOPS over the lookback window and emits a priced
    recommendation only when a reduction is justified by real usage (peak +
    headroom < provisioned). In ``--fast`` mode the CloudWatch reads are skipped
    and a single warning is recorded; a volume with no CloudWatch data is
    skipped with a per-volume warning rather than a fabricated saving.
    """
    from services.ebs_logic import recommend_iops_from_usage

    if getattr(ctx, "fast_mode", False):
        ctx.warn(
            "Skipped EBS over-provisioned IOPS check in --fast mode (no CloudWatch reads); "
            "re-run without --fast to detect IOPS rightsizing savings.",
            service="ebs",
        )
        return

    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["io1", "io2", "gp3"]}]):
        for volume in page.get("Volumes", []):
            volume_type = volume["VolumeType"]
            iops = volume.get("Iops", 0)
            volume_id = volume["VolumeId"]

            # Only volumes with billable provisioned IOPS can yield savings:
            # gp3 bills IOPS above the free 3,000; io1/io2 bill every IOPS.
            if iops <= 0 or (volume_type == "gp3" and iops <= 3000):
                continue

            peak = _observed_peak_iops(ctx, volume_id)
            if peak is None:
                ctx.warn(
                    f"Skipped IOPS rightsizing for {volume_id}: no CloudWatch IOPS data in the "
                    f"{_IOPS_LOOKBACK_DAYS}-day window.",
                    service="ebs",
                )
                continue

            baseline = 3000 if volume_type == "gp3" else 100
            recommended = recommend_iops_from_usage(iops, peak, baseline=baseline)
            if recommended is None:
                continue

            if volume_type == "gp3":
                savings = (max(0, iops - 3000) - max(0, recommended - 3000)) * _gp3_iops_price(
                    ctx, pricing_multiplier
                )
            elif volume_type == "io2":
                savings = _io2_iops_cost(ctx, iops, pricing_multiplier) - _io2_iops_cost(
                    ctx, recommended, pricing_multiplier
                )
            else:  # io1 — flat $/IOPS-month
                rate = (
                    ctx.pricing_engine.get_ebs_iops_monthly_price("io1")
                    if ctx.pricing_engine
                    else 0.065 * pricing_multiplier
                )
                savings = (iops - recommended) * rate

            if savings <= 0:
                continue

            checks["over_provisioned_iops"].append(
                {
                    "VolumeId": volume_id,
                    "VolumeType": volume_type,
                    "CurrentIOPS": iops,
                    "RecommendedIOPS": recommended,
                    "ObservedPeakIOPS": round(peak, 1),
                    "Recommendation": (
                        f"Reduce provisioned IOPS from {iops} to {recommended} "
                        f"(observed {_IOPS_LOOKBACK_DAYS}-day peak ~= {peak:.0f} IOPS)"
                    ),
                    "EstimatedSavings": f"${savings:.2f}/month",
                    "AuditBasis": {
                        "metric": f"VolumeReadOps+VolumeWriteOps peak over {_IOPS_LOOKBACK_DAYS}d",
                        "observed_peak_iops": round(peak, 1),
                        "current_iops": iops,
                        "recommended_iops": recommended,
                        "region": getattr(ctx, "region", ""),
                        "basis": "(billable_current_iops - billable_recommended_iops) x $/IOPS-mo",
                    },
                }
            )


def compute_ebs_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    old_snapshot_days: int = 90,
) -> dict[str, Any]:
    """Get enhanced EBS cost optimization checks.

    Performs these EBS optimization checks:
    1. gp2->gp3 migration (per-volume storage-delta savings)
    2. Over-provisioned IOPS — usage-based, gated on CloudWatch evidence
    3. Old snapshots (> ``old_snapshot_days``, storage cost reduction)
    4. Orphaned snapshots (from deleted AMIs)

    Unattached volumes are fetched by ``get_unattached_volumes`` (their own
    source); the previously-emitted "unused encrypted volumes" check was removed
    because every ``available`` encrypted volume is, by definition, also an
    unattached volume — counting it here double-counted the same dollars.

    Uses pagination to support unlimited volumes.

    Returns:
        Dict with 'recommendations' list containing all EBS optimization opportunities
    """
    ec2 = ctx.client("ec2")
    checks: dict[str, Any] = {
        "unattached_volumes": get_unattached_volumes(ctx, pricing_multiplier),
        "gp2_migration": [],
        "old_snapshots": [],
        "over_provisioned_iops": [],
        "orphaned_snapshots": [],
    }

    try:
        # Check for gp2 volumes that can migrate to gp3.
        # Per-volume savings are filled in by the EBS adapter (it has access
        # to the region-correct gp2/gp3 prices via PricingEngine without
        # re-multiplying by ctx.pricing_multiplier).
        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["gp2"]}]):
            for volume in page.get("Volumes", []):
                checks["gp2_migration"].append(
                    {
                        "VolumeId": volume["VolumeId"],
                        "Size": volume["Size"],
                        "CurrentType": "gp2",
                        "RecommendedType": "gp3",
                        "EstimatedSavings": "$0.00/month - adapter computes per-volume",
                        "CheckCategory": "Volume Type Optimization",
                    }
                )

        # Check for over-provisioned IOPS — evidence-based. Each candidate's
        # actual peak IOPS is read from CloudWatch; a recommendation is emitted
        # only when observed demand (plus headroom) is below what is provisioned.
        # No CloudWatch data → no priced finding (a warning is recorded instead).
        _scan_over_provisioned_iops(ctx, ec2, pricing_multiplier, checks)

        # Check for old snapshots (>90 days for Snapshots tab) - with pagination.
        # Snapshots that back a CURRENTLY-REGISTERED self-owned AMI are priced by
        # the AMI adapter (services/adapters/ami.py via the AMI's BlockDeviceMappings),
        # so EBS cedes them to avoid double-counting the same storage across tabs.
        # Only snapshots from DEREGISTERED AMIs (truly orphaned) and standalone
        # snapshots are surfaced here. old vs orphaned are mutually exclusive.
        ami_backed_snapshot_ids = _current_ami_snapshot_ids(ctx)
        snapshot_rate = _snapshot_price_per_gb(ctx, pricing_multiplier)
        paginator = ec2.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snapshot in page["Snapshots"]:
                snapshot_id = snapshot["SnapshotId"]
                if snapshot_id in ami_backed_snapshot_ids:
                    continue  # owned by the AMI tab
                age_days = (datetime.now(snapshot["StartTime"].tzinfo) - snapshot["StartTime"]).days
                if age_days <= old_snapshot_days:
                    continue
                _snapshot_basis = {
                    "metric": "snapshot data stored ($/GB-mo, max estimate)",
                    "rate_per_gb_month": round(snapshot_rate, 6),
                    "region": getattr(ctx, "region", ""),
                    "basis": "VolumeSize × snapshot $/GB-mo; actual lower due to incremental storage",
                }
                estimated = f"${snapshot['VolumeSize'] * snapshot_rate:.2f}/month (max estimate)"
                # A "Created by CreateImage" snapshot NOT backing a current AMI is
                # from a deregistered AMI → orphaned. Everything else → old.
                if snapshot.get("Description", "").startswith("Created by CreateImage"):
                    checks["orphaned_snapshots"].append(
                        {
                            "SnapshotId": snapshot_id,
                            "AgeDays": age_days,
                            "VolumeSize": snapshot["VolumeSize"],
                            "Description": snapshot.get("Description", ""),
                            "Recommendation": (
                                "Snapshot from a deregistered AMI — verify and delete"
                                " (Note: Actual savings may be lower due to incremental storage)"
                            ),
                            "CheckCategory": "Orphaned Snapshots",
                            "EstimatedSavings": estimated,
                            "AuditBasis": _snapshot_basis,
                        }
                    )
                else:
                    checks["old_snapshots"].append(
                        {
                            "SnapshotId": snapshot_id,
                            "AgeDays": age_days,
                            "VolumeSize": snapshot["VolumeSize"],
                            "CheckCategory": "Old Snapshots",
                            "Recommendation": (
                                f"Review {age_days}-day old snapshot for deletion"
                                " (Note: Actual savings may be lower due to incremental storage)"
                            ),
                            "EstimatedSavings": estimated,
                            "AuditBasis": _snapshot_basis,
                        }
                    )

    except Exception as e:
        ctx.warn(f"Could not perform enhanced EBS checks: {e}", service="ebs")

    # Convert to recommendations format
    recommendations: list[dict[str, Any]] = []
    for category, items in checks.items():
        if isinstance(items, list):
            for item in items:
                item["CheckCategory"] = item.get("CheckCategory", category.replace("_", " ").title())
                recommendations.append(item)

    return {"recommendations": recommendations, **checks}

"""EBS storage optimization checks.

Extracted from CostOptimizer EBS-related methods as free functions.
This module will later become EbsModule (T-325) implementing ServiceModule.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.scan_context import ScanContext

logger = logging.getLogger(__name__)


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


def get_ebs_compute_optimizer_recs(
    ctx: ScanContext,
    pricing_multiplier: float,
) -> list[dict[str, Any]]:
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
                    unattached.append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "Size": volume["Size"],
                            "VolumeType": volume["VolumeType"],
                            "CreateTime": volume["CreateTime"].isoformat(),
                            "EstimatedMonthlyCost": _estimate_volume_cost(
                                volume["Size"],
                                volume["VolumeType"],
                                volume.get("Iops"),
                                volume.get("Throughput"),
                                pricing_multiplier,
                                ctx=ctx,
                            ),
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
            extra_iops = max(0, iops - 3000)
            base_cost += extra_iops * 0.005 * pricing_multiplier
        elif volume_type in ["io1", "io2"]:
            if ctx and ctx.pricing_engine:
                iops_price = ctx.pricing_engine.get_ebs_iops_monthly_price(volume_type)
            else:
                iops_price = 0.065 * pricing_multiplier
            base_cost += iops * iops_price

    if throughput and volume_type == "gp3":
        extra_throughput = max(0, throughput - 125)
        base_cost += extra_throughput * 0.04 * pricing_multiplier

    return base_cost


def compute_ebs_checks(
    ctx: ScanContext,
    pricing_multiplier: float,
    old_snapshot_days: int = 90,
) -> dict[str, Any]:
    """Get enhanced EBS cost optimization checks.

    Performs 8 categories of EBS optimization checks:
    1. Unattached volumes (100% savings opportunity)
    2. gp2->gp3 migration (20% savings)
    3. Old snapshots (>90 days, storage cost reduction)
    4. Underutilized volumes (rightsizing opportunity)
    5. Over-provisioned IOPS (cost reduction)
    6. Unused encrypted volumes (storage cost elimination)
    7. Orphaned snapshots (cleanup opportunity)
    8. Snapshot lifecycle policies (automated cost management)

    Uses pagination to support unlimited volumes.

    Returns:
        Dict with 'recommendations' list containing all EBS optimization opportunities
    """
    ec2 = ctx.client("ec2")
    checks: dict[str, Any] = {
        "unattached_volumes": get_unattached_volumes(ctx, pricing_multiplier),
        "gp2_migration": [],
        "old_snapshots": [],
        "underutilized_volumes": [],
        "over_provisioned_iops": [],
        "unused_encrypted_volumes": [],
        "orphaned_snapshots": [],
        "snapshot_lifecycle": [],
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

        # Check for underutilized volumes (basic heuristic - high IOPS but low utilization)
        for page in paginator.paginate():
            for volume in page.get("Volumes", []):
                volume_type = volume.get("VolumeType", "")
                iops = volume.get("Iops", 0)
                size = volume.get("Size", 0)

                # Underutilized high-IOPS volumes flag removed: $0/month, "enable CloudWatch
                # monitoring to validate" is a monitoring-enablement nudge.
                _ = (volume_type, iops, size)

        # Check for snapshot lifecycle opportunities
        paginator = ec2.get_paginator("describe_snapshots")
        snapshot_count = 0
        for page in paginator.paginate(OwnerIds=["self"]):
            for _snapshot in page["Snapshots"]:
                snapshot_count += 1

        # Snapshot Lifecycle finding removed: $0/month, "quantify after enabling DLM"
        # is a feature-enablement nudge — actual savings come from snapshot deletion
        # which is surfaced via the dedicated Snapshots tab.

        # Check for over-provisioned IOPS (heuristic estimates - recommend CloudWatch validation)
        volume_paginator = ec2.get_paginator("describe_volumes")
        for page in volume_paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["io1", "io2", "gp3"]}]):
            for volume in page.get("Volumes", []):
                iops = volume.get("Iops", 0) if volume["VolumeType"] in ["io1", "io2", "gp3"] else 0
                size = volume["Size"]
                volume_type = volume["VolumeType"]

                # Check if IOPS is over-provisioned
                if volume_type == "gp3" and iops > 3000 + size * 30:  # gp3: 3000 baseline + reasonable ratio
                    recommended_iops = 3000 + size * 30
                    extra_iops = iops - recommended_iops
                    savings = extra_iops * _gp3_iops_price(ctx, pricing_multiplier)
                    checks["over_provisioned_iops"].append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "CurrentIOPS": iops,
                            "RecommendedIOPS": recommended_iops,
                            "Recommendation": "Reduce provisioned IOPS based on actual usage",
                            "EstimatedSavings": f"${savings:.2f}/month",
                        }
                    )
                elif volume_type in ["io1", "io2"] and iops > size * 50:  # io1/io2: check for over-provisioning
                    recommended_iops = size * 30
                    if volume_type == "io2":
                        savings = _io2_iops_cost(ctx, iops, pricing_multiplier) - _io2_iops_cost(
                            ctx, recommended_iops, pricing_multiplier
                        )
                    else:  # io1 — flat $0.065/IOPS-mo
                        extra_iops = iops - recommended_iops
                        if ctx.pricing_engine:
                            iops_rate = ctx.pricing_engine.get_ebs_iops_monthly_price("io1")
                        else:
                            iops_rate = 0.065 * pricing_multiplier
                        savings = extra_iops * iops_rate
                    checks["over_provisioned_iops"].append(
                        {
                            "VolumeId": volume["VolumeId"],
                            "VolumeType": volume_type,
                            "CurrentIOPS": iops,
                            "RecommendedIOPS": recommended_iops,
                            "Recommendation": "Reduce provisioned IOPS based on actual usage",
                            "EstimatedSavings": f"${savings:.2f}/month",
                        }
                    )

        # Check for old snapshots (>90 days for Snapshots tab) - with pagination
        snapshot_rate = _snapshot_price_per_gb(ctx, pricing_multiplier)
        paginator = ec2.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snapshot in page["Snapshots"]:
                age_days = (datetime.now(snapshot["StartTime"].tzinfo) - snapshot["StartTime"]).days
                if age_days > old_snapshot_days:  # Only snapshots older than 90 days
                    checks["old_snapshots"].append(
                        {
                            "SnapshotId": snapshot["SnapshotId"],
                            "AgeDays": age_days,
                            "VolumeSize": snapshot["VolumeSize"],
                            "CheckCategory": "Old Snapshots",
                            "Recommendation": (
                                f"Review {age_days}-day old snapshot for deletion"
                                " (Note: Actual savings may be lower due to incremental storage)"
                            ),
                            "EstimatedSavings": (
                                f"${snapshot['VolumeSize'] * snapshot_rate:.2f}/month (max estimate)"
                            ),
                        }
                    )

                # Check for orphaned snapshots (from deleted AMIs) - only if >90 days old
                if (
                    snapshot.get("Description", "").startswith("Created by CreateImage")
                    and age_days > old_snapshot_days
                ):
                    checks["orphaned_snapshots"].append(
                        {
                            "SnapshotId": snapshot["SnapshotId"],
                            "AgeDays": age_days,
                            "VolumeSize": snapshot["VolumeSize"],
                            "Description": snapshot.get("Description", ""),
                            "Recommendation": (
                                "Check if snapshot is from deleted AMI and can be removed"
                                " (Note: Actual savings may be lower due to incremental storage)"
                            ),
                            "CheckCategory": "Orphaned Snapshots",
                            "EstimatedSavings": (
                                f"${snapshot['VolumeSize'] * snapshot_rate:.2f}/month (max estimate)"
                            ),
                        }
                    )

        # Check for unused encrypted volumes
        volume_paginator = ec2.get_paginator("describe_volumes")
        for page in volume_paginator.paginate(
            Filters=[
                {"Name": "encrypted", "Values": ["true"]},
                {"Name": "status", "Values": ["available"]},
            ]
        ):
            for volume in page.get("Volumes", []):
                checks["unused_encrypted_volumes"].append(
                    {
                        "VolumeId": volume["VolumeId"],
                        "Size": volume["Size"],
                        "Encrypted": True,
                        "Recommendation": "Delete unused encrypted volume",
                        "EstimatedSavings": (
                            f"${_estimate_volume_cost(volume['Size'], volume['VolumeType'], volume.get('Iops'), volume.get('Throughput'), pricing_multiplier, ctx=ctx):.2f}"  # noqa: E501
                            "/month"
                        ),
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

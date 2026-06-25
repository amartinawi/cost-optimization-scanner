"""AMI lifecycle management checks.

Extracted from CostOptimizer.get_ami_checks() as a free function.
This module will later become AmiModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from datetime import UTC, datetime
from typing import Any

from core.scan_context import ScanContext

OLD_SNAPSHOT_DAYS: int = 90
UNUSED_MIN_DAYS: int = 30


def _snapshot_storage_gb(ec2: Any, ami: dict[str, Any]) -> tuple[float, list[str]]:
    """Sum the backing EBS-snapshot storage (GB) for an AMI.

    Prefers actual stored bytes (``FullSnapshotSizeInBytes``) over the
    provisioned ``VolumeSize`` — snapshots bill on stored blocks, which are
    typically ~half the volume size, so VolumeSize overstates ~2x. Returns
    ``(total_gb, snapshot_ids)``; instance-store AMIs (no EBS snapshots)
    return ``(0.0, [])``.
    """
    total_gb = 0.0
    snapshot_ids: list[str] = []
    for block_device in ami.get("BlockDeviceMappings", []):
        ebs = block_device.get("Ebs") or {}
        snapshot_id = ebs.get("SnapshotId")
        if not snapshot_id:
            continue
        snapshot_ids.append(snapshot_id)
        try:
            snapshot_response = ec2.describe_snapshots(SnapshotIds=[snapshot_id])
            for snapshot in snapshot_response.get("Snapshots", []):
                full_bytes = snapshot.get("FullSnapshotSizeInBytes")
                if full_bytes:
                    total_gb += float(full_bytes) / (1024**3)
                else:
                    total_gb += snapshot.get("VolumeSize", 0)
        except Exception as e:
            logger.warning(f"⚠️ Error getting snapshot details for {snapshot_id}: {str(e)}")
            total_gb += ebs.get("VolumeSize", 8)
    return total_gb, snapshot_ids


def compute_ami_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Compute AMI deletion-candidate checks.

    Only AMIs that are **unused** (not referenced by any running/stopped
    instance, launch template, or ASG) are deletion candidates — an in-use
    AMI's snapshots cannot be safely deleted. Candidates are split by age
    into two mutually exclusive sources purely for confidence/presentation:

      - ``old_amis``    — unused and older than ``OLD_SNAPSHOT_DAYS`` (stale,
                          high confidence).
      - ``unused_amis`` — unused and ``UNUSED_MIN_DAYS`` < age ≤
                          ``OLD_SNAPSHOT_DAYS`` (newer; verify before deletion).

    Every emitted rec carries a quantified ``EstimatedMonthlySavings`` derived
    from its backing-snapshot storage. AMIs with no resolvable EBS-snapshot
    storage (instance-store) are skipped rather than emitted at $0.

    Returns dict with 'recommendations' (list) and 'total_count' (int).
    """
    ec2 = ctx.client("ec2")
    autoscaling = ctx.client("autoscaling")
    checks: dict[str, list[dict[str, Any]]] = {"unused_amis": [], "old_amis": []}

    try:
        # Paginate to avoid silently dropping pages beyond the default ceiling.
        amis_paginator = ec2.get_paginator("describe_images")
        amis_list: list[dict[str, Any]] = []
        for page in amis_paginator.paginate(Owners=["self"]):
            amis_list.extend(page.get("Images", []))
        amis_response = {"Images": amis_list}

        running_amis: set[str] = set()
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    if instance["State"]["Name"] in ["running", "stopped"]:
                        running_amis.add(instance.get("ImageId"))

        try:
            lt_response = ec2.describe_launch_templates()
            for lt in lt_response.get("LaunchTemplates", []):
                try:
                    lt_versions = ec2.describe_launch_template_versions(LaunchTemplateId=lt["LaunchTemplateId"])
                    for version in lt_versions.get("LaunchTemplateVersions", []):
                        lt_data = version.get("LaunchTemplateData", {})
                        if "ImageId" in lt_data:
                            running_amis.add(lt_data["ImageId"])
                except Exception:
                    pass
        except Exception:
            pass

        try:
            asg_response = autoscaling.describe_auto_scaling_groups()
            for asg in asg_response.get("AutoScalingGroups", []):
                if "LaunchConfigurationName" in asg:
                    try:
                        lc_response = autoscaling.describe_launch_configurations(
                            LaunchConfigurationNames=[asg["LaunchConfigurationName"]]
                        )
                        for lc in lc_response.get("LaunchConfigurations", []):
                            running_amis.add(lc.get("ImageId"))
                    except Exception:
                        pass
        except Exception:
            pass

        # Snapshot storage rate is region-uniform; resolve it once. PricingEngine
        # returns a region-correct $/GB-month — NO additional pricing_multiplier
        # (L2.3.1). Fall back to the constant × multiplier only on failure.
        if ctx.pricing_engine is not None:
            try:
                snapshot_rate = ctx.pricing_engine.get_ebs_snapshot_price_per_gb()
            except Exception:
                snapshot_rate = 0.05 * pricing_multiplier
        else:
            snapshot_rate = 0.05 * pricing_multiplier

        for ami in amis_response.get("Images", []):
            ami_id = ami["ImageId"]
            creation_date = datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - creation_date).days

            # Only unused AMIs are deletion candidates — an AMI referenced by a
            # running/stopped instance, launch template, or ASG must not be
            # flagged for deletion. Age below the floor is too new to action.
            if ami_id in running_amis or age_days <= UNUSED_MIN_DAYS:
                continue

            total_snapshot_size_gb, snapshot_ids = _snapshot_storage_gb(ec2, ami)
            if total_snapshot_size_gb == 0:
                # Instance-store / no resolvable EBS-snapshot storage — there is
                # no storage cost to recover. Skip rather than emit $0 noise.
                continue

            # AWS EBS snapshots are incremental — only changed blocks are billed.
            # VolumeSize is an UPPER BOUND; we emit the MAX with an explicit
            # qualifier in the display string.
            monthly_snapshot_cost = total_snapshot_size_gb * snapshot_rate
            is_old = age_days > OLD_SNAPSHOT_DAYS
            rec = {
                "ImageId": ami_id,
                "Name": ami.get("Name", "N/A"),
                "AgeDays": age_days,
                "CreationDate": ami["CreationDate"],
                "SnapshotSizeGB": total_snapshot_size_gb,
                "SnapshotIds": snapshot_ids,
                "Recommendation": (
                    f"Unused AMI ({age_days} days old, not referenced by any"
                    " running instance, launch template, or ASG) -"
                    f" {'deregister and delete snapshots' if is_old else 'verify then delete'}"
                ),
                "EstimatedSavings": (
                    f"${monthly_snapshot_cost:.2f}/month"
                    f" ({total_snapshot_size_gb:.1f}GB snapshot storage - max estimate)"
                ),
                "EstimatedMonthlySavings": monthly_snapshot_cost,
                "CheckCategory": "Old Unused AMIs" if is_old else "Unused AMIs",
            }
            checks["old_amis" if is_old else "unused_amis"].append(rec)
    except Exception as e:
        logger.warning(f"Warning: Could not get AMI checks: {e}")

    return {
        "recommendations": checks.get("old_amis", []) + checks.get("unused_amis", []),
        "total_count": len(checks.get("old_amis", []) + checks.get("unused_amis", [])),
        **checks,
    }

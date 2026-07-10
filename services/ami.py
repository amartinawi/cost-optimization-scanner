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
from services._aws_errors import record_aws_error

OLD_SNAPSHOT_DAYS: int = 90
UNUSED_MIN_DAYS: int = 30


def _snapshot_storage_gb(
    ec2: Any, ami: dict[str, Any], counted_snapshot_ids: set[str]
) -> tuple[float, list[str], list[str], bool]:
    """Sum the INCREMENTAL backing EBS-snapshot storage (GB) for an AMI.

    Prefers actual stored bytes (``FullSnapshotSizeInBytes``) over the
    provisioned ``VolumeSize`` — snapshots bill on stored blocks, which are
    typically ~half the volume size, so VolumeSize overstates ~2x.

    Cross-AMI de-duplication: a snapshot already attributed to an earlier AMI
    (present in ``counted_snapshot_ids``) is SHARED — an EBS snapshot is billed
    once and cannot be freed until *every* AMI referencing it is deregistered, so
    counting its storage again would overstate the recoverable saving (the
    cardinal-sin double-count). A snapshot is added to ``counted_snapshot_ids``
    (mutated in place) **only once it has actually been sized** — a snapshot whose
    ``describe_snapshots`` failed with no ``VolumeSize`` fallback is NOT claimed, so
    a later AMI that *can* size it still counts it (rather than seeing a phantom
    "shared" claim from an AMI that contributed nothing). A snapshot already sized
    under an EARLIER AMI is returned in ``shared_snapshot_ids``; a snapshot mapped
    twice within *this* AMI is a self-duplicate, counted once and silently ignored
    (not reported as shared).

    Returns ``(incremental_gb, all_snapshot_ids, shared_snapshot_ids, estimated)``
    where ``estimated`` is True when at least one snapshot's size could not be read
    from snapshot metadata (the ``describe_snapshots`` call failed) and was
    inferred from the AMI's block-device-mapping ``VolumeSize`` instead; the caller
    discloses this so the dollar is never presented as fully measured.
    Instance-store AMIs (no EBS snapshots) return ``(0.0, [], [], False)``.
    """
    incremental_gb = 0.0
    all_snapshot_ids: list[str] = []
    shared_snapshot_ids: list[str] = []
    estimated = False
    this_ami_counted: set[str] = set()  # snapshots THIS AMI has already sized
    for block_device in ami.get("BlockDeviceMappings", []):
        ebs = block_device.get("Ebs") or {}
        snapshot_id = ebs.get("SnapshotId")
        if not snapshot_id:
            continue
        all_snapshot_ids.append(snapshot_id)
        if snapshot_id in this_ami_counted:
            # Mapped twice within this same AMI — already counted here; ignore the
            # duplicate silently (it is not "shared with another AMI").
            continue
        if snapshot_id in counted_snapshot_ids:
            # Already sized under an EARLIER AMI — shared; count 0 GB here so the
            # same physical snapshot's storage is never attributed twice.
            shared_snapshot_ids.append(snapshot_id)
            continue
        snap_gb = 0.0
        try:
            snapshot_response = ec2.describe_snapshots(SnapshotIds=[snapshot_id])
            for snapshot in snapshot_response.get("Snapshots", []):
                full_bytes = snapshot.get("FullSnapshotSizeInBytes")
                if full_bytes:
                    snap_gb += float(full_bytes) / (1024**3)
                else:
                    snap_gb += snapshot.get("VolumeSize", 0)
        except Exception as e:
            logger.warning(f"⚠️ Error getting snapshot details for {snapshot_id}: {str(e)}")
            # describe failed — fall back to the AMI block-device mapping's
            # provisioned VolumeSize (an upper bound) and flag the estimate. Never
            # fabricate a size: if the mapping carries no VolumeSize either,
            # contribute 0 GB and let the AMI be skipped rather than emit a guessed
            # dollar (no fabricated dollars; fail safe on missing data).
            bdm_gb = ebs.get("VolumeSize") or 0
            if bdm_gb:
                snap_gb += bdm_gb
                estimated = True
        # Claim the snapshot ONLY when it contributes recoverable GB (> 0). An
        # unsizable snapshot (describe failed AND no VolumeSize) OR a genuinely
        # empty snapshot carries nothing to recover, so it is left unclaimed: a
        # later AMI that CAN size it still counts it, and it never makes a real
        # snapshot look "shared". This guarantees every claimed snapshot was
        # counted under an AMI with incremental_gb > 0 (an emitted rec) — so the
        # "counted under another AMI" advisory is always truthful.
        if snap_gb > 0:
            incremental_gb += snap_gb
            counted_snapshot_ids.add(snapshot_id)
            this_ami_counted.add(snapshot_id)
    return incremental_gb, all_snapshot_ids, shared_snapshot_ids, estimated


def region_snapshot_footprint_gib(ctx: ScanContext) -> float | None:
    """Total full size (GiB) of every self-owned EBS snapshot in the region.

    The denominator for attributing billed ``EBS:SnapshotUsage`` to a subset of
    snapshots. AWS bills the *unique blocks* of the whole snapshot estate; the
    flagged AMIs' backing snapshots are only part of it, so their realizable share
    of that bill is their share of the estate — never the whole bill.

    Returns ``None`` when the estate cannot be enumerated, so the caller demotes
    rather than assume the flagged snapshots are 100% of it.
    """
    total = 0.0
    try:
        paginator = ctx.client("ec2").get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snap in page.get("Snapshots", []):
                full_bytes = snap.get("FullSnapshotSizeInBytes")
                if full_bytes:
                    total += float(full_bytes) / (1024**3)
                else:
                    # Same fallback the per-AMI sizing uses, so numerator and
                    # denominator are measured on the same basis.
                    total += float(snap.get("VolumeSize", 0) or 0)
    except Exception as e:  # noqa: BLE001 — cannot attribute -> caller demotes
        record_aws_error(
            ctx, e, service="ami", context="DescribeSnapshots (region snapshot footprint) failed"
        )
        return None
    return total if total > 0 else None


def compute_ami_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Compute AMI deletion-candidate checks.

    Only AMIs that are **unused** are deletion candidates — an in-use AMI's
    snapshots cannot be safely deleted. An AMI is treated as in-use when it is
    referenced by any running/stopped instance, launch template, ASG, EC2 Fleet
    or Spot Fleet launch spec/override, OR shared cross-account/publicly via
    ``launchPermission`` (a consumer account we cannot enumerate may depend on
    it). Residual gap: EC2 Image Builder references are not enumerated; the rec
    text flags this. Candidates are split by age into two mutually exclusive
    sources purely for confidence/presentation:

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
        # Fail-safe: if ANY reference source (instances / launch templates /
        # ASGs / launch configs) cannot be enumerated, an AMI referenced only by
        # the unresolved source would pass the ``ami_id in running_amis`` guard
        # and be emitted as "deregister and delete snapshots" — a destructive
        # false positive (ami C1). Treat unresolved references as in-use by
        # suppressing every deletion candidate when any read failed.
        references_resolve_failed = False

        try:
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        if instance["State"]["Name"] in ["running", "stopped"]:
                            running_amis.add(instance.get("ImageId"))
        except Exception as e:
            record_aws_error(ctx, e, service="ami", context="describe_instances")
            references_resolve_failed = True

        try:
            lt_paginator = ec2.get_paginator("describe_launch_templates")
            for lt_page in lt_paginator.paginate():
                for lt in lt_page.get("LaunchTemplates", []):
                    try:
                        lt_versions = ec2.describe_launch_template_versions(LaunchTemplateId=lt["LaunchTemplateId"])
                        for version in lt_versions.get("LaunchTemplateVersions", []):
                            lt_data = version.get("LaunchTemplateData", {})
                            if "ImageId" in lt_data:
                                running_amis.add(lt_data["ImageId"])
                    except Exception as e:
                        record_aws_error(ctx, e, service="ami", context="describe_launch_template_versions")
                        references_resolve_failed = True
        except Exception as e:
            record_aws_error(ctx, e, service="ami", context="describe_launch_templates")
            references_resolve_failed = True

        try:
            asg_paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
            for asg_page in asg_paginator.paginate():
                for asg in asg_page.get("AutoScalingGroups", []):
                    if "LaunchConfigurationName" in asg:
                        try:
                            lc_response = autoscaling.describe_launch_configurations(
                                LaunchConfigurationNames=[asg["LaunchConfigurationName"]]
                            )
                            for lc in lc_response.get("LaunchConfigurations", []):
                                running_amis.add(lc.get("ImageId"))
                        except Exception as e:
                            record_aws_error(ctx, e, service="ami", context="describe_launch_configurations")
                            references_resolve_failed = True
        except Exception as e:
            record_aws_error(ctx, e, service="ami", context="describe_auto_scaling_groups")
            references_resolve_failed = True

        # EC2 Fleet: a fleet's launch-template Overrides can pin an ``ImageId``
        # that overrides the template's AMI, so the override AMI is in active
        # use even though no instance/template/ASG references it directly
        # (ami H3). Missing this path flags a live AMI for deregistration.
        try:
            fleet_paginator = ec2.get_paginator("describe_fleets")
            for fleet_page in fleet_paginator.paginate():
                for fleet in fleet_page.get("Fleets", []):
                    for lt_config in fleet.get("LaunchTemplateConfigs", []):
                        for override in lt_config.get("Overrides", []):
                            image_id = override.get("ImageId")
                            if image_id:
                                running_amis.add(image_id)
        except Exception as e:
            record_aws_error(ctx, e, service="ami", context="describe_fleets")
            references_resolve_failed = True

        # Spot Fleet: references AMIs through both inline
        # ``LaunchSpecifications[].ImageId`` and launch-template ``Overrides``
        # (ami H3). Either path keeps the AMI in active use.
        try:
            spot_fleet_paginator = ec2.get_paginator("describe_spot_fleet_requests")
            for spot_page in spot_fleet_paginator.paginate():
                for spot_config in spot_page.get("SpotFleetRequestConfigs", []):
                    request_config = spot_config.get("SpotFleetRequestConfig") or {}
                    for launch_spec in request_config.get("LaunchSpecifications", []):
                        image_id = launch_spec.get("ImageId")
                        if image_id:
                            running_amis.add(image_id)
                    for lt_config in request_config.get("LaunchTemplateConfigs", []):
                        for override in lt_config.get("Overrides", []):
                            image_id = override.get("ImageId")
                            if image_id:
                                running_amis.add(image_id)
        except Exception as e:
            record_aws_error(ctx, e, service="ami", context="describe_spot_fleet_requests")
            references_resolve_failed = True

        if references_resolve_failed:
            ctx.warn(
                "AMI reference enumeration incomplete (instance/launch-template/ASG "
                "read failed); suppressing unused-AMI deletion candidates to avoid "
                "false-positive deregistration.",
                service="ami",
            )
            return {
                "recommendations": [],
                "total_count": 0,
                "unused_amis": [],
                "old_amis": [],
            }

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

        # Tracks every snapshot id already SIZED under an AMI, so a snapshot shared
        # across multiple AMIs is counted exactly once (cardinal-sin double-count
        # fix). A snapshot is claimed only once `_snapshot_storage_gb` actually sizes
        # it AND only after the AMI has passed the pre-attribution running/age and
        # launch-permission checks below — so neither a skipped AMI nor an unsizable
        # snapshot ever "steals" the claim from a genuine candidate sharing it.
        counted_snapshot_ids: set[str] = set()
        for ami in amis_response.get("Images", []):
            ami_id = ami["ImageId"]
            creation_date = datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - creation_date).days

            # Only unused AMIs are deletion candidates — an AMI referenced by a
            # running/stopped instance, launch template, or ASG must not be
            # flagged for deletion. Age below the floor is too new to action.
            if ami_id in running_amis or age_days <= UNUSED_MIN_DAYS:
                continue

            # Cross-account / public sharing (ami H3): an AMI shared via
            # launchPermission can be actively launched by another account whose
            # instances we cannot enumerate, so deregistering it is destructive.
            # Treat any shared AMI as in-use. A FAILED attribute read is
            # ambiguous evidence on a deletion rec, so abstain on this AMI rather
            # than asserting "unused" (fail-safe, global rule 5). Checked BEFORE
            # snapshot attribution so a skipped AMI does not claim a snapshot id.
            try:
                image_attr = ec2.describe_image_attribute(ImageId=ami_id, Attribute="launchPermission")
            except Exception as e:
                record_aws_error(ctx, e, service="ami", context="describe_image_attribute")
                continue
            # Validate the API shape before trusting it (never trust external
            # data): a real response is a dict carrying a LaunchPermissions list.
            launch_permissions = image_attr.get("LaunchPermissions", []) if isinstance(image_attr, dict) else []
            if launch_permissions:
                # Shared with specific accounts and/or the public ("all") — a
                # consumer we cannot see may depend on it. Skip the deletion rec.
                continue

            incremental_gb, snapshot_ids, shared_ids, size_estimated = _snapshot_storage_gb(
                ec2, ami, counted_snapshot_ids
            )
            if not snapshot_ids:
                # Instance-store / no resolvable EBS-snapshot storage — there is
                # no storage cost to recover. Skip rather than emit $0 noise.
                continue

            is_old = age_days > OLD_SNAPSHOT_DAYS
            shared_note = ""
            if shared_ids:
                shared_note = (
                    f" {len(shared_ids)} of {len(snapshot_ids)} backing snapshot(s)"
                    f" are shared with another unused AMI and counted there — this"
                    f" figure is the storage UNIQUE to this AMI (a shared snapshot is"
                    f" freed only once every AMI referencing it is deregistered)."
                )

            if incremental_gb == 0:
                if not shared_ids:
                    # Snapshots are present but NONE could be sized (describe failed
                    # and the block-device mapping carried no VolumeSize) — abstain
                    # rather than emit a guessed or $0 dollar (no fabricated dollars;
                    # fail safe on missing data). This is NOT a sharing case.
                    continue
                # Every sizable backing snapshot is shared with an earlier AMI and
                # already counted there, so deregistering THIS AMI alone frees
                # nothing. Emit a $0 advisory (Counted=False) so it stays visible as
                # a co-dependent deletion candidate without double-counting the
                # shared snapshot storage into the headline.
                rec = {
                    "ImageId": ami_id,
                    "Name": ami.get("Name", "N/A"),
                    "AgeDays": age_days,
                    "CreationDate": ami["CreationDate"],
                    "SnapshotSizeGB": 0.0,
                    "SnapshotIds": snapshot_ids,
                    "SharedSnapshotIds": shared_ids,
                    "SizeEstimated": size_estimated,
                    "Counted": False,
                    "Recommendation": (
                        f"Unused AMI ({age_days} days old) whose backing snapshot(s)"
                        f" are ALL shared with another unused AMI counted elsewhere."
                        f" Deregistering this AMI alone frees nothing; deregister"
                        f" every AMI sharing the snapshot(s) to realize the saving"
                        f" (counted under the other AMI)."
                    ),
                    "EstimatedSavings": (
                        "$0.00/month — advisory: backing snapshots shared with"
                        " another unused AMI (counted there)"
                    ),
                    "EstimatedMonthlySavings": 0.0,
                    "AuditBasis": (
                        "All backing snapshots already attributed to an earlier"
                        " unused-AMI rec; counting them here too would double-count"
                        f" shared snapshot storage. Shared snapshot ids: {', '.join(shared_ids)}."
                    ),
                    "CheckCategory": "Old Unused AMIs" if is_old else "Unused AMIs",
                }
                checks["old_amis" if is_old else "unused_amis"].append(rec)
                continue

            # AWS EBS snapshots are incremental — only changed blocks are billed.
            # VolumeSize is an UPPER BOUND; we emit the MAX with an explicit
            # qualifier in the display string. Round once so the rendered string
            # and the counted numeric agree to the cent (counted == rendered).
            monthly_snapshot_cost = round(incremental_gb * snapshot_rate, 2)
            rec = {
                "ImageId": ami_id,
                "Name": ami.get("Name", "N/A"),
                "AgeDays": age_days,
                "CreationDate": ami["CreationDate"],
                "SnapshotSizeGB": incremental_gb,
                "SnapshotIds": snapshot_ids,
                "SizeEstimated": size_estimated,
                "Recommendation": (
                    f"Unused AMI ({age_days} days old, not referenced by any"
                    " running/stopped instance, launch template, ASG, EC2 Fleet,"
                    " or Spot Fleet, and not shared cross-account) -"
                    f" {'deregister and delete snapshots' if is_old else 'verify then delete'}."
                    " Residual gap: EC2 Image Builder recipe/pipeline references"
                    " are not enumerated — confirm the AMI is not an Image Builder"
                    " output before deregistering."
                    + shared_note
                ),
                "EstimatedSavings": (
                    f"${monthly_snapshot_cost:.2f}/month"
                    f" ({incremental_gb:.1f}GB snapshot storage - max estimate)"
                ),
                "EstimatedMonthlySavings": monthly_snapshot_cost,
                "AuditBasis": (
                    f"{incremental_gb:.1f}GB stored snapshot blocks x"
                    f" ${snapshot_rate:.4f}/GB-Mo EBS snapshot storage"
                    " (AmazonEC2 EBS:SnapshotUsage, region-scaled) ="
                    f" ${monthly_snapshot_cost:.2f}/mo. Upper bound — snapshots"
                    " bill on changed blocks only."
                    + (
                        " Snapshot metadata was unavailable for one or more"
                        " snapshots; that portion of the size was inferred from"
                        " the AMI's provisioned block-device mapping (a further"
                        " overstatement of the billed stored bytes)."
                        if size_estimated
                        else ""
                    )
                    + shared_note
                ),
                "CheckCategory": "Old Unused AMIs" if is_old else "Unused AMIs",
            }
            checks["old_amis" if is_old else "unused_amis"].append(rec)
    except Exception as e:
        record_aws_error(ctx, e, service="ami", context="compute_ami_checks")

    return {
        "recommendations": checks.get("old_amis", []) + checks.get("unused_amis", []),
        "total_count": len(checks.get("old_amis", []) + checks.get("unused_amis", [])),
        **checks,
    }

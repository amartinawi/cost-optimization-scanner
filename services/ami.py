"""AMI lifecycle management checks.

Extracted from CostOptimizer.get_ami_checks() as a free function.
This module will later become AmiModule (T-321) implementing ServiceModule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.scan_context import ScanContext

OLD_SNAPSHOT_DAYS: int = 90


def compute_ami_checks(ctx: ScanContext, pricing_multiplier: float = 1.0) -> dict[str, Any]:
    """Compute AMI optimization checks (unused + old AMIs).

    Returns dict with 'recommendations' (list) and 'total_count' (int).
    """
    print("🔍 [services/ami.py] AMI module active")
    ec2 = ctx.client("ec2")
    autoscaling = ctx.client("autoscaling")
    checks: dict[str, list[dict[str, Any]]] = {"unused_amis": [], "old_amis": []}

    try:
        amis_response = ec2.describe_images(Owners=["self"])

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

        for ami in amis_response.get("Images", []):
            ami_id = ami["ImageId"]
            creation_date = datetime.strptime(ami["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - creation_date).days

            if ami_id not in running_amis and age_days > 30:
                checks["unused_amis"].append(
                    {
                        "ImageId": ami_id,
                        "Name": ami.get("Name", "N/A"),
                        "AgeDays": age_days,
                        "Recommendation": (
                            f"AMI appears unused (not found in running instances,"
                            f" launch templates, or ASGs) and is {age_days} days"
                            " old - verify before deletion"
                        ),
                        "EstimatedSavings": "Snapshot storage costs (varies by AMI size)",
                        "CheckCategory": "Unused AMIs",
                    }
                )

            if age_days > OLD_SNAPSHOT_DAYS:
                total_snapshot_size_gb = 0
                snapshot_ids: list[str] = []

                for block_device in ami.get("BlockDeviceMappings", []):
                    if "Ebs" in block_device and "SnapshotId" in block_device["Ebs"]:
                        snapshot_id = block_device["Ebs"]["SnapshotId"]
                        snapshot_ids.append(snapshot_id)

                        try:
                            snapshot_response = ec2.describe_snapshots(SnapshotIds=[snapshot_id])
                            for snapshot in snapshot_response.get("Snapshots", []):
                                total_snapshot_size_gb += snapshot.get("VolumeSize", 0)
                        except Exception as e:
                            print(f"⚠️ Error getting snapshot details for {snapshot_id}: {str(e)}")
                            total_snapshot_size_gb += block_device["Ebs"].get("VolumeSize", 8)

                if total_snapshot_size_gb == 0:
                    total_snapshot_size_gb = 8

                monthly_snapshot_cost = total_snapshot_size_gb * 0.05 * pricing_multiplier

                checks["old_amis"].append(
                    {
                        "ImageId": ami["ImageId"],
                        "Name": ami.get("Name", "N/A"),
                        "AgeDays": age_days,
                        "CreationDate": ami["CreationDate"],
                        "SnapshotSizeGB": total_snapshot_size_gb,
                        "SnapshotIds": snapshot_ids,
                        "Recommendation": f"Review {age_days}-day old AMI for deletion",
                        "EstimatedSavings": (
                            f"${monthly_snapshot_cost:.2f}/month"
                            f" ({total_snapshot_size_gb}GB snapshot storage - max estimate)"
                        ),
                        "EstimatedMonthlySavings": monthly_snapshot_cost,
                    }
                )
    except Exception as e:
        print(f"Warning: Could not get AMI checks: {e}")

    return {"recommendations": checks["old_amis"], "total_count": len(checks["old_amis"])}

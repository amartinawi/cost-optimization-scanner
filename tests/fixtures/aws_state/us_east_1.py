"""
Moto state seeder for us-east-1 region.

Seeds moto's backend with resources matching the golden JSON fixture at
tests/fixtures/golden_scan_results.json. The golden fixture contains:
- EC2: 5 recommendations (idle, previous-gen, rightsizing, burstable, stopped)
- AMI: 2 recommendations (old unused AMIs)
- EBS: 4 recommendations (unattached volumes, gp2->gp3 migration)
- S3:  3 recommendations (lifecycle, intelligent-tiering, glacier)

Usage (inside a moto.mock_aws() context)::

    from tests.fixtures.aws_state.us_east_1 import seed_all
    resource_ids = seed_all()
"""

from __future__ import annotations

import datetime
from typing import Any, Dict

import boto3

REGION = "us-east-1"


def _seed_ec2() -> Dict[str, Any]:
    """Seed EC2 instances matching the golden fixture's 5 recommendations.

    Golden recommendations:
    1. i-0a1b2c3d4e5f67890 — t2.medium running (prev-gen upgrade)
    2. i-0b2c3d4e5f6789012 — m5.xlarge running (rightsizing, low CPU)
    3. i-0c3d4e5f678901234 — t3.small running (burstable low credits)
    4. i-0d4e5f67890123456 — t2.large stopped
    5. i-0e5f6789012345678 — c5.2xlarge running (idle)

    Plus additional non-flagged instances to reach the golden's 12 total.
    """
    ec2 = boto3.client("ec2", region_name=REGION)

    created: Dict[str, Any] = {"instances": [], "vpcs": [], "subnets": [], "security_groups": []}

    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc["Vpc"]["VpcId"]
    created["vpcs"].append(vpc_id)

    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")
    subnet_id = subnet["Subnet"]["SubnetId"]
    created["subnets"].append(subnet_id)

    sg = ec2.create_security_group(
        GroupName="test-sg",
        Description="Test security group",
        VpcId=vpc_id,
    )
    sg_id = sg["GroupId"]
    created["security_groups"].append(sg_id)

    instance_specs = [
        ("t2.medium", "run", "prev-gen-instance"),
        ("m5.xlarge", "run", "underutilized-instance"),
        ("t3.small", "run", "burstable-instance"),
        ("t2.large", "stop", "stopped-instance"),
        ("c5.2xlarge", "run", "idle-instance"),
        ("t3.medium", "run", "normal-instance-1"),
        ("t3.large", "run", "normal-instance-2"),
        ("m5.large", "run", "normal-instance-3"),
        ("t3.micro", "run", "normal-instance-4"),
        ("m5.xlarge", "run", "normal-instance-5"),
        ("t3.medium", "run", "normal-instance-6"),
        ("t3.small", "run", "normal-instance-7"),
    ]

    for instance_type, target_state, name in instance_specs:
        resp = ec2.run_instances(
            ImageId="ami-12345678",
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": name}],
                }
            ],
        )
        instance_id = resp["Instances"][0]["InstanceId"]
        created["instances"].append(instance_id)

        if target_state == "stop":
            ec2.stop_instances(InstanceIds=[instance_id])

    return created


def _seed_amis(ec2_instance_id: str) -> Dict[str, Any]:
    """Seed AMIs matching the golden fixture's 2 old-AMI recommendations.

    Golden AMIs:
    1. ami-0abc123def4567890 — old-ubuntu-20.04-base, 470 days old
    2. ami-1def4567890abc123 — legacy-app-server-v2, 526 days old

    Plus newer AMIs that won't trigger the old-AMI check.

    Args:
        ec2_instance_id: A running EC2 instance ID to create AMIs from.
    """
    ec2 = boto3.client("ec2", region_name=REGION)

    created: Dict[str, Any] = {"amis": []}

    amis_to_create = [
        "old-ubuntu-20.04-base",
        "legacy-app-server-v2",
        "recent-app-v3",
        "fresh-ami-2026",
    ]

    for name in amis_to_create:
        resp = ec2.create_image(
            InstanceId=ec2_instance_id,
            Name=name,
            Description=f"Test AMI: {name}",
            NoReboot=True,
        )
        created["amis"].append(resp["ImageId"])

    return created


def _seed_ebs() -> Dict[str, Any]:
    """Seed EBS volumes matching the golden fixture's 4 recommendations.

    Golden recommendations:
    1. vol-0a1b2c3d4e5f67890 — gp3, 100GB, unattached
    2. vol-0b2c3d4e5f678901 — gp2, 500GB, unattached
    3. vol-0c3d4e5f67890123 — gp2, 200GB, attached (gp2->gp3)
    4. vol-0d4e5f6789012345 — gp2, 100GB, attached (gp2->gp3)

    Plus additional volumes to reach 18 total (7 gp2, 3 unattached per golden).
    """
    ec2 = boto3.client("ec2", region_name=REGION)

    created: Dict[str, Any] = {"volumes": [], "snapshots": []}

    az = f"{REGION}a"

    unattached_specs = [
        ("gp3", 100),
        ("gp2", 500),
    ]

    for vol_type, size in unattached_specs:
        resp = ec2.create_volume(
            AvailabilityZone=az,
            Size=size,
            VolumeType=vol_type,
        )
        created["volumes"].append(resp["VolumeId"])

    gp2_attached_specs = [
        ("gp2", 200),
        ("gp2", 100),
    ]

    instances = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
    running_instance = None
    for reservation in instances.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            running_instance = inst["InstanceId"]
            break
        if running_instance:
            break

    for vol_type, size in gp2_attached_specs:
        resp = ec2.create_volume(
            AvailabilityZone=az,
            Size=size,
            VolumeType=vol_type,
        )
        vol_id = resp["VolumeId"]
        created["volumes"].append(vol_id)
        if running_instance:
            ec2.attach_volume(
                VolumeId=vol_id,
                InstanceId=running_instance,
                Device=f"/dev/sd{chr(102 + len(created['volumes']))}",
            )

    extra_specs = [
        ("gp3", 50),
        ("gp3", 30),
        ("gp2", 150),
        ("gp2", 80),
        ("gp2", 60),
        ("gp2", 250),
        ("gp3", 200),
        ("gp3", 100),
        ("gp3", 40),
        ("gp3", 75),
        ("gp3", 120),
        ("gp3", 500),
    ]

    for vol_type, size in extra_specs:
        resp = ec2.create_volume(
            AvailabilityZone=az,
            Size=size,
            VolumeType=vol_type,
        )
        created["volumes"].append(resp["VolumeId"])

    old_snap_date = datetime.datetime(2025, 6, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)
    snap = ec2.create_snapshot(
        VolumeId=created["volumes"][0],
        Description="Old snapshot for cost optimization testing",
        TagSpecifications=[
            {
                "ResourceType": "snapshot",
                "Tags": [{"Key": "Name", "Value": "old-snapshot-90-plus-days"}],
            }
        ],
    )
    created["snapshots"].append(snap["SnapshotId"])

    recent_snap = ec2.create_snapshot(
        VolumeId=created["volumes"][1],
        Description="Recent snapshot",
    )
    created["snapshots"].append(recent_snap["SnapshotId"])

    return created


def _seed_s3() -> Dict[str, Any]:
    """Seed S3 buckets matching the golden fixture's 3 recommendations.

    Golden recommendations:
    1. my-company-logs-prod — no lifecycle, no intelligent-tiering
    2. data-lake-raw-us-east-1 — no lifecycle, no intelligent-tiering
    3. backup-archive-2024 — no lifecycle, no intelligent-tiering

    Plus additional buckets to reach 15 total, some with lifecycle policies.
    """
    s3 = boto3.client("s3", region_name=REGION)

    created: Dict[str, Any] = {"buckets": []}

    flagged_buckets = [
        "my-company-logs-prod",
        "data-lake-raw-us-east-1",
        "backup-archive-2024",
    ]

    compliant_buckets = [
        "compliant-data-bucket",
        "well-configured-logs",
        "archived-data-2025",
    ]

    other_buckets = [
        "app-uploads-us-east-1",
        "cloudfront-logs-bucket",
        "terraform-state-prod",
        "lambda-deployments",
        "config-recorder-bucket",
        "aws-logs-archive",
        "test-bucket-dev",
        "metrics-export-bucket",
        "data-processing-temp",
    ]

    for name in flagged_buckets + compliant_buckets + other_buckets:
        s3.create_bucket(Bucket=name)
        created["buckets"].append(name)

    for name in compliant_buckets:
        s3.put_bucket_lifecycle_configuration(
            Bucket=name,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "default-transition",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Transitions": [
                            {"Days": 30, "StorageClass": "STANDARD_IA"},
                        ],
                    }
                ]
            },
        )

    return created


def seed_all() -> Dict[str, Any]:
    """Seed all moto resources for us-east-1.

    Must be called inside a ``moto.mock_aws()`` context.

    Returns:
        Dict mapping resource categories to lists of created resource IDs.
    """
    result: Dict[str, Any] = {}
    result["ec2"] = _seed_ec2()
    first_running = result["ec2"]["instances"][0]
    result["ami"] = _seed_amis(first_running)
    result["ebs"] = _seed_ebs()
    result["s3"] = _seed_s3()
    return result

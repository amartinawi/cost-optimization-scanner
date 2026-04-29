"""Elastic IP cost optimization checks.

Extracted from CostOptimizer.get_elastic_ip_checks() as a free function.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/elastic_ip.py] Elastic IP module active")


def get_elastic_ip_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 1: Elastic IPs & Public Addressing optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "unassociated_eips": [],
        "eips_on_stopped_instances": [],
        "multiple_eips_per_instance": [],
        "public_ips_should_be_private": [],
    }

    try:
        ec2 = ctx.client("ec2")

        eips_response = ec2.describe_addresses()
        addresses = eips_response.get("Addresses", [])

        paginator = ec2.get_paginator("describe_instances")
        instances: dict[str, dict[str, Any]] = {}
        for page in paginator.paginate():
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    instances[instance["InstanceId"]] = instance

        instance_eip_count: dict[str, int] = {}

        for eip in addresses:
            allocation_id = eip.get("AllocationId", "N/A")
            public_ip = eip.get("PublicIp", "N/A")

            if not eip.get("InstanceId") and not eip.get("NetworkInterfaceId"):
                eip_name = f"EIP {public_ip} ({allocation_id})"

                checks["unassociated_eips"].append(
                    {
                        "AllocationId": allocation_id,
                        "PublicIp": public_ip,
                        "ResourceName": eip_name,
                        "Recommendation": "Release unassociated Elastic IP to avoid charges",
                        "EstimatedSavings": "$3.65/month per EIP",
                        "CheckCategory": "Unassociated EIPs",
                    }
                )

            elif eip.get("InstanceId"):
                instance_id = eip["InstanceId"]
                instance = instances.get(instance_id)

                instance_eip_count[instance_id] = instance_eip_count.get(instance_id, 0) + 1

                if instance and instance.get("State", {}).get("Name") == "stopped":
                    checks["eips_on_stopped_instances"].append(
                        {
                            "AllocationId": allocation_id,
                            "PublicIp": public_ip,
                            "InstanceId": instance_id,
                            "InstanceState": "stopped",
                            "Recommendation": "Release EIP from stopped instance or start instance",
                            "EstimatedSavings": "$3.65/month per EIP",
                            "CheckCategory": "EIPs on Stopped Instances",
                        }
                    )

        for instance_id, eip_count in instance_eip_count.items():
            if eip_count > 1:
                instance = instances.get(instance_id, {})
                checks["multiple_eips_per_instance"].append(
                    {
                        "InstanceId": instance_id,
                        "EIPCount": eip_count,
                        "InstanceType": instance.get("InstanceType", "N/A"),
                        "Recommendation": f"Instance has {eip_count} EIPs - review if all are necessary",
                        "EstimatedSavings": f"${(eip_count - 1) * 3.65:.2f}/month if reduced to 1 EIP",
                        "CheckCategory": "Multiple EIPs per Instance",
                    }
                )

        for instance_id, instance in instances.items():
            if instance.get("PublicIpAddress") and instance.get("State", {}).get("Name") == "running":
                subnet_id = instance.get("SubnetId")
                if subnet_id:
                    try:
                        subnet_response = ec2.describe_subnets(SubnetIds=[subnet_id])
                        subnet = subnet_response["Subnets"][0]
                        if not subnet.get("MapPublicIpOnLaunch", False):
                            instance_name = "Unknown"
                            for tag in instance.get("Tags", []):
                                if tag.get("Key") == "Name":
                                    instance_name = tag.get("Value", "Unknown")
                                    break

                            if instance_name == "Unknown":
                                instance_type = instance.get("InstanceType", "unknown")
                                instance_name = f"{instance_type} ({instance_id})"

                            checks["public_ips_should_be_private"].append(
                                {
                                    "InstanceId": instance_id,
                                    "InstanceName": instance_name,
                                    "InstanceType": instance.get("InstanceType", "unknown"),
                                    "PublicIp": instance.get("PublicIpAddress"),
                                    "SubnetId": subnet_id,
                                    "Recommendation": "Instance in private subnet has public IP - review necessity",
                                    "EstimatedSavings": "$3.65/month per public IP if removed",
                                    "CheckCategory": "Public IP Optimization",
                                }
                            )
                    except Exception as e:
                        print(f"Warning: Could not check instance {instance_id}: {e}")
                        continue

    except Exception as e:
        print(f"Warning: Could not perform Elastic IP checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

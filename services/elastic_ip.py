"""Elastic IP cost optimization checks.

Extracted from CostOptimizer.get_elastic_ip_checks() as a free function.
"""

from __future__ import annotations

import logging
from typing import Any

from core.pricing_engine import FALLBACK_EIP_MONTH
from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

logger = logging.getLogger(__name__)


def get_elastic_ip_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 1: Elastic IPs & Public Addressing optimization checks."""
    # Public IPv4 / EIP is a FLAT $3.65/mo charge in every commercial region —
    # not region-scaled — so the pricing_engine=None fallback uses the flat
    # constant directly (multiplying by pricing_multiplier would fabricate a
    # region-specific rate for a globally flat charge).
    eip_monthly = (
        ctx.pricing_engine.get_eip_monthly_price()
        if ctx.pricing_engine is not None
        else FALLBACK_EIP_MONTH
    )
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
                        "EstimatedSavings": f"${eip_monthly:.2f}/month per EIP",
                        "EstimatedMonthlySavings": round(eip_monthly, 2),
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
                            "EstimatedSavings": f"${eip_monthly:.2f}/month per EIP",
                            "EstimatedMonthlySavings": round(eip_monthly, 2),
                            "CheckCategory": "EIPs on Stopped Instances",
                        }
                    )

        # An EIP on a stopped instance is already counted in
        # eips_on_stopped_instances; counting its instance again under
        # multiple_eips_per_instance attributes the same $/EIP twice across two
        # categories. Exclude stopped instances from the multiple-EIPs lever so
        # the saving is counted once (network_cost NET-03 double-count).
        stopped_ids = {r["InstanceId"] for r in checks["eips_on_stopped_instances"]}
        for instance_id, eip_count in instance_eip_count.items():
            if eip_count > 1 and instance_id not in stopped_ids:
                instance = instances.get(instance_id, {})
                checks["multiple_eips_per_instance"].append(
                    {
                        "InstanceId": instance_id,
                        "EIPCount": eip_count,
                        "InstanceType": instance.get("InstanceType", "N/A"),
                        "Recommendation": f"Instance has {eip_count} EIPs - review if all are necessary",
                        "EstimatedSavings": f"${(eip_count - 1) * eip_monthly:.2f}/month if reduced to 1 EIP",
                        "EstimatedMonthlySavings": round((eip_count - 1) * eip_monthly, 2),
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
                                    "Recommendation": (
                                        "Instance in a private subnet has a public IP — review necessity. "
                                        "Recoverable only if the instance does not need public reachability "
                                        "(NAT/VPN/bastion hosts legitimately require it)."
                                    ),
                                    # Advisory, NOT a counted saving: this is an architectural "should be
                                    # private" nudge on a RUNNING instance whose public IP may be required by
                                    # design (e.g. a VPN server). The $ is realizable only if the user removes
                                    # the IP, so it is rendered as a $0 advisory, never summed (network public-IP
                                    # fix). Distinct from an unassociated/unused EIP, which IS a definite saving.
                                    "EstimatedSavings": (
                                        f"$0.00/month — advisory: ${eip_monthly:.2f}/mo recoverable only "
                                        "if the public IP can be removed"
                                    ),
                                    "EstimatedMonthlySavings": 0.0,
                                    "Counted": False,
                                    "CheckCategory": "Public IP Optimization",
                                }
                            )
                    except Exception as e:
                        record_aws_error(
                            ctx, e, service="network", context=f"EIP subnet check for {instance_id} failed"
                        )
                        continue

    except Exception as e:
        record_aws_error(ctx, e, service="network", context="Elastic IP checks failed")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

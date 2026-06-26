# ruff: noqa: E501
"""VPC Endpoints cost optimization checks.

Extracted from CostOptimizer.get_vpc_endpoints_checks() as a free function.
"""

from __future__ import annotations

import logging
from typing import Any

from core.pricing_engine import FALLBACK_VPC_ENDPOINT_MONTH
from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

logger = logging.getLogger(__name__)


def _interface_az_count(endpoint: dict[str, Any]) -> int:
    """Number of AZs an Interface endpoint bills for (one ENI per subnet/AZ).

    Interface endpoints are billed per-AZ-hour, so an endpoint spanning N subnets
    bills N× the hourly rate. Falls back to 1 when subnet data is absent.
    """
    subnets = endpoint.get("SubnetIds") or endpoint.get("NetworkInterfaceIds") or []
    return max(1, len(subnets))


def get_vpc_endpoints_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 3: VPC Endpoints optimization checks."""
    vpc_ep_monthly = (
        ctx.pricing_engine.get_vpc_endpoint_monthly_price()
        if ctx.pricing_engine is not None
        else FALLBACK_VPC_ENDPOINT_MONTH * ctx.pricing_multiplier
    )
    checks: dict[str, list[dict[str, Any]]] = {
        "missing_gateway_endpoints": [],
        "unused_interface_endpoints": [],
        "interface_endpoints_in_nonprod": [],
        "duplicate_endpoints": [],
        "no_traffic_endpoints": [],
    }

    try:
        ec2 = ctx.client("ec2")

        vpcs_paginator = ec2.get_paginator("describe_vpcs")
        vpcs: list[dict[str, Any]] = []
        for page in vpcs_paginator.paginate():
            vpcs.extend(page.get("Vpcs", []))

        endpoints_paginator = ec2.get_paginator("describe_vpc_endpoints")
        endpoints: list[dict[str, Any]] = []
        for page in endpoints_paginator.paginate():
            endpoints.extend(page.get("VpcEndpoints", []))

        for vpc in vpcs:
            vpc_id = vpc["VpcId"]
            vpc_endpoints_in_vpc = [ep for ep in endpoints if ep.get("VpcId") == vpc_id]

            has_s3_gateway = any(
                ep.get("ServiceName", "").endswith(".s3") and ep.get("VpcEndpointType") == "Gateway"
                for ep in vpc_endpoints_in_vpc
            )

            has_dynamodb_gateway = any(
                ep.get("ServiceName", "").endswith(".dynamodb") and ep.get("VpcEndpointType") == "Gateway"
                for ep in vpc_endpoints_in_vpc
            )

            if not has_s3_gateway:
                checks["missing_gateway_endpoints"].append(
                    {
                        "VpcId": vpc_id,
                        "MissingService": "S3",
                        "EndpointType": "Gateway",
                        "Recommendation": "Create S3 Gateway endpoint to reduce NAT Gateway costs",
                        "EstimatedSavings": "$0.01/GB data processing + NAT costs",
                        "CheckCategory": "Missing S3 Gateway Endpoint",
                    }
                )

            if not has_dynamodb_gateway:
                checks["missing_gateway_endpoints"].append(
                    {
                        "VpcId": vpc_id,
                        "MissingService": "DynamoDB",
                        "EndpointType": "Gateway",
                        "Recommendation": "Create DynamoDB Gateway endpoint to reduce NAT Gateway costs",
                        "EstimatedSavings": "$0.01/GB data processing + NAT costs",
                        "CheckCategory": "Missing DynamoDB Gateway Endpoint",
                    }
                )

        # Interface endpoints bill per-AZ; Gateway endpoints are free and must
        # never be priced. Group ONLY interface endpoints for the duplicate check.
        interface_by_service: dict[str, list[dict[str, Any]]] = {}
        for endpoint in endpoints:
            endpoint_id = endpoint.get("VpcEndpointId")
            service_name = endpoint.get("ServiceName", "")
            endpoint_type = endpoint.get("VpcEndpointType", "")
            vpc_id = endpoint.get("VpcId")

            if endpoint_type != "Interface":
                continue

            az_count = _interface_az_count(endpoint)
            tags = {tag["Key"]: tag["Value"] for tag in endpoint.get("Tags", [])}
            environment = tags.get("Environment", "").lower()

            if environment in ["dev", "test", "development", "staging"]:
                savings = vpc_ep_monthly * az_count
                checks["interface_endpoints_in_nonprod"].append(
                    {
                        "VpcEndpointId": endpoint_id,
                        "ServiceName": service_name,
                        "VpcId": vpc_id,
                        "Environment": environment,
                        "AvailabilityZoneCount": az_count,
                        "Recommendation": f"Interface endpoint in non-prod may be unnecessary ({az_count} AZ(s) billed)",
                        "EstimatedSavings": f"${savings:.2f}/month per endpoint",
                        "EstimatedMonthlySavings": round(savings, 2),
                        "CheckCategory": "Interface Endpoints in Non-Prod",
                    }
                )

            service_key = f"{vpc_id}:{service_name}"
            interface_by_service.setdefault(service_key, []).append(
                {"endpoint_id": endpoint_id, "az_count": az_count, "state": endpoint.get("State", "")}
            )

        for service_key, service_endpoints in interface_by_service.items():
            if len(service_endpoints) > 2:
                vpc_id, service_name = service_key.split(":", 1)
                # Keep two endpoints (valid for distinct route tables/policies);
                # the rest are removable at their own per-AZ cost.
                removable = service_endpoints[2:]
                savings = sum(vpc_ep_monthly * ep["az_count"] for ep in removable)
                checks["duplicate_endpoints"].append(
                    {
                        "VpcId": vpc_id,
                        "ServiceName": service_name,
                        "EndpointCount": len(service_endpoints),
                        "EndpointIds": [ep["endpoint_id"] for ep in service_endpoints],
                        "Recommendation": f"{len(service_endpoints)} Interface endpoints for same service - review if all needed (multiple can be valid for different route tables/policies)",
                        "EstimatedSavings": f"${savings:.2f}/month if some consolidated",
                        "EstimatedMonthlySavings": round(savings, 2),
                        "CheckCategory": "Multiple VPC Endpoints",
                    }
                )

    except Exception as e:
        record_aws_error(ctx, e, service="network", context="VPC Endpoints checks failed")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

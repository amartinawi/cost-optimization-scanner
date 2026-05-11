# ruff: noqa: E501
"""VPC Endpoints cost optimization checks.

Extracted from CostOptimizer.get_vpc_endpoints_checks() as a free function.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/vpc_endpoints.py] VPC Endpoints module active")


def get_vpc_endpoints_checks(ctx: ScanContext) -> dict[str, Any]:
    vpc_ep_monthly = ctx.pricing_engine.get_vpc_endpoint_monthly_price() if ctx.pricing_engine is not None else 7.30
    """Category 3: VPC Endpoints optimization checks"""
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

        endpoint_services: dict[str, list[dict[str, str | None]]] = {}
        for endpoint in endpoints:
            endpoint_id = endpoint.get("VpcEndpointId")
            service_name = endpoint.get("ServiceName", "")
            endpoint_type = endpoint.get("VpcEndpointType", "")
            vpc_id = endpoint.get("VpcId")

            tags = {tag["Key"]: tag["Value"] for tag in endpoint.get("Tags", [])}
            environment = tags.get("Environment", "").lower()

            if endpoint_type == "Interface" and environment in ["dev", "test", "development", "staging"]:
                checks["interface_endpoints_in_nonprod"].append(
                    {
                        "VpcEndpointId": endpoint_id,
                        "ServiceName": service_name,
                        "VpcId": vpc_id,
                        "Environment": environment,
                        "Recommendation": "Interface endpoints in non-prod may be unnecessary",
                        "EstimatedSavings": f"${vpc_ep_monthly:.2f}/month per endpoint",
                        "CheckCategory": "Interface Endpoints in Non-Prod",
                    }
                )

            service_key = f"{vpc_id}:{service_name}"
            if service_key not in endpoint_services:
                endpoint_services[service_key] = []
            endpoint_services[service_key].append(
                {"endpoint_id": endpoint_id, "type": endpoint_type, "state": endpoint.get("State", "")}
            )

        for service_key, service_endpoints in endpoint_services.items():
            if len(service_endpoints) > 2:
                vpc_id, service_name = service_key.split(":", 1)
                checks["duplicate_endpoints"].append(
                    {
                        "VpcId": vpc_id,
                        "ServiceName": service_name,
                        "EndpointCount": len(service_endpoints),
                        "EndpointIds": [ep["endpoint_id"] for ep in service_endpoints],
                        "Recommendation": f"{len(service_endpoints)} endpoints for same service - review if all needed (multiple can be valid for different route tables/policies)",
                        "EstimatedSavings": f"${(len(service_endpoints) - 2) * vpc_ep_monthly:.2f}/month if some consolidated",
                        "CheckCategory": "Multiple VPC Endpoints",
                    }
                )

    except Exception as e:
        print(f"Warning: Could not perform VPC Endpoints checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

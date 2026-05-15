# ruff: noqa: E501
"""NAT Gateway cost optimization checks.

Extracted from CostOptimizer.get_nat_gateway_checks() as a free function.
"""

from __future__ import annotations

from typing import Any

from core.scan_context import ScanContext

print("🔍 [services/nat_gateway.py] NAT Gateway module active")


def get_nat_gateway_checks(ctx: ScanContext) -> dict[str, Any]:
    nat_monthly = ctx.pricing_engine.get_nat_gateway_monthly_price() if ctx.pricing_engine is not None else 32.0
    """Category 2: NAT Gateway & VPC Design optimization checks"""
    checks: dict[str, list[dict[str, Any]]] = {
        "low_throughput_nat_gateways": [],
        "unnecessary_nat_per_az": [],
        "nat_for_aws_services": [],
        "nat_in_dev_test": [],
        "multiple_nat_gateways": [],
    }

    try:
        ec2 = ctx.client("ec2")

        paginator = ec2.get_paginator("describe_nat_gateways")
        nat_gateways: list[dict[str, Any]] = []
        for page in paginator.paginate():
            nat_gateways.extend(page.get("NatGateways", []))

        endpoints_paginator = ec2.get_paginator("describe_vpc_endpoints")
        vpc_endpoints: list[dict[str, Any]] = []
        for page in endpoints_paginator.paginate():
            vpc_endpoints.extend(page.get("VpcEndpoints", []))

        vpc_nat_count: dict[str, int] = {}
        az_nat_count: dict[str, int] = {}

        for nat in nat_gateways:
            if nat.get("State") == "available":
                vpc_id = nat.get("VpcId", "")
                subnet_id = nat.get("SubnetId", "")
                nat_id = nat.get("NatGatewayId", "")

                try:
                    subnet_response = ec2.describe_subnets(SubnetIds=[subnet_id])
                    az = subnet_response["Subnets"][0].get("AvailabilityZone", "")

                    vpc_nat_count[vpc_id] = vpc_nat_count.get(vpc_id, 0) + 1
                    az_key = f"{vpc_id}:{az}"
                    az_nat_count[az_key] = az_nat_count.get(az_key, 0) + 1

                    tags = {tag["Key"]: tag["Value"] for tag in nat.get("Tags", [])}
                    environment = tags.get("Environment", "").lower()

                    if environment in ["dev", "test", "development", "staging"]:
                        nat_name = f"NAT Gateway {nat_id} ({az})"

                        checks["nat_in_dev_test"].append(
                            {
                                "NatGatewayId": nat_id,
                                "VpcId": vpc_id,
                                "AvailabilityZone": az,
                                "Environment": environment,
                                "ResourceName": nat_name,
                                "Recommendation": "Consider NAT instance or scheduled shutdown for dev/test",
                                "EstimatedSavings": f"${nat_monthly + 0.85:.2f}/month base + data processing fees",
                                "CheckCategory": "Dev/Test NAT Optimization",
                            }
                        )

                    vpc_has_s3_endpoint = any(
                        ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".s3") for ep in vpc_endpoints
                    )
                    vpc_has_dynamodb_endpoint = any(
                        ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".dynamodb")
                        for ep in vpc_endpoints
                    )

                    if not vpc_has_s3_endpoint or not vpc_has_dynamodb_endpoint:
                        missing_endpoints = []
                        if not vpc_has_s3_endpoint:
                            missing_endpoints.append("S3")
                        if not vpc_has_dynamodb_endpoint:
                            missing_endpoints.append("DynamoDB")

                        checks["nat_for_aws_services"].append(
                            {
                                "NatGatewayId": nat_id,
                                "VpcId": vpc_id,
                                "MissingEndpoints": missing_endpoints,
                                "Recommendation": f"Create VPC endpoints for {', '.join(missing_endpoints)} to reduce NAT costs",
                                "EstimatedSavings": "$0.01/GB data processing savings",
                                "CheckCategory": "VPC Endpoints Missing",
                            }
                        )

                except Exception as e:
                    print(f"Warning: Could not analyze NAT gateway {nat_id}: {e}")

        for az_key, count in az_nat_count.items():
            if count > 1:
                vpc_id, az = az_key.split(":")
                # Same-AZ duplicates are pure waste (no HA trade-off — both
                # gateways already share the same AZ failure domain).
                # Saving = all redundant NATs above 1.
                az_consolidate_savings = (count - 1) * nat_monthly
                checks["multiple_nat_gateways"].append(
                    {
                        "VpcId": vpc_id,
                        "AvailabilityZone": az,
                        "NatGatewayCount": count,
                        "Recommendation": f"{count} NAT Gateways in same AZ - review if all are needed",
                        "EstimatedSavings": f"${az_consolidate_savings:.2f}/month if consolidated",
                        "EstimatedMonthlySavings": round(az_consolidate_savings, 2),
                        "CheckCategory": "Multiple NAT Gateways",
                    }
                )

        for nat in nat_gateways:
            if nat.get("State") == "available":
                nat_id = nat.get("NatGatewayId", "N/A")
                # Real low-throughput savings depend on actual CW
                # BytesOutToDestination metric × $0.045/GB cross-AZ.
                # Without that data, emit 0 + warning rather than the
                # previous invented "$25/month" string.
                checks["low_throughput_nat_gateways"].append(
                    {
                        "NatGatewayId": nat_id,
                        "VpcId": nat.get("VpcId", "N/A"),
                        "Recommendation": "Monitor CloudWatch metrics - consider NAT instance for low throughput",
                        "EstimatedSavings": "$0.00/month - requires CW BytesOutToDestination metric",
                        "EstimatedMonthlySavings": 0.0,
                        "PricingWarning": "requires CW BytesOutToDestination × cross-AZ rate for quantified savings",
                        "CheckCategory": "Low Throughput NAT Gateway",
                    }
                )

        for vpc_id, count in vpc_nat_count.items():
            if count > 1:
                # Cross-AZ NAT consolidation TRADES OFF availability.
                # AWS recommends 1 NAT per AZ for HA. Realistic savings =
                # count − az_count (the redundant NATs beyond AZ count).
                # We don't have AZ count per VPC available here; emit a
                # conservative estimate = full count - 1 with explicit
                # availability-trade-off warning so the user reads it.
                cross_az_savings = (count - 1) * nat_monthly
                checks["unnecessary_nat_per_az"].append(
                    {
                        "VpcId": vpc_id,
                        "NatGatewayCount": count,
                        "Recommendation": f"{count} NAT Gateways in VPC - consider consolidation; AWS recommends 1 NAT per AZ for HA",
                        "EstimatedSavings": f"${cross_az_savings:.2f}/month maximum (sacrifices per-AZ HA)",
                        "EstimatedMonthlySavings": round(cross_az_savings, 2),
                        "PricingWarning": "consolidation eliminates per-AZ failure isolation",
                        "CheckCategory": "Unnecessary NAT per AZ",
                    }
                )

    except Exception as e:
        print(f"Warning: Could not perform NAT Gateway checks: {e}")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

# ruff: noqa: E501
"""NAT Gateway cost optimization checks.

Extracted from CostOptimizer.get_nat_gateway_checks() as a free function.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from core.pricing_engine import FALLBACK_NAT_MONTH
from core.scan_context import ScanContext
from services._aws_errors import record_aws_error

logger = logging.getLogger(__name__)

_DEV_TEST_ENVS = frozenset({"dev", "test", "development", "staging"})


def get_nat_gateway_checks(ctx: ScanContext) -> dict[str, Any]:
    """Category 2: NAT Gateway & VPC Design optimization checks.

    Savings are attributed to each NAT at most once. Same-AZ duplicates are pure
    waste (no HA trade-off) and counted fully; cross-AZ consolidation only counts
    the *incremental* NATs beyond one-per-AZ (sacrificing HA), so a VPC's NATs are
    never double-counted across the same-AZ and per-VPC findings (audit H3).
    """
    nat_monthly = (
        ctx.pricing_engine.get_nat_gateway_monthly_price()
        if ctx.pricing_engine is not None
        else FALLBACK_NAT_MONTH * ctx.pricing_multiplier
    )
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

        # First pass: resolve each available NAT's VPC, AZ, and environment.
        available: list[dict[str, Any]] = []
        for nat in nat_gateways:
            if nat.get("State") != "available":
                continue
            nat_id = nat.get("NatGatewayId", "N/A")
            vpc_id = nat.get("VpcId", "")
            subnet_id = nat.get("SubnetId", "")
            try:
                subnet_response = ec2.describe_subnets(SubnetIds=[subnet_id])
                az = subnet_response["Subnets"][0].get("AvailabilityZone", "")
            except Exception as e:
                record_aws_error(ctx, e, service="network", context=f"NAT {nat_id} subnet lookup failed")
                continue
            tags = {tag["Key"]: tag["Value"] for tag in nat.get("Tags", [])}
            available.append(
                {
                    "nat_id": nat_id,
                    "vpc_id": vpc_id,
                    "az": az,
                    "environment": tags.get("Environment", "").lower(),
                }
            )

        vpc_nats: dict[str, list[dict[str, Any]]] = defaultdict(list)
        az_nats: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for n in available:
            vpc_nats[n["vpc_id"]].append(n)
            az_nats[(n["vpc_id"], n["az"])].append(n)

        # Same-AZ duplicates: pure waste, no HA trade-off → count (count - 1) fully.
        for (vpc_id, az), nats in az_nats.items():
            if len(nats) > 1:
                save = (len(nats) - 1) * nat_monthly
                checks["multiple_nat_gateways"].append(
                    {
                        "VpcId": vpc_id,
                        "AvailabilityZone": az,
                        "NatGatewayCount": len(nats),
                        "Recommendation": f"{len(nats)} NAT Gateways in same AZ - same AZ shares one failure domain, so duplicates add no HA",
                        "EstimatedSavings": f"${save:.2f}/month if consolidated",
                        "EstimatedMonthlySavings": round(save, 2),
                        "CheckCategory": "Multiple NAT Gateways",
                    }
                )

        # Cross-AZ consolidation: AWS recommends 1 NAT per AZ for HA. The same-AZ
        # waste above already counts everything beyond one-per-AZ, so here we count
        # ONLY the incremental (num_azs - 1) NATs removed by collapsing AZs — this
        # is the HA-sacrificing portion and does not overlap the same-AZ finding.
        for vpc_id, nats in vpc_nats.items():
            azs = {n["az"] for n in nats}
            if len(azs) > 1:
                save = (len(azs) - 1) * nat_monthly
                checks["unnecessary_nat_per_az"].append(
                    {
                        "VpcId": vpc_id,
                        "NatGatewayCount": len(nats),
                        "AvailabilityZoneCount": len(azs),
                        "Recommendation": f"{len(nats)} NAT Gateways across {len(azs)} AZs in VPC - collapsing to a single AZ sacrifices per-AZ HA",
                        "EstimatedSavings": f"${save:.2f}/month maximum (sacrifices per-AZ HA)",
                        "EstimatedMonthlySavings": round(save, 2),
                        "PricingWarning": "consolidation eliminates per-AZ failure isolation; incremental to same-AZ de-duplication",
                        "CheckCategory": "Unnecessary NAT per AZ",
                    }
                )

        # Missing S3/DynamoDB gateway endpoints: data-processing savings are a
        # per-GB rate, not a quantified monthly total → advisory ($0).
        for vpc_id, nats in vpc_nats.items():
            vpc_has_s3_endpoint = any(
                ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".s3") for ep in vpc_endpoints
            )
            vpc_has_dynamodb_endpoint = any(
                ep.get("VpcId") == vpc_id and ep.get("ServiceName", "").endswith(".dynamodb") for ep in vpc_endpoints
            )
            if vpc_has_s3_endpoint and vpc_has_dynamodb_endpoint:
                continue
            missing_endpoints = []
            if not vpc_has_s3_endpoint:
                missing_endpoints.append("S3")
            if not vpc_has_dynamodb_endpoint:
                missing_endpoints.append("DynamoDB")
            checks["nat_for_aws_services"].append(
                {
                    "NatGatewayId": nats[0]["nat_id"],
                    "VpcId": vpc_id,
                    "MissingEndpoints": missing_endpoints,
                    "Recommendation": f"Create VPC endpoints for {', '.join(missing_endpoints)} to reduce NAT data-processing costs",
                    "EstimatedSavings": "$0.01/GB data processing savings",
                    "CheckCategory": "VPC Endpoints Missing",
                }
            )

        # Dev/test NATs: count the base only when the NAT is the sole NAT in its
        # VPC (no consolidation finding owns the dollars). When the VPC has >1 NAT,
        # the consolidation findings already count the removable base, so dev/test
        # is advisory to avoid stacking the same NAT's cost (audit H3 + M1).
        for n in available:
            if n["environment"] not in _DEV_TEST_ENVS:
                continue
            sole_in_vpc = len(vpc_nats[n["vpc_id"]]) == 1
            if sole_in_vpc:
                savings_str = f"${nat_monthly:.2f}/month base if replaced with a NAT instance or scheduled off"
                monthly = round(nat_monthly, 2)
            else:
                savings_str = "$0.00/month - removable base counted under NAT consolidation findings"
                monthly = 0.0
            checks["nat_in_dev_test"].append(
                {
                    "NatGatewayId": n["nat_id"],
                    "VpcId": n["vpc_id"],
                    "AvailabilityZone": n["az"],
                    "Environment": n["environment"],
                    "ResourceName": f"NAT Gateway {n['nat_id']} ({n['az']})",
                    "Recommendation": "Consider a NAT instance or scheduled shutdown for dev/test",
                    "EstimatedSavings": savings_str,
                    "EstimatedMonthlySavings": monthly,
                    "CheckCategory": "Dev/Test NAT Optimization",
                }
            )

        # Low-throughput: real savings need the CloudWatch BytesOutToDestination
        # metric × the per-GB data-processing rate. Without that data, advisory.
        for n in available:
            checks["low_throughput_nat_gateways"].append(
                {
                    "NatGatewayId": n["nat_id"],
                    "VpcId": n["vpc_id"],
                    "Recommendation": "Monitor CloudWatch metrics - consider NAT instance for low throughput",
                    "EstimatedSavings": "$0.00/month - requires CW BytesOutToDestination metric",
                    "EstimatedMonthlySavings": 0.0,
                    "PricingWarning": "requires CW BytesOutToDestination × data-processing rate for quantified savings",
                    "CheckCategory": "Low Throughput NAT Gateway",
                }
            )

    except Exception as e:
        record_aws_error(ctx, e, service="network", context="NAT Gateway checks failed")

    recommendations: list[dict[str, Any]] = []
    for _category, items in checks.items():
        recommendations.extend(items)

    return {"recommendations": recommendations, **checks}

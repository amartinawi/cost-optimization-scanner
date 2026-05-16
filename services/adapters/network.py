"""Multi-source adapter for network infrastructure (EIP, NAT, VPC, ELB, ASG) optimization."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import parse_dollar_savings
from services.ec2 import get_auto_scaling_checks
from services.elastic_ip import get_elastic_ip_checks
from services.load_balancer import get_load_balancer_checks
from services.nat_gateway import get_nat_gateway_checks
from services.vpc_endpoints import get_vpc_endpoints_checks


def _sum_savings(recs: list[dict[str, Any]]) -> float:
    """Sum parsed dollar savings across a recommendation list."""
    return sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in recs)


class NetworkModule(BaseServiceModule):
    """ServiceModule adapter for EIP, NAT, VPC, ELB, and ASG. Composite savings strategy."""

    key: str = "network"
    cli_aliases: tuple[str, ...] = ("network",)
    display_name: str = "Network & Infrastructure"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for network infrastructure scanning."""
        return ("ec2", "elbv2", "autoscaling", "elb")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Elastic IPs, NAT Gateways, VPC Endpoints, Load Balancers, and ASGs.

        Each sub-area produces its own SourceBlock so the report can show
        per-domain savings rather than a single rolled-up "enhanced_checks"
        bucket.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with `elastic_ips`, `nat_gateways`, `vpc_endpoints`,
            `load_balancers`, and `auto_scaling_groups` SourceBlocks.
        """

        eip_recs = list(get_elastic_ip_checks(ctx).get("recommendations", []))
        nat_recs = list(get_nat_gateway_checks(ctx).get("recommendations", []))
        vpc_recs = list(get_vpc_endpoints_checks(ctx).get("recommendations", []))
        lb_recs = list(get_load_balancer_checks(ctx).get("recommendations", []))
        asg_recs = list(get_auto_scaling_checks(ctx).get("recommendations", []))

        all_recs = eip_recs + nat_recs + vpc_recs + lb_recs + asg_recs
        total_savings = _sum_savings(all_recs)

        return ServiceFindings(
            service_name="Network & Infrastructure",
            total_recommendations=len(all_recs),
            total_monthly_savings=total_savings,
            sources={
                "elastic_ips": SourceBlock(
                    count=len(eip_recs),
                    recommendations=tuple(eip_recs),
                ),
                "nat_gateways": SourceBlock(
                    count=len(nat_recs),
                    recommendations=tuple(nat_recs),
                ),
                "vpc_endpoints": SourceBlock(
                    count=len(vpc_recs),
                    recommendations=tuple(vpc_recs),
                ),
                "load_balancers": SourceBlock(
                    count=len(lb_recs),
                    recommendations=tuple(lb_recs),
                ),
                "auto_scaling_groups": SourceBlock(
                    count=len(asg_recs),
                    recommendations=tuple(asg_recs),
                ),
            },
            optimization_descriptions={
                "elastic_ips": {
                    "title": "Elastic IP Optimization",
                    "description": "Unattached Elastic IPs and IPs attached to stopped instances",
                },
                "nat_gateways": {
                    "title": "NAT Gateway Optimization",
                    "description": "Unused, redundant, or low-throughput NAT gateways",
                },
                "vpc_endpoints": {
                    "title": "VPC Endpoint Optimization",
                    "description": "Unused VPC endpoints and consolidation opportunities",
                },
                "load_balancers": {
                    "title": "Load Balancer Optimization",
                    "description": "Idle, low-traffic, or Classic Load Balancers eligible for removal or migration",
                },
                "auto_scaling_groups": {
                    "title": "Auto Scaling Group Optimization",
                    "description": "ASG configuration and instance-type optimization opportunities",
                },
            },
        )

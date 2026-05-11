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


class NetworkModule(BaseServiceModule):
    """ServiceModule adapter for EIP, NAT, VPC, ELB, and ASG. Composite savings strategy."""

    key: str = "network"
    cli_aliases: tuple[str, ...] = ("network",)
    display_name: str = "Network & Infrastructure"

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for network infrastructure scanning."""
        return ("ec2", "elasticloadbalancingv2", "autoscaling", "elb")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Elastic IPs, NAT Gateways, VPC Endpoints, Load Balancers, and ASGs.

        Consults Elastic IP, NAT Gateway, VPC Endpoints, Load Balancer, and
        Auto Scaling checks. Savings parsed from dollar-amount strings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with consolidated enhanced_checks SourceBlock.
        """
        print("\U0001f50d [services/adapters/network.py] Network module active")

        eip_result = get_elastic_ip_checks(ctx)
        nat_result = get_nat_gateway_checks(ctx)
        vpc_result = get_vpc_endpoints_checks(ctx)
        lb_result = get_load_balancer_checks(ctx)
        asg_result = get_auto_scaling_checks(ctx)

        eip_recs = eip_result.get("recommendations", [])
        nat_recs = nat_result.get("recommendations", [])
        vpc_recs = vpc_result.get("recommendations", [])
        lb_recs = lb_result.get("recommendations", [])
        asg_recs = asg_result.get("recommendations", [])

        all_recs = eip_recs + nat_recs + vpc_recs + lb_recs + asg_recs

        savings = sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in all_recs)

        total_recs = len(all_recs)

        return ServiceFindings(
            service_name="Network & Infrastructure",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "enhanced_checks": SourceBlock(count=total_recs, recommendations=tuple(all_recs)),
            },
        )

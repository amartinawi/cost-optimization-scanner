"""Multi-source adapter for network infrastructure (EIP, NAT, VPC, ELB, ASG) optimization."""

from __future__ import annotations

import re
from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.ec2 import get_auto_scaling_checks
from services.elastic_ip import get_elastic_ip_checks
from services.load_balancer import get_load_balancer_checks
from services.nat_gateway import get_nat_gateway_checks
from services.vpc_endpoints import get_vpc_endpoints_checks


class NetworkModule(BaseServiceModule):
    key: str = "network"
    cli_aliases: tuple[str, ...] = ("network",)
    display_name: str = "Network & Infrastructure"

    def required_clients(self) -> tuple[str, ...]:
        return ("ec2", "elasticloadbalancingv2", "autoscaling")

    def scan(self, ctx: Any) -> ServiceFindings:
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

        savings = 0.0
        for rec in all_recs:
            savings_str = rec.get("EstimatedSavings", "")
            if "$" in savings_str and "/month" in savings_str:
                match = re.search(r"\$(\d+\.?\d*)", savings_str)
                if match:
                    savings += float(match.group(1))

        total_recs = len(all_recs)

        return ServiceFindings(
            service_name="Network & Infrastructure",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "elastic_ip_checks": SourceBlock(count=len(eip_recs), recommendations=tuple(eip_recs)),
                "nat_gateway_checks": SourceBlock(count=len(nat_recs), recommendations=tuple(nat_recs)),
                "vpc_endpoints_checks": SourceBlock(count=len(vpc_recs), recommendations=tuple(vpc_recs)),
                "load_balancer_checks": SourceBlock(count=len(lb_recs), recommendations=tuple(lb_recs)),
                "auto_scaling_checks": SourceBlock(count=len(asg_recs), recommendations=tuple(asg_recs)),
            },
        )

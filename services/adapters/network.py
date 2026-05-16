"""Multi-source adapter for network infrastructure (EIP, NAT, VPC, ELB, ASG) optimization."""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services._savings import parse_dollar_savings
from services.ec2 import get_auto_scaling_checks
from services.elastic_ip import get_elastic_ip_checks
from services.load_balancer import get_load_balancer_checks
from services.nat_gateway import get_nat_gateway_checks
from services.vpc_endpoints import get_vpc_endpoints_checks

logger = logging.getLogger(__name__)

# Severity thresholds for `_derive_severity`. Values are conservative
# midpoints chosen to align with the audit doc's qualitative buckets:
# idle resources costing ≥$30/month (e.g. a full NAT gateway, multiple
# EIPs) → HIGH; mid-range optimizations → MEDIUM; sub-$10 → LOW.
_SEVERITY_HIGH_USD: float = 30.0
_SEVERITY_MEDIUM_USD: float = 10.0


def _derive_severity(rec: dict[str, Any]) -> str:
    """Return a severity tag for a network recommendation based on parsed savings.

    Honors any explicit `severity` already set by the sub-shim; only fills it
    in when missing. Magnitude-based fallback keeps the rule simple and
    defensible (audit L3-002).
    """
    existing = rec.get("severity") or rec.get("Severity") or rec.get("priority") or rec.get("Priority")
    if existing:
        return str(existing).upper()
    monthly = parse_dollar_savings(rec.get("EstimatedSavings", ""))
    if monthly >= _SEVERITY_HIGH_USD:
        return "HIGH"
    if monthly >= _SEVERITY_MEDIUM_USD:
        return "MEDIUM"
    return "LOW"


def _annotate_severity(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a `severity` key to each rec in-place using `_derive_severity`."""
    for rec in recs:
        rec["severity"] = _derive_severity(rec)
    return recs


def _safe_collect(label: str, fn: Callable[..., dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    """Run a sub-shim and return its recommendations, swallowing failures.

    One failing sub-shim must not blank the whole network tab (audit L1-005).
    """
    try:
        return list(fn(ctx).get("recommendations", []))
    except Exception as e:
        logger.warning(f"[network] {label} sub-check failed: {e}")
        return []


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
        bucket. Each sub-call is isolated so a single failure does not blank
        the whole tab. Recommendations missing an explicit `severity` are
        tagged HIGH/MEDIUM/LOW from their parsed dollar savings.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with `elastic_ips`, `nat_gateways`, `vpc_endpoints`,
            `load_balancers`, and `auto_scaling_groups` SourceBlocks.
        """

        eip_recs = _annotate_severity(_safe_collect("elastic_ip", get_elastic_ip_checks, ctx))
        nat_recs = _annotate_severity(_safe_collect("nat_gateway", get_nat_gateway_checks, ctx))
        vpc_recs = _annotate_severity(_safe_collect("vpc_endpoints", get_vpc_endpoints_checks, ctx))
        lb_recs = _annotate_severity(_safe_collect("load_balancer", get_load_balancer_checks, ctx))
        asg_recs = _annotate_severity(_safe_collect("auto_scaling", get_auto_scaling_checks, ctx))

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

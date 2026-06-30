"""Multi-source adapter for network infrastructure (EIP, NAT, VPC, ELB, ASG) optimization."""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.contracts import ServiceFindings, SourceBlock
from services._aws_errors import record_aws_error
from services._base import BaseServiceModule
from services._coh_dedup import coh_key, coh_savings, is_renderable_coh_rec
from services._savings import mark_zero_savings_advisory, parse_dollar_savings
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
    """Run a sub-shim and return its recommendations, isolating failures.

    One failing sub-shim must not blank the whole network tab (audit L1-005), but
    the failure must still surface: a permission gap or throttle is recorded on
    ``ctx`` (permission_issue vs warn) rather than swallowed with a logger call
    that never reaches the report (audit H4).
    """
    try:
        return list(fn(ctx).get("recommendations", []))
    except Exception as e:
        record_aws_error(ctx, e, service="network", context=f"{label} sub-check failed")
        return []


def _mark_advisory(recs: list[dict[str, Any]], note: str) -> list[dict[str, Any]]:
    """Force a recommendation list to advisory (Counted=False) with an explanatory note.

    Used for the ASG block: instance rightsizing is a compute lever owned by the
    EC2 tab (AWS Compute Optimizer + launch-template member dedup). Surfacing the
    same dollars here too would double-count across the EC2 and Network grand
    totals (audit H2), so Network shows them as advisory context only.
    """
    for rec in recs:
        rec["Counted"] = False
        rec.setdefault("AdvisoryNote", note)
    return recs


def _sum_savings(recs: list[dict[str, Any]]) -> float:
    """Sum parsed dollar savings across a recommendation list."""
    return sum(parse_dollar_savings(rec.get("EstimatedSavings", "")) for rec in recs)


def _coh_nat_recs(ctx: Any) -> list[dict[str, Any]]:
    """Renderable Cost Optimization Hub NAT-gateway findings worth counting.

    Only NatGateway recs with a POSITIVE ``estimatedMonthlySavings`` are returned:
    a ``$0`` CoH rec carries no dollar, so it must NOT cause its NAT to be excluded
    from the local topology math (which would silently zero that VPC's savings for
    no gain). RI/SP purchase recs and ``N/A`` resources are filtered by
    ``is_renderable_coh_rec``.
    """
    coh_raw = (getattr(ctx, "cost_hub_splits", {}) or {}).get("network", [])
    return [
        r
        for r in coh_raw
        if r.get("currentResourceType") == "NatGateway"
        and is_renderable_coh_rec(r)
        and coh_savings(r) > 0
    ]


def _collect_nat_with_topology(
    ctx: Any,
    exclude_nat_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Run the NAT shim once, excluding CoH-owned NATs, returning ``(recs, map)``.

    ``exclude_nat_ids`` are the NATs a Cost Optimization Hub finding already counts,
    so the shim drops them from its consolidation / per-NAT math — the remaining
    NATs in the same VPC are still consolidated normally (NAT-id granularity, no
    over-suppression of independent NATs). ``_safe_collect`` would discard the
    topology map, so the shim is called directly with the same error-classification
    contract.
    """
    try:
        result = get_nat_gateway_checks(ctx, exclude_nat_ids=exclude_nat_ids)
        return list(result.get("recommendations", [])), dict(result.get("nat_vpc_map", {}))
    except Exception as e:
        record_aws_error(ctx, e, service="network", context="nat_gateway sub-check failed")
        return [], {}


def _normalize_coh_nat(
    coh_raw: list[dict[str, Any]],
    nat_vpc_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert raw CoH NAT findings into counted recs for the NAT source block.

    Each finding's NAT was excluded from the local topology math, so counting the
    AWS-computed dollar here can never double-count. The VPC is taken from the
    full topology map for display only.
    """
    out: list[dict[str, Any]] = []
    for r in coh_raw:
        nat_id = coh_key(r)
        amt = coh_savings(r)
        out.append(
            {
                "NatGatewayId": nat_id or "N/A",
                "VpcId": nat_vpc_map.get(nat_id, "N/A"),
                "Recommendation": "AWS Cost Optimization Hub: idle/underused NAT gateway — review for removal",
                "EstimatedSavings": f"${amt:.2f}/month (AWS Cost Optimization Hub)",
                "EstimatedMonthlySavings": round(amt, 2),
                "Counted": True,
                "Source": "CostOptimizationHub",
                "CheckCategory": "NAT Gateway (Cost Optimization Hub)",
            }
        )
    return out


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
        # NAT: take AWS Cost Optimization Hub per-NAT idle findings first, EXCLUDE
        # those NATs from the local topology math (NAT-id granularity), then render
        # CoH counted + the local consolidation on the *remaining* NATs. Excluding
        # rather than VPC-demoting means an independent NAT's saving in the same
        # VPC is never suppressed, and a demoted rec can never leak a numeric
        # (CoH > heuristic, no double-count).
        coh_nat_raw = _coh_nat_recs(ctx)
        coh_nat_ids = {coh_key(r) for r in coh_nat_raw}
        local_nat_recs, nat_vpc_map = _collect_nat_with_topology(ctx, coh_nat_ids)
        nat_recs = _annotate_severity(local_nat_recs) + _annotate_severity(
            _normalize_coh_nat(coh_nat_raw, nat_vpc_map)
        )
        vpc_recs = _annotate_severity(_safe_collect("vpc_endpoints", get_vpc_endpoints_checks, ctx))
        lb_recs = _annotate_severity(_safe_collect("load_balancer", get_load_balancer_checks, ctx))
        # ASG rightsizing is owned by the EC2 tab (Compute Optimizer + member dedup);
        # surface it here as advisory only so it is visible but never double-counted.
        asg_recs = _mark_advisory(
            _annotate_severity(_safe_collect("auto_scaling", get_auto_scaling_checks, ctx)),
            "ASG rightsizing is counted under the EC2 tab (Compute Optimizer); shown here for context.",
        )

        all_recs = eip_recs + nat_recs + vpc_recs + lb_recs + asg_recs
        # Best-practice nudges that parse to $0 (missing endpoints, metric-gated
        # NAT/LB) are advisory — shown but not counted.
        mark_zero_savings_advisory(all_recs, lambda r: parse_dollar_savings(r.get("EstimatedSavings", "")))
        total_savings = _sum_savings([r for r in all_recs if r.get("Counted", True)])

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
                    "description": "Missing S3/DynamoDB gateway endpoints, non-prod interface endpoints, and duplicate interface-endpoint consolidation opportunities",
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

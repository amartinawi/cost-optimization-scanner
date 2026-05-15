"""Network data transfer cost optimization adapter.

Analyzes AWS Cost Explorer data and EC2 network configuration to surface
data transfer cost savings opportunities:

- Cross-region data transfer (most expensive at $0.02-$0.09/GB)
- Cross-AZ data transfer ($0.01/GB each direction, often overlooked)
- Internet egress patterns (direct vs CloudFront)
- Transit Gateway vs VPC Peering cost comparison

AWS API cost: Cost Explorer charges $0.01 per API request. This adapter
makes ~1 CE call per scan. The calls are:

1. ``get_cost_and_usage`` — data transfer spend by USAGE_TYPE

EC2 calls are free (no per-request charge).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

CROSS_AZ_SAVINGS_FACTOR: float = 0.5
CLOUDFRONT_SAVINGS_FACTOR: float = 0.40
TGW_ATTACHMENT_COST_PER_GB: float = 0.05
TGW_PROCESSING_COST_PER_GB: float = 0.02


def _time_period() -> dict[str, str]:
    """Build a 30-day Cost Explorer time period ending today.

    Returns:
        Dict with ``Start`` and ``End`` ISO-format date strings.
    """
    end = date.today()
    start = end - timedelta(days=30)
    return {"Start": start.isoformat(), "End": end.isoformat()}


class NetworkCostModule(BaseServiceModule):
    """ServiceModule adapter for data transfer cost optimization.

    Uses Cost Explorer and EC2 APIs to detect expensive data transfer
    patterns and recommend architectural changes for cost reduction.

    CE API cost: ~$0.01 per scan (1 call).
    """

    key: str = "network_cost"
    cli_aliases: tuple[str, ...] = ("network_cost", "data_transfer")
    display_name: str = "Data Transfer"

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(
            label="Transfer Spend (30d)",
            source_path="extras.total_data_transfer_spend_30d",
            formatter="currency",
        ),
        StatCardSpec(
            label="Cross-Region Spend",
            source_path="extras.cross_region_spend_30d",
            formatter="currency",
        ),
        StatCardSpec(
            label="Monthly Savings",
            source_path="total_monthly_savings",
            formatter="currency",
        ),
    )

    grouping = GroupingSpec(by="check_category")

    requires_cloudwatch: bool = False
    reads_fast_mode: bool = False

    def required_clients(self) -> tuple[str, ...]:
        """Returns Cost Explorer and EC2 client names."""
        return ("ce", "ec2")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan data transfer costs and network topology for savings.

        Uses Cost Explorer to analyze 30-day transfer spend by usage type,
        then cross-references EC2 peering and Transit Gateway configuration
        to identify optimization opportunities.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with cross_region_transfer, cross_az_transfer,
            internet_egress, and tgw_vs_peering source blocks.
        """
        print("\U0001f50d [services/adapters/network_cost.py] Network Cost module active")

        ce = ctx.client("ce")
        ec2 = ctx.client("ec2")
        if not ce:
            return self._empty_findings()

        multiplier = ctx.pricing_multiplier
        tp = _time_period()

        transfer_spend, usage_breakdown = self._fetch_transfer_spend(ce, tp)

        cross_region_spend = usage_breakdown.get("cross_region", 0.0)
        cross_az_spend = usage_breakdown.get("cross_az", 0.0)
        egress_spend = usage_breakdown.get("egress", 0.0)

        cross_region_recs = self._analyze_cross_region(cross_region_spend, multiplier)
        cross_az_recs = self._analyze_cross_az(cross_az_spend, multiplier)
        egress_recs = self._analyze_internet_egress(egress_spend, multiplier)

        peering_count = 0
        tgw_count = 0
        tgw_recs: list[dict[str, Any]] = []
        if ec2:
            peering_count, tgw_count = self._fetch_network_topology(ec2)
            tgw_recs = self._analyze_tgw_vs_peering(peering_count, tgw_count, usage_breakdown, multiplier)

        all_recs = cross_region_recs + cross_az_recs + egress_recs + tgw_recs
        total_savings = sum(r.get("monthly_savings", 0.0) for r in all_recs)

        return ServiceFindings(
            service_name="Network Transfer Costs",
            total_recommendations=len(all_recs),
            total_monthly_savings=round(total_savings, 2),
            sources={
                "cross_region_transfer": SourceBlock(
                    count=len(cross_region_recs),
                    recommendations=tuple(cross_region_recs),
                ),
                "cross_az_transfer": SourceBlock(
                    count=len(cross_az_recs),
                    recommendations=tuple(cross_az_recs),
                ),
                "internet_egress": SourceBlock(
                    count=len(egress_recs),
                    recommendations=tuple(egress_recs),
                ),
                "tgw_vs_peering": SourceBlock(
                    count=len(tgw_recs),
                    recommendations=tuple(tgw_recs),
                ),
            },
            extras={
                "total_data_transfer_spend_30d": round(transfer_spend, 2),
                "cross_region_spend_30d": round(cross_region_spend, 2),
                "peering_count": peering_count,
                "tgw_count": tgw_count,
            },
        )

    def _fetch_transfer_spend(self, ce: Any, tp: dict[str, str]) -> tuple[float, dict[str, float]]:
        """Query Cost Explorer for data transfer spend breakdown.

        Fetches 30-day data transfer costs grouped by USAGE_TYPE and
        categorizes them into cross-region, cross-AZ, and internet egress.

        Args:
            ce: Cost Explorer boto3 client.
            tp: Time period dict with Start/End keys.

        Returns:
            Tuple of (total spend float, breakdown dict with cross_region,
            cross_az, and egress keys).
        """
        total = 0.0
        breakdown: dict[str, float] = {"cross_region": 0.0, "cross_az": 0.0, "egress": 0.0}

        try:
            results: list[dict[str, Any]] = []
            next_token: str | None = None

            while True:
                kwargs: dict[str, Any] = {
                    "TimePeriod": tp,
                    "Granularity": "MONTHLY",
                    "Filter": {
                        "Dimensions": {
                            "Key": "USAGE_TYPE_GROUP",
                            "Values": ["AWS Data Transfer"],
                        }
                    },
                    "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
                    "Metrics": ["UnblendedCost", "UsageQuantity"],
                }
                if next_token:
                    kwargs["NextPageToken"] = next_token

                resp = ce.get_cost_and_usage(**kwargs)

                for page in resp.get("ResultsByTime", []):
                    for group in page.get("Groups", []):
                        results.append(group)

                next_token = resp.get("NextPageToken")
                if not next_token:
                    break

            for group in results:
                keys = group.get("Keys", [])
                cost = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0))
                if cost <= 0:
                    continue

                total += cost
                usage_type = keys[0].lower() if keys else ""

                if any(kw in usage_type for kw in ("interregion", "cross-region", "region")):
                    breakdown["cross_region"] += cost
                elif any(kw in usage_type for kw in ("transfer-region", "az", "availability-zone")):
                    breakdown["cross_az"] += cost
                elif any(kw in usage_type for kw in ("egress", "internet", "data-transfer-out")):
                    breakdown["egress"] += cost
                else:
                    breakdown["egress"] += cost

        except Exception as e:
            print(f"Warning: Data transfer spend query failed: {e}")

        return total, breakdown

    def _analyze_cross_region(self, cross_region_spend: float, multiplier: float) -> list[dict[str, Any]]:
        """Analyze cross-region data transfer for optimization opportunities.

        Cross-region transfer is the most expensive data transfer type
        ($0.02-$0.09/GB). This check identifies significant spend and
        recommends architectural patterns to reduce it.

        Args:
            cross_region_spend: 30-day cross-region transfer spend from CE.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            List of cross-region transfer recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        if cross_region_spend > 0:
            monthly_est = cross_region_spend
            potential_savings = monthly_est * 0.30

            recs.append(
                {
                    "resource_id": "cross-region-transfer",
                    "check_type": "cross_region_transfer",
                    "check_category": "Cross-Region Transfer",
                    "current_value": f"${monthly_est:.2f}/mo cross-region transfer spend",
                    "recommended_value": "Consolidate workloads to fewer regions or use VPC endpoints",
                    "monthly_savings": round(potential_savings, 2),
                    "severity": "HIGH" if monthly_est > 100 else "MEDIUM",
                    "reason": f"Cross-region data transfer costs ${monthly_est:.2f}/mo; "
                    f"architectural changes (regional consolidation, caching, "
                    f"endpoint placement) could reduce by up to 30%",
                }
            )

        return recs

    def _analyze_cross_az(self, cross_az_spend: float, multiplier: float) -> list[dict[str, Any]]:
        """Analyze cross-AZ data transfer for AZ-local optimization.

        Cross-AZ transfer costs $0.01/GB each direction. Approximately 50%
        of cross-AZ traffic can be made AZ-local through careful placement
        of communicating services in the same AZ.

        Args:
            cross_az_spend: 30-day cross-AZ transfer spend from CE.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            List of cross-AZ transfer recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        if cross_az_spend > 0:
            monthly_est = cross_az_spend
            potential_savings = monthly_est * CROSS_AZ_SAVINGS_FACTOR

            recs.append(
                {
                    "resource_id": "cross-az-transfer",
                    "check_type": "cross_az_transfer",
                    "check_category": "Cross-AZ Transfer",
                    "current_value": f"${monthly_est:.2f}/mo cross-AZ transfer spend",
                    "recommended_value": "Co-locate communicating services in the same AZ",
                    "monthly_savings": round(potential_savings, 2),
                    "severity": "MEDIUM",
                    "reason": f"Cross-AZ transfer costs ${monthly_est:.2f}/mo; "
                    f"co-locating services in the same AZ could save ~50%",
                }
            )

        return recs

    def _analyze_internet_egress(self, egress_spend: float, multiplier: float) -> list[dict[str, Any]]:
        """Analyze internet egress for CloudFront savings opportunity.

        CloudFront typically provides 40-60% savings on internet egress
        compared to direct EC2/S3 delivery, plus better performance.

        Args:
            egress_spend: 30-day internet egress spend from CE.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            List of internet egress recommendation dicts.
        """
        recs: list[dict[str, Any]] = []

        if egress_spend > 0:
            monthly_est = egress_spend
            # `egress_spend` comes from Cost Explorer which already returns
            # real region-priced dollars. Do NOT apply pricing_multiplier
            # (L2.3.1). Cross-region and cross-AZ paths correctly omit
            # multiplier; egress now matches for consistency.
            _ = multiplier
            potential_savings = monthly_est * CLOUDFRONT_SAVINGS_FACTOR

            recs.append(
                {
                    "resource_id": "internet-egress",
                    "check_type": "internet_egress",
                    "check_category": "Internet Egress",
                    "current_value": f"${monthly_est:.2f}/mo direct internet egress",
                    "recommended_value": "Route traffic through CloudFront for egress optimization",
                    "monthly_savings": round(potential_savings, 2),
                    "severity": "MEDIUM",
                    "reason": f"Direct internet egress costs ${monthly_est:.2f}/mo; "
                    f"CloudFront could reduce costs by ~40% with better performance",
                }
            )

        return recs

    def _fetch_network_topology(self, ec2: Any) -> tuple[int, int]:
        """Fetch VPC peering and Transit Gateway counts from EC2.

        Args:
            ec2: EC2 boto3 client.

        Returns:
            Tuple of (active peering connection count, TGW count).
        """
        peering_count = 0
        tgw_count = 0

        try:
            resp = ec2.describe_vpc_peering_connections(Filters=[{"Name": "status-code", "Values": ["active"]}])
            peering_count = len(resp.get("VpcPeeringConnections", []))
        except Exception as e:
            print(f"Warning: VPC peering query failed: {e}")

        try:
            resp = ec2.describe_transit_gateways()
            tgws = resp.get("TransitGateways", [])
            tgw_count = len(tgws)
        except Exception as e:
            print(f"Warning: Transit Gateway query failed: {e}")

        return peering_count, tgw_count

    def _analyze_tgw_vs_peering(
        self,
        peering_count: int,
        tgw_count: int,
        usage_breakdown: dict[str, float],
        multiplier: float,
    ) -> list[dict[str, Any]]:
        """Compare Transit Gateway vs VPC Peering cost characteristics.

        TGW charges $0.05/GB attachment + $0.02/GB processed. VPC Peering
        only charges inter-region data transfer cost (no per-GB processing).
        For small-scale inter-VPC communication, peering is cheaper.

        Args:
            peering_count: Number of active VPC peering connections.
            tgw_count: Number of Transit Gateways.
            usage_breakdown: Transfer spend breakdown dict.
            multiplier: Regional pricing multiplier for savings estimates.

        Returns:
            List of TGW vs peering recommendation dicts.
        """
        recs: list[dict[str, Any]] = []
        tgw_total_per_gb = TGW_ATTACHMENT_COST_PER_GB + TGW_PROCESSING_COST_PER_GB

        if tgw_count > 0:
            recs.append(
                {
                    "resource_id": "tgw-cost-review",
                    "check_type": "tgw_vs_peering",
                    "check_category": "TGW vs Peering",
                    "current_value": f"{tgw_count} Transit Gateway(s) at ${tgw_total_per_gb:.2f}/GB",
                    "recommended_value": "Review routes and consider VPC Peering for low-volume connections",
                    "monthly_savings": 0.0,
                    "severity": "LOW",
                    "reason": f"{tgw_count} Transit Gateway(s) incur ${tgw_total_per_gb:.2f}/GB "
                    f"(attachment + processing). For low-volume inter-VPC traffic, "
                    f"VPC Peering may be cheaper with no per-GB processing fee.",
                }
            )

        if peering_count > 0 and tgw_count == 0:
            recs.append(
                {
                    "resource_id": "peering-scalability",
                    "check_type": "tgw_vs_peering",
                    "check_category": "TGW vs Peering",
                    "current_value": f"{peering_count} VPC Peering connection(s), no TGW",
                    "recommended_value": "Consider Transit Gateway for centralized routing at scale",
                    "monthly_savings": 0.0,
                    "severity": "LOW",
                    "reason": f"{peering_count} peering connections managed individually. "
                    f"At scale (>10 VPCs), Transit Gateway simplifies routing but adds "
                    f"${tgw_total_per_gb:.2f}/GB cost. Evaluate trade-off.",
                }
            )

        if tgw_count > 0 and peering_count > 0:
            transfer_spend = usage_breakdown.get("cross_region", 0.0) + usage_breakdown.get("cross_az", 0.0)
            if transfer_spend > 0:
                estimated_tgw_data_gb = transfer_spend / 0.02
                potential_savings = estimated_tgw_data_gb * TGW_PROCESSING_COST_PER_GB * 0.20
                if potential_savings > 1.0:
                    recs.append(
                        {
                            "resource_id": "tgw-route-optimization",
                            "check_type": "tgw_vs_peering",
                            "check_category": "TGW vs Peering",
                            "current_value": f"TGW + {peering_count} peering connections, ${transfer_spend:.2f}/mo transfer",
                            "recommended_value": "Optimize TGW route tables to minimize cross-hub traffic",
                            "monthly_savings": round(potential_savings, 2),
                            "severity": "MEDIUM",
                            "reason": f"Mixed TGW/peering topology with ${transfer_spend:.2f}/mo transfer. "
                            f"Optimizing TGW routes could save ~20% on processing costs.",
                        }
                    )

        return recs

    @staticmethod
    def _empty_findings() -> ServiceFindings:
        """Return empty ServiceFindings when CE is unavailable.

        Returns:
            ServiceFindings with zero counts and empty source blocks.
        """
        return ServiceFindings(
            service_name="Network Transfer Costs",
            total_recommendations=0,
            total_monthly_savings=0.0,
            sources={
                "cross_region_transfer": SourceBlock(count=0, recommendations=()),
                "cross_az_transfer": SourceBlock(count=0, recommendations=()),
                "internet_egress": SourceBlock(count=0, recommendations=()),
                "tgw_vs_peering": SourceBlock(count=0, recommendations=()),
            },
            extras={
                "total_data_transfer_spend_30d": 0.0,
                "cross_region_spend_30d": 0.0,
                "peering_count": 0,
                "tgw_count": 0,
            },
        )

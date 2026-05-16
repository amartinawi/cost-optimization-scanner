"""Cost Optimization Hub adapter for unrouted Savings Plans and cross-service recommendations.

Surfaces Savings Plans opportunities and cross-service recommendations from
AWS Cost Optimization Hub that are NOT already routed to existing service
adapters (EC2, EBS, Lambda, RDS). Consumes pre-fetched ctx.cost_hub_splits
data and supplements with additional API calls for recommendation types not
covered by the pre-fetch.
"""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import GroupingSpec, ServiceFindings, SourceBlock, StatCardSpec
from services._base import BaseServiceModule

logger = logging.getLogger(__name__)


_ROUTED_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "Ec2Instance",
        "EbsVolume",
        "LambdaFunction",
        "RdsDbInstance",
    }
)

_SAVINGS_PLANS_TYPES: frozenset[str] = frozenset(
    {
        "RdsComputeSavingsPlans",
        "ComputeSavingsPlans",
        "Ec2InstanceSavingsPlans",
        "LambdaEventBridgeIdleRule",
    }
)


class CostOptimizationHubModule(BaseServiceModule):
    """ServiceModule adapter for Cost Optimization Hub.

    Surfaces unrouted Savings Plans recommendations and cross-service
    recommendations not consumed by existing adapters (EC2, EBS, Lambda, RDS).
    """

    key: str = "cost_optimization_hub"
    cli_aliases: tuple[str, ...] = ("cost_optimization_hub", "cost_hub", "hub")
    display_name: str = "Cost Optimization Hub"
    requires_cloudwatch: bool = False

    stat_cards: tuple[StatCardSpec, ...] = (
        StatCardSpec(label="Savings Plan Opps", source_path="sources.savings_plans.count", formatter="int"),
        StatCardSpec(label="Cross-Service Recs", source_path="sources.cross_service.count", formatter="int"),
        StatCardSpec(label="Monthly Savings", source_path="total_monthly_savings", formatter="currency"),
    )

    grouping = GroupingSpec(by="check_category")

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for Cost Optimization Hub scanning."""
        return ("cost-optimization-hub",)

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan Cost Optimization Hub for unrouted savings opportunities.

        Consumes pre-fetched ctx.cost_hub_splits data for already-fetched
        recommendations, then queries the Cost Optimization Hub API for
        additional recommendation types not covered by the pre-fetch.
        Filters out recommendations already routed to existing adapters
        (EC2, EBS, Lambda, RDS) to prevent double-counting.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with savings_plans and cross_service SourceBlock
            entries containing only unrouted recommendations.
        """

        all_recs: list[dict[str, Any]] = self._collect_prefetched(ctx)
        all_recs.extend(self._fetch_supplemental(ctx))

        savings_plans_recs: list[dict[str, Any]] = []
        cross_service_recs: list[dict[str, Any]] = []
        type_counts: dict[str, int] = {}
        seen_ids: set[str] = set()

        for rec in all_recs:
            resource_type = rec.get("currentResourceType", "")
            rec_type = rec.get("recommendationType", "")
            rec_id = rec.get("recommendationId", "")

            if resource_type in _ROUTED_RESOURCE_TYPES:
                continue

            if rec_id and rec_id in seen_ids:
                continue
            if rec_id:
                seen_ids.add(rec_id)

            type_counts[rec_type] = type_counts.get(rec_type, 0) + 1

            normalized = self._normalize(rec)
            if rec_type in _SAVINGS_PLANS_TYPES:
                savings_plans_recs.append(normalized)
            else:
                cross_service_recs.append(normalized)

        sp_savings = sum(r.get("estimatedMonthlySavings", 0) for r in savings_plans_recs)
        cs_savings = sum(r.get("estimatedMonthlySavings", 0) for r in cross_service_recs)
        total_savings = sp_savings + cs_savings
        total_recs = len(savings_plans_recs) + len(cross_service_recs)

        return ServiceFindings(
            service_name="Cost Optimization Hub",
            total_recommendations=total_recs,
            total_monthly_savings=total_savings,
            sources={
                "savings_plans": SourceBlock(
                    count=len(savings_plans_recs),
                    recommendations=tuple(savings_plans_recs),
                ),
                "cross_service": SourceBlock(
                    count=len(cross_service_recs),
                    recommendations=tuple(cross_service_recs),
                ),
            },
            extras={
                "recommendation_type_counts": type_counts,
                "total_recommendations_in_hub": len(all_recs),
            },
            optimization_descriptions={
                "savings_plans": {
                    "title": "Savings Plans Opportunities",
                    "description": "Commitment-based discounts identified by Cost Optimization Hub",
                },
                "cross_service": {
                    "title": "Cross-Service Recommendations",
                    "description": "Recommendations not addressed by individual service adapters",
                },
            },
        )

    def _collect_prefetched(self, ctx: Any) -> list[dict[str, Any]]:
        """Gather all pre-fetched Cost Hub recommendations from ctx.cost_hub_splits.

        Args:
            ctx: ScanContext with pre-fetched cost_hub_splits data.

        Returns:
            List of recommendation dicts from all service splits.
        """
        recs: list[dict[str, Any]] = []
        for service_recs in ctx.cost_hub_splits.values():
            recs.extend(service_recs)
        return recs

    def _fetch_supplemental(self, ctx: Any) -> list[dict[str, Any]]:
        """Fetch additional recommendations not covered by the pre-fetch.

        Queries Cost Optimization Hub API for recommendations in the scan
        region. Only used when the pre-fetched data does not cover all
        recommendation types (e.g. Savings Plans, cross-service).

        Args:
            ctx: ScanContext with region and client registry.

        Returns:
            List of additional recommendation dicts not already in pre-fetch.
        """
        recs: list[dict[str, Any]] = []
        cost_hub = ctx.client("cost-optimization-hub", region="us-east-1")
        if not cost_hub:
            return recs

        existing_ids: set[str] = set()
        for service_recs in ctx.cost_hub_splits.values():
            for r in service_recs:
                rid = r.get("recommendationId", "")
                if rid:
                    existing_ids.add(rid)

        try:
            response = cost_hub.list_recommendations(
                filter={"regions": [ctx.region]},
                maxResults=100,
            )
            for item in response.get("items", []):
                rid = item.get("recommendationId", "")
                if rid and rid in existing_ids:
                    continue
                try:
                    detailed = cost_hub.get_recommendation(recommendationId=rid)
                    recs.append(detailed)
                except Exception:
                    recs.append(item)

            while response.get("nextToken"):
                response = cost_hub.list_recommendations(
                    filter={"regions": [ctx.region]},
                    nextToken=response["nextToken"],
                    maxResults=100,
                )
                for item in response.get("items", []):
                    rid = item.get("recommendationId", "")
                    if rid and rid in existing_ids:
                        continue
                    try:
                        detailed = cost_hub.get_recommendation(recommendationId=rid)
                        recs.append(detailed)
                    except Exception:
                        recs.append(item)

        except Exception as e:
            logger.warning(f"Cost Optimization Hub supplemental fetch error: {e}")

        return recs

    @staticmethod
    def _normalize(rec: dict[str, Any]) -> dict[str, Any]:
        """Normalize a recommendation dict with adjusted savings and category.

        Sets a check_category field for grouping in reports.
        COH-returned estimatedMonthlySavings are real-dollar values
        that already reflect regional pricing, so no multiplier is applied.

        Args:
            rec: Raw recommendation dict from Cost Optimization Hub.

        Returns:
            Normalized recommendation dict.
        """
        raw_savings = rec.get("estimatedMonthlySavings", 0)
        if isinstance(raw_savings, str):
            try:
                raw_savings = float(raw_savings)
            except (ValueError, TypeError):
                raw_savings = 0.0

        normalized = dict(rec)
        normalized["estimatedMonthlySavings"] = raw_savings

        rec_type = rec.get("recommendationType", "Unknown")
        if rec_type in _SAVINGS_PLANS_TYPES:
            normalized["check_category"] = "Savings Plans"
        else:
            normalized["check_category"] = "Cross-Service Optimization"

        return normalized

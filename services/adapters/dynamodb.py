"""Multi-source adapter for DynamoDB with table analysis and enhanced checks."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.dynamodb import (
    DYNAMODB_ADVISORY_CATEGORIES,
    get_dynamodb_optimization_descriptions,
    get_dynamodb_table_analysis,
    get_enhanced_dynamodb_checks,
)

# AWS DynamoDB hourly provisioned rates (us-east-1, verified LIVE via Pricing API
# 2026-06-27): RCU SKU 4V475Q49DCKGXQZ2 usagetype ReadCapacityUnit-Hrs op
# CommittedThroughput = $0.00013/RCU-hr; WCU SKU R6PXMNYCEDGZ2EYN usagetype
# WriteCapacityUnit-Hrs = $0.00065/WCU-hr. Region-scaled via pricing_multiplier
# at the per-rec emit site below.
_DYNAMODB_RCU_HOURLY: float = 0.00013
_DYNAMODB_WCU_HOURLY: float = 0.00065
_HOURS_PER_MONTH: int = 730


def _provisioned_monthly_cost(read_capacity: float, write_capacity: float, multiplier: float) -> float:
    """Monthly provisioned-capacity cost for a RCU/WCU pair, region-scaled."""
    return (
        (read_capacity * _DYNAMODB_RCU_HOURLY + write_capacity * _DYNAMODB_WCU_HOURLY)
        * _HOURS_PER_MONTH
        * multiplier
    )


def _over_provisioned_savings(rec: dict[str, Any], multiplier: float) -> tuple[float, float, bool]:
    """Counted rightsizing dollars for an over-provisioned table (DynamoDB H1).

    Saving = current provisioned cost - rightsized cost, where the shim computed
    the rightsized target from measured low utilization (target =
    ``ceil(avg_consumed x buffer)``, summed over the base table and each GSI). The
    saving is only counted when the shim attached a real CloudWatch metric AND
    measured low utilization; otherwise the table is a $0 advisory (no fabricated
    blanket factor). Returns ``(counted_savings, current_cost, counted)``.
    """
    current_read = float(rec.get("ReadCapacityUnits", 0) or 0)
    current_write = float(rec.get("WriteCapacityUnits", 0) or 0)
    target_read = float(rec.get("RightsizedReadCapacity", current_read))
    target_write = float(rec.get("RightsizedWriteCapacity", current_write))
    current_cost = _provisioned_monthly_cost(current_read, current_write, multiplier)
    target_cost = _provisioned_monthly_cost(target_read, target_write, multiplier)
    savings = max(current_cost - target_cost, 0.0)
    counted = bool(rec.get("MetricsAvailable") and rec.get("LowUtilization") and savings > 0)
    return savings, current_cost, counted


class DynamoDbModule(BaseServiceModule):
    """ServiceModule adapter for DynamoDB. Multi-source savings strategy."""

    key: str = "dynamodb"
    cli_aliases: tuple[str, ...] = ("dynamodb",)
    display_name: str = "DynamoDB"
    # Shim hits cloudwatch.get_metric_statistics for ConsumedReadCapacityUnits
    # and ConsumedWriteCapacityUnits per table (and per GSI).
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for DynamoDB scanning."""
        return ("dynamodb", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan DynamoDB tables for cost optimization opportunities.

        Counted savings come only from evidence: the metric-gated
        over-provisioned-capacity delta (current minus a CloudWatch-rightsized
        target, DynamoDB H1) and AWS Cost Optimization Hub. Reserved Capacity is
        a commitment lever, demoted to a $0 advisory (DynamoDB H2). Table-analysis
        rows carry no per-table utilization metric, so they are surfaced as $0
        advisories rather than a blanket savings factor.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with table_analysis, enhanced_checks, and Cost Hub sources.
        """

        dynamodb_data = get_dynamodb_table_analysis(ctx)
        enhanced_result = get_enhanced_dynamodb_checks(ctx)
        enhanced_recs = enhanced_result.get("recommendations", [])

        opt_opps = dynamodb_data.get("optimization_opportunities", [])
        multiplier = ctx.pricing_multiplier

        savings = 0.0

        # DynamoDB H1: table_analysis carries no per-table utilization metric, so a
        # blanket savings factor over its provisioned/on-demand cost is unbacked.
        # Surface these rows as $0 advisories; counted rightsizing dollars come only
        # from the metric-gated enhanced over-provisioned path and Cost Hub.
        for rec in opt_opps:
            rec["EstimatedMonthlySavings"] = 0.0
            rec["Counted"] = False
            rec.setdefault(
                "EstimatedSavings",
                "$0.00/month — advisory: enable CloudWatch capacity metrics to quantify rightsizing",
            )

        # Enhanced checks. Only the over-provisioned-capacity rec yields a counted
        # dollar, and only with CloudWatch low-utilization evidence; everything else
        # (Reserved Capacity, unused/billing-mode/lifecycle nudges) is advisory. A
        # safety per-table guard ensures one counted rec per table.
        counted_tables: set[str] = set()
        for rec in enhanced_recs:
            category = rec.get("CheckCategory", "")

            if category in DYNAMODB_ADVISORY_CATEGORIES:
                rec["EstimatedMonthlySavings"] = 0.0
                rec["Counted"] = False
                rec["EstimatedSavings"] = (
                    "$0.00/month — advisory: Reserved Capacity is a commitment purchase "
                    "(see Commitment Analysis); excluded from rightsizing savings"
                )
                continue

            if "over-provisioned" in category.lower():
                rec_savings, current_cost, counted = _over_provisioned_savings(rec, multiplier)
                table_name = rec.get("TableName") or ""
                if counted and table_name not in counted_tables:
                    counted_tables.add(table_name)
                    # current - target can never exceed current; cap defensively.
                    capped = min(rec_savings, current_cost) if current_cost > 0 else rec_savings
                    rec["EstimatedMonthlySavings"] = round(capped, 2)
                    rec["Counted"] = True
                    rec["EstimatedSavings"] = f"${capped:,.2f}/month"
                    rec["AuditBasis"] = {
                        "rcu_rate_per_hr": _DYNAMODB_RCU_HOURLY,
                        "wcu_rate_per_hr": _DYNAMODB_WCU_HOURLY,
                        "hours_per_month": _HOURS_PER_MONTH,
                        "region_multiplier": round(multiplier, 4),
                        "metric": f"Consumed R/W CapacityUnits avg over {rec.get('MetricWindowDays', 7)}d",
                        "current_rcu": rec.get("ReadCapacityUnits", 0),
                        "current_wcu": rec.get("WriteCapacityUnits", 0),
                        "target_rcu": rec.get("RightsizedReadCapacity"),
                        "target_wcu": rec.get("RightsizedWriteCapacity"),
                        "buffer": rec.get("Buffer"),
                        "formula": (
                            "(current_rcu - target_rcu) x rcu_rate x 730 x mult + "
                            "(current_wcu - target_wcu) x wcu_rate x 730 x mult; "
                            "target = ceil(avg_consumed x buffer) per base+GSI when utilization < 20%"
                        ),
                    }
                    savings += capped
                else:
                    rec["EstimatedMonthlySavings"] = 0.0
                    rec["Counted"] = False
                    rec["EstimatedSavings"] = (
                        "$0.00/month — advisory: table rightsizing already counted"
                        if counted
                        else "$0.00/month — advisory: no CloudWatch low-utilization evidence"
                    )
                continue

            # Categories with no per-table capacity dollar (unused / billing-mode /
            # data-lifecycle). Keep their descriptive string; surface as advisory.
            rec["EstimatedMonthlySavings"] = 0.0
            rec["Counted"] = False

        # AWS Cost Optimization Hub DynamoDBTable recommendations (e.g. capacity
        # mode / reserved-capacity savings). De-duplicate by table name against the
        # per-table checks above so a table covered by both is not counted twice.
        covered_tables = {r.get("TableName") for r in opt_opps} | {r.get("TableName") for r in enhanced_recs}
        covered_tables.discard(None)
        cost_hub_recs = getattr(ctx, "cost_hub_splits", {}).get("dynamodb", [])
        coh_kept: list[dict[str, Any]] = []
        for rec in cost_hub_recs:
            resource_id = str(rec.get("resourceId", "") or "")
            # Extract the table-name segment for both plain table ARNs
            # (...:table/Name) and index ARNs (...:table/Name/index/GSI). A naive
            # split("/")[-1] yields the GSI name for index ARNs, so a CoH rec
            # targeting an index on an already-covered table would not dedupe and
            # its savings would be double-counted (DynamoDB L3).
            table_name = (
                resource_id.split(":table/")[-1].split("/")[0]
                if ":table/" in resource_id
                else resource_id.split("/")[-1]
            )
            if table_name and table_name in covered_tables:
                continue
            coh_kept.append(rec)
            savings += float(rec.get("estimatedMonthlySavings", 0.0) or 0.0)

        # Count hygiene: $0 advisory recs are shown in their source block but excluded
        # from the headline rec count (mirrors services/_savings.mark_zero_savings_advisory).
        total_recs = (
            sum(1 for r in opt_opps if r.get("Counted") is not False)
            + sum(1 for r in enhanced_recs if r.get("Counted") is not False)
            + len(coh_kept)
        )

        return ServiceFindings(
            service_name="DynamoDB",
            total_recommendations=total_recs,
            total_monthly_savings=savings,
            sources={
                "dynamodb_table_analysis": SourceBlock(count=len(opt_opps), recommendations=tuple(opt_opps)),
                "enhanced_checks": SourceBlock(count=len(enhanced_recs), recommendations=tuple(enhanced_recs)),
                "cost_optimization_hub": SourceBlock(count=len(coh_kept), recommendations=tuple(coh_kept)),
            },
            optimization_descriptions=get_dynamodb_optimization_descriptions(),
            total_count=dynamodb_data.get("total_tables", 0),
            extras={
                "table_counts": {
                    "total": dynamodb_data.get("total_tables", 0),
                    "provisioned": len(dynamodb_data.get("provisioned_tables", [])),
                    "on_demand": len(dynamodb_data.get("on_demand_tables", [])),
                }
            },
        )

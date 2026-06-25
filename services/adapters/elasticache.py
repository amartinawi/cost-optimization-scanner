"""Keyword-rate adapter for ElastiCache."""

from __future__ import annotations

from typing import Any

from core.contracts import ServiceFindings, SourceBlock
from services._base import BaseServiceModule
from services.elasticache import get_enhanced_elasticache_checks


class ElasticacheModule(BaseServiceModule):
    """ServiceModule adapter for ElastiCache. Keyword-rate savings strategy."""

    key: str = "elasticache"
    cli_aliases: tuple[str, ...] = ("elasticache",)
    display_name: str = "ElastiCache"
    # Shim hits cloudwatch.get_metric_statistics for CPU-utilization
    # analysis of underutilized clusters.
    requires_cloudwatch: bool = True

    def required_clients(self) -> tuple[str, ...]:
        """Returns boto3 client names required for ElastiCache scanning."""
        return ("elasticache", "cloudwatch")

    def scan(self, ctx: Any) -> ServiceFindings:
        """Scan ElastiCache clusters for cost optimization opportunities.

        Consults the elasticache service module for Graviton migration, reserved
        nodes, underutilized clusters, engine version review, and Valkey evaluation.
        Savings calculated via keyword matching on EstimatedSavings text.

        Args:
            ctx: ScanContext with region, clients, and pricing data.

        Returns:
            ServiceFindings with an "enhanced_checks" SourceBlock entry.
        """
        result = get_enhanced_elasticache_checks(ctx)
        recs = result.get("recommendations", [])

        # Per-category discount rates keyed on the structured CheckCategory (not
        # fragile EstimatedSavings substrings — the old "Underutilized" branch was
        # dead because that rec's text is "30-50%"). Reserved Nodes is a COMMITMENT
        # lever (overlaps the commitment tab) → advisory, never counted here.
        rate_by_category = {
            "Underutilized Cluster": 0.30,  # evidence-gated (CloudWatch CPU)
            "Valkey Migration": 0.20,
            "Graviton Migration": 0.20,
        }

        # Price every rec and attach the per-rec dollar figure (fixes counted!=
        # rendered — the report previously showed only "30-50%").
        for rec in recs:
            category = rec.get("CheckCategory", "")
            node_type = rec.get("NodeType")
            num_nodes = rec.get("NumNodes", 1) or 1
            monthly_node = 0.0
            if ctx.pricing_engine is not None and node_type:
                monthly_node = ctx.pricing_engine.get_instance_monthly_price("AmazonElastiCache", node_type)
            rate = rate_by_category.get(category, 0.0)
            rec["EstimatedMonthlySavings"] = round(monthly_node * num_nodes * rate, 2)
            if "Reserved" in category:
                rec["Counted"] = False  # commitment lever — advisory

        # De-duplicate per cluster: Valkey / Graviton / downsize are alternative
        # remediations on the SAME node — counting all of them stacked overlapping
        # discounts on one node. Keep the single highest-$ lever counted per
        # cluster; mark the rest advisory.
        best_per_cluster: dict[str, dict[str, Any]] = {}
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            cid = rec.get("ClusterId", "")
            cur = best_per_cluster.get(cid)
            if cur is None or rec["EstimatedMonthlySavings"] > cur["EstimatedMonthlySavings"]:
                best_per_cluster[cid] = rec

        counted_ids = {id(r) for r in best_per_cluster.values()}
        savings = 0.0
        for rec in recs:
            if rec.get("Counted") is False:
                continue
            if id(rec) in counted_ids and rec["EstimatedMonthlySavings"] > 0:
                rec["Counted"] = True
                savings += rec["EstimatedMonthlySavings"]
            else:
                rec["Counted"] = False  # superseded by a better lever on the same cluster

        return ServiceFindings(
            service_name="ElastiCache",
            total_recommendations=len(recs),
            total_monthly_savings=round(savings, 2),
            sources={"enhanced_checks": SourceBlock(count=len(recs), recommendations=tuple(recs))},
        )
